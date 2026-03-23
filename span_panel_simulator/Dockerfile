ARG BUILD_FROM=ghcr.io/home-assistant/aarch64-base-python:3.13-alpine3.21
FROM ${BUILD_FROM}

# Install mosquitto, jq, and build tools needed for native Python extensions
RUN apk add --no-cache mosquitto mosquitto-clients jq \
    && apk add --no-cache --virtual .build-deps \
       gcc g++ musl-dev cmake make

WORKDIR /app

# Install Python dependencies (then remove build tools to keep image small)
COPY pyproject.toml .
COPY src/ src/
RUN pip install --no-cache-dir . \
    && apk del .build-deps

# Copy Mosquitto directory (for fallback use; run.sh generates config at runtime)
COPY mosquitto/ mosquitto/

# Copy default panel configs (run.sh seeds /config/span_simulator/ from these)
COPY configs/ configs/

# Create directories
RUN mkdir -p /app/certs /mosquitto/data /data/certs

# Copy add-on entry point
COPY span_panel_simulator/run.sh /run.sh
RUN chmod +x /run.sh

EXPOSE 18883 8081 18080

LABEL io.hass.name="SPAN Panel Simulator" \
      io.hass.description="Simulates a SPAN electrical panel for testing and upgrade modeling" \
      io.hass.type="addon" \
      io.hass.version="1.0.5" \
      io.hass.arch="aarch64|amd64"

CMD ["/run.sh"]
