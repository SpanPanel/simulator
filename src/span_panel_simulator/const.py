"""Constants for the standalone eBus simulator."""

from __future__ import annotations

# Default ports
MQTTS_PORT = 8883
WS_PORT = 9001
WSS_PORT = 9002
HTTPS_PORT = 80

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
