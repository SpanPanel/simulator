"""Layer 1: Component unit tests for the energy system."""

from __future__ import annotations

from span_panel_simulator.energy.components import GridMeter, LoadGroup, PVSource
from span_panel_simulator.energy.types import (
    BusState,
    ComponentRole,
    PowerContribution,
)


class TestPowerContribution:
    def test_default_zero(self) -> None:
        pc = PowerContribution()
        assert pc.demand_w == 0.0
        assert pc.supply_w == 0.0

    def test_demand_only(self) -> None:
        pc = PowerContribution(demand_w=5000.0)
        assert pc.demand_w == 5000.0
        assert pc.supply_w == 0.0


class TestBusState:
    def test_net_deficit_positive(self) -> None:
        bs = BusState(total_demand_w=5000.0, total_supply_w=3000.0)
        assert bs.net_deficit_w == 2000.0

    def test_net_deficit_negative_means_excess(self) -> None:
        bs = BusState(total_demand_w=2000.0, total_supply_w=5000.0)
        assert bs.net_deficit_w == -3000.0

    def test_balanced_when_grid_absorbs_residual(self) -> None:
        bs = BusState(
            total_demand_w=5000.0,
            total_supply_w=3000.0,
            grid_power_w=2000.0,
        )
        assert bs.is_balanced()

    def test_not_balanced_when_residual_exists(self) -> None:
        bs = BusState(
            total_demand_w=5000.0,
            total_supply_w=3000.0,
            grid_power_w=1000.0,
        )
        assert not bs.is_balanced()


class TestComponentRole:
    def test_role_ordering(self) -> None:
        roles = [
            ComponentRole.SLACK,
            ComponentRole.LOAD,
            ComponentRole.STORAGE,
            ComponentRole.SOURCE,
        ]
        sorted_roles = sorted(roles, key=lambda r: r.value)
        assert sorted_roles == [
            ComponentRole.LOAD,
            ComponentRole.SOURCE,
            ComponentRole.STORAGE,
            ComponentRole.SLACK,
        ]


class TestLoadGroup:
    def test_returns_demand(self) -> None:
        load = LoadGroup(demand_w=5000.0)
        contribution = load.resolve(BusState())
        assert contribution.demand_w == 5000.0
        assert contribution.supply_w == 0.0

    def test_zero_demand(self) -> None:
        load = LoadGroup(demand_w=0.0)
        contribution = load.resolve(BusState())
        assert contribution.demand_w == 0.0


class TestGridMeter:
    def test_absorbs_deficit(self) -> None:
        grid = GridMeter(connected=True)
        bus = BusState(total_demand_w=5000.0, total_supply_w=3000.0)
        contribution = grid.resolve(bus)
        assert contribution.supply_w == 2000.0
        assert contribution.demand_w == 0.0

    def test_absorbs_excess(self) -> None:
        grid = GridMeter(connected=True)
        bus = BusState(total_demand_w=2000.0, total_supply_w=5000.0)
        contribution = grid.resolve(bus)
        assert contribution.demand_w == 3000.0
        assert contribution.supply_w == 0.0

    def test_zero_when_balanced(self) -> None:
        grid = GridMeter(connected=True)
        bus = BusState(total_demand_w=3000.0, total_supply_w=3000.0)
        contribution = grid.resolve(bus)
        assert contribution.demand_w == 0.0
        assert contribution.supply_w == 0.0

    def test_zero_when_disconnected(self) -> None:
        grid = GridMeter(connected=False)
        bus = BusState(total_demand_w=5000.0, total_supply_w=0.0)
        contribution = grid.resolve(bus)
        assert contribution.demand_w == 0.0
        assert contribution.supply_w == 0.0


class TestPVSource:
    def test_returns_available_power_when_online(self) -> None:
        pv = PVSource(available_power_w=4000.0, online=True)
        contribution = pv.resolve(BusState())
        assert contribution.supply_w == 4000.0
        assert contribution.demand_w == 0.0

    def test_returns_zero_when_offline(self) -> None:
        pv = PVSource(available_power_w=4000.0, online=False)
        contribution = pv.resolve(BusState())
        assert contribution.supply_w == 0.0
        assert contribution.demand_w == 0.0

    def test_zero_production(self) -> None:
        pv = PVSource(available_power_w=0.0, online=True)
        contribution = pv.resolve(BusState())
        assert contribution.supply_w == 0.0
