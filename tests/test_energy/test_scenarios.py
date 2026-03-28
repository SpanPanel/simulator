"""Layer 3: Topology and scenario tests covering all identified issues."""

from __future__ import annotations

from span_panel_simulator.energy.system import EnergySystem
from span_panel_simulator.energy.types import (
    BESSConfig,
    EnergySystemConfig,
    GridConfig,
    LoadConfig,
    PowerInputs,
    PVConfig,
)


def _grid_online() -> GridConfig:
    return GridConfig(connected=True)


def _grid_offline() -> GridConfig:
    return GridConfig(connected=False)


def _pv(nameplate_w: float = 6000.0, inverter_type: str = "ac_coupled") -> PVConfig:
    return PVConfig(nameplate_w=nameplate_w, inverter_type=inverter_type)


def _bess(
    *,
    nameplate_kwh: float = 13.5,
    max_discharge_w: float = 5000.0,
    max_charge_w: float = 3500.0,
    hybrid: bool = False,
    backup_reserve_pct: float = 20.0,
    initial_soe_kwh: float | None = None,
) -> BESSConfig:
    return BESSConfig(
        nameplate_kwh=nameplate_kwh,
        max_discharge_w=max_discharge_w,
        max_charge_w=max_charge_w,
        hybrid=hybrid,
        backup_reserve_pct=backup_reserve_pct,
        initial_soe_kwh=initial_soe_kwh,
    )


class TestGFEThrottling:
    def test_grid_never_negative_from_bess_discharge(self) -> None:
        system = EnergySystem.from_config(
            EnergySystemConfig(
                grid=_grid_online(),
                pv=_pv(),
                bess=_bess(),
                loads=[LoadConfig(demand_w=3000.0)],
            )
        )
        state = system.tick(
            1000.0,
            PowerInputs(
                pv_available_w=2000.0,
                bess_scheduled_state="discharging",
                bess_requested_w=5000.0,
                load_demand_w=3000.0,
                grid_connected=True,
            ),
        )
        assert state.balanced
        assert state.grid_power_w >= -0.01
        assert state.bess_power_w == 1000.0

    def test_bess_covers_exact_deficit(self) -> None:
        system = EnergySystem.from_config(
            EnergySystemConfig(
                grid=_grid_online(),
                bess=_bess(),
                loads=[LoadConfig(demand_w=3000.0)],
            )
        )
        state = system.tick(
            1000.0,
            PowerInputs(
                bess_scheduled_state="discharging",
                bess_requested_w=5000.0,
                load_demand_w=3000.0,
                grid_connected=True,
            ),
        )
        assert state.balanced
        assert abs(state.grid_power_w) < 0.01
        assert abs(state.bess_power_w - 3000.0) < 0.01

    def test_bess_idle_when_pv_exceeds_load(self) -> None:
        system = EnergySystem.from_config(
            EnergySystemConfig(
                grid=_grid_online(),
                pv=_pv(),
                bess=_bess(),
                loads=[LoadConfig(demand_w=2000.0)],
            )
        )
        state = system.tick(
            1000.0,
            PowerInputs(
                pv_available_w=5000.0,
                bess_scheduled_state="discharging",
                bess_requested_w=5000.0,
                load_demand_w=2000.0,
                grid_connected=True,
            ),
        )
        assert state.balanced
        assert state.bess_power_w == 0.0
        assert state.bess_state == "idle"


