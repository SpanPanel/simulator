#!/usr/bin/env bash
set -euo pipefail

CERT_DIR="${CERT_DIR:-/app/certs}"
MOSQUITTO_CONF="/app/mosquitto/mosquitto.conf"
BROKER_USERNAME="${BROKER_USERNAME:-span}"
BROKER_PASSWORD="${BROKER_PASSWORD:-sim-password}"

ADVERTISE_ADDRESS="${ADVERTISE_ADDRESS:-}"

echo "==> Generating TLS certificates..."
python -c "
from span_panel_simulator.certs import generate_certificates
from pathlib import Path
addr = '${ADVERTISE_ADDRESS}' or None
generate_certificates(Path('${CERT_DIR}'), advertise_address=addr)
"

# Mosquitto runs as the 'mosquitto' user — ensure it can read certs
chmod 644 "${CERT_DIR}"/*.crt "${CERT_DIR}"/*.key

echo "==> Setting up Mosquitto credentials..."
mosquitto_passwd -b -c /app/mosquitto/passwd "${BROKER_USERNAME}" "${BROKER_PASSWORD}"
chmod 600 /app/mosquitto/passwd
chown mosquitto:mosquitto /app/mosquitto/passwd

# Copy config template
cp /app/mosquitto/mosquitto.conf.template "${MOSQUITTO_CONF}"

echo "==> Starting Mosquitto..."
mosquitto -c "${MOSQUITTO_CONF}" -d

# Give Mosquitto a moment to bind
sleep 1

echo "==> Starting simulator..."
export CERT_DIR
exec python -m span_panel_simulator "$@"
