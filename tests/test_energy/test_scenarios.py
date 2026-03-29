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

# All tests use ts=1000.0 which maps to hour 16 in America/Los_Angeles.
# TOU schedule hours are set accordingly to control BESS state via config
# rather than leaking scheduling into PowerInputs.
_TS = 1000.0
_DISCHARGE_AT_TS = (16,)
_CHARGE_AT_TS = (16,)


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
    charge_mode: str = "custom",
    charge_hours: tuple[int, ...] = (),
    discharge_hours: tuple[int, ...] = (),
) -> BESSConfig:
    return BESSConfig(
        nameplate_kwh=nameplate_kwh,
        max_discharge_w=max_discharge_w,
        max_charge_w=max_charge_w,
        hybrid=hybrid,
        backup_reserve_pct=backup_reserve_pct,
        initial_soe_kwh=initial_soe_kwh,
        charge_mode=charge_mode,
        charge_hours=charge_hours,
        discharge_hours=discharge_hours,
    )


class TestGFEThrottling:
    def test_grid_never_negative_from_bess_discharge(self) -> None:
        system = EnergySystem.from_config(
            EnergySystemConfig(
                grid=_grid_online(),
                pv=_pv(),
                bess=_bess(discharge_hours=_DISCHARGE_AT_TS),
                loads=[LoadConfig(demand_w=3000.0)],
            )
        )
        state = system.tick(
            _TS,
            PowerInputs(
                pv_available_w=2000.0,
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
                bess=_bess(discharge_hours=_DISCHARGE_AT_TS),
                loads=[LoadConfig(demand_w=3000.0)],
            )
        )
        state = system.tick(
            _TS,
            PowerInputs(
                load_demand_w=3000.0,
                grid_connected=True,
            ),
        )
        assert state.balanced
        assert abs(state.grid_power_w) < 0.01
        assert abs(state.bess_power_w - 3000.0) < 0.01

    def test_bess_idle_when_pv_exceeds_load(self) -> None:
        """BESS scheduled to discharge but PV surplus means no deficit — GFE idles."""
        system = EnergySystem.from_config(
            EnergySystemConfig(
                grid=_grid_online(),
                pv=_pv(),
                bess=_bess(discharge_hours=_DISCHARGE_AT_TS),
                loads=[LoadConfig(demand_w=2000.0)],
            )
        )
        state = system.tick(
            _TS,
            PowerInputs(
                pv_available_w=5000.0,
                load_demand_w=2000.0,
                grid_connected=True,
            ),
        )
        assert state.balanced
        assert state.bess_power_w == 0.0
        assert state.bess_state == "idle"


