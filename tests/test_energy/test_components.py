"""Layer 1: Component unit tests for the energy system."""

from __future__ import annotations

from span_panel_simulator.energy.components import BESSUnit, GridMeter, LoadGroup, PVSource
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


class TestBESSUnitDischarge:
    def _make_bess(
        self,
        *,
        nameplate_kwh: float = 13.5,
        max_discharge_w: float = 5000.0,
        max_charge_w: float = 3500.0,
        soe_kwh: float = 10.0,
        backup_reserve_pct: float = 20.0,
        hard_min_pct: float = 5.0,
        scheduled_state: str = "discharging",
        requested_power_w: float = 5000.0,
    ) -> BESSUnit:
        return BESSUnit(
            nameplate_capacity_kwh=nameplate_kwh,
            max_charge_w=max_charge_w,
            max_discharge_w=max_discharge_w,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            backup_reserve_pct=backup_reserve_pct,
            hard_min_pct=hard_min_pct,
            hybrid=False,
            pv_source=None,
            soe_kwh=soe_kwh,
            scheduled_state=scheduled_state,
            requested_power_w=requested_power_w,
        )

    def test_discharge_throttled_to_deficit(self) -> None:
        """GFE: only source what loads demand."""
        bess = self._make_bess(requested_power_w=5000.0)
        bus = BusState(total_demand_w=2000.0, total_supply_w=0.0)
        contribution = bess.resolve(bus)
        assert contribution.supply_w == 2000.0
        assert bess.effective_state == "discharging"

    def test_discharge_idle_when_no_deficit(self) -> None:
        """Solar covers all loads — no discharge needed."""
        bess = self._make_bess(requested_power_w=5000.0)
        bus = BusState(total_demand_w=3000.0, total_supply_w=4000.0)
        contribution = bess.resolve(bus)
        assert contribution.supply_w == 0.0
        assert bess.effective_state == "idle"
        assert bess.effective_power_w == 0.0

    def test_discharge_limited_by_max_rate(self) -> None:
        bess = self._make_bess(max_discharge_w=2000.0, requested_power_w=5000.0)
        bus = BusState(total_demand_w=8000.0, total_supply_w=0.0)
        contribution = bess.resolve(bus)
        assert contribution.supply_w == 2000.0

    def test_discharge_limited_by_requested(self) -> None:
        bess = self._make_bess(requested_power_w=1500.0)
        bus = BusState(total_demand_w=5000.0, total_supply_w=0.0)
        contribution = bess.resolve(bus)
        assert contribution.supply_w == 1500.0

    def test_discharge_stops_at_backup_reserve(self) -> None:
        """SOE at backup reserve — cannot discharge."""
        bess = self._make_bess(
            nameplate_kwh=10.0,
            soe_kwh=2.0,  # exactly 20% of 10 kWh
            backup_reserve_pct=20.0,
        )
        bus = BusState(total_demand_w=5000.0, total_supply_w=0.0)
        contribution = bess.resolve(bus)
        assert contribution.supply_w == 0.0
        assert bess.effective_state == "idle"

    def test_idle_state_returns_zero(self) -> None:
        bess = self._make_bess(scheduled_state="idle")
        bus = BusState(total_demand_w=5000.0, total_supply_w=0.0)
        contribution = bess.resolve(bus)
        assert contribution.supply_w == 0.0
        assert contribution.demand_w == 0.0


class TestBESSUnitCharge:
    def _make_bess(self, **kwargs: object) -> BESSUnit:
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
            soe_kwh=6.75,
            scheduled_state="charging",
            requested_power_w=3000.0,
        )
        defaults.update(kwargs)
        return BESSUnit(**defaults)

    def test_charge_at_requested_rate(self) -> None:
        bess = self._make_bess(requested_power_w=2000.0)
        bus = BusState(total_demand_w=1000.0, total_supply_w=5000.0)
        contribution = bess.resolve(bus)
        assert contribution.demand_w == 2000.0
        assert bess.effective_state == "charging"

    def test_charge_limited_by_max_rate(self) -> None:
        bess = self._make_bess(max_charge_w=1500.0, requested_power_w=3000.0)
        bus = BusState()
        contribution = bess.resolve(bus)
        assert contribution.demand_w == 1500.0

    def test_charge_stops_at_full_soe(self) -> None:
        bess = self._make_bess(nameplate_capacity_kwh=10.0, soe_kwh=10.0)
        bus = BusState()
        contribution = bess.resolve(bus)
        assert contribution.demand_w == 0.0
        assert bess.effective_state == "idle"


