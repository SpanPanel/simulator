"""Component-based energy system for the SPAN panel simulator.

Public API:
    EnergySystem    — top-level resolver (construct via from_config)
    SystemState     — resolved output of a tick
    PowerInputs     — external inputs that drive each tick
    EnergySystemConfig, GridConfig, PVConfig, BESSConfig, LoadConfig — configuration
"""

from span_panel_simulator.energy.system import EnergySystem
from span_panel_simulator.energy.types import (
    BESSConfig,
    EnergySystemConfig,
    GridConfig,
    LoadConfig,
    PowerInputs,
    PVConfig,
    SystemState,
)

__all__ = [
    "BESSConfig",
    "EnergySystem",
    "EnergySystemConfig",
    "GridConfig",
    "LoadConfig",
    "PVConfig",
    "PowerInputs",
    "SystemState",
]
