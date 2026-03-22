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
from typing import TYPE_CHECKING, TypeVar

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
    TYPE_PCS,
    TYPE_POWER_FLOWS,
    TYPE_PV,
)
from span_panel_simulator.schema import validate_value

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Sequence
    from typing import Any

    from span_panel_simulator.models import (
        SpanBatterySnapshot,
        SpanCircuitSnapshot,
        SpanEvseSnapshot,
        SpanPanelSnapshot,
        SpanPcsSnapshot,
        SpanPVSnapshot,
    )
    from span_panel_simulator.schema import HomieSchemaRegistry

_LOGGER = logging.getLogger(__name__)

_T = TypeVar("_T")

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
NODE_PCS = "pcs-0"
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


def _opt_float(v: float | None, precision: int = 2) -> str | None:
    """Format an optional float for MQTT publication."""
    return _format_float(v, precision) if v is not None else None


def _opt_int(v: float | None) -> str | None:
    """Format an optional numeric value as integer string."""
    return str(int(v)) if v is not None else None


# ------------------------------------------------------------------
# Declarative property extractors (Phase 4)
#
# Each list maps (property_name, extractor) pairs for a node type.
# The extractor receives the relevant snapshot object and returns the
# string value to publish, or None to skip the property.
# ------------------------------------------------------------------

_CORE_EXTRACTORS: Sequence[tuple[str, Callable[[SpanPanelSnapshot], str | None]]] = [
    ("vendor-name", lambda s: "SPAN"),
    ("serial-number", lambda s: s.serial_number),
    ("hardware-version", lambda s: "H201"),
    ("software-version", lambda s: s.firmware_version),
    ("door", lambda s: s.door_state or "CLOSED"),
    ("grid-islandable", lambda s: _format_bool(bool(s.grid_islandable))),
    ("dominant-power-source", lambda s: s.dominant_power_source or "GRID"),
    ("relay", lambda s: s.main_relay_state or "CLOSED"),
    ("l1-voltage", lambda s: _opt_float(s.l1_voltage, 1)),
    ("l2-voltage", lambda s: _opt_float(s.l2_voltage, 1)),
    ("breaker-rating", lambda s: _opt_int(s.main_breaker_rating_a)),
    ("ethernet", lambda s: _format_bool(s.eth0_link)),
    ("wifi", lambda s: _format_bool(s.wlan_link)),
    ("wifi-ssid", lambda s: s.wifi_ssid),
    ("vendor-cloud", lambda s: s.vendor_cloud),
    ("postal-code", lambda s: s.postal_code),
    ("time-zone", lambda s: s.time_zone),
    ("model", lambda s: s.panel_model),
]

_UPSTREAM_LUGS_EXTRACTORS: Sequence[tuple[str, Callable[[SpanPanelSnapshot], str | None]]] = [
    ("direction", lambda s: "UPSTREAM"),
    ("feed", lambda s: "GRID"),
    ("active-power", lambda s: _format_float(-s.instant_grid_power_w)),
    ("imported-energy", lambda s: _format_float(s.main_meter_energy_consumed_wh)),
    ("exported-energy", lambda s: _format_float(s.main_meter_energy_produced_wh)),
    ("l1-current", lambda s: _opt_float(s.upstream_l1_current_a)),
    ("l2-current", lambda s: _opt_float(s.upstream_l2_current_a)),
]

_DOWNSTREAM_LUGS_EXTRACTORS: Sequence[tuple[str, Callable[[SpanPanelSnapshot], str | None]]] = [
    ("direction", lambda s: "DOWNSTREAM"),
    ("feed", lambda s: ""),
    ("active-power", lambda s: _format_float(s.feedthrough_power_w)),
    ("imported-energy", lambda s: _format_float(s.feedthrough_energy_consumed_wh)),
    ("exported-energy", lambda s: _format_float(s.feedthrough_energy_produced_wh)),
    ("l1-current", lambda s: _opt_float(s.downstream_l1_current_a)),
    ("l2-current", lambda s: _opt_float(s.downstream_l2_current_a)),
]

_CIRCUIT_EXTRACTORS: Sequence[tuple[str, Callable[[SpanCircuitSnapshot], str | None]]] = [
    ("name", lambda c: c.name),
    ("relay", lambda c: c.relay_state),
    ("relay-requester", lambda c: c.relay_requester or "NONE"),
    ("breaker-rating", lambda c: _opt_int(c.breaker_rating_a)),
    ("current", lambda c: _opt_float(c.current_a)),
    ("active-power", lambda c: _format_float(-c.instant_power_w)),
    ("exported-energy", lambda c: _format_float(c.consumed_energy_wh)),
    ("imported-energy", lambda c: _format_float(c.produced_energy_wh)),
    # Lug-only / feed circuits have no breaker space; omit (Homie format is 1:32, not 0).
    ("space", lambda c: str(c.tabs[0]) if c.tabs else None),
    ("dipole", lambda c: _format_bool(c.is_240v)),
    ("shed-priority", lambda c: _PRIORITY_V1_TO_V2.get(c.priority, c.priority)),
    ("pcs-managed", lambda _: _format_bool(False)),
    ("pcs-priority", lambda _: "0"),
    ("sheddable", lambda c: _format_bool(c.is_sheddable)),
    ("never-backup", lambda c: _format_bool(c.is_never_backup)),
    ("always-on", lambda c: _format_bool(c.always_on)),
]

