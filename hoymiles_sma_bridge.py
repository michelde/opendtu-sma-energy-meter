#!/usr/bin/env python3
"""
Hoymiles → SMA EMETER Speedwire Bridge
=======================================
Liest die aktuelle PV-Erzeugung vom Hoymiles DTU (DTU-Pro oder OpenDTU)
und sendet sie als SMA Speedwire EMETER UDP-Multicast-Paket, damit der
SMA Home Manager 2.0 den Hoymiles-Wechselrichter als virtuellen Erzeuger
erkennt und beim Überschussladen der SMA Wallbox berücksichtigt.

Unterstützte DTU-Varianten:
  - Hoymiles DTU-Pro  (HTTP JSON API)
  - OpenDTU           (HTTP JSON API)
  - AhoyDTU           (HTTP JSON API)

Verwendung:
  pip install requests
  python3 hoymiles_sma_bridge.py --dtu-type dtupro --dtu-host 192.168.1.x
  python3 hoymiles_sma_bridge.py --dtu-type opendtu --dtu-host 192.168.1.x
  python3 hoymiles_sma_bridge.py --dtu-type ahoydtu --dtu-host 192.168.1.x

SMA EMETER Speedwire Protokoll (v1.0):
  Multicast-Adresse : 239.12.255.254
  UDP-Port          : 9522
  Obis-Code         : 1:2.4.0  → aktuelle Wirkleistung Einspeisung [W × 10, Einheit 0.1 W]
  Obis-Code         : 1:2.8.0  → Energie-Zähler Einspeisung [Wh × 3600, Einheit 1 Ws]
"""

import argparse
import logging
import socket
import time

import os
import re

import requests

# ---------------------------------------------------------------------------
# Konstanten
# ---------------------------------------------------------------------------

SMA_EMETER_MCAST_ADDR = "239.12.255.254"
SMA_EMETER_UDP_PORT   = 9522

# OBIS-IDs (SMA EMETER Protokoll, Spec Section 3.1 + Beispiel-Telegramm Section 4):
#   OBIS 4-Byte Format: [B=Channel][C=MeasuredValue][D=Type][E=Tariff]
#   B=0x01 = Kanal 1 (Sums), laut Spec-Beispiel Offset 28: 0x01 0x01 0x08 0x00
#   C=1 = Active power/energy +  (Netzbezug)
#   C=2 = Active power/energy −  (Einspeisung)
OBIS_P_CONSUME_W  = 0x01010400  # Wirkleistung Netzbezug    [0.1 W]
OBIS_E_CONSUME_WH = 0x01010800  # Energie Netzbezug         [1 Ws]
OBIS_P_SUPPLY_W   = 0x01020400  # Wirkleistung Einspeisung  [0.1 W]
OBIS_E_SUPPLY_WH  = 0x01020800  # Energie Einspeisung       [1 Ws]

