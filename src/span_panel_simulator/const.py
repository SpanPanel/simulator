"""Constants for the standalone eBus simulator."""

from __future__ import annotations

# Default ports — offset from standard ports to avoid collisions with
# Home Assistant (8123), the Mosquitto add-on (1883/8883), and other
# common services when running on the same host.
MQTTS_PORT = 18883
WS_PORT = 19001
WSS_PORT = 19002
DEFAULT_BASE_HTTP_PORT = 8081
DASHBOARD_PORT = 18080

# Default simulation parameters
DEFAULT_TICK_INTERVAL_S = 1.0
DEFAULT_LOG_LEVEL = "INFO"

# Bootstrap HTTP paths
PATH_STATUS = "/api/v2/status"
PATH_REGISTER = "/api/v2/auth/register"
PATH_CA_CERT = "/api/v2/certificate/ca"
PATH_HOMIE_SCHEMA = "/api/v2/homie/schema"

# Simulated firmware version
DEFAULT_FIRMWARE_VERSION = "spanos2/sim/01"

# Default MQTT credentials (returned by /register)
DEFAULT_BROKER_USERNAME = "span"
DEFAULT_BROKER_PASSWORD = "sim-password"
