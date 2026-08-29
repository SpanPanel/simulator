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

# Auto-detect the address a client on the LAN reaches this add-on at. It goes
# into the leaf certificate's SAN and into the mDNS advertisement, so getting it
# wrong leaves no address a client can verify us by.
#
# This ran `ip route | awk '/default/ { print $3 }'` and took the *gateway*. The
# reasoning held for a bridge-networked container, where the default gateway is
# the host -- but this add-on sets `host_network: true` (config.yaml), so the
# container shares the host's network namespace and reads the host's routing
# table. `$3` of `default via 192.168.65.1 dev eth0` is then the upstream
# router: a neighbouring device, named in our certificate, that is not us.
#
# `ip route get` is a routing-table lookup rather than a probe -- it sends no
# packets and needs nothing at the far address to be reachable. It answers the
# question that actually matters, which source address the kernel would put on a
# reply, and stays right on an interface holding several addresses where taking
# the first one listed would not.
#
#   $ ip -4 route get 1.1.1.1
#   1.1.1.1 via 192.168.65.1 dev eth0 src 192.168.65.19 uid 0
#                                         ^^^^^^^^^^^^^ what we want
#
# Control characters are stripped because some container `ip` implementations
# emit trailing non-printables, which would break cert generation downstream.
detect_advertise_address() {
    ip -4 route get 1.1.1.1 2>/dev/null \
        | awk '{ for (i = 1; i < NF; i++) if ($i == "src") { print $(i + 1); exit } }' \
        | tr -d '[:cntrl:]' || true
}

# An address supplied by the environment wins over detection: scripts/run-local.sh
# sets one, and an operator on a multi-homed host may need to pick which of its
# addresses the panel is known by.
ADVERTISE_ADDRESS="${ADVERTISE_ADDRESS:-$(detect_advertise_address)}"
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
import os
from span_panel_simulator.certs import generate_certificates
from pathlib import Path
addr = os.environ.get('ADVERTISE_ADDRESS') or None
generate_certificates(Path(os.environ['CERT_DIR']), advertise_address=addr)
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