# Dummy-Einträge ab Kanal 3 in der Reihenfolge wie ein echter SMA Energy Meter sendet.
# Quelle: sma-emeter-simulator main.cpp + Spec Section 3.1 Kanal-Tabelle.
# Kanäle 1+2 (Active+/-) kommen ZUERST im Paket (vor diesen Dummy-Einträgen).
OBIS_DUMMY_SEQUENCE = [
    ('M32', 0x01030400), ('C64', 0x01030800),  # Reactive+ gesamt
    ('M32', 0x01040400), ('C64', 0x01040800),  # Reactive- gesamt
    ('M32', 0x01090400), ('C64', 0x01090800),  # Apparent+ gesamt
    ('M32', 0x010A0400), ('C64', 0x010A0800),  # Apparent- gesamt
    ('M32', 0x010D0400),                        # Power Factor gesamt
    ('M32', 0x01150400), ('C64', 0x01150800),  # Active+  L1
    ('M32', 0x01160400), ('C64', 0x01160800),  # Active-  L1
    ('M32', 0x01170400), ('C64', 0x01170800),  # Reactive+ L1
    ('M32', 0x01180400), ('C64', 0x01180800),  # Reactive- L1
    ('M32', 0x011D0400), ('C64', 0x011D0800),  # Apparent+ L1
    ('M32', 0x011E0400), ('C64', 0x011E0800),  # Apparent- L1
    ('M32', 0x011F0400),                        # Current L1
    ('M32', 0x01200400),                        # Voltage L1
    ('M32', 0x01210400),                        # Power Factor L1
    ('M32', 0x01290400), ('C64', 0x01290800),  # Active+  L2
    ('M32', 0x012A0400), ('C64', 0x012A0800),  # Active-  L2
    ('M32', 0x012B0400), ('C64', 0x012B0800),  # Reactive+ L2
    ('M32', 0x012C0400), ('C64', 0x012C0800),  # Reactive- L2
    ('M32', 0x01310400), ('C64', 0x01310800),  # Apparent+ L2
    ('M32', 0x01320400), ('C64', 0x01320800),  # Apparent- L2
    ('M32', 0x01330400),                        # Current L2
    ('M32', 0x01340400),                        # Voltage L2
    ('M32', 0x01350400),                        # Power Factor L2
    ('M32', 0x013D0400), ('C64', 0x013D0800),  # Active+  L3
    ('M32', 0x013E0400), ('C64', 0x013E0800),  # Active-  L3
    ('M32', 0x013F0400), ('C64', 0x013F0800),  # Reactive+ L3
    ('M32', 0x01400400), ('C64', 0x01400800),  # Reactive- L3
    ('M32', 0x01450400), ('C64', 0x01450800),  # Apparent+ L3
    ('M32', 0x01460400), ('C64', 0x01460800),  # Apparent- L3
    ('M32', 0x01470400),                        # Current L3
    ('M32', 0x01480400),                        # Voltage L3
    ('M32', 0x01490400),                        # Power Factor L3
]

# ---------------------------------------------------------------------------
# Hilfsfunktionen – EMETER-Paket bauen
# ---------------------------------------------------------------------------

