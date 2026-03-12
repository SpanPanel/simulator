"""Simulator data models — transport-agnostic snapshot types.

These are standalone definitions matching the field names and semantics
of the span-panel-api snapshot dataclasses so that the publisher can
map them to Homie v5 MQTT messages identically.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class SpanCircuitSnapshot:
    """Transport-agnostic circuit state."""

    circuit_id: str
    name: str

    relay_state: str  # OPEN | CLOSED | UNKNOWN
    instant_power_w: float  # Positive = consumption, negative = production
    produced_energy_wh: float
    consumed_energy_wh: float

    tabs: list[int]
    priority: str  # MUST_HAVE | NICE_TO_HAVE | NON_ESSENTIAL | NEVER | SOC_THRESHOLD | OFF_GRID
    is_user_controllable: bool
    is_sheddable: bool
    is_never_backup: bool

    device_type: str = "circuit"
    relative_position: str = ""
    is_240v: bool = False
    current_a: float | None = None
    breaker_rating_a: float | None = None
    always_on: bool = False
    relay_requester: str = "UNKNOWN"
    energy_accum_update_time_s: int = 0
    instant_power_update_time_s: int = 0


@dataclass(frozen=True, slots=True)
class SpanPVSnapshot:
    """PV inverter metadata."""

    vendor_name: str | None = None
    product_name: str | None = None
    nameplate_capacity_w: float | None = None


@dataclass(frozen=True, slots=True)
class SpanEvseSnapshot:
    """EV Charger (EVSE) state."""

    node_id: str
    feed_circuit_id: str
    status: str = "UNKNOWN"
    lock_state: str = "UNKNOWN"
    advertised_current_a: float | None = None

    vendor_name: str | None = None
    product_name: str | None = None
    part_number: str | None = None
    serial_number: str | None = None
    software_version: str | None = None


@dataclass(frozen=True, slots=True)
class SpanBatterySnapshot:
    """Battery state."""

    soe_percentage: float | None = None
    soe_kwh: float | None = None

    vendor_name: str | None = None
    product_name: str | None = None
    model: str | None = None
    serial_number: str | None = None
    software_version: str | None = None
    nameplate_capacity_kwh: float | None = None
    connected: bool | None = None


@dataclass(frozen=True, slots=True)
class SpanPanelSnapshot:
    """Complete panel state — single point-in-time view."""

    serial_number: str
    firmware_version: str

    main_relay_state: str
    instant_grid_power_w: float
    feedthrough_power_w: float
    main_meter_energy_consumed_wh: float
    main_meter_energy_produced_wh: float
    feedthrough_energy_consumed_wh: float
    feedthrough_energy_produced_wh: float

    dsm_state: str
    current_run_config: str

    door_state: str
    proximity_proven: bool
    uptime_s: int
    eth0_link: bool
    wlan_link: bool
    wwan_link: bool
    panel_size: int

    dominant_power_source: str | None = None
    grid_state: str | None = None
    grid_islandable: bool | None = None
    l1_voltage: float | None = None
    l2_voltage: float | None = None
    main_breaker_rating_a: int | None = None
    wifi_ssid: str | None = None
    vendor_cloud: str | None = None

    power_flow_pv: float | None = None
    power_flow_battery: float | None = None
    power_flow_grid: float | None = None
    power_flow_site: float | None = None

    upstream_l1_current_a: float | None = None
    upstream_l2_current_a: float | None = None
    downstream_l1_current_a: float | None = None
    downstream_l2_current_a: float | None = None

    circuits: dict[str, SpanCircuitSnapshot] = field(default_factory=dict)
    battery: SpanBatterySnapshot = field(default_factory=SpanBatterySnapshot)
    pv: SpanPVSnapshot = field(default_factory=SpanPVSnapshot)
    evse: dict[str, SpanEvseSnapshot] = field(default_factory=dict)
