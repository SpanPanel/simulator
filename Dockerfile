FROM python:3.12-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends mosquitto mosquitto-clients && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY pyproject.toml .
COPY src/ src/
RUN pip install --no-cache-dir .

# Copy configuration templates
COPY mosquitto/ mosquitto/
COPY scripts/ scripts/

# Create directories
RUN mkdir -p /app/certs /app/configs /mosquitto/data

EXPOSE 18883 19001 19002 8081 18080

ENTRYPOINT ["/app/scripts/entrypoint.sh"]
