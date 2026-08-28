"""Typed accessor over ``DeviceInstance.metadata`` for physics-relevant fields.

The producer puts strings in ``DeviceInstance.metadata``; the emitter reads them
through this view. One central place to define every key the emitter consumes,
its parser, its default, and its validation rules. Adding a new physics field
is a one-line addition here plus a docs note in the README.

Validation runs once when ``ManifestPhysicsView`` is constructed (typically at
``Emitter.__init__``). Missing required keys, malformed values, and contradictory
physics (e.g. ``dipole`` flag inconsistent with ``tab-numbers`` count) raise
``ManifestValidationError`` with the offending instance_id."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from span_panel_simulator.flat_emitter.conventions.tab_legs import Leg, legs_for_tabs
from span_panel_simulator.flat_emitter.exceptions import ManifestValidationError

if TYPE_CHECKING:
    from span_panel_simulator.flat_emitter.manifest import DeviceInstance, DeviceManifest


# ---------------------------------------------------------------------------
# Per-entity-class typed views — built once, queried many times.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PanelPhysics:
    serial_number: str
    vendor_name: str
    firmware_version: str
    hardware_version: str
    panel_size: int
    main_breaker_rating_a: int
    panel_model: str
    postal_code: str
    time_zone: str
    service_voltage_v: float
    line_voltage_v: float
    islandable: bool
    # ``flat`` = legacy single-Homie-device shape (one device, many nodes).
    # ``parent-child`` = post-migration shape where children become separate
    # Homie devices. Producer-overridable via metadata key ``schema-topology``.
    topology: Literal["flat", "parent-child"] = "flat"


@dataclass(frozen=True, slots=True)
class LugsPhysics:
    direction: str  # "upstream" | "downstream"


@dataclass(frozen=True, slots=True)
class CircuitPhysics:
    tabs: tuple[int, ...]
    legs: tuple[Leg, ...]
    dipole: bool
    breaker_rating_a: float
    default_priority: str
    relay_behavior: str  # "controllable" | "always-on" | "non-controllable"
    placement: str  # "upstream-of-lugs" | "downstream-of-lugs"
    always_on: bool
    # Installer commissioning lock: the circuit is permanently OFF_GRID and its
    # priority is not settable. Independent of ``default_priority`` — a circuit
    # whose priority is NEVER is an ordinary settable circuit, not a locked one.
    never_backup: bool
    initial_consumed_wh: float
    initial_produced_wh: float
    pcs_priority: int = 0


@dataclass(frozen=True, slots=True)
class BessPhysics:
    vendor_name: str
    nameplate_capacity_kwh: float
    initial_soe_kwh: float | None
    product_name: str | None
    model: str | None
    serial_number: str | None
    firmware_version: str | None
    relative_position: str | None
    feed: str | None


@dataclass(frozen=True, slots=True)
class PvPhysics:
    vendor_name: str
    nameplate_capacity_w: float
    inverter_type: str  # "hybrid" | "ac-coupled"
    product_name: str | None
    serial_number: str | None
    firmware_version: str | None
    relative_position: str | None
    feed: str | None


@dataclass(frozen=True, slots=True)
class EvsePhysics:
    vendor_name: str
    product_name: str
    part_number: str
    serial_number: str
    firmware_version: str
    max_current_a: float
    feed: str | None


# ---------------------------------------------------------------------------
# Top-level view
# ---------------------------------------------------------------------------


_VALID_PRIORITIES = frozenset(
    {
        "MUST_HAVE",
        "NICE_TO_HAVE",
        "NON_ESSENTIAL",
        "NEVER",
        "SOC_THRESHOLD",
        "OFF_GRID",
    }
)
_VALID_RELAY_BEHAVIORS = frozenset({"controllable", "always-on", "non-controllable"})
_VALID_PLACEMENTS = frozenset({"upstream-of-lugs", "downstream-of-lugs"})
_VALID_LUGS_DIRECTIONS = frozenset({"upstream", "downstream"})
_VALID_INVERTER_TYPES = frozenset({"hybrid", "ac-coupled"})
_VALID_TOPOLOGIES = frozenset({"flat", "parent-child"})


class ManifestPhysicsView:
    """Validated, typed view over a ``DeviceManifest``'s metadata.

    Built once at ``Emitter`` construction time. Holds parsed physics for every
    instance keyed by ``instance_id``. Raises ``ManifestValidationError`` at
    construction if any instance is missing required keys or has malformed
    values; the emitter never sees a partially-validated manifest."""

    def __init__(self, manifest: DeviceManifest) -> None:
        self._panel: PanelPhysics | None = None
        self._lugs: dict[str, LugsPhysics] = {}
        self._circuits: dict[str, CircuitPhysics] = {}
        self._bess: dict[str, BessPhysics] = {}
        self._pv: dict[str, PvPhysics] = {}
        self._evse: dict[str, EvsePhysics] = {}

        for inst in manifest.instances:
            ec = inst.entity_class
            try:
                if ec == "panel":
                    if self._panel is not None:
                        raise ManifestValidationError(
                            "Multiple panel instances in manifest; expected exactly one",
                        )
                    self._panel = _parse_panel(inst)
                elif ec == "lugs":
                    self._lugs[inst.instance_id] = _parse_lugs(inst)
                elif ec == "circuit":
                    self._circuits[inst.instance_id] = _parse_circuit(inst)
                elif ec == "bess":
                    self._bess[inst.instance_id] = _parse_bess(inst)
                elif ec == "pv":
                    self._pv[inst.instance_id] = _parse_pv(inst)
                elif ec == "evse":
                    self._evse[inst.instance_id] = _parse_evse(inst)
                # Unknown entity_class: leave to graph builder to reject.
            except ManifestValidationError as exc:
                raise ManifestValidationError(f"{ec}/{inst.instance_id}: {exc}") from exc

        if self._panel is None:
            raise ManifestValidationError("Manifest has no panel instance")

    # -- accessors -----------------------------------------------------------

    @property
    def panel(self) -> PanelPhysics:
        assert self._panel is not None  # checked in __init__
        return self._panel

    def lugs(self, instance_id: str) -> LugsPhysics:
        return self._lugs[instance_id]

    def circuit(self, instance_id: str) -> CircuitPhysics:
        return self._circuits[instance_id]

    def bess(self, instance_id: str) -> BessPhysics:
        return self._bess[instance_id]

    def pv(self, instance_id: str) -> PvPhysics:
        return self._pv[instance_id]

    def evse(self, instance_id: str) -> EvsePhysics:
        return self._evse[instance_id]

    def all_circuits(self) -> dict[str, CircuitPhysics]:
        return dict(self._circuits)

    def all_lugs(self) -> dict[str, LugsPhysics]:
        return dict(self._lugs)

    def all_bess(self) -> dict[str, BessPhysics]:
        return dict(self._bess)

    def all_pv(self) -> dict[str, PvPhysics]:
        return dict(self._pv)

    def all_evse(self) -> dict[str, EvsePhysics]:
        return dict(self._evse)


# ---------------------------------------------------------------------------
# Parsers — one per entity_class.
# ---------------------------------------------------------------------------


def _require(md: dict[str, str], key: str) -> str:
    if key not in md:
        raise ManifestValidationError(f"missing required metadata key {key!r}")
    return md[key]


def _opt_float(md: dict[str, str], key: str, default: float) -> float:
    if key not in md:
        return default
    try:
        return float(md[key])
    except ValueError as exc:
        raise ManifestValidationError(f"key {key!r}: not a float ({md[key]!r})") from exc


def _opt_int(md: dict[str, str], key: str, default: int) -> int:
    if key not in md:
        return default
    try:
        return int(md[key])
    except ValueError as exc:
        raise ManifestValidationError(f"key {key!r}: not an int ({md[key]!r})") from exc


def _opt_bool(md: dict[str, str], key: str, default: bool) -> bool:
    if key not in md:
        return default
    raw = md[key].strip().lower()
    if raw in ("true", "1", "yes"):
        return True
    if raw in ("false", "0", "no"):
        return False
    raise ManifestValidationError(f"key {key!r}: not a bool ({md[key]!r})")


def _opt_str(md: dict[str, str], key: str) -> str | None:
    value = md.get(key)
    return value if value not in (None, "") else None


def _req_float(md: dict[str, str], key: str) -> float:
    raw = _require(md, key)
    try:
        return float(raw)
    except ValueError as exc:
        raise ManifestValidationError(f"key {key!r}: not a float ({raw!r})") from exc


def _req_int(md: dict[str, str], key: str) -> int:
    raw = _require(md, key)
    try:
        return int(raw)
    except ValueError as exc:
        raise ManifestValidationError(f"key {key!r}: not an int ({raw!r})") from exc


def _parse_panel(inst: DeviceInstance) -> PanelPhysics:
    md = inst.metadata
    topology_raw = md.get("schema-topology", "flat")
    if topology_raw not in _VALID_TOPOLOGIES:
        raise ManifestValidationError(
            f"key 'schema-topology': must be one of {sorted(_VALID_TOPOLOGIES)}, "
            f"got {topology_raw!r}"
        )
    # ``cast`` would also work here, but a direct comparison keeps the Literal
    # narrow without an explicit import-only typing helper.
    topology: Literal["flat", "parent-child"] = (
        "parent-child" if topology_raw == "parent-child" else "flat"
    )
    return PanelPhysics(
        serial_number=_require(md, "serial-number"),
        vendor_name=_require(md, "vendor-name"),
        firmware_version=_opt_str(md, "firmware-version") or _require(md, "software-version"),
        hardware_version=_require(md, "hardware-version"),
        panel_size=_req_int(md, "panel-size"),
        main_breaker_rating_a=_req_int(md, "main-breaker-rating-a"),
        panel_model=_require(md, "panel-model"),
        postal_code=_require(md, "postal-code"),
        time_zone=_require(md, "time-zone"),
        service_voltage_v=_opt_float(md, "service-voltage-v", 240.0),
        line_voltage_v=_opt_float(md, "line-voltage-v", 120.0),
        islandable=_opt_bool(md, "islandable", False),
        topology=topology,
    )


def _parse_lugs(inst: DeviceInstance) -> LugsPhysics:
    direction = _require(inst.metadata, "direction")
    if direction not in _VALID_LUGS_DIRECTIONS:
        raise ManifestValidationError(
            f"key 'direction': must be one of {sorted(_VALID_LUGS_DIRECTIONS)}, got {direction!r}"
        )
    return LugsPhysics(direction=direction)


def _parse_circuit(inst: DeviceInstance) -> CircuitPhysics:
    md = inst.metadata
    raw_tabs = _require(md, "tab-numbers")
    try:
        tabs = tuple(int(t.strip()) for t in raw_tabs.split(",") if t.strip())
    except ValueError as exc:
        raise ManifestValidationError(
            f"key 'tab-numbers': not a comma-separated int list ({raw_tabs!r})"
        ) from exc
    if not tabs:
        raise ManifestValidationError("key 'tab-numbers': must list at least one tab")
    try:
        legs = legs_for_tabs(tabs)
    except ValueError as exc:
        raise ManifestValidationError(f"key 'tab-numbers': {exc}") from exc
    dipole_declared = _opt_bool(md, "dipole", default=len(tabs) > 1)
    # NOTE: ``dipole`` + leg-spanning is not strictly validated. Real SPAN panels
    # use slot numberings where two adjacent breaker positions on the same leg
    # can still be ganged as a "240 V" feed (e.g. tabs 20+22). The convention
    # in ``conventions/tab_legs.py`` is informational for per-leg current
    # calculation; producers are trusted to declare dipole correctly.
    if not dipole_declared and len(tabs) > 1:
        raise ManifestValidationError(
            f"dipole=false but {len(tabs)} tabs declared; single-tab circuits only"
        )

    priority = _require(md, "default-priority")
    if priority not in _VALID_PRIORITIES:
        raise ManifestValidationError(
            f"key 'default-priority': must be one of {sorted(_VALID_PRIORITIES)}, got {priority!r}"
        )

    relay_behavior = _require(md, "relay-behavior")
    if relay_behavior not in _VALID_RELAY_BEHAVIORS:
        raise ManifestValidationError(
            f"key 'relay-behavior': must be one of {sorted(_VALID_RELAY_BEHAVIORS)}, "
            f"got {relay_behavior!r}"
        )

    placement = _require(md, "placement")
    if placement not in _VALID_PLACEMENTS:
        raise ManifestValidationError(
            f"key 'placement': must be one of {sorted(_VALID_PLACEMENTS)}, got {placement!r}"
        )

    always_on = _opt_bool(md, "always-on", default=relay_behavior == "always-on")
    # ``never-backup`` is a commissioning input in its own right, so it has no
    # default derived from anything else: a circuit is locked only when an
    # installer locked it. Deriving it from ``default-priority == "NEVER"``
    # inverted the meaning — NEVER is "never shed", not "not backed up".
    never_backup = _opt_bool(md, "never-backup", default=False)

    return CircuitPhysics(
        tabs=tabs,
        legs=legs,
        dipole=dipole_declared,
        breaker_rating_a=_req_float(md, "breaker-rating-a"),
        default_priority=priority,
        relay_behavior=relay_behavior,
        placement=placement,
        always_on=always_on,
        never_backup=never_backup,
        pcs_priority=_opt_int(md, "pcs-priority", 0),
        initial_consumed_wh=_opt_float(md, "initial-consumed-wh", 0.0),
        initial_produced_wh=_opt_float(md, "initial-produced-wh", 0.0),
    )


def _parse_bess(inst: DeviceInstance) -> BessPhysics:
    md = inst.metadata
    initial_soe: float | None = None
    if "initial-soe-kwh" in md:
        initial_soe = _opt_float(md, "initial-soe-kwh", 0.0)
    return BessPhysics(
        vendor_name=_require(md, "vendor-name"),
        nameplate_capacity_kwh=_req_float(md, "nameplate-capacity-kwh"),
        initial_soe_kwh=initial_soe,
        product_name=_opt_str(md, "product-name"),
        model=_opt_str(md, "model"),
        serial_number=_opt_str(md, "serial-number"),
        firmware_version=_opt_str(md, "firmware-version") or _opt_str(md, "software-version"),
        relative_position=_opt_str(md, "relative-position") or "UPSTREAM",
        feed=_opt_str(md, "feed") or _opt_str(md, "feed-circuit-id"),
    )


def _parse_pv(inst: DeviceInstance) -> PvPhysics:
    md = inst.metadata
    inverter_type = _require(md, "inverter-type")
    if inverter_type not in _VALID_INVERTER_TYPES:
        raise ManifestValidationError(
            f"key 'inverter-type': must be one of {sorted(_VALID_INVERTER_TYPES)}, "
            f"got {inverter_type!r}"
        )
    return PvPhysics(
        vendor_name=_require(md, "vendor-name"),
        nameplate_capacity_w=_req_float(md, "nameplate-capacity-w"),
        inverter_type=inverter_type,
        product_name=_opt_str(md, "product-name"),
        serial_number=_opt_str(md, "serial-number"),
        firmware_version=_opt_str(md, "firmware-version") or _opt_str(md, "software-version"),
        relative_position=_opt_str(md, "relative-position") or "IN_PANEL",
        feed=_opt_str(md, "feed") or _opt_str(md, "feed-circuit-id"),
    )


def _parse_evse(inst: DeviceInstance) -> EvsePhysics:
    md = inst.metadata
    return EvsePhysics(
        vendor_name=_require(md, "vendor-name"),
        product_name=_require(md, "product-name"),
        part_number=_require(md, "part-number"),
        serial_number=_require(md, "serial-number"),
        firmware_version=_opt_str(md, "firmware-version") or _require(md, "software-version"),
        max_current_a=_req_float(md, "max-current-a"),
        feed=_opt_str(md, "feed") or _opt_str(md, "feed-circuit-id"),
    )
