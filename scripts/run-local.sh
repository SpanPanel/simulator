#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "${SCRIPT_DIR}")"

usage() {
    cat <<EOF
Usage: $(basename "$0") [OPTIONS]

Run the SPAN panel simulator natively on macOS.
Starts Mosquitto and the simulator directly — no container layer.
Ports bind to the host's LAN interface for full mDNS visibility.

Prerequisites: brew install mosquitto

Options:
  --stop        Stop the running simulator and Mosquitto
  --restart     Stop then start the simulator and Mosquitto
  --status      Show running processes
  -h, --help    Show this help message

Environment variables:
  ADVERTISE_ADDRESS Override mDNS advertised IP (auto-detected from en0/en1)
  CONFIG_DIR        Config directory (default: ./configs)
  CONFIG_NAME       Specific config file to load (e.g., default_config.yaml)
  TICK_INTERVAL     Simulation tick interval in seconds (default: 1.0)
  LOG_LEVEL         Logging level (default: INFO)
  BROKER_USERNAME   MQTT broker username (default: span)
  BROKER_PASSWORD   MQTT broker password (default: sim-password)
  HTTP_PORT         Bootstrap HTTP port (default: 8081)
  DASHBOARD_PORT    Dashboard UI port (default: 18080)
  BROKER_PORT       MQTTS port (default: 18883)
EOF
    exit 0
}

CERT_DIR="${REPO_DIR}/.local/certs"
MOSQUITTO_DIR="${REPO_DIR}/.local/mosquitto"
PID_DIR="${REPO_DIR}/.local/pids"
VENV_DIR="${REPO_DIR}/.venv"
BROKER_USERNAME="${BROKER_USERNAME:-span}"
BROKER_PASSWORD="${BROKER_PASSWORD:-sim-password}"
HTTP_PORT="${HTTP_PORT:-8081}"
DASHBOARD_PORT="${DASHBOARD_PORT:-18080}"
ADVERTISE_HTTP_PORT="${ADVERTISE_HTTP_PORT:-${HTTP_PORT}}"
BROKER_PORT="${BROKER_PORT:-18883}"

get_host_ip() {
    ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo ""
}

ensure_prerequisites() {
    if ! command -v mosquitto &>/dev/null; then
        echo "Error: mosquitto not installed. Install with: brew install mosquitto"
        exit 1
    fi
    if ! command -v uv &>/dev/null; then
        echo "Error: uv not found. Install with: brew install uv"
        exit 1
    fi
}

ensure_venv() {
    echo "==> Syncing dependencies..."
    uv sync --quiet
    # shellcheck disable=SC1091
    source "${VENV_DIR}/bin/activate"
}

generate_certs() {
    echo "==> Checking TLS certificates..."
    mkdir -p "${CERT_DIR}"
    python3 -c "
from span_panel_simulator.certs import generate_certificates
from pathlib import Path
generate_certificates(Path('${CERT_DIR}'), advertise_address='${ADVERTISE_ADDR}')
"
}

setup_mosquitto() {
    mkdir -p "${MOSQUITTO_DIR}" "${PID_DIR}"

    # Password file (remove first — mosquitto_passwd -c may fail if file exists)
    rm -f "${MOSQUITTO_DIR}/passwd"
    mosquitto_passwd -b -c "${MOSQUITTO_DIR}/passwd" "${BROKER_USERNAME}" "${BROKER_PASSWORD}" 2>/dev/null

    # Config
    cat > "${MOSQUITTO_DIR}/mosquitto.conf" <<CONF
listener ${BROKER_PORT}
cafile ${CERT_DIR}/ca.crt
certfile ${CERT_DIR}/server.crt
keyfile ${CERT_DIR}/server.key
require_certificate false

allow_anonymous false
password_file ${MOSQUITTO_DIR}/passwd

persistence false

log_dest stdout
log_type warning
log_type error
log_type notice

pid_file ${PID_DIR}/mosquitto.pid
CONF
}

start_mosquitto() {
    if [[ -f "${PID_DIR}/mosquitto.pid" ]] && kill -0 "$(cat "${PID_DIR}/mosquitto.pid")" 2>/dev/null; then
        echo "==> Mosquitto already running (pid $(cat "${PID_DIR}/mosquitto.pid"))"
        return
    fi
    echo "==> Starting Mosquitto on port ${BROKER_PORT}..."
    mosquitto -c "${MOSQUITTO_DIR}/mosquitto.conf" -d
    sleep 1
    if [[ -f "${PID_DIR}/mosquitto.pid" ]]; then
        echo "==> Mosquitto started (pid $(cat "${PID_DIR}/mosquitto.pid"))"
    else
        echo "Error: Mosquitto failed to start"
        exit 1
    fi
}

