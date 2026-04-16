# Hoymiles → SMA EMETER Bridge

Liest die aktuelle PV-Erzeugung vom Hoymiles DTU und sendet sie als
**SMA Speedwire EMETER v1.0** UDP-Multicast-Paket. Der SMA Home Manager 2.0
erkennt das Gerät als virtuellen Energiemesser und kann den erzeugten Strom
beim Überschussladen der SMA Wallbox berücksichtigen.

```
Hoymiles Balkonkraftwerk (2000 W)
        │
        ▼
   [Hoymiles DTU]  ← HTTP JSON API
        │
        ▼
  [dieses Skript / Docker Container]
        │  UDP Multicast 239.12.255.254:9522
        ▼
[SMA Home Manager 2.0]
        │  erkennt virtuellen Erzeuger
        ▼
  [SMA Wallbox] ← Überschussladen mit bis zu 2000 W mehr
```

[![Build and Push Docker Image](https://github.com/michelde/opendtu-sma-energy-meter/actions/workflows/docker-publish.yml/badge.svg)](https://github.com/michelde/opendtu-sma-energy-meter/actions/workflows/docker-publish.yml)
[![Docker Pulls](https://img.shields.io/docker/pulls/michelmu/opendtu-sma-energy-meter)](https://hub.docker.com/r/michelmu/opendtu-sma-energy-meter)

---

## Inhaltsverzeichnis

- [Docker (empfohlen)](#docker-empfohlen)
- [Direktstart (Python)](#direktstart-python)
- [Unterstützte DTU-Varianten](#unterstützte-dtu-varianten)
- [SMA Sunny Portal – Virtuellen Erzeuger anlegen](#sma-sunny-portal--virtuellen-erzeuger-anlegen)
- [Dauerbetrieb als systemd-Dienst](#dauerbetrieb-als-systemd-dienst-raspberry-pi--linux)
- [Protokoll-Details](#protokoll-details-sma-speedwire-emeter-v10)
- [Fehlerbehebung](#fehlerbehebung)

---

## Docker (empfohlen)

Ein fertiges Multi-Architektur-Image ist auf Docker Hub verfügbar:
`michelmu/opendtu-sma-energy-meter`

Unterstützte Architekturen: `linux/amd64`, `linux/arm64`, `linux/arm/v7` (Raspberry Pi 2/3)

> **Wichtig:** UDP-Multicast erfordert `--network host`. Standard-Port-Mapping (`-p`) funktioniert für Multicast-Traffic nicht.

### Schnellstart mit Docker

```bash
docker run -d \
  --name hoymiles-sma-bridge \
  --network host \
  --restart unless-stopped \
  -e DTU_HOST=192.168.1.100 \
  -e DTU_USER=admin \
  -e DTU_PASSWORD=openDTU42 \
  michelmu/opendtu-sma-energy-meter:latest
```

### Docker Compose (empfohlen für Dauerbetrieb)

```bash
# docker-compose.yml herunterladen und anpassen
curl -O https://raw.githubusercontent.com/michelde/opendtu-sma-energy-meter/main/docker-compose.yml
# DTU_HOST (und ggf. DTU_USER / DTU_PASSWORD) eintragen:
nano docker-compose.yml
# Starten:
docker compose up -d
docker compose logs -f
```

### Umgebungsvariablen

Alle Konfiguration erfolgt über Umgebungsvariablen. CLI-Argumente (`--dtu-host` usw.)
funktionieren weiterhin und haben Vorrang vor den Umgebungsvariablen.

| Variable           | Standard        | Beschreibung                                                                |
|--------------------|-----------------|-----------------------------------------------------------------------------|
| `DTU_TYPE`         | `opendtu`       | DTU-Firmware: `opendtu`, `dtupro` oder `ahoydtu`                           |
| `DTU_HOST`         | `192.168.1.100` | IP-Adresse oder Hostname des DTU                                            |
| `DTU_TIMEOUT`      | `5`             | HTTP-Timeout in Sekunden                                                    |
| `DTU_USER`         | *(leer)*        | Benutzername für HTTP Basic Auth (leer lassen wenn nicht erforderlich)      |
| `DTU_PASSWORD`     | *(leer)*        | Passwort für HTTP Basic Auth (leer lassen wenn nicht erforderlich)          |
| `EMETER_SERIAL`    | `900000001`     | Seriennummer des virtuellen EMETER – muss im LAN eindeutig sein             |
| `EMETER_INTERVAL`  | `5.0`           | Sendeintervall in Sekunden                                                  |
| `EMETER_INTERFACE` | *(leer)*        | Quell-IP für Multicast (nur bei mehreren Netzwerkkarten erforderlich)       |
| `LOG_LEVEL`        | `INFO`          | Log-Verbosity: `DEBUG`, `INFO`, `WARNING` oder `ERROR`                      |

### Image-Tags

| Tag      | Bedeutung                                               |
|----------|---------------------------------------------------------|
| `latest` | Aktuellster Build vom `main`-Branch                     |
| `1.2.3`  | Spezifisches Release (empfohlen für Produktions-Pinning)|
| `1.2`    | Neuester Patch einer Minor-Version                      |

### Logs anzeigen

```bash
docker logs -f hoymiles-sma-bridge
# oder mit docker compose:
docker compose logs -f
```

### Stoppen

```bash
docker compose down
# oder:
docker stop hoymiles-sma-bridge
```

---

## Direktstart (Python)

### Voraussetzungen

- Python 3.9+
- Hoymiles DTU im gleichen LAN wie SMA Home Manager
- SMA Home Manager 2.0 (Firmware ≥ 1.07)

```bash
pip install -r requirements.txt
python3 hoymiles_sma_bridge.py --dtu-type opendtu --dtu-host 192.168.1.100
```

### Alle CLI-Optionen

```
--dtu-type     dtupro | opendtu | ahoydtu    [env: DTU_TYPE,         Standard: opendtu]
--dtu-host     IP oder Hostname des DTU      [env: DTU_HOST,         Standard: 192.168.1.100]
--dtu-timeout  HTTP-Timeout in Sekunden      [env: DTU_TIMEOUT,      Standard: 5]
--dtu-user     Benutzername Basic Auth        [env: DTU_USER,         Standard: (leer)]
--dtu-password Passwort Basic Auth            [env: DTU_PASSWORD,     Standard: (leer)]
--serial       Seriennummer virt. Zähler      [env: EMETER_SERIAL,    Standard: 900000001]
--interval     Sendeintervall in Sekunden     [env: EMETER_INTERVAL,  Standard: 5.0]
--interface    Quell-IP bei mehreren NICs     [env: EMETER_INTERFACE, Standard: (leer)]
--log-level    DEBUG|INFO|WARNING|ERROR       [env: LOG_LEVEL,        Standard: INFO]
```

---

## Unterstützte DTU-Varianten

| DTU-Typ    | `DTU_TYPE`  | API-Endpunkt              | Hinweis                          |
|------------|-------------|---------------------------|----------------------------------|
| `opendtu`  | `opendtu`   | `/api/livedata/status`    | Open-Source ESP32-Firmware       |
| `dtupro`   | `dtupro`    | `/api/status`             | Originale Hoymiles Hardware      |
| `ahoydtu`  | `ahoydtu`   | `/api/record/live`        | Alternative Open-Source-Firmware |

---

## SMA Sunny Portal – Virtuellen Erzeuger anlegen

1. **Sunny Portal** → Konfiguration → Geräteverwaltung
2. Beim SMA Home Manager 2.0 auf das Kontextmenü klicken
3. **„Als Erzeuger konfigurieren"** wählen
4. Erzeugertyp: **PV-Generator**
5. Den neu erscheinenden virtuellen Zähler mit der konfigurierten
   Seriennummer auswählen und speichern

> Der Home Manager erkennt den Zähler, sobald der Container läuft und
> das erste Paket gesendet wurde (nach spätestens `EMETER_INTERVAL` Sekunden).

---

## Dauerbetrieb als systemd-Dienst (Raspberry Pi / Linux)

```bash
sudo cp hoymiles_sma_bridge.py /opt/hoymiles-sma-emeter/
sudo cp hoymiles-sma-bridge.service /etc/systemd/system/

# IP-Adresse in der Service-Datei anpassen:
sudo nano /etc/systemd/system/hoymiles-sma-bridge.service

sudo systemctl daemon-reload
sudo systemctl enable --now hoymiles-sma-bridge
sudo journalctl -u hoymiles-sma-bridge -f
```

---

## Protokoll-Details (SMA Speedwire EMETER v1.0)

| Parameter          | Wert                        |
|--------------------|-----------------------------|
| Transport          | UDP Multicast               |
| Multicast-Adresse  | `239.12.255.254`            |
| Port               | `9522`                      |
| Protocol-ID        | `0x6069`                    |
| SUSyID             | `0x010E` (270)              |
| OBIS 0:2.4.0       | Wirkleistung Einspeisung [0.1 W]  |
| OBIS 0:2.8.0       | Energie Einspeisung [Joule / Ws]  |

---

## Fehlerbehebung

**Zähler wird nicht erkannt:**
- Sicherstellen, dass Container/Skript und SMA Home Manager im gleichen LAN-Segment sind
- Multicast erfordert `--network host` im Docker-Betrieb
- Wireshark/tcpdump: `udp port 9522` – Pakete sichtbar?
- Seriennummer im Portal mit `EMETER_SERIAL` überprüfen

**Keine Erzeugungswerte im Portal:**
- `LOG_LEVEL=DEBUG` aktivieren und DTU-Antwort prüfen
- OpenDTU: Readonly-Zugriff aktivieren oder `DTU_USER`/`DTU_PASSWORD` setzen

**DTU nicht erreichbar:**
- Der Container sendet bei Ausfall `0 W`, damit der HM2 nicht auf alte Werte stecken bleibt
- `LOG_LEVEL=DEBUG` zeigt die rohen HTTP-Antworten und Fehlermeldungen

**Falsche Leistungswerte bei DTU-Pro:**
- API-Pfade können je nach Firmware-Version variieren
- `LOG_LEVEL=DEBUG` zeigt die rohe JSON-Antwort zur Analyse

---

## GitHub Actions / CI

Bei jedem Push auf `main` wird automatisch ein neues Docker-Image gebaut und auf
Docker Hub als `latest` veröffentlicht. Bei `v*`-Tags (z.B. `v1.2.3`) werden
zusätzlich versionierte Tags (`1.2.3`, `1.2`) gepusht.

**Erforderliche GitHub Repository Secrets:**

| Secret               | Beschreibung                        |
|----------------------|-------------------------------------|
| `DOCKERHUB_USERNAME` | Docker Hub Benutzername (`michelmu`)|
| `DOCKERHUB_TOKEN`    | Docker Hub Access Token             |

Token erstellen: Docker Hub → Account Settings → Security → New Access Token