def build_emeter_packet(
    serial: int,
    power_w: float,
    energy_wh: float,
    ticker: int,
) -> bytes:
    """
    Baut ein vollständiges SMA EMETER Speedwire-Paket.

    Format (verifiziert gegen daimoniac/pysmaemeter):

    Offset  Len  Inhalt
    ------  ---  -----------------------------------------------
     0       4   SMA\x00  (Signatur)
     4       2   0x0004   (tag_len = 4)
     6       2   0x02A0   (Tag-ID: Group)
     8       4   0x00000001 (group_id = 1)
    12       2   data2_len  (interne Payload-Länge, wird am Ende gesetzt)
    14       2   0x0010   (Tag-ID: Data2)
    16       2   0x6069   (Protocol-ID: EMETER)
    18       2   0x010E   (SUSyID = 270)
    20       4   serial
    24       4   ticker   (ms)
    28+          OBIS-Einträge + 0x90000000 + 0x01020452 + End-Marker
    """
    buf = bytearray(1000)

    def w16(p: int, v: int) -> int:
        buf[p] = (v >> 8) & 0xFF; buf[p+1] = v & 0xFF
        return p + 2

    def w32(p: int, v: int) -> int:
        return w16(w16(p, (v >> 16) & 0xFFFF), v & 0xFFFF)

    def w64(p: int, v: int) -> int:
        return w32(w32(p, (v >> 32) & 0xFFFFFFFF), v & 0xFFFFFFFF)

    # Fester Header
    buf[0:4] = b"SMA\x00"
    pos = w16(4,  0x0004)
    pos = w16(pos, 0x02A0)
    pos = w32(pos, 0x00000001)
    data_size_pos = pos          # Offset 12: data2_len, später befüllen
    pos = w16(pos, 0x0000)       # placeholder
    pos = w16(pos, 0x0010)
    pos = w16(pos, 0x6069)
    pos = w16(pos, 0x010E)       # SUSyID = 270
    pos = w32(pos, serial)
    pos = w32(pos, ticker)
    # pos == 28

    payload_len = 12  # INITIAL_PAYLOAD_LENGTH aus Referenz

    # Kanal 1 (Netzbezug) + Kanal 2 (Einspeisung) ZUERST – laut Spec Section 3.1
    # und Simulator main.cpp Z. 121-124 (Active vor Reactive/Apparent).
    # Skalierung laut Spec Section 3.3:
    #   Leistung [W]  → ×10   (Einheit 0.1 W)
    #   Energie  [Wh] → ×3600 (Einheit 1 Ws = Watt-Sekunde)
    pos = w32(pos, OBIS_P_CONSUME_W);  pos = w32(pos, 0); payload_len += 8
    pos = w32(pos, OBIS_E_CONSUME_WH); pos = w64(pos, 0); payload_len += 12
    pos = w32(pos, OBIS_P_SUPPLY_W);   pos = w32(pos, max(0, int(round(power_w * 10)))); payload_len += 8
    pos = w32(pos, OBIS_E_SUPPLY_WH);  pos = w64(pos, max(0, int(round(energy_wh * 3600)))); payload_len += 12

    # Dummy-Werte ab Kanal 3 (Reactive/Apparent/L1/L2/L3)
    for typ, obis in OBIS_DUMMY_SEQUENCE:
        pos = w32(pos, obis)
        if typ == 'M32':
            pos = w32(pos, 0); payload_len += 8
        else:
            pos = w64(pos, 0); payload_len += 12

    # Version + Konstante (Abschluss wie in Referenz)
    pos = w32(pos, 0x90000000); pos = w32(pos, 0x01020452); payload_len += 8

    # data2_len eintragen (ohne End-Marker, wie in Referenz)
    w16(data_size_pos, payload_len)

    # End-Marker (wird nicht zu payload_len gezählt)
    pos = w32(pos, 0x00000000)

    # Gleiche Formel wie Referenz: _headerLength(28) + data2_len - INITIAL_PAYLOAD_LENGTH(12) + 4
    total_len = 28 + payload_len - 12 + 4
    return bytes(buf[:total_len])


# ---------------------------------------------------------------------------
# DTU-Adapter
# ---------------------------------------------------------------------------

class DTUReader:
    """Basisklasse – liefert (power_w, energy_wh)."""

    def read(self) -> tuple[float, float]:
        raise NotImplementedError


class DTUProReader(DTUReader):
    """Hoymiles DTU-Pro – proprietäre HTTP-API."""

    def __init__(self, host: str, timeout: int = 5,
                 username: str = "", password: str = ""):
        self.url = f"http://{host}/api/status"
        self.timeout = timeout
        self.auth = (username, password) if username else None

    def read(self) -> tuple[float, float]:
        r = requests.get(self.url, timeout=self.timeout, auth=self.auth)
        r.raise_for_status()
        data = r.json()
        # DTU-Pro liefert unter "dtu" → "power" (W) und "today_energy" (Wh)
        # Fallback-Pfade je nach Firmware-Version
        try:
            power_w  = float(data["dtu"]["power"])
            energy_wh = float(data["dtu"]["today_energy"])
        except (KeyError, TypeError):
            # Ältere Firmware: direkt im Root
            power_w  = float(data.get("power", 0))
            energy_wh = float(data.get("today_energy", 0))
        return power_w, energy_wh