class TestIslanding:
    def test_non_hybrid_pv_offline_bess_covers_all(self) -> None:
        system = EnergySystem.from_config(
            EnergySystemConfig(
                grid=_grid_offline(),
                pv=_pv(inverter_type="ac_coupled"),
                bess=_bess(hybrid=False),
                loads=[LoadConfig(demand_w=3000.0)],
            )
        )
        state = system.tick(
            1000.0,
            PowerInputs(
                pv_available_w=4000.0,
                bess_scheduled_state="discharging",
                bess_requested_w=5000.0,
                load_demand_w=3000.0,
                grid_connected=False,
            ),
        )
        assert state.balanced
        assert state.pv_power_w == 0.0
        assert state.bess_power_w == 3000.0
        assert state.grid_power_w == 0.0

    def test_hybrid_pv_online_bess_covers_gap(self) -> None:
        system = EnergySystem.from_config(
            EnergySystemConfig(
                grid=_grid_offline(),
                pv=_pv(inverter_type="hybrid"),
                bess=_bess(hybrid=True),
                loads=[LoadConfig(demand_w=5000.0)],
            )
        )
        state = system.tick(
            1000.0,
            PowerInputs(
                pv_available_w=3000.0,
                bess_scheduled_state="discharging",
                bess_requested_w=5000.0,
                load_demand_w=5000.0,
                grid_connected=False,
            ),
        )
        assert state.balanced
        assert state.pv_power_w == 3000.0
        assert state.bess_power_w == 2000.0
        assert state.grid_power_w == 0.0

    def test_hybrid_island_solar_excess_charges_bess(self) -> None:
        system = EnergySystem.from_config(
            EnergySystemConfig(
                grid=_grid_offline(),
                pv=_pv(inverter_type="hybrid"),
                bess=_bess(hybrid=True, max_charge_w=3000.0, initial_soe_kwh=5.0),
                loads=[LoadConfig(demand_w=2000.0)],
            )
        )
        state = system.tick(
            1000.0,
            PowerInputs(
                pv_available_w=5000.0,
                bess_scheduled_state="charging",
                bess_requested_w=3000.0,
                load_demand_w=2000.0,
                grid_connected=False,
            ),
        )
        assert state.balanced
        assert state.pv_power_w == 5000.0
        assert state.bess_state == "charging"
        assert state.bess_power_w == 3000.0
        assert state.grid_power_w == 0.0

    def test_non_hybrid_island_ignores_solar_excess(self) -> None:
        system = EnergySystem.from_config(
            EnergySystemConfig(
                grid=_grid_offline(),
                pv=_pv(inverter_type="ac_coupled"),
                bess=_bess(hybrid=False),
                loads=[LoadConfig(demand_w=3000.0)],
            )
        )
        state = system.tick(
            1000.0,
            PowerInputs(
                pv_available_w=5000.0,
                bess_scheduled_state="charging",
                bess_requested_w=3000.0,
                load_demand_w=3000.0,
                grid_connected=False,
            ),
        )
        assert state.balanced
        assert state.pv_power_w == 0.0
        assert state.bess_power_w == 3000.0
        assert state.bess_state == "discharging"


class TestGridImpact:
    def test_charging_increases_grid_import(self) -> None:
        system = EnergySystem.from_config(
            EnergySystemConfig(
                grid=_grid_online(),
                bess=_bess(max_charge_w=3000.0),
                loads=[LoadConfig(demand_w=2000.0)],
            )
        )
        state = system.tick(
            1000.0,
            PowerInputs(
                bess_scheduled_state="charging",
                bess_requested_w=3000.0,
                load_demand_w=2000.0,
                grid_connected=True,
            ),
        )
        assert state.balanced
        assert abs(state.grid_power_w - 5000.0) < 0.01

    def test_discharging_decreases_grid_import(self) -> None:
        system = EnergySystem.from_config(
            EnergySystemConfig(
                grid=_grid_online(),
                bess=_bess(max_discharge_w=3000.0),
                loads=[LoadConfig(demand_w=5000.0)],
            )
        )
        state = system.tick(
            1000.0,
            PowerInputs(
                bess_scheduled_state="discharging",
                bess_requested_w=3000.0,
                load_demand_w=5000.0,
                grid_connected=True,
            ),
        )
        assert state.balanced
        assert abs(state.grid_power_w - 2000.0) < 0.01

    def test_add_bess_reduces_grid_in_modeling(self) -> None:
        config_no_bess = EnergySystemConfig(
            grid=_grid_online(),
            loads=[LoadConfig(demand_w=5000.0)],
        )
        config_with_bess = EnergySystemConfig(
            grid=_grid_online(),
            bess=_bess(max_discharge_w=3000.0),
            loads=[LoadConfig(demand_w=5000.0)],
        )
        sys_b = EnergySystem.from_config(config_no_bess)
        sys_a = EnergySystem.from_config(config_with_bess)

        state_b = sys_b.tick(1000.0, PowerInputs(load_demand_w=5000.0))
        state_a = sys_a.tick(
            1000.0,
            PowerInputs(
                load_demand_w=5000.0,
                bess_scheduled_state="discharging",
                bess_requested_w=3000.0,
            ),
        )
        assert state_b.balanced and state_a.balanced
        assert abs(state_b.grid_power_w - 5000.0) < 0.01
        assert abs(state_a.grid_power_w - 2000.0) < 0.01