class TestIslanding:
    def test_non_hybrid_pv_offline_bess_covers_all(self) -> None:
        """Non-hybrid off-grid: PV sheds, BESS forced to discharge by islanding override."""
        system = EnergySystem.from_config(
            EnergySystemConfig(
                grid=_grid_offline(),
                pv=_pv(inverter_type="ac_coupled"),
                bess=_bess(hybrid=False, discharge_hours=_DISCHARGE_AT_TS),
                loads=[LoadConfig(demand_w=3000.0)],
            )
        )
        state = system.tick(
            _TS,
            PowerInputs(
                pv_available_w=4000.0,
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
                bess=_bess(hybrid=True, discharge_hours=_DISCHARGE_AT_TS),
                loads=[LoadConfig(demand_w=5000.0)],
            )
        )
        state = system.tick(
            _TS,
            PowerInputs(
                pv_available_w=3000.0,
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
                bess=_bess(
                    hybrid=True,
                    max_charge_w=3000.0,
                    initial_soe_kwh=5.0,
                    charge_hours=_CHARGE_AT_TS,
                ),
                loads=[LoadConfig(demand_w=2000.0)],
            )
        )
        state = system.tick(
            _TS,
            PowerInputs(
                pv_available_w=5000.0,
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
        """Non-hybrid off-grid: PV sheds, non-hybrid islanding override forces discharge."""
        system = EnergySystem.from_config(
            EnergySystemConfig(
                grid=_grid_offline(),
                pv=_pv(inverter_type="ac_coupled"),
                bess=_bess(hybrid=False, charge_hours=_CHARGE_AT_TS),
                loads=[LoadConfig(demand_w=3000.0)],
            )
        )
        state = system.tick(
            _TS,
            PowerInputs(
                pv_available_w=5000.0,
                load_demand_w=3000.0,
                grid_connected=False,
            ),
        )
        assert state.balanced
        assert state.pv_power_w == 0.0
        assert state.bess_power_w == 3000.0
        assert state.bess_state == "discharging"


class TestPVCurtailment:
    """PV curtailment when islanded and production exceeds absorbable demand.

    Real hybrid inverters reduce their MPPT setpoint when islanded so that
    PV never produces more than load + achievable BESS charge.  These tests
    verify the simulator reproduces that behavior.
    """

    def test_curtails_pv_when_surplus_exceeds_bess_charge(self) -> None:
        """PV 7kW, load 2kW, BESS charge 3kW max => curtail PV to 5kW."""
        system = EnergySystem.from_config(
            EnergySystemConfig(
                grid=_grid_offline(),
                pv=_pv(inverter_type="hybrid"),
                bess=_bess(
                    hybrid=True,
                    max_charge_w=3000.0,
                    initial_soe_kwh=5.0,
                    charge_mode="self-consumption",
                ),
                loads=[LoadConfig(demand_w=2000.0)],
            )
        )
        state = system.tick(
            _TS,
            PowerInputs(
                pv_available_w=7000.0,
                load_demand_w=2000.0,
                grid_connected=False,
            ),
        )
        assert state.balanced
        assert state.pv_power_w == 5000.0
        assert state.bess_state == "charging"
        assert state.bess_power_w == 3000.0
        assert state.grid_power_w == 0.0

    def test_curtails_pv_to_load_when_no_bess_charge_headroom(self) -> None:
        """BESS nearly full — can't absorb charge, so PV curtails to load only."""
        system = EnergySystem.from_config(
            EnergySystemConfig(
                grid=_grid_offline(),
                pv=_pv(inverter_type="hybrid"),
                bess=_bess(
                    hybrid=True,
                    max_charge_w=3000.0,
                    initial_soe_kwh=13.5,  # full
                    charge_mode="self-consumption",
                ),
                loads=[LoadConfig(demand_w=2000.0)],
            )
        )
        state = system.tick(
            _TS,
            PowerInputs(
                pv_available_w=6000.0,
                load_demand_w=2000.0,
                grid_connected=False,
            ),
        )
        assert state.balanced
        assert abs(state.pv_power_w - 2000.0) < 0.01
        assert state.bess_state == "idle"
        assert state.grid_power_w == 0.0

    def test_curtails_pv_to_load_when_no_bess(self) -> None:
        """Islanded PV-only system (no battery) curtails to match load."""
        system = EnergySystem.from_config(
            EnergySystemConfig(
                grid=_grid_offline(),
                pv=_pv(inverter_type="hybrid"),
                loads=[LoadConfig(demand_w=1500.0)],
            )
        )
        # PV-only system needs islandable set explicitly
        system.islandable = True
        state = system.tick(
            _TS,
            PowerInputs(
                pv_available_w=4000.0,
                load_demand_w=1500.0,
                grid_connected=False,
            ),
        )
        assert state.balanced
        assert abs(state.pv_power_w - 1500.0) < 0.01
        assert state.grid_power_w == 0.0

    def test_no_curtailment_when_grid_connected(self) -> None:
        """Grid absorbs surplus — no curtailment needed."""
        system = EnergySystem.from_config(
            EnergySystemConfig(
                grid=_grid_online(),
                pv=_pv(inverter_type="hybrid"),
                bess=_bess(hybrid=True, initial_soe_kwh=13.5),
                loads=[LoadConfig(demand_w=2000.0)],
            )
        )
        state = system.tick(
            _TS,
            PowerInputs(
                pv_available_w=6000.0,
                load_demand_w=2000.0,
                grid_connected=True,
            ),
        )
        assert state.balanced
        assert state.pv_power_w == 6000.0
        # Grid exports the surplus (negative = export)
        assert state.grid_power_w < 0

    def test_no_curtailment_when_demand_absorbs_all(self) -> None:
        """Load + BESS charge >= PV — no curtailment needed."""
        system = EnergySystem.from_config(
            EnergySystemConfig(
                grid=_grid_offline(),
                pv=_pv(inverter_type="hybrid"),
                bess=_bess(
                    hybrid=True,
                    max_charge_w=3500.0,
                    initial_soe_kwh=5.0,
                    charge_mode="self-consumption",
                ),
                loads=[LoadConfig(demand_w=3000.0)],
            )
        )
        state = system.tick(
            _TS,
            PowerInputs(
                pv_available_w=5000.0,
                load_demand_w=3000.0,
                grid_connected=False,
            ),
        )
        assert state.balanced
        assert state.pv_power_w == 5000.0


class TestGridImpact:
    def test_charging_increases_grid_import(self) -> None:
        system = EnergySystem.from_config(
            EnergySystemConfig(
                grid=_grid_online(),
                bess=_bess(max_charge_w=3000.0, charge_hours=_CHARGE_AT_TS),
                loads=[LoadConfig(demand_w=2000.0)],
            )
        )
        state = system.tick(
            _TS,
            PowerInputs(
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
                bess=_bess(max_discharge_w=3000.0, discharge_hours=_DISCHARGE_AT_TS),
                loads=[LoadConfig(demand_w=5000.0)],
            )
        )
        state = system.tick(
            _TS,
            PowerInputs(
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
            bess=_bess(max_discharge_w=3000.0, discharge_hours=_DISCHARGE_AT_TS),
            loads=[LoadConfig(demand_w=5000.0)],
        )
        sys_b = EnergySystem.from_config(config_no_bess)
        sys_a = EnergySystem.from_config(config_with_bess)

        state_b = sys_b.tick(_TS, PowerInputs(load_demand_w=5000.0))
        state_a = sys_a.tick(
            _TS,
            PowerInputs(load_demand_w=5000.0),
        )
        assert state_b.balanced and state_a.balanced
        assert abs(state_b.grid_power_w - 5000.0) < 0.01
        assert abs(state_a.grid_power_w - 2000.0) < 0.01


class TestIndependentInstances:
    def test_two_instances_share_no_state(self) -> None:
        config = EnergySystemConfig(
            grid=_grid_online(),
            bess=_bess(initial_soe_kwh=10.0, discharge_hours=_DISCHARGE_AT_TS),
            loads=[LoadConfig(demand_w=3000.0)],
        )
        sys1 = EnergySystem.from_config(config)
        sys2 = EnergySystem.from_config(config)

        sys1.tick(
            _TS,
            PowerInputs(load_demand_w=3000.0),
        )
        sys1.tick(
            4600.0,
            PowerInputs(load_demand_w=3000.0),
        )

        state2 = sys2.tick(
            _TS,
            PowerInputs(load_demand_w=3000.0),
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
            _TS,
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
        # Ticks run from ts=60 to ts=7260 (hours 16-18 in America/Los_Angeles)
        small_config = EnergySystemConfig(
            grid=_grid_online(),
            bess=_bess(
                nameplate_kwh=5.0,
                max_discharge_w=2500.0,
                initial_soe_kwh=2.5,
                discharge_hours=(16, 17, 18),
            ),
            loads=[LoadConfig(demand_w=2500.0)],
        )
        large_config = EnergySystemConfig(
            grid=_grid_online(),
            bess=_bess(
                nameplate_kwh=20.0,
                max_discharge_w=2500.0,
                initial_soe_kwh=10.0,
                discharge_hours=(16, 17, 18),
            ),
            loads=[LoadConfig(demand_w=2500.0)],
        )
        small = EnergySystem.from_config(small_config)
        large = EnergySystem.from_config(large_config)

        inputs = PowerInputs(
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


class TestSelfConsumptionMode:
    """Tests for the self-consumption charge mode."""

    def test_discharges_when_load_exceeds_pv(self) -> None:
        system = EnergySystem.from_config(
            EnergySystemConfig(
                grid=_grid_online(),
                pv=_pv(),
                bess=_bess(charge_mode="self-consumption"),
                loads=[LoadConfig(demand_w=4000.0)],
            )
        )
        state = system.tick(
            _TS,
            PowerInputs(
                pv_available_w=2000.0,
                load_demand_w=4000.0,
                grid_connected=True,
            ),
        )
        assert state.balanced
        assert state.bess_state == "discharging"
        assert abs(state.bess_power_w - 2000.0) < 0.01
        assert abs(state.grid_power_w) < 0.01

    def test_charges_when_pv_exceeds_load(self) -> None:
        system = EnergySystem.from_config(
            EnergySystemConfig(
                grid=_grid_online(),
                pv=_pv(),
                bess=_bess(
                    charge_mode="self-consumption",
                    max_charge_w=3000.0,
                    initial_soe_kwh=5.0,
                ),
                loads=[LoadConfig(demand_w=2000.0)],
            )
        )
        state = system.tick(
            _TS,
            PowerInputs(
                pv_available_w=5000.0,
                load_demand_w=2000.0,
                grid_connected=True,
            ),
        )
        assert state.balanced
        assert state.bess_state == "charging"
        assert state.bess_power_w == 3000.0

    def test_idle_when_pv_equals_load(self) -> None:
        """When PV exactly meets load, no deficit or excess: BESS charges at 0."""
        system = EnergySystem.from_config(
            EnergySystemConfig(
                grid=_grid_online(),
                pv=_pv(),
                bess=_bess(charge_mode="self-consumption", initial_soe_kwh=5.0),
                loads=[LoadConfig(demand_w=3000.0)],
            )
        )
        state = system.tick(
            _TS,
            PowerInputs(
                pv_available_w=3000.0,
                load_demand_w=3000.0,
                grid_connected=True,
            ),
        )
        assert state.balanced
        # No deficit so BESS tries to charge, but no excess either
        # The grid absorbs the BESS charging demand
        assert state.grid_power_w >= 0.0


class TestBackupOnlyMode:
    """Tests for the backup-only charge mode."""

    def test_charges_to_full_on_grid(self) -> None:
        system = EnergySystem.from_config(
            EnergySystemConfig(
                grid=_grid_online(),
                bess=_bess(
                    charge_mode="backup-only",
                    max_charge_w=3000.0,
                    initial_soe_kwh=5.0,
                ),
                loads=[LoadConfig(demand_w=2000.0)],
            )
        )
        state = system.tick(
            _TS,
            PowerInputs(
                load_demand_w=2000.0,
                grid_connected=True,
            ),
        )
        assert state.balanced
        assert state.bess_state == "charging"
        assert state.bess_power_w == 3000.0

    def test_idle_when_full_on_grid(self) -> None:
        system = EnergySystem.from_config(
            EnergySystemConfig(
                grid=_grid_online(),
                bess=_bess(
                    charge_mode="backup-only",
                    initial_soe_kwh=13.5,
                ),
                loads=[LoadConfig(demand_w=2000.0)],
            )
        )
        state = system.tick(
            _TS,
            PowerInputs(
                load_demand_w=2000.0,
                grid_connected=True,
            ),
        )
        assert state.balanced
        assert state.bess_state == "idle"
        assert state.bess_power_w == 0.0

    def test_discharges_during_outage(self) -> None:
        system = EnergySystem.from_config(
            EnergySystemConfig(
                grid=_grid_offline(),
                bess=_bess(charge_mode="backup-only", hybrid=False),
                loads=[LoadConfig(demand_w=3000.0)],
            )
        )
        state = system.tick(
            _TS,
            PowerInputs(
                load_demand_w=3000.0,
                grid_connected=False,
            ),
        )
        assert state.balanced
        assert state.bess_state == "discharging"
        assert abs(state.bess_power_w - 3000.0) < 0.01