class TestBESSUnitSOEIntegration:
    def _make_bess(self, **kwargs: object) -> BESSUnit:
        defaults: dict[str, object] = dict(
            nameplate_capacity_kwh=10.0,
            max_charge_w=3500.0,
            max_discharge_w=5000.0,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            backup_reserve_pct=20.0,
            hard_min_pct=5.0,
            hybrid=False,
            pv_source=None,
            soe_kwh=5.0,
            scheduled_state="idle",
            requested_power_w=0.0,
        )
        defaults.update(kwargs)
        return BESSUnit(**defaults)

    def test_discharge_decreases_soe(self) -> None:
        bess = self._make_bess(
            soe_kwh=5.0,
            scheduled_state="discharging",
            requested_power_w=2000.0,
        )
        bus = BusState(total_demand_w=5000.0, total_supply_w=0.0)
        bess.resolve(bus)
        # Seed the timestamp, then drive 12 x 300 s steps = 3600 s (1 hour)
        # respecting _MAX_INTEGRATION_DELTA_S.  The first call seeds _last_ts
        # without integrating; subsequent calls each integrate 300 s.
        bess.integrate_energy(0.0)
        ts = 300.0
        for _ in range(12):
            bess.integrate_energy(ts)
            ts += 300.0
        # 2000W for 1 hour / 0.95 efficiency = ~2.105 kWh consumed
        assert bess.soe_kwh < 5.0
        expected = 5.0 - (2.0 / 0.95)
        assert abs(bess.soe_kwh - expected) < 0.01

    def test_charge_increases_soe(self) -> None:
        bess = self._make_bess(
            soe_kwh=3.0,
            scheduled_state="charging",
            requested_power_w=2000.0,
        )
        bus = BusState()
        bess.resolve(bus)
        # Seed the timestamp, then drive 12 x 300 s steps = 3600 s (1 hour)
        # respecting _MAX_INTEGRATION_DELTA_S.
        bess.integrate_energy(0.0)
        ts = 300.0
        for _ in range(12):
            bess.integrate_energy(ts)
            ts += 300.0
        # 2000W for 1 hour * 0.95 efficiency = 1.9 kWh stored
        expected = 3.0 + (2.0 * 0.95)
        assert abs(bess.soe_kwh - expected) < 0.01

    def test_soe_clamped_to_bounds(self) -> None:
        bess = self._make_bess(nameplate_capacity_kwh=10.0, soe_kwh=9.9)
        bess.effective_state = "charging"
        bess.effective_power_w = 50000.0
        bess.integrate_energy(0.0)
        bess.integrate_energy(3600.0)
        assert bess.soe_kwh <= 10.0


class TestBESSUnitHybridPV:
    def test_hybrid_keeps_pv_online_when_grid_disconnected(self) -> None:
        pv = PVSource(available_power_w=4000.0, online=True)
        bess = BESSUnit(
            nameplate_capacity_kwh=13.5,
            max_charge_w=3500.0,
            max_discharge_w=5000.0,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            backup_reserve_pct=20.0,
            hard_min_pct=5.0,
            hybrid=True,
            pv_source=pv,
            soe_kwh=6.75,
        )
        bess.update_pv_online_status(grid_connected=False)
        assert pv.online is True

    def test_non_hybrid_sheds_pv_when_grid_disconnected(self) -> None:
        pv = PVSource(available_power_w=4000.0, online=True)
        bess = BESSUnit(
            nameplate_capacity_kwh=13.5,
            max_charge_w=3500.0,
            max_discharge_w=5000.0,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            backup_reserve_pct=20.0,
            hard_min_pct=5.0,
            hybrid=False,
            pv_source=pv,
            soe_kwh=6.75,
        )
        bess.update_pv_online_status(grid_connected=False)
        assert pv.online is False

    def test_non_hybrid_pv_online_when_grid_connected(self) -> None:
        pv = PVSource(available_power_w=4000.0, online=False)
        bess = BESSUnit(
            nameplate_capacity_kwh=13.5,
            max_charge_w=3500.0,
            max_discharge_w=5000.0,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            backup_reserve_pct=20.0,
            hard_min_pct=5.0,
            hybrid=False,
            pv_source=pv,
            soe_kwh=6.75,
        )
        bess.update_pv_online_status(grid_connected=True)
        assert pv.online is True
