"""HomiePublisher — reverse of HomieDeviceConsumer.

Maps SpanPanelSnapshot fields to individual retained MQTT messages following
the Homie v5 convention.  Diffs successive snapshots and publishes only
properties whose values changed.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from span_panel_simulator.homie_const import (
    DESCRIPTION_TOPIC_FMT,
    HOMIE_STATE_INIT,
    HOMIE_STATE_READY,
    PROPERTY_SET_TOPIC_FMT,
    PROPERTY_TOPIC_FMT,
    STATE_TOPIC_FMT,
    TYPE_BESS,
    TYPE_CIRCUIT,
    TYPE_CORE,
    TYPE_EVSE,
    TYPE_LUGS,
    TYPE_POWER_FLOWS,
    TYPE_PV,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from typing import Any

    from span_panel_simulator.models import SpanPanelSnapshot

_LOGGER = logging.getLogger(__name__)

# eBus priority names mapped from simulation v1 names
_PRIORITY_V1_TO_V2: dict[str, str] = {
    "MUST_HAVE": "NEVER",
    "NICE_TO_HAVE": "SOC_THRESHOLD",
    "NON_ESSENTIAL": "OFF_GRID",
    # v2 names pass through
    "NEVER": "NEVER",
    "SOC_THRESHOLD": "SOC_THRESHOLD",
    "OFF_GRID": "OFF_GRID",
    "UNKNOWN": "UNKNOWN",
}

# Node IDs for well-known singleton nodes
NODE_CORE = "core"
NODE_UPSTREAM_LUGS = "upstream-lugs"
NODE_DOWNSTREAM_LUGS = "downstream-lugs"
NODE_BESS = "bess-0"
NODE_PV = "pv-0"
NODE_EVSE = "evse-0"
NODE_POWER_FLOWS = "power-flows"


def _stable_circuit_uuid(circuit_id: str) -> str:
    """Generate a deterministic dashless UUID from a circuit identifier.

    Uses UUID v5 with a fixed namespace so the same circuit_id always produces
    the same UUID across restarts.
    """
    ns = uuid.UUID("a1b2c3d4-e5f6-7890-abcd-ef1234567890")
    return str(uuid.uuid5(ns, circuit_id)).replace("-", "")


def _format_float(value: float, precision: int = 2) -> str:
    """Format a float for MQTT publication."""
    return f"{value:.{precision}f}"


def _format_bool(value: bool) -> str:
    """Format a boolean for Homie convention."""
    return "true" if value else "false"


@dataclass(slots=True)
class _PublishedState:
    """Tracks the last-published value for each topic to enable diffing."""

    values: dict[str, str] = field(default_factory=dict)

    def diff(self, new_values: dict[str, str]) -> dict[str, str]:
        """Return only entries whose value differs from the last publish."""
        changed: dict[str, str] = {}
        for topic, value in new_values.items():
            if self.values.get(topic) != value:
                changed[topic] = value
        self.values.update(new_values)
        return changed


class HomiePublisher:
    """Maps SpanPanelSnapshot to retained MQTT messages.

    Responsibilities:
      1. Build ``$description`` JSON from panel configuration
      2. Generate stable UUIDs for circuits (deterministic from circuit ID)
      3. Convert snapshot fields to Homie property values (correct types, units)
      4. Diff against previous snapshot — publish only changed properties
      5. Handle ``/set`` subscriptions and feed changes back to the simulation
    """

    def __init__(
        self,
        serial_number: str,
        *,
        publish_fn: Callable[[str, str, bool], Coroutine[Any, Any, None]],
    ) -> None:
        """Initialise the publisher.

        Args:
            serial_number: Panel serial (used in topic prefix).
            publish_fn: Async callable ``(topic, payload, retain) -> None``
                        provided by the MQTT client layer.
        """
        self._serial = serial_number
        self._publish = publish_fn
        self._state = _PublishedState()
        # Mapping: simulation circuit_id → dashless UUID used as Homie node_id
        self._circuit_uuid_map: dict[str, str] = {}
        # Reverse map for /set handling
        self._uuid_to_circuit_id: dict[str, str] = {}
        self._description_published = False

    # ------------------------------------------------------------------
    # Topic helpers
    # ------------------------------------------------------------------

    def _prop_topic(self, node: str, prop: str) -> str:
        return PROPERTY_TOPIC_FMT.format(serial=self._serial, node=node, prop=prop)

    def _set_topic(self, node: str, prop: str) -> str:
        return PROPERTY_SET_TOPIC_FMT.format(serial=self._serial, node=node, prop=prop)

    # ------------------------------------------------------------------
    # $description builder
    # ------------------------------------------------------------------

    def _build_description(self, snapshot: SpanPanelSnapshot) -> dict[str, Any]:
        """Build Homie ``$description`` JSON from the current snapshot."""
        nodes: dict[str, dict[str, str]] = {}

        # Core is always present
        nodes[NODE_CORE] = {"type": TYPE_CORE}

        # Lugs
        nodes[NODE_UPSTREAM_LUGS] = {"type": TYPE_LUGS}
        nodes[NODE_DOWNSTREAM_LUGS] = {"type": TYPE_LUGS}

        # Circuits — generate stable UUIDs
        for cid in snapshot.circuits:
            if cid.startswith("unmapped_tab_"):
                continue
            node_uuid = self._ensure_circuit_uuid(cid)
            nodes[node_uuid] = {"type": TYPE_CIRCUIT}

        # Optional peripheral nodes
        if snapshot.battery.soe_percentage is not None:
            nodes[NODE_BESS] = {"type": TYPE_BESS}
        if snapshot.pv.vendor_name is not None or snapshot.pv.nameplate_capacity_w is not None:
            nodes[NODE_PV] = {"type": TYPE_PV}
        for idx in range(len(snapshot.evse)):
            nodes[f"evse-{idx}"] = {"type": TYPE_EVSE}

        # Power flows always present
        nodes[NODE_POWER_FLOWS] = {"type": TYPE_POWER_FLOWS}

        return {"nodes": nodes}

    def _ensure_circuit_uuid(self, circuit_id: str) -> str:
        """Get or create a stable UUID for a simulation circuit ID."""
        if circuit_id not in self._circuit_uuid_map:
            node_uuid = _stable_circuit_uuid(circuit_id)
            self._circuit_uuid_map[circuit_id] = node_uuid
            self._uuid_to_circuit_id[node_uuid] = circuit_id
        return self._circuit_uuid_map[circuit_id]

    # ------------------------------------------------------------------
    # Snapshot → property map
    # ------------------------------------------------------------------

    def _snapshot_to_properties(self, snapshot: SpanPanelSnapshot) -> dict[str, str]:
        """Convert a full snapshot into a flat ``{topic: value}`` mapping."""
        props: dict[str, str] = {}

        self._map_core(snapshot, props)
        self._map_upstream_lugs(snapshot, props)
        self._map_downstream_lugs(snapshot, props)
        self._map_circuits(snapshot, props)
        self._map_bess(snapshot, props)
        self._map_pv(snapshot, props)
        self._map_evse(snapshot, props)
        self._map_power_flows(snapshot, props)

        return props

    def _map_core(self, s: SpanPanelSnapshot, p: dict[str, str]) -> None:
        n = NODE_CORE
        p[self._prop_topic(n, "vendor-name")] = "SPAN"
        p[self._prop_topic(n, "serial-number")] = s.serial_number
        p[self._prop_topic(n, "hardware-version")] = "H201"
        p[self._prop_topic(n, "software-version")] = s.firmware_version
        p[self._prop_topic(n, "door")] = s.door_state or "CLOSED"
        p[self._prop_topic(n, "grid-islandable")] = _format_bool(
            s.grid_islandable if s.grid_islandable is not None else False
        )
        p[self._prop_topic(n, "dominant-power-source")] = s.dominant_power_source or "GRID"
        p[self._prop_topic(n, "relay")] = s.main_relay_state or "CLOSED"
        if s.l1_voltage is not None:
            p[self._prop_topic(n, "l1-voltage")] = _format_float(s.l1_voltage, 1)
        if s.l2_voltage is not None:
            p[self._prop_topic(n, "l2-voltage")] = _format_float(s.l2_voltage, 1)
        if s.main_breaker_rating_a is not None:
            p[self._prop_topic(n, "breaker-rating")] = str(s.main_breaker_rating_a)
        p[self._prop_topic(n, "ethernet")] = _format_bool(s.eth0_link)
        p[self._prop_topic(n, "wifi")] = _format_bool(s.wlan_link)

    def _map_upstream_lugs(self, s: SpanPanelSnapshot, p: dict[str, str]) -> None:
        n = NODE_UPSTREAM_LUGS
        p[self._prop_topic(n, "direction")] = "UPSTREAM"
        # Consumer negates grid power; publisher must negate back
        p[self._prop_topic(n, "active-power")] = _format_float(-s.instant_grid_power_w)
        p[self._prop_topic(n, "imported-energy")] = _format_float(s.main_meter_energy_consumed_wh)
        p[self._prop_topic(n, "exported-energy")] = _format_float(s.main_meter_energy_produced_wh)
        if s.upstream_l1_current_a is not None:
            p[self._prop_topic(n, "l1-current")] = _format_float(s.upstream_l1_current_a)
        if s.upstream_l2_current_a is not None:
            p[self._prop_topic(n, "l2-current")] = _format_float(s.upstream_l2_current_a)

    def _map_downstream_lugs(self, s: SpanPanelSnapshot, p: dict[str, str]) -> None:
        n = NODE_DOWNSTREAM_LUGS
        p[self._prop_topic(n, "direction")] = "DOWNSTREAM"
        p[self._prop_topic(n, "active-power")] = _format_float(s.feedthrough_power_w)
        p[self._prop_topic(n, "imported-energy")] = _format_float(s.feedthrough_energy_consumed_wh)
        p[self._prop_topic(n, "exported-energy")] = _format_float(s.feedthrough_energy_produced_wh)
        if s.downstream_l1_current_a is not None:
            p[self._prop_topic(n, "l1-current")] = _format_float(s.downstream_l1_current_a)
        if s.downstream_l2_current_a is not None:
            p[self._prop_topic(n, "l2-current")] = _format_float(s.downstream_l2_current_a)

    def _map_circuits(self, s: SpanPanelSnapshot, p: dict[str, str]) -> None:
        for cid, circ in s.circuits.items():
            if cid.startswith("unmapped_tab_"):
                continue
            node = self._ensure_circuit_uuid(cid)
            p[self._prop_topic(node, "name")] = circ.name
            p[self._prop_topic(node, "relay")] = circ.relay_state
            p[self._prop_topic(node, "relay-requester")] = circ.relay_requester or "NONE"

            if circ.breaker_rating_a is not None:
                p[self._prop_topic(node, "breaker-rating")] = str(int(circ.breaker_rating_a))

            if circ.current_a is not None:
                p[self._prop_topic(node, "current")] = _format_float(circ.current_a)

            # Circuit active-power is published in kW (Homie schema unit).
            # Snapshot has watts.  Consumer negates on read, so we negate on write.
            power_kw = -circ.instant_power_w / 1000.0
            p[self._prop_topic(node, "active-power")] = _format_float(power_kw, 4)

            # Energy: consumer swaps imported/exported perspective.
            # "exported-energy" on wire = consumed_energy_wh in snapshot
            # "imported-energy" on wire = produced_energy_wh in snapshot
            p[self._prop_topic(node, "exported-energy")] = _format_float(circ.consumed_energy_wh)
            p[self._prop_topic(node, "imported-energy")] = _format_float(circ.produced_energy_wh)

            # Tab position → space (first tab)
            if circ.tabs:
                p[self._prop_topic(node, "space")] = str(circ.tabs[0])
            p[self._prop_topic(node, "dipole")] = _format_bool(circ.is_240v)

            # Priority mapping (v1 → v2)
            priority = _PRIORITY_V1_TO_V2.get(circ.priority, circ.priority)
            p[self._prop_topic(node, "shed-priority")] = priority
            p[self._prop_topic(node, "sheddable")] = _format_bool(circ.is_sheddable)
            p[self._prop_topic(node, "never-backup")] = _format_bool(circ.is_never_backup)
            p[self._prop_topic(node, "always-on")] = _format_bool(circ.always_on)

    def _map_bess(self, s: SpanPanelSnapshot, p: dict[str, str]) -> None:
        bat = s.battery
        if bat.soe_percentage is None:
            return
        n = NODE_BESS
        p[self._prop_topic(n, "vendor-name")] = bat.vendor_name or "Simulated"
        p[self._prop_topic(n, "product-name")] = bat.product_name or "Virtual Battery"
        if bat.serial_number:
            p[self._prop_topic(n, "serial-number")] = bat.serial_number
        if bat.model:
            p[self._prop_topic(n, "model")] = bat.model
        if bat.software_version:
            p[self._prop_topic(n, "software-version")] = bat.software_version
        if bat.nameplate_capacity_kwh is not None:
            p[self._prop_topic(n, "nameplate-capacity")] = _format_float(
                bat.nameplate_capacity_kwh
            )
        if bat.feed_circuit_id:
            circuit_uuid = self._ensure_circuit_uuid(bat.feed_circuit_id)
            p[self._prop_topic(n, "feed")] = circuit_uuid
        p[self._prop_topic(n, "soc")] = _format_float(bat.soe_percentage)
        if bat.soe_kwh is not None:
            p[self._prop_topic(n, "soe")] = _format_float(bat.soe_kwh)
        p[self._prop_topic(n, "connected")] = _format_bool(
            bat.connected if bat.connected is not None else True
        )
        p[self._prop_topic(n, "grid-state")] = s.grid_state or "ON_GRID"
        p[self._prop_topic(n, "relative-position")] = "DOWNSTREAM"

    def _map_pv(self, s: SpanPanelSnapshot, p: dict[str, str]) -> None:
        pv = s.pv
        if pv.vendor_name is None and pv.nameplate_capacity_w is None:
            return
        n = NODE_PV
        # feed references the circuit node UUID so the integration can
        # annotate the circuit with device_type="pv"
        if pv.feed_circuit_id:
            circuit_uuid = self._ensure_circuit_uuid(pv.feed_circuit_id)
            p[self._prop_topic(n, "feed")] = circuit_uuid
        p[self._prop_topic(n, "vendor-name")] = pv.vendor_name or "Simulated"
        p[self._prop_topic(n, "product-name")] = pv.product_name or "Virtual PV"
        p[self._prop_topic(n, "serial-number")] = f"SIM-PV-{self._serial}"
        if pv.nameplate_capacity_w is not None:
            p[self._prop_topic(n, "nameplate-capacity")] = _format_float(
                pv.nameplate_capacity_w, 0
            )
        p[self._prop_topic(n, "relative-position")] = "DOWNSTREAM"

    def _map_evse(self, s: SpanPanelSnapshot, p: dict[str, str]) -> None:
        if not s.evse:
            return
        for idx, evse in enumerate(s.evse.values()):
            n = f"evse-{idx}"
            # feed references the circuit node UUID so the integration can
            # annotate the circuit with device_type="evse"
            if evse.feed_circuit_id:
                circuit_uuid = self._ensure_circuit_uuid(evse.feed_circuit_id)
                p[self._prop_topic(n, "feed")] = circuit_uuid
            p[self._prop_topic(n, "vendor-name")] = evse.vendor_name or "SPAN"
            p[self._prop_topic(n, "product-name")] = evse.product_name or "SPAN Drive"
            if evse.serial_number:
                p[self._prop_topic(n, "serial-number")] = evse.serial_number
            if evse.software_version:
                p[self._prop_topic(n, "software-version")] = evse.software_version
            p[self._prop_topic(n, "status")] = evse.status
            p[self._prop_topic(n, "lock-state")] = evse.lock_state
            if evse.advertised_current_a is not None:
                p[self._prop_topic(n, "advertised-current")] = _format_float(
                    evse.advertised_current_a
                )

    def _map_power_flows(self, s: SpanPanelSnapshot, p: dict[str, str]) -> None:
        n = NODE_POWER_FLOWS
        if s.power_flow_pv is not None:
            p[self._prop_topic(n, "pv")] = _format_float(s.power_flow_pv)
        if s.power_flow_battery is not None:
            p[self._prop_topic(n, "battery")] = _format_float(s.power_flow_battery)
        if s.power_flow_grid is not None:
            p[self._prop_topic(n, "grid")] = _format_float(s.power_flow_grid)
        if s.power_flow_site is not None:
            p[self._prop_topic(n, "site")] = _format_float(s.power_flow_site)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def publish_init(self, snapshot: SpanPanelSnapshot) -> None:
        """Publish initial device state: ``$state=init``, ``$description``, all properties.

        Call once at startup before entering the tick loop.
        """
        state_topic = STATE_TOPIC_FMT.format(serial=self._serial)
        desc_topic = DESCRIPTION_TOPIC_FMT.format(serial=self._serial)

        # 1. $state → init
        await self._publish(state_topic, HOMIE_STATE_INIT, True)

        # 2. $description
        description = self._build_description(snapshot)
        await self._publish(desc_topic, json.dumps(description), True)
        self._description_published = True

        # 3. All properties (full publish, no diff)
        all_props = self._snapshot_to_properties(snapshot)
        for topic, value in all_props.items():
            await self._publish(topic, value, True)
        self._state.values = dict(all_props)

        # 4. $state → ready
        await self._publish(state_topic, HOMIE_STATE_READY, True)

        _LOGGER.info(
            "Published init sequence: %d properties for %d nodes",
            len(all_props),
            len(description["nodes"]),
        )

    async def publish_diff(self, snapshot: SpanPanelSnapshot) -> int:
        """Diff *snapshot* against the previous one and publish changed properties.

        Returns the number of properties published.
        """
        new_props = self._snapshot_to_properties(snapshot)
        changed = self._state.diff(new_props)

        for topic, value in changed.items():
            await self._publish(topic, value, True)

        if changed:
            _LOGGER.debug("Published %d changed properties", len(changed))
        return len(changed)

    def get_set_topics(self) -> list[str]:
        """Return the list of ``/set`` topics the simulator should subscribe to."""
        topics: list[str] = []
        # Core settable: dominant-power-source
        topics.append(self._set_topic(NODE_CORE, "dominant-power-source"))
        # Circuit settable: relay, shed-priority
        for node_uuid in self._circuit_uuid_map.values():
            topics.append(self._set_topic(node_uuid, "relay"))
            topics.append(self._set_topic(node_uuid, "shed-priority"))
        return topics

    def resolve_set_message(self, topic: str) -> tuple[str, str, str] | None:
        """Parse a ``/set`` topic into ``(target_type, circuit_id_or_empty, property_name)``.

        Returns None if the topic is not a recognised ``/set`` topic.
        """
        # Expected format: ebus/5/{serial}/{node}/{prop}/set
        parts = topic.split("/")
        if len(parts) < 6 or parts[-1] != "set":
            return None

        node = parts[3]
        prop = parts[4]

        if node == NODE_CORE:
            return ("core", "", prop)

        circuit_id = self._uuid_to_circuit_id.get(node)
        if circuit_id is not None:
            return ("circuit", circuit_id, prop)

        return None