stop_all() {
    echo "==> Stopping simulator..."
    if [[ -f "${PID_DIR}/simulator.pid" ]]; then
        kill "$(cat "${PID_DIR}/simulator.pid")" 2>/dev/null || true
        rm -f "${PID_DIR}/simulator.pid"
    fi
    echo "==> Stopping Mosquitto..."
    if [[ -f "${PID_DIR}/mosquitto.pid" ]]; then
        kill "$(cat "${PID_DIR}/mosquitto.pid")" 2>/dev/null || true
        rm -f "${PID_DIR}/mosquitto.pid"
    fi
    echo "==> Stopped"
}

show_status() {
    echo "==> Mosquitto:"
    if [[ -f "${PID_DIR}/mosquitto.pid" ]] && kill -0 "$(cat "${PID_DIR}/mosquitto.pid")" 2>/dev/null; then
        echo "  Running (pid $(cat "${PID_DIR}/mosquitto.pid"))"
    else
        echo "  Not running"
    fi
    echo ""
    echo "==> Simulator:"
    if [[ -f "${PID_DIR}/simulator.pid" ]] && kill -0 "$(cat "${PID_DIR}/simulator.pid")" 2>/dev/null; then
        echo "  Running (pid $(cat "${PID_DIR}/simulator.pid"))"
    else
        echo "  Not running"
    fi
}

run_simulator() {
    local advertise_addr="${ADVERTISE_ADDR}"

    local config_dir="${CONFIG_DIR:-${REPO_DIR}/configs}"

    echo "==> Starting simulator..."
    echo "    Config:     ${config_dir}"
    echo "    HTTP:       ${advertise_addr}:${HTTP_PORT}"
    echo "    Dashboard:  http://${advertise_addr:-localhost}:${DASHBOARD_PORT}"
    echo "    MQTTS:      ${advertise_addr}:${BROKER_PORT}"
    if [[ -n "${advertise_addr}" ]]; then
        echo "    mDNS:    ${advertise_addr}"
    fi

    "${VENV_DIR}/bin/python3" -m span_panel_simulator \
        --config-dir "${config_dir}" \
        --http-port "${HTTP_PORT}" \
        --broker-port "${BROKER_PORT}" \
        --broker-username "${BROKER_USERNAME}" \
        --broker-password "${BROKER_PASSWORD}" \
        --cert-dir "${CERT_DIR}" \
        --dashboard-port "${DASHBOARD_PORT}" \
        --tick-interval "${TICK_INTERVAL:-1.0}" \
        --log-level "${LOG_LEVEL:-INFO}" \
        ${advertise_addr:+--advertise-address "${advertise_addr}"} \
        --advertise-http-port "${ADVERTISE_HTTP_PORT}" \
        ${CONFIG_NAME:+--config "${CONFIG_NAME}"} &

    local sim_pid=$!
    echo "${sim_pid}" > "${PID_DIR}/simulator.pid"
    echo "==> Simulator started (pid ${sim_pid})"
    echo ""
    echo "    Stop:    $(basename "$0") --stop"
    echo "    Reload:  curl -X POST http://${advertise_addr:-localhost}:${HTTP_PORT}/admin/reload"
    echo ""

    # Wait for the simulator (foreground)
    wait "${sim_pid}" || true
    rm -f "${PID_DIR}/simulator.pid"
}

# --- Main ---

ACTION="run"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --stop)    ACTION="stop"; shift ;;
        --restart) ACTION="restart"; shift ;;
        --status)  ACTION="status"; shift ;;
        -h|--help) usage ;;
        *) echo "Unknown option: $1"; usage ;;
    esac
done

case "${ACTION}" in
    stop)
        stop_all
        ;;
    restart)
        stop_all
        sleep 1
        ADVERTISE_ADDR="${ADVERTISE_ADDRESS:-$(get_host_ip)}"
        ensure_prerequisites
        ensure_venv
        generate_certs
        setup_mosquitto
        start_mosquitto
        run_simulator
        ;;
    status)
        show_status
        ;;
    run)
        ADVERTISE_ADDR="${ADVERTISE_ADDRESS:-$(get_host_ip)}"
        ensure_prerequisites
        ensure_venv
        generate_certs
        setup_mosquitto
        start_mosquitto
        run_simulator
        ;;
esac
