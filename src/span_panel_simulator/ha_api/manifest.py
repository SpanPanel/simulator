"""Circuit manifest — typed wrapper around the SPAN integration service.

Calls ``span_panel.export_circuit_manifest`` to retrieve all panels with
their circuits, entity IDs, template names, device types, and tabs.  This
replaces the fragile entity-discovery approach that reverse-engineers entity
IDs by pattern-matching HA states.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from span_panel_simulator.ha_api.client import HAClient

_LOGGER = logging.getLogger(__name__)

# Device types that are hardware-driven and should not receive usage profiles.
_NON_PROFILE_DEVICE_TYPES = frozenset({"pv", "battery", "evse"})


@dataclass(frozen=True, slots=True)
class CircuitManifestEntry:
    """A single circuit from the integration's manifest."""

    entity_id: str
    template: str
    device_type: str
    tabs: list[int]


@dataclass(frozen=True, slots=True)
class PanelManifest:
    """All circuits for a single physical SPAN panel."""

    serial: str
    host: str
    circuits: list[CircuitManifestEntry]

    def profile_circuits(self) -> list[CircuitManifestEntry]:
        """Circuits eligible for profile building (excludes pv/battery/evse)."""
        return [c for c in self.circuits if c.device_type not in _NON_PROFILE_DEVICE_TYPES]

    def profile_entity_ids(self) -> list[str]:
        """Entity IDs for profile-eligible circuits."""
        return [c.entity_id for c in self.profile_circuits()]

    def entity_to_template(self) -> dict[str, str]:
        """Map entity_id -> template name for re-keying profile builder output."""
        return {c.entity_id: c.template for c in self.circuits}


def _parse_panel(raw: dict[str, object]) -> PanelManifest | None:
    """Parse a single panel dict from the service response."""
    serial = raw.get("serial")
    if not isinstance(serial, str) or not serial:
        _LOGGER.warning("Panel entry missing serial: %s", raw)
        return None

    raw_host = raw.get("host")
    host = raw_host if isinstance(raw_host, str) else ""

    raw_circuits = raw.get("circuits")
    if not isinstance(raw_circuits, list):
        _LOGGER.warning("Panel %s has no circuits list", serial)
        return PanelManifest(serial=serial, host=host, circuits=[])

    entries: list[CircuitManifestEntry] = []
    for item in raw_circuits:
        if not isinstance(item, dict):
            continue

        entity_id = item.get("entity_id")
        template = item.get("template")
        if not isinstance(entity_id, str) or not isinstance(template, str):
            continue

        device_type = str(item.get("device_type", "consumer"))
        raw_tabs = item.get("tabs")
        tabs = list(raw_tabs) if isinstance(raw_tabs, list) else []

        entries.append(
            CircuitManifestEntry(
                entity_id=entity_id,
                template=template,
                device_type=device_type,
                tabs=tabs,
            )
        )

    return PanelManifest(serial=serial, host=host, circuits=entries)


async def fetch_all_manifests(client: HAClient) -> list[PanelManifest]:
    """Call export_circuit_manifest and parse the response into typed manifests."""
    response = await client.async_call_service(
        "span_panel",
        "export_circuit_manifest",
        return_response=True,
    )

    if not isinstance(response, dict):
        _LOGGER.warning("Unexpected manifest response type: %s", type(response).__name__)
        return []

    _LOGGER.debug("Manifest response keys: %s", list(response.keys()))

    # The service response may be at different nesting levels depending
    # on the HA REST API version and call path:
    #   - Direct: {"panels": [...]}
    #   - REST wrapped: {"response": {"panels": [...]}}
    #   - Legacy wrapped: {"service_response": {"panels": [...]}}
    raw_panels = response.get("panels")
    if not isinstance(raw_panels, list):
        for wrapper_key in ("response", "service_response"):
            nested = response.get(wrapper_key)
            if isinstance(nested, dict):
                raw_panels = nested.get("panels")
                if isinstance(raw_panels, list):
                    break
        if not isinstance(raw_panels, list):
            _LOGGER.warning("No panels list in manifest response: %s", list(response.keys()))
            return []

    manifests: list[PanelManifest] = []
    for raw_panel in raw_panels:
        if not isinstance(raw_panel, dict):
            continue
        manifest = _parse_panel(raw_panel)
        if manifest is not None:
            manifests.append(manifest)

    _LOGGER.info("Fetched manifest for %d panel(s)", len(manifests))
    return manifests