_BESS_EXTRACTORS: Sequence[tuple[str, Callable[[SpanBatterySnapshot], str | None]]] = [
    ("vendor-name", lambda b: b.vendor_name or "Simulated"),
    ("product-name", lambda b: b.product_name or "Virtual Battery"),
    ("serial-number", lambda b: b.serial_number),
    ("model", lambda b: b.model),
    ("software-version", lambda b: b.software_version),
    ("nameplate-capacity", lambda b: _opt_float(b.nameplate_capacity_kwh)),
    ("soc", lambda b: _opt_float(b.soe_percentage)),
    ("soe", lambda b: _opt_float(b.soe_kwh)),
    ("connected", lambda b: _format_bool(b.connected if b.connected is not None else True)),
]

_PV_EXTRACTORS: Sequence[tuple[str, Callable[[SpanPVSnapshot], str | None]]] = [
    ("vendor-name", lambda p: p.vendor_name or "Simulated"),
    ("product-name", lambda p: p.product_name or "Virtual PV"),
    ("software-version", lambda p: p.software_version),
    ("nameplate-capacity", lambda p: _opt_float(p.nameplate_capacity_w, 0)),
    ("relative-position", lambda _: "DOWNSTREAM"),
]

_EVSE_EXTRACTORS: Sequence[tuple[str, Callable[[SpanEvseSnapshot], str | None]]] = [
    ("vendor-name", lambda e: e.vendor_name or "SPAN"),
    ("product-name", lambda e: e.product_name or "SPAN Drive"),
    ("part-number", lambda e: e.part_number),
    ("serial-number", lambda e: e.serial_number),
    ("software-version", lambda e: e.software_version),
    ("status", lambda e: e.status),
    ("lock-state", lambda e: e.lock_state),
    ("advertised-current", lambda e: _opt_float(e.advertised_current_a)),
]

_PCS_EXTRACTORS: Sequence[tuple[str, Callable[[SpanPcsSnapshot], str | None]]] = [
    ("enabled", lambda p: _format_bool(p.enabled)),
    ("active", lambda p: _format_bool(p.active)),
    ("import-limit", lambda p: _format_float(p.import_limit_a)),
    ("feed-import-limit", lambda p: _format_float(p.feed_import_limit_a)),
    ("feed-import-limit-enablement", lambda p: p.feed_import_limit_enablement),
    ("feed-import-limit-active", lambda p: _format_bool(p.feed_import_limit_active)),
    ("grid-import-limit", lambda p: _format_float(p.grid_import_limit_a)),
    ("grid-import-limit-enablement", lambda p: p.grid_import_limit_enablement),
    ("grid-import-limit-active", lambda p: _format_bool(p.grid_import_limit_active)),
    ("off-grid-import-limit", lambda p: _format_float(p.off_grid_import_limit_a)),
    ("off-grid-import-limit-enablement", lambda p: p.off_grid_import_limit_enablement),
    ("off-grid-import-limit-active", lambda p: _format_bool(p.off_grid_import_limit_active)),
    ("requested-import-limit", lambda p: _format_float(p.requested_import_limit_a)),
    ("requested-import-limit-enablement", lambda p: p.requested_import_limit_enablement),
    ("requested-import-limit-active", lambda p: _format_bool(p.requested_import_limit_active)),
]

