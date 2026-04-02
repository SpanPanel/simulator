"""Unit tests for the rate-aware TOU dispatch module."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from span_panel_simulator.energy.tou import resolve_tou_dispatch

_TZ = ZoneInfo("America/Los_Angeles")


def _ts(year: int, month: int, day: int, hour: int) -> float:
    """Build a UNIX timestamp for a specific local time in America/Los_Angeles."""
    dt = datetime(year, month, day, hour, tzinfo=_TZ)
    return dt.timestamp()


# -- Minimal PG&E E-ELEC-like rate fixture with seasonal variation ------
#
# Summer (Jun-Sep):
#   Off-peak  P2 = $0.397  hours 00-14
#   Part-peak P1 = $0.454  hours 15, 21-23
#   Peak      P0 = $0.616  hours 16-20
#
# Winter (Oct-May):
#   Off-peak  P5 = $0.348  hours 00-14
#   Part-peak P4 = $0.362  hours 15, 21-23
#   Peak      P3 = $0.384  hours 16-20

_SUMMER_ROW = [2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 1, 0, 0, 0, 0, 0, 1, 1, 1]
_WINTER_ROW = [5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 4, 3, 3, 3, 3, 3, 4, 4, 4]

_RATE_STRUCTURE = [
    [{"rate": 0.61578, "unit": "kWh"}],  # P0 - summer peak
    [{"rate": 0.45390, "unit": "kWh"}],  # P1 - summer partial-peak
    [{"rate": 0.39722, "unit": "kWh"}],  # P2 - summer off-peak
    [{"rate": 0.38426, "unit": "kWh"}],  # P3 - winter peak
    [{"rate": 0.36217, "unit": "kWh"}],  # P4 - winter partial-peak
    [{"rate": 0.34831, "unit": "kWh"}],  # P5 - winter off-peak
]


def _build_rate_record(
    weekday_schedule: list[list[int]] | None = None,
    weekend_schedule: list[list[int]] | None = None,
) -> dict:
    """Build a URDB-like rate record. Defaults to PG&E E-ELEC structure."""
    if weekday_schedule is None:
        # 12 months: Jan-May winter, Jun-Sep summer, Oct-Dec winter
        weekday_schedule = [
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
    if weekend_schedule is None:
        weekend_schedule = weekday_schedule

    return {
        "energyratestructure": _RATE_STRUCTURE,
        "energyweekdayschedule": weekday_schedule,
        "energyweekendschedule": weekend_schedule,
    }


_DEFAULT_RECORD = _build_rate_record()


class TestOffPeakDispatch:
    """Off-peak hours should charge."""

    def test_summer_midnight_charges_from_grid(self) -> None:
        ts = _ts(2026, 7, 15, 0)  # July, midnight, Wednesday
        result = resolve_tou_dispatch(
            ts,
            _TZ,
            _DEFAULT_RECORD,
            soe_pct=50.0,
            backup_reserve_pct=20.0,
            max_charge_w=3500.0,
            max_discharge_w=3500.0,
            pv_excess_w=0.0,
            load_deficit_w=0.0,
        )
        assert result.state == "charging"
        assert result.reason == "offpeak_grid_charge"
        assert result.requested_power_w == 3500.0

    def test_offpeak_with_pv_excess_charges_from_solar(self) -> None:
        ts = _ts(2026, 7, 15, 10)  # July, 10 AM (off-peak, solar available)
        result = resolve_tou_dispatch(
            ts,
            _TZ,
            _DEFAULT_RECORD,
            soe_pct=50.0,
            backup_reserve_pct=20.0,
            max_charge_w=3500.0,
            max_discharge_w=3500.0,
            pv_excess_w=2000.0,
            load_deficit_w=0.0,
        )
        assert result.state == "charging"
        assert result.reason == "offpeak_solar_charge"
        assert result.requested_power_w == 2000.0

    def test_offpeak_full_battery_idles(self) -> None:
        ts = _ts(2026, 7, 15, 2)
        result = resolve_tou_dispatch(
            ts,
            _TZ,
            _DEFAULT_RECORD,
            soe_pct=100.0,
            backup_reserve_pct=20.0,
            max_charge_w=3500.0,
            max_discharge_w=3500.0,
            pv_excess_w=0.0,
            load_deficit_w=0.0,
        )
        assert result.state == "idle"
        assert result.reason == "offpeak_full"

    def test_winter_offpeak_charges(self) -> None:
        ts = _ts(2026, 1, 15, 2)  # January, 2 AM
        result = resolve_tou_dispatch(
            ts,
            _TZ,
            _DEFAULT_RECORD,
            soe_pct=40.0,
            backup_reserve_pct=20.0,
            max_charge_w=3500.0,
            max_discharge_w=3500.0,
            pv_excess_w=0.0,
            load_deficit_w=0.0,
        )
        assert result.state == "charging"
        assert result.reason == "offpeak_grid_charge"


class TestPeakDispatch:
    """Peak hours should discharge to cover load."""

    def test_summer_peak_discharges(self) -> None:
        ts = _ts(2026, 7, 15, 17)  # July, 5 PM (peak)
        result = resolve_tou_dispatch(
            ts,
            _TZ,
            _DEFAULT_RECORD,
            soe_pct=80.0,
            backup_reserve_pct=20.0,
            max_charge_w=3500.0,
            max_discharge_w=3500.0,
            pv_excess_w=0.0,
            load_deficit_w=2000.0,
        )
        assert result.state == "discharging"
        assert result.reason == "peak_discharge"

    def test_peak_no_deficit_idles(self) -> None:
        ts = _ts(2026, 7, 15, 17)
        result = resolve_tou_dispatch(
            ts,
            _TZ,
            _DEFAULT_RECORD,
            soe_pct=80.0,
            backup_reserve_pct=20.0,
            max_charge_w=3500.0,
            max_discharge_w=3500.0,
            pv_excess_w=500.0,
            load_deficit_w=0.0,
        )
        assert result.state == "idle"
        assert result.reason == "peak_no_deficit"

    def test_peak_at_reserve_holds(self) -> None:
        ts = _ts(2026, 7, 15, 18)
        result = resolve_tou_dispatch(
            ts,
            _TZ,
            _DEFAULT_RECORD,
            soe_pct=20.0,
            backup_reserve_pct=20.0,
            max_charge_w=3500.0,
            max_discharge_w=3500.0,
            pv_excess_w=0.0,
            load_deficit_w=3000.0,
        )
        assert result.state == "idle"
        assert result.reason == "peak_reserve_hold"

    def test_winter_peak_discharges(self) -> None:
        ts = _ts(2026, 1, 15, 17)  # January, 5 PM (winter peak)
        result = resolve_tou_dispatch(
            ts,
            _TZ,
            _DEFAULT_RECORD,
            soe_pct=60.0,
            backup_reserve_pct=20.0,
            max_charge_w=3500.0,
            max_discharge_w=3500.0,
            pv_excess_w=0.0,
            load_deficit_w=1500.0,
        )
        assert result.state == "discharging"
        assert result.reason == "peak_discharge"


class TestPartialPeakDispatch:
    """Partial-peak hours use self-consumption (no grid interaction)."""

    def test_partial_peak_with_deficit_discharges(self) -> None:
        ts = _ts(2026, 7, 15, 15)  # July, 3 PM (partial-peak)
        result = resolve_tou_dispatch(
            ts,
            _TZ,
            _DEFAULT_RECORD,
            soe_pct=70.0,
            backup_reserve_pct=20.0,
            max_charge_w=3500.0,
            max_discharge_w=3500.0,
            pv_excess_w=0.0,
            load_deficit_w=1000.0,
        )
        assert result.state == "discharging"
        assert result.reason == "selfcon_discharge"

    def test_partial_peak_with_pv_excess_charges_solar(self) -> None:
        ts = _ts(2026, 7, 15, 15)
        result = resolve_tou_dispatch(
            ts,
            _TZ,
            _DEFAULT_RECORD,
            soe_pct=70.0,
            backup_reserve_pct=20.0,
            max_charge_w=3500.0,
            max_discharge_w=3500.0,
            pv_excess_w=800.0,
            load_deficit_w=0.0,
        )
        assert result.state == "charging"
        assert result.reason == "selfcon_solar_charge"
        assert result.requested_power_w == 800.0

    def test_partial_peak_balanced_idles(self) -> None:
        ts = _ts(2026, 7, 15, 22)  # July, 10 PM (partial-peak)
        result = resolve_tou_dispatch(
            ts,
            _TZ,
            _DEFAULT_RECORD,
            soe_pct=50.0,
            backup_reserve_pct=20.0,
            max_charge_w=3500.0,
            max_discharge_w=3500.0,
            pv_excess_w=0.0,
            load_deficit_w=0.0,
        )
        assert result.state == "idle"
        assert result.reason == "selfcon_idle"


class TestSeasonalVariation:
    """Same hour produces different dispatch in different months."""

    def test_hour_17_summer_is_peak(self) -> None:
        ts = _ts(2026, 7, 15, 17)
        result = resolve_tou_dispatch(
            ts,
            _TZ,
            _DEFAULT_RECORD,
            soe_pct=60.0,
            backup_reserve_pct=20.0,
            max_charge_w=3500.0,
            max_discharge_w=3500.0,
            pv_excess_w=0.0,
            load_deficit_w=2000.0,
        )
        assert result.reason == "peak_discharge"

    def test_hour_17_winter_is_also_peak(self) -> None:
        """Winter period 3 ($0.384) is the max for winter — still peak."""
        ts = _ts(2026, 1, 15, 17)
        result = resolve_tou_dispatch(
            ts,
            _TZ,
            _DEFAULT_RECORD,
            soe_pct=60.0,
            backup_reserve_pct=20.0,
            max_charge_w=3500.0,
            max_discharge_w=3500.0,
            pv_excess_w=0.0,
            load_deficit_w=2000.0,
        )
        assert result.reason == "peak_discharge"


class TestWeekdayWeekend:
    """Weekday vs weekend uses the correct schedule matrix."""

    def test_different_schedule_on_weekend(self) -> None:
        """Use a record where weekend has flat rates (all same period)."""
        flat_weekend_row = [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]
        record = _build_rate_record(
            weekend_schedule=[flat_weekend_row] * 12,
        )
        # Saturday, July 18 2026 = Saturday
        ts = _ts(2026, 7, 18, 17)  # Would be peak on weekday
        result = resolve_tou_dispatch(
            ts,
            _TZ,
            record,
            soe_pct=60.0,
            backup_reserve_pct=20.0,
            max_charge_w=3500.0,
            max_discharge_w=3500.0,
            pv_excess_w=0.0,
            load_deficit_w=2000.0,
        )
        # Flat rate → self-consumption fallback
        assert result.reason == "selfcon_discharge"

    def test_weekday_uses_weekday_schedule(self) -> None:
        # Wednesday, July 15 2026
        ts = _ts(2026, 7, 15, 17)
        result = resolve_tou_dispatch(
            ts,
            _TZ,
            _DEFAULT_RECORD,
            soe_pct=60.0,
            backup_reserve_pct=20.0,
            max_charge_w=3500.0,
            max_discharge_w=3500.0,
            pv_excess_w=0.0,
            load_deficit_w=2000.0,
        )
        assert result.reason == "peak_discharge"


class TestFlatRate:
    """Flat rate (single period) falls back to self-consumption."""

    def test_flat_rate_with_deficit_discharges(self) -> None:
        flat_row = [0] * 24
        record = {
            "energyratestructure": [[{"rate": 0.30, "unit": "kWh"}]],
            "energyweekdayschedule": [flat_row] * 12,
            "energyweekendschedule": [flat_row] * 12,
        }
        ts = _ts(2026, 7, 15, 17)
        result = resolve_tou_dispatch(
            ts,
            _TZ,
            record,
            soe_pct=60.0,
            backup_reserve_pct=20.0,
            max_charge_w=3500.0,
            max_discharge_w=3500.0,
            pv_excess_w=0.0,
            load_deficit_w=2000.0,
        )
        assert result.state == "discharging"
        assert result.reason == "selfcon_discharge"


class TestEdgeCases:
    """Empty/missing data and boundary conditions."""

    def test_empty_rate_record_idles(self) -> None:
        result = resolve_tou_dispatch(
            _ts(2026, 7, 15, 12),
            _TZ,
            {},
            soe_pct=50.0,
            backup_reserve_pct=20.0,
            max_charge_w=3500.0,
            max_discharge_w=3500.0,
            pv_excess_w=0.0,
            load_deficit_w=0.0,
        )
        assert result.state == "idle"
        assert result.reason == "no_rate_data"

    def test_missing_schedule_idles(self) -> None:
        record = {"energyratestructure": _RATE_STRUCTURE}
        result = resolve_tou_dispatch(
            _ts(2026, 7, 15, 12),
            _TZ,
            record,
            soe_pct=50.0,
            backup_reserve_pct=20.0,
            max_charge_w=3500.0,
            max_discharge_w=3500.0,
            pv_excess_w=0.0,
            load_deficit_w=0.0,
        )
        assert result.state == "idle"
        assert result.reason == "no_rate_data"

    def test_pv_excess_clamped_to_max_charge(self) -> None:
        ts = _ts(2026, 7, 15, 10)  # off-peak
        result = resolve_tou_dispatch(
            ts,
            _TZ,
            _DEFAULT_RECORD,
            soe_pct=50.0,
            backup_reserve_pct=20.0,
            max_charge_w=3500.0,
            max_discharge_w=3500.0,
            pv_excess_w=5000.0,
            load_deficit_w=0.0,
        )
        assert result.state == "charging"
        assert result.requested_power_w == 3500.0
