"""Home Assistant API client — dual-mode access for add-on and local development.

When running as an HA add-on, the Supervisor injects ``SUPERVISOR_TOKEN``
and the API is reachable at ``http://supervisor/core/api``.  When running
locally (via ``run-local.sh``), the same HA REST API is reached at the
user's HA instance URL with a long-lived access token.

The client abstracts this so callers never need to know which mode is
active.  All methods return the same types regardless of transport.
"""

from __future__ import annotations

from span_panel_simulator.ha_api.client import HAClient
from span_panel_simulator.ha_api.manifest import (
    CircuitManifestEntry,
    PanelManifest,
    fetch_all_manifests,
)
from span_panel_simulator.ha_api.opower import (
    OpowerAccount,
    OpowerCostResult,
    async_discover_opower,
    async_get_opower_cost,
)

__all__ = [
    "CircuitManifestEntry",
    "HAClient",
    "OpowerAccount",
    "OpowerCostResult",
    "PanelManifest",
    "async_discover_opower",
    "async_get_opower_cost",
    "fetch_all_manifests",
]
