"""Physical energy components for the panel bus.

Each component has a role (LOAD, SOURCE, STORAGE, SLACK) and implements
``resolve()`` which returns a ``PowerContribution`` given the current
``BusState``.  All power values are non-negative magnitudes; direction
is expressed by which field (``demand_w`` vs ``supply_w``) is populated.
"""

from __future__ import annotations

from span_panel_simulator.energy.types import (
    BusState,
    ComponentRole,
    PowerContribution,
)


class Component:
    """Base class for all bus components."""

    role: ComponentRole

    def resolve(self, bus_state: BusState) -> PowerContribution:
        raise NotImplementedError


class LoadGroup(Component):
    """Consumer load — declares demand on the bus."""

    role = ComponentRole.LOAD

    def __init__(self, demand_w: float = 0.0) -> None:
        self.demand_w = demand_w

    def resolve(self, bus_state: BusState) -> PowerContribution:
        return PowerContribution(demand_w=self.demand_w)


class GridMeter(Component):
    """Utility grid connection — slack bus that absorbs residual."""

    role = ComponentRole.SLACK

    def __init__(self, connected: bool = True) -> None:
        self.connected = connected

    def resolve(self, bus_state: BusState) -> PowerContribution:
        if not self.connected:
            return PowerContribution()
        deficit = bus_state.net_deficit_w
        if deficit > 0:
            return PowerContribution(supply_w=deficit)
        elif deficit < 0:
            return PowerContribution(demand_w=-deficit)
        return PowerContribution()


class PVSource(Component):
    """Solar PV inverter — declares available production."""

    role = ComponentRole.SOURCE

    def __init__(self, available_power_w: float = 0.0, online: bool = True) -> None:
        self.available_power_w = available_power_w
        self.online = online

    def resolve(self, bus_state: BusState) -> PowerContribution:
        if not self.online:
            return PowerContribution()
        return PowerContribution(supply_w=self.available_power_w)