_POWER_FLOW_EXTRACTORS: Sequence[tuple[str, Callable[[SpanPanelSnapshot], str | None]]] = [
    ("pv", lambda s: _opt_float(s.power_flow_pv)),
    ("battery", lambda s: _opt_float(s.power_flow_battery)),
    ("grid", lambda s: _opt_float(s.power_flow_grid)),
    ("site", lambda s: _opt_float(s.power_flow_site)),
]


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
        schema: HomieSchemaRegistry | None = None,
    ) -> None:
        """Initialise the publisher.

        Args:
            serial_number: Panel serial (used in topic prefix).
            publish_fn: Async callable ``(topic, payload, retain) -> None``
                        provided by the MQTT client layer.
            schema: Parsed Homie schema for validation and /set discovery.
        """
        self._serial = serial_number
        self._publish = publish_fn
        self._schema = schema
        self._state = _PublishedState()
        # Mapping: simulation circuit_id → dashless UUID used as Homie node_id
        self._circuit_uuid_map: dict[str, str] = {}
        # Reverse map for /set handling
        self._uuid_to_circuit_id: dict[str, str] = {}
        self._description_published = False
        # Populated by _build_description: node_id → schema type_id
        self._description_nodes: dict[str, str] = {}

    def override_serial(self, serial: str) -> None:
        """Replace the serial used for MQTT topic prefixes.

        Resets published state so the next ``publish_init`` re-publishes
        everything under the new topic namespace.
        """
        self._serial = serial
        self._state = _PublishedState()
        self._description_published = False

    # ------------------------------------------------------------------
    # Topic helpers
    # ------------------------------------------------------------------

    def _prop_topic(self, node: str, prop: str) -> str:
        return PROPERTY_TOPIC_FMT.format(serial=self._serial, node=node, prop=prop)

    def _set_topic(self, node: str, prop: str) -> str:
        return PROPERTY_SET_TOPIC_FMT.format(serial=self._serial, node=node, prop=prop)

    # ------------------------------------------------------------------
    # Generic extractor application
    # ------------------------------------------------------------------

    def _apply_extractors(
        self,
        node_id: str,
        extractors: Sequence[tuple[str, Callable[[_T], str | None]]],
        source: _T,
        props: dict[str, str],
    ) -> None:
        """Apply declarative property extractors to a source object.

        Each extractor is a ``(property_name, callable)`` pair.  The callable
        receives *source* and returns the string value to publish, or ``None``
        to skip the property.
        """
        for prop_name, extractor in extractors:
            value = extractor(source)
            if value is not None:
                props[self._prop_topic(node_id, prop_name)] = value

    # ------------------------------------------------------------------
    # $description builder
    # ------------------------------------------------------------------

    def _build_description(self, snapshot: SpanPanelSnapshot) -> dict[str, Any]:
        """Build Homie ``$description`` JSON from the current snapshot.

        Also populates ``_description_nodes`` so that schema-driven methods
        (``get_set_topics``, ``_validate_against_schema``) can iterate over
        the active node → type mapping.
        """
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

        # PCS always present (even if disabled)
        nodes[NODE_PCS] = {"type": TYPE_PCS}

        # Power flows always present
        nodes[NODE_POWER_FLOWS] = {"type": TYPE_POWER_FLOWS}

        # Cache node → type mapping for schema-driven methods
        self._description_nodes = {nid: ndef["type"] for nid, ndef in nodes.items()}

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
        self._map_pcs(snapshot, props)
        self._map_power_flows(snapshot, props)

        return props

    def _map_core(self, s: SpanPanelSnapshot, p: dict[str, str]) -> None:
        self._apply_extractors(NODE_CORE, _CORE_EXTRACTORS, s, p)

    def _map_upstream_lugs(self, s: SpanPanelSnapshot, p: dict[str, str]) -> None:
        self._apply_extractors(NODE_UPSTREAM_LUGS, _UPSTREAM_LUGS_EXTRACTORS, s, p)

    def _map_downstream_lugs(self, s: SpanPanelSnapshot, p: dict[str, str]) -> None:
        self._apply_extractors(NODE_DOWNSTREAM_LUGS, _DOWNSTREAM_LUGS_EXTRACTORS, s, p)

    def _map_circuits(self, s: SpanPanelSnapshot, p: dict[str, str]) -> None:
        for cid, circ in s.circuits.items():
            if cid.startswith("unmapped_tab_"):
                continue
            node = self._ensure_circuit_uuid(cid)
            self._apply_extractors(node, _CIRCUIT_EXTRACTORS, circ, p)

    def _map_bess(self, s: SpanPanelSnapshot, p: dict[str, str]) -> None:
        bat = s.battery
        if bat.soe_percentage is None:
            return
        n = NODE_BESS
        self._apply_extractors(n, _BESS_EXTRACTORS, bat, p)
        if bat.feed_circuit_id:
            p[self._prop_topic(n, "feed")] = self._ensure_circuit_uuid(bat.feed_circuit_id)
        p[self._prop_topic(n, "grid-state")] = s.grid_state or "ON_GRID"
        p[self._prop_topic(n, "relative-position")] = "DOWNSTREAM"

    def _map_pv(self, s: SpanPanelSnapshot, p: dict[str, str]) -> None:
        pv = s.pv
        if pv.vendor_name is None and pv.nameplate_capacity_w is None:
            return
        n = NODE_PV
        self._apply_extractors(n, _PV_EXTRACTORS, pv, p)
        if pv.feed_circuit_id:
            p[self._prop_topic(n, "feed")] = self._ensure_circuit_uuid(pv.feed_circuit_id)
        p[self._prop_topic(n, "serial-number")] = f"SIM-PV-{self._serial}"

    def _map_evse(self, s: SpanPanelSnapshot, p: dict[str, str]) -> None:
        if not s.evse:
            return
        for idx, evse in enumerate(s.evse.values()):
            n = f"evse-{idx}"
            if evse.feed_circuit_id:
                p[self._prop_topic(n, "feed")] = self._ensure_circuit_uuid(evse.feed_circuit_id)
            self._apply_extractors(n, _EVSE_EXTRACTORS, evse, p)

    def _map_pcs(self, s: SpanPanelSnapshot, p: dict[str, str]) -> None:
        self._apply_extractors(NODE_PCS, _PCS_EXTRACTORS, s.pcs, p)

    def _map_power_flows(self, s: SpanPanelSnapshot, p: dict[str, str]) -> None:
        self._apply_extractors(NODE_POWER_FLOWS, _POWER_FLOW_EXTRACTORS, s, p)

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

        # 5. Validate published properties against schema
        self._validate_against_schema(all_props, snapshot)

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
        """Return ``/set`` topics derived from schema ``settable`` flags.

        Falls back to a hardcoded list when no schema is available.
        """
        if self._schema is not None and self._description_nodes:
            return self._get_set_topics_from_schema()
        return self._get_set_topics_hardcoded()

    def _get_set_topics_from_schema(self) -> list[str]:
        """Derive /set topics from schema settable properties."""
        topics: list[str] = []
        for node_id, type_id in self._description_nodes.items():
            node_type = self._schema.get_node_type(type_id) if self._schema else None
            if node_type is None:
                continue
            for prop_name, prop in node_type.properties.items():
                if prop.settable:
                    topics.append(self._set_topic(node_id, prop_name))
        return topics

    def _get_set_topics_hardcoded(self) -> list[str]:
        """Hardcoded /set topics (used when schema is unavailable)."""
        topics: list[str] = []
        topics.append(self._set_topic(NODE_CORE, "dominant-power-source"))
        for node_uuid in self._circuit_uuid_map.values():
            topics.append(self._set_topic(node_uuid, "relay"))
            topics.append(self._set_topic(node_uuid, "shed-priority"))
        return topics

    # ------------------------------------------------------------------
    # Schema validation
    # ------------------------------------------------------------------

    def _validate_against_schema(
        self,
        published_props: dict[str, str],
        snapshot: SpanPanelSnapshot | None = None,
    ) -> None:
        """Compare published properties against schema declarations.

        Logs warnings for:
          - Schema properties not published by the simulator (missing)
          - Published properties not declared in the schema (extra)
          - Values that fail type validation (debug mode only)

        Circuits with no ``tabs`` (lug feeds) intentionally omit ``space`` because
        the Homie schema restricts it to 1..32.

        Only runs when a schema registry is available.
        """
        if self._schema is None or not self._description_nodes:
            return

        prefix = f"{PROPERTY_TOPIC_FMT.split('{')[0]}{self._serial}/"
        run_type_checks = _LOGGER.isEnabledFor(logging.DEBUG)

        for node_id, type_id in self._description_nodes.items():
            node_type = self._schema.get_node_type(type_id)
            if node_type is None:
                _LOGGER.warning(
                    "Schema validation: node %s type %s not found in schema",
                    node_id,
                    type_id,
                )
                continue

            # Collect property names and values published for this node
            node_prefix = f"{prefix}{node_id}/"
            published_for_node = {
                topic[len(node_prefix) :]: published_props[topic]
                for topic in published_props
                if topic.startswith(node_prefix)
            }

            schema_props = set(node_type.properties)
            published_names = set(published_for_node)
            missing = schema_props - published_names
            if snapshot is not None:
                circuit_id = self._uuid_to_circuit_id.get(node_id)
                if circuit_id is not None:
                    circ = snapshot.circuits.get(circuit_id)
                    if circ is not None and not circ.tabs:
                        missing.discard("space")
            extra = published_names - schema_props

            if missing:
                _LOGGER.warning(
                    "Schema validation: node %s (%s) missing properties: %s",
                    node_id,
                    type_id,
                    ", ".join(sorted(missing)),
                )
            if extra:
                _LOGGER.debug(
                    "Schema validation: node %s (%s) has extra properties: %s",
                    node_id,
                    type_id,
                    ", ".join(sorted(extra)),
                )

            # Phase 5: Type validation (dev/test — gated by debug level)
            if run_type_checks:
                for prop_name, value in published_for_node.items():
                    prop = node_type.properties.get(prop_name)
                    if prop is None:
                        continue
                    error = validate_value(prop, value)
                    if error:
                        _LOGGER.warning(
                            "Type validation: %s/%s: %s",
                            node_id,
                            prop_name,
                            error,
                        )

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
