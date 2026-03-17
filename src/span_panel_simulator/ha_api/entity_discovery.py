"""Entity discovery — map SPAN panel circuits to HA recorder entities.

.. deprecated::
    Superseded by :mod:`span_panel_simulator.ha_api.manifest`, which uses
    the ``span_panel.export_circuit_manifest`` service instead of
    pattern-matching entity states.  No callers remain after the routes
    migration.  Kept for reference; will be removed in a follow-up.

Queries HA's states API to find SPAN panel power and energy sensor
entities.  Produces a mapping from circuit keys to HA entity IDs that
the profile builder can use to query recorder statistics.

This replaces the integration's role as the authority on the circuit-to-
entity mapping.  The add-on discovers the mapping on demand via the HA
states API, so it stays correct even when the integration renames
entities or the panel configuration changes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from span_panel_simulator.ha_api.client import HAClient

_LOGGER = logging.getLogger(__name__)

# Entity ID prefix for SPAN panel sensors.
_SPAN_SENSOR_PREFIX = "sensor.span_panel_"


@dataclass(frozen=True, slots=True)
class CircuitEntityMapping:
    """Associates a circuit key with its HA entity IDs."""

    circuit_key: str  # e.g. "kitchen_disposal"
    circuit_name: str  # human-readable, e.g. "Kitchen Disposal"
    power_entity_id: str | None  # e.g. "sensor.span_panel_kitchen_disposal_power"
    energy_entity_id: str | None  # e.g. "sensor.span_panel_kitchen_disposal_energy"


@dataclass(frozen=True, slots=True)
class PanelEntityMap:
    """Complete mapping for a discovered SPAN panel."""

    panel_name: str
    circuits: list[CircuitEntityMapping]

    def power_statistic_ids(self) -> list[str]:
        """Return all power entity IDs suitable for recorder queries."""
        return [c.power_entity_id for c in self.circuits if c.power_entity_id is not None]

    def energy_statistic_ids(self) -> list[str]:
        """Return all energy entity IDs suitable for recorder queries."""
        return [c.energy_entity_id for c in self.circuits if c.energy_entity_id is not None]


async def discover_span_panel(
    client: HAClient,
) -> PanelEntityMap | None:
    """Discover SPAN panel circuit entities from HA's states API.

    Finds all ``sensor.span_panel_*`` entities with power (W) or energy
    (Wh/kWh) units of measurement, groups them by circuit, and returns
    a mapping.

    Returns ``None`` if no SPAN panel entities are found.
    """
    states = await client.async_get_states()

    # Collect power and energy sensors keyed by circuit
    circuit_groups: dict[str, dict[str, str]] = {}
    circuit_names: dict[str, str] = {}

    for state in states:
        entity_id = str(state.get("entity_id", ""))
        if not entity_id.startswith(_SPAN_SENSOR_PREFIX):
            continue

        attrs = state.get("attributes")
        if not isinstance(attrs, dict):
            continue

        unit = str(attrs.get("unit_of_measurement", ""))
        friendly_name = str(attrs.get("friendly_name", ""))

        if unit == "W":
            key = _circuit_key(entity_id, "_power")
            if key:
                circuit_groups.setdefault(key, {})["power"] = entity_id
                circuit_names[key] = _derive_circuit_name(friendly_name, " Power")
        elif unit in ("Wh", "kWh"):
            # The SPAN integration creates _consumed_energy, _produced_energy,
            # and _net_energy variants per circuit.  We prefer consumed for
            # profile building (it tracks imported energy).
            for suffix, etype in (
                ("_consumed_energy", "consumed"),
                ("_produced_energy", "produced"),
                ("_net_energy", "net"),
                ("_energy", "energy"),
            ):
                key = _circuit_key(entity_id, suffix)
                if key is None:
                    continue
                circuit_groups.setdefault(key, {})[etype] = entity_id
                if key not in circuit_names:
                    circuit_names[key] = _derive_circuit_name(
                        friendly_name, f" {etype.title()} Energy"
                    )
                break  # only match the first suffix

    if not circuit_groups:
        _LOGGER.warning("No SPAN panel circuit entities found in HA")
        return None

    # Skip panel-level aggregates (current_power, feed_through_power, etc.)
    # by only including circuits that have both power AND energy sensors,
    # or that have a name suggesting they are a circuit.
    # Panel-level sensors don't have matching energy counterparts with
    # the same circuit key pattern.

    mappings: list[CircuitEntityMapping] = []
    for key in sorted(circuit_groups):
        group = circuit_groups[key]
        # Only include entries that have a power sensor — these are
        # actual circuits.  Energy-only entries (consumed/net/produced
        # without a matching power sensor) are sub-entities, not
        # independent circuits.
        if "power" not in group:
            continue
        name = circuit_names.get(key, key)
        # Prefer consumed energy for profiles; fall back to energy/net
        energy_id = group.get("consumed") or group.get("energy") or group.get("net")
        mappings.append(
            CircuitEntityMapping(
                circuit_key=key,
                circuit_name=name,
                power_entity_id=group.get("power"),
                energy_entity_id=energy_id,
            )
        )

    # Derive a panel name from the common prefix
    panel_name = "SPAN Panel"

    _LOGGER.info(
        "Discovered %d SPAN circuit mappings (%d with power, %d with energy)",
        len(mappings),
        sum(1 for m in mappings if m.power_entity_id),
        sum(1 for m in mappings if m.energy_entity_id),
    )

    return PanelEntityMap(
        panel_name=panel_name,
        circuits=mappings,
    )


def _circuit_key(entity_id: str, suffix: str) -> str | None:
    """Extract the circuit key by stripping prefix and suffix.

    ``sensor.span_panel_kitchen_disposal_power`` -> ``kitchen_disposal``
    ``sensor.span_panel_spa_energy`` -> ``spa``
    """
    if not entity_id.endswith(suffix):
        return None
    if not entity_id.startswith(_SPAN_SENSOR_PREFIX):
        return None
    return entity_id[len(_SPAN_SENSOR_PREFIX) : -len(suffix)]


def _derive_circuit_name(friendly_name: str, strip_suffix: str) -> str:
    """Derive a human-readable circuit name from the HA friendly name.

    ``"SPAN Panel Kitchen Disposal Power"`` -> ``"Kitchen Disposal"``
    """
    # Strip the suffix first
    name = friendly_name
    if name.endswith(strip_suffix):
        name = name[: -len(strip_suffix)]

    # Strip common panel prefixes
    for prefix in ("SPAN Panel ", "Span Panel "):
        if name.startswith(prefix):
            name = name[len(prefix) :]
            break

    return name.strip() or friendly_name