class OpenDTUReader(DTUReader):
    """OpenDTU – offene Firmware auf Hoymiles-kompatiblen ESP32-Geräten.

    API-Endpunkt: GET /api/livedata/status
    Relevante Felder im JSON:
      total.Power.v      – Gesamtleistung aller Wechselrichter [W]
      total.YieldTotal.v – Gesamtertrag aller Wechselrichter [kWh]
    """

    def __init__(self, host: str, timeout: int = 5,
                 username: str = "", password: str = ""):
        self.url = f"http://{host}/api/livedata/status"
        self.timeout = timeout
        self.auth = (username, password) if username else None

    def read(self) -> tuple[float, float]:
        r = requests.get(self.url, timeout=self.timeout, auth=self.auth)
        r.raise_for_status()
        data = r.json()
        # Gesamtwerte direkt aus dem "total"-Block lesen
        total = data.get("total", {})
        power_w   = float(total.get("Power",      {}).get("v", 0))
        energy_wh = float(total.get("YieldTotal", {}).get("v", 0)) * 1000
        return power_w, energy_wh


class AhoyDTUReader(DTUReader):
    """AhoyDTU – alternative Open-Source-Firmware."""

    def __init__(self, host: str, timeout: int = 5,
                 username: str = "", password: str = ""):
        self.url_record = f"http://{host}/api/record/live"
        self.timeout = timeout
        self.auth = (username, password) if username else None

    def read(self) -> tuple[float, float]:
        r = requests.get(self.url_record, timeout=self.timeout, auth=self.auth)
        r.raise_for_status()
        data = r.json()
        # AhoyDTU liefert unter "inverter" → Liste von Wechselrichtern
        total_power_w   = 0.0
        total_energy_wh = 0.0
        for inv in data.get("inverter", []):
            # Felder: "P_AC" (W), "YieldDay" (Wh)
            total_power_w   += float(inv.get("P_AC", 0))
            total_energy_wh += float(inv.get("YieldDay", 0))
        return total_power_w, total_energy_wh


DTU_TYPES: dict[str, type[DTUReader]] = {
    "dtupro":  DTUProReader,
    "opendtu": OpenDTUReader,
    "ahoydtu": AhoyDTUReader,
}


# ---------------------------------------------------------------------------
# UDP-Sender
# ---------------------------------------------------------------------------

class EMETERSender:
    """Sendet SMA EMETER-Pakete per UDP-Multicast."""

    def __init__(
        self,
        serial: int,
        mcast_addr: str = SMA_EMETER_MCAST_ADDR,
        port: int       = SMA_EMETER_UDP_PORT,
        interface: str  = "",
    ):
        self.serial     = serial
        self.mcast_addr = mcast_addr
        self.port       = port
        self.interface  = interface
        self._sock      = self._create_socket()

    def _create_socket(self) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 32)
        if self.interface:
            # Interface kann als IP-Adresse ("192.168.10.12") oder
            # Interface-Name ("eth0", "br0") angegeben werden.
            if re.match(r'^\d+\.\d+\.\d+\.\d+$', self.interface):
                # Sieht aus wie eine IP-Adresse → IP_MULTICAST_IF
                sock.setsockopt(
                    socket.IPPROTO_IP,
                    socket.IP_MULTICAST_IF,
                    socket.inet_aton(self.interface),
                )
            else:
                # Interface-Name → SO_BINDTODEVICE (braucht NET_RAW capability)
                sock.setsockopt(
                    socket.SOL_SOCKET,
                    socket.SO_BINDTODEVICE,
                    self.interface.encode(),
                )
        return sock

    def _ticker(self) -> int:
        return int(time.time() * 1000) & 0xFFFFFFFF  # Unix-ms, auf 32-bit begrenzt

    def send(self, power_w: float, energy_wh: float) -> None:
        pkt = build_emeter_packet(
            serial=self.serial,
            power_w=power_w,
            energy_wh=energy_wh,
            ticker=self._ticker(),
        )
        self._sock.sendto(pkt, (self.mcast_addr, self.port))

    def close(self) -> None:
        self._sock.close()


# ---------------------------------------------------------------------------
# Hauptschleife
# ---------------------------------------------------------------------------

