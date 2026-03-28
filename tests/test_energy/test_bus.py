"""Layer 2: Bus integration tests — conservation enforcement."""

from __future__ import annotations

from span_panel_simulator.energy.bus import PanelBus
from span_panel_simulator.energy.components import BESSUnit, GridMeter, LoadGroup, PVSource


def _make_bess(**kwargs: object) -> BESSUnit:
    defaults: dict[str, object] = dict(
        nameplate_capacity_kwh=13.5,
        max_charge_w=3500.0,
        max_discharge_w=5000.0,
        charge_efficiency=0.95,
        discharge_efficiency=0.95,
        backup_reserve_pct=20.0,
        hard_min_pct=5.0,
        hybrid=False,
        pv_source=None,
        soe_kwh=10.0,
        scheduled_state="idle",
        requested_power_w=0.0,
    )
    defaults.update(kwargs)
    return BESSUnit(**defaults)


class TestBusConservation:
    def test_load_only_grid_covers(self) -> None:
        bus = PanelBus(components=[LoadGroup(demand_w=5000.0), GridMeter(connected=True)])
        state = bus.resolve()
        assert state.is_balanced()
        assert state.grid_power_w == 5000.0

    def test_load_and_pv_grid_covers_deficit(self) -> None:
        bus = PanelBus(
            components=[
                LoadGroup(demand_w=5000.0),
                PVSource(available_power_w=3000.0, online=True),
                GridMeter(connected=True),
            ]
        )
        state = bus.resolve()
        assert state.is_balanced()
        assert abs(state.grid_power_w - 2000.0) < 0.01

    def test_pv_exceeds_load_grid_absorbs(self) -> None:
        bus = PanelBus(
            components=[
                LoadGroup(demand_w=2000.0),
                PVSource(available_power_w=5000.0, online=True),
                GridMeter(connected=True),
            ]
        )
        state = bus.resolve()
        assert state.is_balanced()
        assert abs(state.grid_power_w - (-3000.0)) < 0.01
        assert abs(state.total_demand_w - 2000.0) < 0.01
        assert abs(state.total_supply_w - 5000.0) < 0.01

    def test_bess_discharge_reduces_grid(self) -> None:
        bess = _make_bess(scheduled_state="discharging", requested_power_w=3000.0)
        bus = PanelBus(
            components=[
                LoadGroup(demand_w=5000.0),
                bess,
                GridMeter(connected=True),
            ]
        )
        state = bus.resolve()
        assert state.is_balanced()
        assert abs(state.grid_power_w - 2000.0) < 0.01

    def test_bess_charge_increases_grid(self) -> None:
        bess = _make_bess(scheduled_state="charging", requested_power_w=3000.0, soe_kwh=5.0)
        bus = PanelBus(
            components=[
                LoadGroup(demand_w=2000.0),
                bess,
                GridMeter(connected=True),
            ]
        )
        state = bus.resolve()
        assert state.is_balanced()
        assert abs(state.grid_power_w - 5000.0) < 0.01

    def test_grid_never_negative_from_bess_discharge(self) -> None:
        bess = _make_bess(scheduled_state="discharging", requested_power_w=5000.0)
        bus = PanelBus(
            components=[
                LoadGroup(demand_w=2000.0),
                PVSource(available_power_w=1000.0, online=True),
                bess,
                GridMeter(connected=True),
            ]
        )
        state = bus.resolve()
        assert state.is_balanced()
        assert state.grid_power_w >= -0.01

    def test_conservation_grid_disconnected(self) -> None:
        bess = _make_bess(scheduled_state="discharging", requested_power_w=5000.0)
        bus = PanelBus(
            components=[
                LoadGroup(demand_w=3000.0),
                bess,
                GridMeter(connected=False),
            ]
        )
        state = bus.resolve()
        assert state.is_balanced()
        assert abs(state.grid_power_w) < 0.01

    def test_conservation_no_bess(self) -> None:
        bus = PanelBus(
            components=[
                LoadGroup(demand_w=5000.0),
                PVSource(available_power_w=2000.0, online=True),
                GridMeter(connected=True),
            ]
        )
        state = bus.resolve()
        assert state.is_balanced()

    def test_conservation_no_pv(self) -> None:
        bus = PanelBus(
            components=[
                LoadGroup(demand_w=5000.0),
                GridMeter(connected=True),
            ]
        )
        state = bus.resolve()
        assert state.is_balanced()
