"""Constants for the standalone eBus simulator."""

from __future__ import annotations

# Default ports — offset from standard ports to avoid collisions with
# Home Assistant (8123), the Mosquitto add-on (1883/8883), and other
# common services when running on the same host.
MQTTS_PORT = 18883
WS_PORT = 19001
WSS_PORT = 19002
DEFAULT_BASE_HTTP_PORT = 8081

# Real panels serve the bootstrap API over plain HTTP on 80 and over TLS on 443,
# and keep both across a firmware upgrade. Simulated panels sit on non-standard
# HTTP ports so they can share a host, and their TLS port is derived from it by a
# fixed offset rather than allocated separately: a client that has been told one
# port can then be told the other by the same discovery record, with no second
# pool to keep in step.
#
# The offset matches panelbench. Stopping this simulator and starting panelbench
# rehearses a firmware upgrade on one panel, so the two have to agree on where a
# given panel serves TLS -- a panel that moved its TLS port across an upgrade is
# not a panel that was upgraded.
DEFAULT_HTTPS_PORT = 443
HTTPS_PORT_OFFSET = 1000


def https_port_for(http_port: int) -> int:
    """Return the TLS port a panel serves on given its bootstrap HTTP port.

    One definition, because three parties need the same answer: the panel that
    binds the listener, the discovery records that publish it, and anyone
    reading a port out of the log to add a panel by hand.
    """
    return http_port + HTTPS_PORT_OFFSET


DASHBOARD_PORT = 18080

# Default simulation parameters
DEFAULT_TICK_INTERVAL_S = 1.0
DEFAULT_LOG_LEVEL = "INFO"

# Bootstrap HTTP paths
PATH_STATUS = "/api/v2/status"
PATH_REGISTER = "/api/v2/auth/register"
PATH_CA_CERT = "/api/v2/certificate/ca"
PATH_HOMIE_SCHEMA = "/api/v2/homie/schema"


# Simulated firmware version — derived from the package version so that
# HTTP bootstrap, MQTT snapshots, and mDNS all report the same value.
def _firmware_version() -> str:
    from span_panel_simulator import __version__

    return f"sim/v{__version__}"


DEFAULT_FIRMWARE_VERSION = _firmware_version()

# Default MQTT credentials (returned by /register)
DEFAULT_BROKER_USERNAME = "span"
DEFAULT_BROKER_PASSWORD = "sim-password"
