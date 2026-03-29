"""Core types for the component-based energy system."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum


class ComponentRole(IntEnum):
    """Role of a component on the bus, ordered by resolution priority."""

    LOAD = 1
    SOURCE = 2
    STORAGE = 3
    SLACK = 4


@dataclass
class PowerContribution:
    """Power contribution from a single component on the bus.

    All values are non-negative magnitudes; direction is expressed by
    which field is populated (demand_w vs supply_w).
    """

    demand_w: float = 0.0
    supply_w: float = 0.0


@dataclass
class BusState:
    """Aggregate state of the energy bus at a point in time."""

    total_demand_w: float = 0.0
    total_supply_w: float = 0.0
    storage_contribution_w: float = 0.0
    grid_power_w: float = 0.0

    @property
    def net_deficit_w(self) -> float:
        """Positive means demand exceeds supply; negative means surplus."""
        return self.total_demand_w - self.total_supply_w

    def is_balanced(self) -> bool:
        """Return True when grid power accounts for any residual imbalance."""
        return abs(self.total_demand_w - self.total_supply_w - self.grid_power_w) < 0.01


@dataclass
class PowerInputs:
    """External inputs fed into the energy resolution pipeline.

    The BESS operates like a real system — it delivers whatever power
    the home needs (up to its inverter rate), gated only by schedule
    and SOE.  There is no ``bess_requested_w``; the energy system uses
    the max inverter rate from the BESSConfig.
    """

    pv_available_w: float = 0.0
    bess_scheduled_state: str = "idle"
    load_demand_w: float = 0.0
    grid_connected: bool = True


@dataclass
class SystemState:
    """Resolved system state after energy dispatch."""

    grid_power_w: float
    pv_power_w: float
    bess_power_w: float
    bess_state: str
    load_power_w: float
    soe_kwh: float
    soe_percentage: float
    balanced: bool


@dataclass(frozen=True)
class GridConfig:
    """Configuration for the utility grid connection."""

    connected: bool = True


@dataclass(frozen=True)
class PVConfig:
    """Configuration for a solar PV inverter."""

    nameplate_w: float = 0.0
    inverter_type: str = "ac_coupled"


@dataclass(frozen=True)
class BESSConfig:
    """Configuration for a battery energy storage system."""

    nameplate_kwh: float = 13.5
    max_charge_w: float = 3500.0
    max_discharge_w: float = 3500.0
    charge_efficiency: float = 0.95
    discharge_efficiency: float = 0.95
    backup_reserve_pct: float = 20.0
    hard_min_pct: float = 5.0
    hybrid: bool = False
    initial_soe_kwh: float | None = None
    panel_serial: str = ""
    feed_circuit_id: str = ""
    charge_hours: tuple[int, ...] = ()
    discharge_hours: tuple[int, ...] = ()
    panel_timezone: str = "America/Los_Angeles"


@dataclass(frozen=True)
class LoadConfig:
    """Configuration for a load group."""

    demand_w: float = 0.0


@dataclass(frozen=True)
class EnergySystemConfig:
    """Top-level configuration for the entire energy system."""

    grid: GridConfig
    pv: PVConfig | None = None
    bess: BESSConfig | None = None
    loads: list[LoadConfig] = field(default_factory=list)
