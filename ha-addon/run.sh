#!/usr/bin/env bash
set -euo pipefail

# Read add-on options from standard HA location
OPTIONS_FILE="/data/options.json"

CONFIG_FILE=$(jq -r '.config_file' "${OPTIONS_FILE}")
TICK_INTERVAL=$(jq -r '.tick_interval' "${OPTIONS_FILE}")
LOG_LEVEL=$(jq -r '.log_level' "${OPTIONS_FILE}")
ADVERTISE_ADDRESS=$(jq -r '.advertise_address // empty' "${OPTIONS_FILE}")
DASHBOARD_ENABLED=$(jq -r '.dashboard_enabled' "${OPTIONS_FILE}")

# Detect the HA host IP for mDNS advertisement if not explicitly set.
# Inside a bridge-networked container the default gateway is the host.
if [ -z "${ADVERTISE_ADDRESS}" ]; then
    ADVERTISE_ADDRESS=$(ip route | awk '/default/ { print $3 }' || true)
fi

export ADVERTISE_ADDRESS
export CERT_DIR="/data/certs"
export BROKER_USERNAME="span"
export BROKER_PASSWORD="sim-password"

mkdir -p "${CERT_DIR}"

# Generate TLS certs
python3 -c "
from span_panel_simulator.certs import generate_certificates
from pathlib import Path
addr = '${ADVERTISE_ADDRESS}' or None
generate_certificates(Path('${CERT_DIR}'), advertise_address=addr)
"

chmod 644 "${CERT_DIR}"/*.crt "${CERT_DIR}"/*.key

# Set up Mosquitto credentials
mosquitto_passwd -b -c /app/mosquitto/passwd "${BROKER_USERNAME}" "${BROKER_PASSWORD}"
chmod 600 /app/mosquitto/passwd
chown mosquitto:mosquitto /app/mosquitto/passwd
cp /app/mosquitto/mosquitto.conf.template /app/mosquitto/mosquitto.conf

# Start Mosquitto
mosquitto -c /app/mosquitto/mosquitto.conf -d
sleep 1

# Build simulator CLI arguments
ARGS=(
    --config "/config/${CONFIG_FILE}"
    --tick-interval "${TICK_INTERVAL}"
    --log-level "${LOG_LEVEL}"
    --http-port 8081
)

if [ -n "${ADVERTISE_ADDRESS}" ]; then
    ARGS+=(--advertise-address "${ADVERTISE_ADDRESS}")
fi

if [ "${DASHBOARD_ENABLED}" = "true" ]; then
    ARGS+=(--dashboard-port 18080)
fi

exec python3 -m span_panel_simulator "${ARGS[@]}"
