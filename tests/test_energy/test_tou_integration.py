"""Integration tests: EnergySystem + TOU rate-aware dispatch."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from span_panel_simulator.energy.system import EnergySystem
from span_panel_simulator.energy.types import (
    BESSConfig,
    EnergySystemConfig,
    GridConfig,
    LoadConfig,
    PowerInputs,
    PVConfig,
)

_TZ = ZoneInfo("America/Los_Angeles")
_TZ_STR = "America/Los_Angeles"


def _ts(year: int, month: int, day: int, hour: int) -> float:
    return datetime(year, month, day, hour, tzinfo=_TZ).timestamp()


# -- PG&E E-ELEC-like rate fixture (same as unit tests) ------------------

_SUMMER_ROW = [2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 1, 0, 0, 0, 0, 0, 1, 1, 1]
_WINTER_ROW = [5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 4, 3, 3, 3, 3, 3, 4, 4, 4]

_RATE_STRUCTURE: list[list[dict[str, Any]]] = [
    [{"rate": 0.61578, "unit": "kWh"}],  # P0 - summer peak
    [{"rate": 0.45390, "unit": "kWh"}],  # P1 - summer partial-peak
    [{"rate": 0.39722, "unit": "kWh"}],  # P2 - summer off-peak
    [{"rate": 0.38426, "unit": "kWh"}],  # P3 - winter peak
    [{"rate": 0.36217, "unit": "kWh"}],  # P4 - winter partial-peak
    [{"rate": 0.34831, "unit": "kWh"}],  # P5 - winter off-peak
]

_WEEKDAY_SCHEDULE = [
    _WINTER_ROW,  # Jan
    _WINTER_ROW,  # Feb
    _WINTER_ROW,  # Mar
    _WINTER_ROW,  # Apr
    _WINTER_ROW,  # May
    _SUMMER_ROW,  # Jun
    _SUMMER_ROW,  # Jul
    _SUMMER_ROW,  # Aug
    _SUMMER_ROW,  # Sep
    _WINTER_ROW,  # Oct
    _WINTER_ROW,  # Nov
    _WINTER_ROW,  # Dec
]

_DEFAULT_RECORD: dict[str, Any] = {
    "energyratestructure": _RATE_STRUCTURE,
    "energyweekdayschedule": _WEEKDAY_SCHEDULE,
    "energyweekendschedule": _WEEKDAY_SCHEDULE,
}


def _build_system(
    rate_record: dict[str, Any] | None = None,
    charge_hours: tuple[int, ...] = (),
    discharge_hours: tuple[int, ...] = (),
    charge_mode: str = "custom",
    initial_soe_kwh: float = 8.0,
) -> EnergySystem:
    bess = BESSConfig(
        nameplate_kwh=13.5,
        max_charge_w=3500.0,
        max_discharge_w=3500.0,
        backup_reserve_pct=20.0,
        panel_timezone=_TZ_STR,
        charge_mode=charge_mode,
        charge_hours=charge_hours,
        discharge_hours=discharge_hours,
        rate_record=rate_record,
        initial_soe_kwh=initial_soe_kwh,
    )
    config = EnergySystemConfig(
        grid=GridConfig(connected=True),
        pv=PVConfig(nameplate_w=6000.0),
        bess=bess,
        loads=[LoadConfig()],
    )
    return EnergySystem.from_config(config)


# -- Task 6 Step 1: Seasonal dispatch changes ----------------------------


class TestSeasonalDispatch:
    """Verify dispatch adapts to summer vs winter rate schedules."""

    def test_summer_peak_discharges(self) -> None:
        sys = _build_system(rate_record=_DEFAULT_RECORD)
        ts = _ts(2026, 7, 15, 17)  # July 5 PM — summer peak
        inputs = PowerInputs(
            pv_available_w=1000.0,
            load_demand_w=4000.0,
            grid_connected=True,
        )
        sys.tick(ts, inputs)
        assert sys.bess is not None
        assert sys.bess.scheduled_state == "discharging"

    def test_summer_midnight_charges(self) -> None:
        sys = _build_system(rate_record=_DEFAULT_RECORD)
        ts = _ts(2026, 7, 15, 0)  # July midnight — summer off-peak
        inputs = PowerInputs(
            pv_available_w=0.0,
            load_demand_w=500.0,
            grid_connected=True,
        )
        sys.tick(ts, inputs)
        assert sys.bess is not None
        assert sys.bess.scheduled_state == "charging"

    def test_winter_peak_discharges(self) -> None:
        sys = _build_system(rate_record=_DEFAULT_RECORD)
        ts = _ts(2026, 1, 15, 17)  # January 5 PM — winter peak
        inputs = PowerInputs(
            pv_available_w=0.0,
            load_demand_w=3000.0,
            grid_connected=True,
        )
        sys.tick(ts, inputs)
        assert sys.bess is not None
        assert sys.bess.scheduled_state == "discharging"

    def test_winter_midnight_charges(self) -> None:
        sys = _build_system(rate_record=_DEFAULT_RECORD)
        ts = _ts(2026, 1, 15, 0)  # January midnight — winter off-peak
        inputs = PowerInputs(
            pv_available_w=0.0,
            load_demand_w=500.0,
            grid_connected=True,
        )
        sys.tick(ts, inputs)
        assert sys.bess is not None
        assert sys.bess.scheduled_state == "charging"


# -- Task 6 Step 2: Weekday vs weekend -----------------------------------


class TestWeekdayWeekendDispatch:
    """Verify dispatch uses the correct schedule matrix by day-type."""

    def test_weekday_peak_discharges(self) -> None:
        """Wednesday July 15, 2026 at 5 PM — weekday peak."""
        sys = _build_system(rate_record=_DEFAULT_RECORD)
        ts = _ts(2026, 7, 15, 17)
        inputs = PowerInputs(
            pv_available_w=0.0,
            load_demand_w=3000.0,
            grid_connected=True,
        )
        sys.tick(ts, inputs)
        assert sys.bess is not None
        assert sys.bess.scheduled_state == "discharging"

    def test_weekend_with_different_schedule(self) -> None:
        """Weekend schedule is flat → self-consumption fallback."""
        flat_row = [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]
        record: dict[str, Any] = {
            "energyratestructure": _RATE_STRUCTURE,
            "energyweekdayschedule": _WEEKDAY_SCHEDULE,
            "energyweekendschedule": [flat_row] * 12,
        }
        sys = _build_system(rate_record=record)
        # Saturday July 18, 2026
        ts = _ts(2026, 7, 18, 17)
        inputs = PowerInputs(
            pv_available_w=0.0,
            load_demand_w=3000.0,
            grid_connected=True,
        )
        sys.tick(ts, inputs)
        assert sys.bess is not None
        # Flat rate on weekend → self-consumption discharge (not peak_discharge)
        assert sys.bess.scheduled_state == "discharging"


# -- Task 6 Step 3: Partial-peak self-consumption ------------------------


class TestPartialPeakSelfConsumption:
    """Partial-peak hours should provide self-consumption, not idle."""

    def test_partial_peak_with_deficit_discharges(self) -> None:
        sys = _build_system(rate_record=_DEFAULT_RECORD)
        ts = _ts(2026, 7, 15, 15)  # 3 PM summer partial-peak
        inputs = PowerInputs(
            pv_available_w=500.0,
            load_demand_w=3000.0,
            grid_connected=True,
        )
        sys.tick(ts, inputs)
        assert sys.bess is not None
        assert sys.bess.scheduled_state == "discharging"

    def test_partial_peak_with_pv_excess_charges_solar_only(self) -> None:
        sys = _build_system(rate_record=_DEFAULT_RECORD)
        ts = _ts(2026, 7, 15, 15)  # 3 PM partial-peak
        inputs = PowerInputs(
            pv_available_w=5000.0,
            load_demand_w=2000.0,
            grid_connected=True,
        )
        sys.tick(ts, inputs)
        assert sys.bess is not None
        assert sys.bess.scheduled_state == "charging"
        # Should charge from PV excess (3000 W), clamped to max_charge
        assert sys.bess.requested_power_w <= 3500.0

    def test_partial_peak_balanced_idles(self) -> None:
        sys = _build_system(rate_record=_DEFAULT_RECORD)
        ts = _ts(2026, 7, 15, 22)  # 10 PM partial-peak
        inputs = PowerInputs(
            pv_available_w=0.0,
            load_demand_w=0.0,
            grid_connected=True,
        )
        sys.tick(ts, inputs)
        assert sys.bess is not None
        assert sys.bess.scheduled_state == "idle"


# -- Task 6 Step 4: Flat-rate fallback -----------------------------------


class TestFlatRateFallback:
    """Flat-rate plan should fall back to self-consumption."""

    def test_flat_rate_self_consumption(self) -> None:
        flat_row = [0] * 24
        record: dict[str, Any] = {
            "energyratestructure": [[{"rate": 0.30, "unit": "kWh"}]],
            "energyweekdayschedule": [flat_row] * 12,
            "energyweekendschedule": [flat_row] * 12,
        }
        sys = _build_system(rate_record=record)
        ts = _ts(2026, 7, 15, 17)
        inputs = PowerInputs(
            pv_available_w=0.0,
            load_demand_w=3000.0,
            grid_connected=True,
        )
        sys.tick(ts, inputs)
        assert sys.bess is not None
        # Flat rate: self-consumption discharge
        assert sys.bess.scheduled_state == "discharging"


# -- Task 6 Step 5: Backward compat — no rate record, static hours -------


class TestStaticHoursFallback:
    """With rate_record=None, the old static-schedule behaviour is preserved."""

    def test_static_discharge_hours(self) -> None:
        sys = _build_system(
            rate_record=None,
            charge_hours=(0, 1, 2, 3),
            discharge_hours=(16, 17, 18, 19),
        )
        # Hour 17 is in discharge_hours
        ts = _ts(2026, 7, 15, 17)
        inputs = PowerInputs(
            pv_available_w=0.0,
            load_demand_w=3000.0,
            grid_connected=True,
        )
        sys.tick(ts, inputs)
        assert sys.bess is not None
        assert sys.bess.scheduled_state == "discharging"

    def test_static_charge_hours(self) -> None:
        sys = _build_system(
            rate_record=None,
            charge_hours=(0, 1, 2, 3),
            discharge_hours=(16, 17, 18, 19),
        )
        # Hour 2 is in charge_hours
        ts = _ts(2026, 7, 15, 2)
        inputs = PowerInputs(
            pv_available_w=0.0,
            load_demand_w=500.0,
            grid_connected=True,
        )
        sys.tick(ts, inputs)
        assert sys.bess is not None
        assert sys.bess.scheduled_state == "charging"

    def test_static_idle_hours(self) -> None:
        sys = _build_system(
            rate_record=None,
            charge_hours=(0, 1, 2, 3),
            discharge_hours=(16, 17, 18, 19),
        )
        # Hour 10 is neither charge nor discharge
        ts = _ts(2026, 7, 15, 10)
        inputs = PowerInputs(
            pv_available_w=0.0,
            load_demand_w=500.0,
            grid_connected=True,
        )
        sys.tick(ts, inputs)
        assert sys.bess is not None
        assert sys.bess.scheduled_state == "idle"
