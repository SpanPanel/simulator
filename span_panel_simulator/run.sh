#!/usr/bin/env bash
set -euo pipefail

# Import s6-overlay container environment so Supervisor-injected vars
# (SUPERVISOR_TOKEN, etc.) are visible to this process tree.
if [ -d /run/s6/container_environment ]; then
    for _f in /run/s6/container_environment/*; do
        [ -f "$_f" ] || continue
        export "$(basename "$_f")=$(cat "$_f")"
    done
    unset _f
fi

# Read add-on options from standard HA location
OPTIONS_FILE="/data/options.json"

TICK_INTERVAL=$(jq -r '.tick_interval' "${OPTIONS_FILE}")
LOG_LEVEL=$(jq -r '.log_level' "${OPTIONS_FILE}")
DASHBOARD_ENABLED=$(jq -r '.dashboard_enabled' "${OPTIONS_FILE}")
BASE_HTTP_PORT=$(jq -r '.base_http_port // 8081' "${OPTIONS_FILE}")

# Auto-detect host IP for TLS cert SAN.
# Inside a bridge-networked container the default gateway is the host.
ADVERTISE_ADDRESS=$(ip route | awk '/default/ { print $3 }' || true)
export ADVERTISE_ADDRESS
export CERT_DIR="/data/certs"
export BROKER_USERNAME="span"
export BROKER_PASSWORD="sim-password"

# Ensure config directory exists and seed any missing configs from the image
CONFIG_DIR="/config/span_simulator"
mkdir -p "${CONFIG_DIR}"
for src in /app/configs/*.yaml /app/configs/*.yml; do
    [ -f "${src}" ] || continue
    dest="${CONFIG_DIR}/$(basename "${src}")"
    if [ ! -f "${dest}" ]; then
        cp "${src}" "${dest}"
        echo "Seeded config: $(basename "${src}")"
    fi
done

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

# Build simulator CLI arguments
ARGS=(
    --config-dir "${CONFIG_DIR}"
    --tick-interval "${TICK_INTERVAL}"
    --log-level "${LOG_LEVEL}"
    --base-http-port "${BASE_HTTP_PORT}"
)

if [ "${DASHBOARD_ENABLED}" = "true" ]; then
    ARGS+=(--dashboard-port 18080)
fi

exec python3 -m span_panel_simulator "${ARGS[@]}"
