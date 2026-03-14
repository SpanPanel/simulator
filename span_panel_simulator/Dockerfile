ARG BUILD_FROM=ghcr.io/home-assistant/aarch64-base-python:3.13-alpine3.21
FROM ${BUILD_FROM}

# Install mosquitto and jq (for reading options.json)
RUN apk add --no-cache mosquitto mosquitto-clients jq

WORKDIR /app

# Install Python dependencies
COPY pyproject.toml .
COPY src/ src/
RUN pip install --no-cache-dir .

# Copy Mosquitto directory (for fallback use; run.sh generates config at runtime)
COPY mosquitto/ mosquitto/

# Create directories
RUN mkdir -p /app/certs /app/configs /mosquitto/data /data/certs

# Copy add-on entry point
COPY ha-addon/run.sh /run.sh
RUN chmod +x /run.sh

EXPOSE 18883 8081 18080

LABEL io.hass.name="SPAN Panel Simulator"
LABEL io.hass.description="Simulates a SPAN electrical panel for development and testing"
LABEL io.hass.type="addon"
LABEL io.hass.version="0.1.0"

CMD ["/run.sh"]