class TestIndependentInstances:
    def test_two_instances_share_no_state(self) -> None:
        config = EnergySystemConfig(
            grid=_grid_online(),
            bess=_bess(initial_soe_kwh=10.0),
            loads=[LoadConfig(demand_w=3000.0)],
        )
        sys1 = EnergySystem.from_config(config)
        sys2 = EnergySystem.from_config(config)

        sys1.tick(
            1000.0,
            PowerInputs(
                bess_scheduled_state="discharging",
                bess_requested_w=3000.0,
                load_demand_w=3000.0,
            ),
        )
        sys1.tick(
            4600.0,
            PowerInputs(
                bess_scheduled_state="discharging",
                bess_requested_w=3000.0,
                load_demand_w=3000.0,
            ),
        )

        state2 = sys2.tick(
            1000.0,
            PowerInputs(
                bess_scheduled_state="discharging",
                bess_requested_w=3000.0,
                load_demand_w=3000.0,
            ),
        )
        assert state2.soe_kwh == 10.0


class TestEVSE:
    def test_evse_is_pure_load(self) -> None:
        system = EnergySystem.from_config(
            EnergySystemConfig(
                grid=_grid_online(),
                loads=[
                    LoadConfig(demand_w=3000.0),
                    LoadConfig(demand_w=7200.0),
                ],
            )
        )
        state = system.tick(
            1000.0,
            PowerInputs(
                load_demand_w=10200.0,
                grid_connected=True,
            ),
        )
        assert state.balanced
        assert abs(state.grid_power_w - 10200.0) < 0.01
        assert abs(state.load_power_w - 10200.0) < 0.01


class TestNameplateDuration:
    def test_larger_nameplate_sustains_longer(self) -> None:
        small_config = EnergySystemConfig(
            grid=_grid_online(),
            bess=_bess(nameplate_kwh=5.0, max_discharge_w=2500.0, initial_soe_kwh=2.5),
            loads=[LoadConfig(demand_w=2500.0)],
        )
        large_config = EnergySystemConfig(
            grid=_grid_online(),
            bess=_bess(nameplate_kwh=20.0, max_discharge_w=2500.0, initial_soe_kwh=10.0),
            loads=[LoadConfig(demand_w=2500.0)],
        )
        small = EnergySystem.from_config(small_config)
        large = EnergySystem.from_config(large_config)

        inputs = PowerInputs(
            bess_scheduled_state="discharging",
            bess_requested_w=2500.0,
            load_demand_w=2500.0,
            grid_connected=True,
        )

        ts = 0.0
        s_small = None
        s_large = None
        for _ in range(120):
            ts += 60.0
            s_small = small.tick(ts, inputs)
            s_large = large.tick(ts, inputs)

        assert s_small is not None
        assert s_large is not None
        assert s_small.bess_state == "idle"
        assert s_large.bess_state == "discharging"
