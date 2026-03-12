#!/usr/bin/env bash
set -euo pipefail

CERT_DIR="${CERT_DIR:-/app/certs}"
MOSQUITTO_CONF="/app/mosquitto/mosquitto.conf"
BROKER_USERNAME="${BROKER_USERNAME:-span}"
BROKER_PASSWORD="${BROKER_PASSWORD:-sim-password}"

echo "==> Generating TLS certificates..."
python -c "
from span_panel_simulator.certs import generate_certificates
from pathlib import Path
generate_certificates(Path('${CERT_DIR}'))
"

echo "==> Setting up Mosquitto credentials..."
# Create password file
mosquitto_passwd -b -c /app/mosquitto/passwd "${BROKER_USERNAME}" "${BROKER_PASSWORD}"

# Copy config template
cp /app/mosquitto/mosquitto.conf.template "${MOSQUITTO_CONF}"

echo "==> Starting Mosquitto..."
mosquitto -c "${MOSQUITTO_CONF}" -d

# Give Mosquitto a moment to bind
sleep 1

echo "==> Starting simulator..."
exec python -m span_panel_simulator "$@"
