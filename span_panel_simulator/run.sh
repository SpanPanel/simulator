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

# Ensure config directory exists and has a default config
CONFIG_DIR="/config/span_simulator"
mkdir -p "${CONFIG_DIR}"
if [ ! -f "${CONFIG_DIR}/default_config.yaml" ] && [ -f "/app/configs/default_config.yaml" ]; then
    cp /app/configs/default_config.yaml "${CONFIG_DIR}/default_config.yaml"
    echo "Copied default config to ${CONFIG_DIR}/default_config.yaml"
fi

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
chmod 644 /app/mosquitto/passwd

# Generate Mosquitto config with correct cert paths
cat > /app/mosquitto/mosquitto.conf <<CONF
listener 18883
cafile ${CERT_DIR}/ca.crt
certfile ${CERT_DIR}/server.crt
keyfile ${CERT_DIR}/server.key
require_certificate false

allow_anonymous false
password_file /app/mosquitto/passwd

persistence false

log_dest stdout
log_type warning
log_type error
log_type notice
CONF

# Start Mosquitto
mosquitto -c /app/mosquitto/mosquitto.conf -d
sleep 1

# Split config option into directory and filename
# CONFIG_FILE is e.g. "span_simulator/default_config.yaml"
CONFIG_BASENAME=$(basename "${CONFIG_FILE}")
CONFIG_SUBDIR=$(dirname "${CONFIG_FILE}")

# Build simulator CLI arguments
ARGS=(
    --config-dir "/config/${CONFIG_SUBDIR}"
    --config "${CONFIG_BASENAME}"
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