def run(
    dtu: DTUReader,
    sender: EMETERSender,
    interval: float,
    log: logging.Logger,
) -> None:
    log.info("Bridge gestartet – sende alle %.1f s an %s:%d",
             interval, SMA_EMETER_MCAST_ADDR, SMA_EMETER_UDP_PORT)

    while True:
        try:
            power_w, energy_wh = dtu.read()

            sender.send(power_w, energy_wh)
            log.info("Gesendet: %.1f W  |  %.3f kWh gesamt", power_w, energy_wh / 1000)

        except requests.RequestException as exc:
            log.warning("DTU nicht erreichbar: %s", exc)
            sender.send(0.0, 0.0)

        except Exception as exc:  # pylint: disable=broad-except
            log.error("Fehler: %s", exc)

        time.sleep(interval)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Hoymiles DTU → SMA EMETER Speedwire Bridge",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--dtu-type",
        choices=list(DTU_TYPES.keys()),
        default=os.environ.get("DTU_TYPE", "opendtu"),
        help="DTU-Typ / Firmware  [env: DTU_TYPE]",
    )
    parser.add_argument(
        "--dtu-host",
        default=os.environ.get("DTU_HOST", "192.168.1.100"),
        help="IP-Adresse oder Hostname des DTU  [env: DTU_HOST]",
    )
    parser.add_argument(
        "--dtu-timeout",
        type=int,
        default=int(os.environ.get("DTU_TIMEOUT", "5")),
        help="HTTP-Timeout in Sekunden  [env: DTU_TIMEOUT]",
    )
    parser.add_argument(
        "--dtu-user",
        default=os.environ.get("DTU_USER", ""),
        help="Benutzername für Basic Auth  [env: DTU_USER]",
    )
    parser.add_argument(
        "--dtu-password",
        default=os.environ.get("DTU_PASSWORD", ""),
        help="Passwort für Basic Auth  [env: DTU_PASSWORD]",
    )
    parser.add_argument(
        "--serial",
        type=int,
        default=int(os.environ.get("EMETER_SERIAL", "900000001")),
        help="Seriennummer des virtuellen EMETER (32-bit, im LAN eindeutig)  [env: EMETER_SERIAL]",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=float(os.environ.get("EMETER_INTERVAL", "5.0")),
        help="Sendeintervall in Sekunden  [env: EMETER_INTERVAL]",
    )
    parser.add_argument(
        "--interface",
        default=os.environ.get("EMETER_INTERFACE", ""),
        help="Interface für Multicast: IP-Adresse ('192.168.10.12') oder Name ('eth0', 'br0')  [env: EMETER_INTERFACE]",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("LOG_LEVEL", "INFO"),
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log-Level  [env: LOG_LEVEL]",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger("bridge")

    log.info("=== Hoymiles → SMA EMETER Bridge ===")
    log.info("  DTU type     : %s", args.dtu_type)
    log.info("  DTU host     : %s", args.dtu_host)
    log.info("  DTU timeout  : %s s", args.dtu_timeout)
    log.info("  DTU user     : %s", args.dtu_user or "(none)")
    log.info("  DTU password : %s", "***" if args.dtu_password else "(none)")
    log.info("  EMETER serial: %s", args.serial)
    log.info("  Interval     : %s s", args.interval)
    log.info("  Interface    : %s", args.interface or "(default)")
    log.info("  Log level    : %s", args.log_level)

    dtu_class = DTU_TYPES[args.dtu_type]
    dtu = dtu_class(
        host=args.dtu_host,
        timeout=args.dtu_timeout,
        username=args.dtu_user,
        password=args.dtu_password,
    )
    sender = EMETERSender(
        serial=args.serial,
        interface=args.interface,
    )

    try:
        run(dtu=dtu, sender=sender, interval=args.interval, log=log)
    except KeyboardInterrupt:
        log.info("Beendet.")
    finally:
        sender.close()


if __name__ == "__main__":
    main()
