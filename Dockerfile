FROM python:3.12-alpine

WORKDIR /app

# Install dependencies in a separate layer for better cache reuse
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY hoymiles_sma_bridge.py .

# Run as non-root user
RUN adduser -D appuser
USER appuser

# All configuration via environment variables (overridable with CLI args)
ENV DTU_TYPE=opendtu \
    DTU_HOST=192.168.1.100 \
    DTU_TIMEOUT=5 \
    DTU_USER="" \
    DTU_PASSWORD="" \
    EMETER_SERIAL=900000001 \
    EMETER_INTERVAL=5.0 \
    EMETER_INTERFACE="" \
    LOG_LEVEL=INFO

# -u: unbuffered output so Docker logs show lines immediately
ENTRYPOINT ["python", "-u", "hoymiles_sma_bridge.py"]
