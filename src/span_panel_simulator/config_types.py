"""Configuration TypedDicts for the simulation engine.

These types define the shape of YAML configuration files used to configure
simulated panels: circuit templates, energy profiles, battery behavior,
tab synchronization, and global simulation parameters.
"""

from __future__ import annotations

from typing import Any, Literal, NotRequired, TypedDict


class PanelConfig(TypedDict):
    """Panel configuration."""

    serial_number: str
    total_tabs: int
    main_size: int  # Main breaker size in Amps
    latitude: NotRequired[float]  # degrees north, default 37.7
    longitude: NotRequired[float]  # degrees east, default -122.4
    soc_shed_threshold: NotRequired[float]  # SOC % below which SOC_THRESHOLD circuits are shed


class CyclingPattern(TypedDict, total=False):
    """Cycling behavior configuration."""

    on_duration: int  # Seconds
    off_duration: int  # Seconds


class TimeOfDayProfile(TypedDict, total=False):
    """Time-based behavior configuration."""

    enabled: bool
    peak_hours: list[int]  # Hours of day for peak activity
    hour_factors: dict[int, float]  # Hour-specific production factors
    production_hours: list[int]  # Hours when solar should produce
    night_hours: list[int]  # Hours when solar should not produce
    peak_factor: float  # Peak production factor


class SmartBehavior(TypedDict, total=False):
    """Smart load behavior configuration."""

    responds_to_grid: bool
    max_power_reduction: float  # 0.0 to 1.0


class EnergyProfile(TypedDict):
    """Energy profile defining production/consumption behavior."""

    mode: str  # "consumer", "producer", "bidirectional"
    power_range: list[float]  # [min, max] in Watts (negative for production)
    typical_power: float  # Watts (negative for production)
    power_variation: float  # 0.0 to 1.0 (percentage)


class EnergyProfileExtended(EnergyProfile, total=False):
    """Extended energy profile with optional features."""

    efficiency: float  # Energy conversion efficiency (0.0 to 1.0)
    nameplate_capacity_w: float  # PV nameplate rating in watts (positive)


class CircuitTemplate(TypedDict):
    """Circuit template configuration."""

    energy_profile: EnergyProfileExtended
    relay_behavior: str  # "controllable", "non_controllable"
    priority: str  # "MUST_HAVE", "NON_ESSENTIAL"


class BatteryBehavior(TypedDict, total=False):
    """Battery behavior configuration."""

    enabled: bool
    charge_mode: Literal["solar-gen", "solar-excess", "custom"]
    charge_power: float
    discharge_power: float
    idle_power: float
    charge_efficiency: float
    discharge_efficiency: float
    nameplate_capacity_kwh: float  # Total battery capacity in kWh
    backup_reserve_pct: float  # SOE % reserved for outages (default 20)
    charge_hours: list[int]
    discharge_hours: list[int]
    max_charge_power: float
    max_discharge_power: float
    idle_hours: list[int]
    idle_power_range: list[float]
    solar_intensity_profile: dict[int, float]
    demand_factor_profile: dict[int, float]


class CircuitTemplateExtended(CircuitTemplate, total=False):
    """Extended circuit template with optional behaviors."""

    cycling_pattern: CyclingPattern
    time_of_day_profile: TimeOfDayProfile
    smart_behavior: SmartBehavior
    battery_behavior: BatteryBehavior
    device_type: str  # Explicit override: "circuit", "evse", "pv"
    hvac_type: str  # "central_ac", "heat_pump", "heat_pump_aux"


class CircuitDefinition(TypedDict):
    """Individual circuit definition."""

    id: str
    name: str
    template: str
    tabs: list[int]


class CircuitDefinitionExtended(CircuitDefinition, total=False):
    """Extended circuit definition with overrides."""

    overrides: dict[str, Any]


class TabSynchronization(TypedDict):
    """Tab synchronization configuration."""

    tabs: list[int]
    behavior: str  # e.g., "240v_split_phase", "generator_paralleled"
    power_split: str  # "equal", "primary_secondary", "custom_ratio"
    energy_sync: bool
    template: str  # Template name to apply to synchronized group


class SimulationParams(TypedDict, total=False):
    """Global simulation parameters."""

    update_interval: int  # Seconds
    time_acceleration: float  # Multiplier for time progression
    noise_factor: float  # Random noise percentage
    enable_realistic_behaviors: bool
    simulation_start_time: str  # ISO format datetime string (e.g., "2024-06-15T12:00:00")
    use_simulation_time: bool  # Whether to use simulation time vs system time


class SimulationConfig(TypedDict):
    """Complete simulation configuration."""

    panel_config: PanelConfig
    circuit_templates: dict[str, CircuitTemplateExtended]
    circuits: list[CircuitDefinitionExtended]
    unmapped_tabs: list[int]
    simulation_params: SimulationParams
    unmapped_tab_templates: NotRequired[dict[str, CircuitTemplateExtended]]
    tab_synchronizations: NotRequired[list[TabSynchronization]]
