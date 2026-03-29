"""Tests for ToU rate resolution from URDB schedule matrices."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from span_panel_simulator.rates.resolver import resolve_rate


def _epoch(year: int, month: int, day: int, hour: int, tz: str) -> int:
    """Return epoch seconds for a local datetime."""
    dt = datetime(year, month, day, hour, tzinfo=ZoneInfo(tz))
    return int(dt.timestamp())


# Minimal URDB record for testing: 2 periods (off-peak=0, peak=1)
# Peak: weekdays 16-21, all months
# Off-peak: everything else
_WEEKDAY_SCHEDULE = [[0] * 24 for _ in range(12)]
for _m in range(12):
    for _h in range(16, 21):
        _WEEKDAY_SCHEDULE[_m][_h] = 1

_WEEKEND_SCHEDULE = [[0] * 24 for _ in range(12)]

_RECORD: dict = {
    "energyratestructure": [
        [{"rate": 0.10}],  # period 0: off-peak $0.10/kWh
        [{"rate": 0.30}],  # period 1: peak $0.30/kWh
    ],
    "energyweekdayschedule": _WEEKDAY_SCHEDULE,
    "energyweekendschedule": _WEEKEND_SCHEDULE,
    "sell": [
        [{"rate": 0.04}],  # period 0: off-peak export $0.04/kWh
        [{"rate": 0.08}],  # period 1: peak export $0.08/kWh
    ],
}

TZ = "America/Los_Angeles"


class TestResolveRate:
    """Rate resolution against URDB schedule matrices."""

    def test_weekday_peak_hour(self) -> None:
        # Wednesday 2026-07-15 at 17:00 local -> peak (period 1)
        ts = _epoch(2026, 7, 15, 17, TZ)
        imp, exp = resolve_rate(ts, TZ, _RECORD)
        assert imp == 0.30
        assert exp == 0.08

    def test_weekday_offpeak_hour(self) -> None:
        # Wednesday 2026-07-15 at 10:00 local -> off-peak (period 0)
        ts = _epoch(2026, 7, 15, 10, TZ)
        imp, exp = resolve_rate(ts, TZ, _RECORD)
        assert imp == 0.10
        assert exp == 0.04

    def test_weekend_always_offpeak(self) -> None:
        # Saturday 2026-07-18 at 17:00 local -> off-peak (weekend schedule)
        ts = _epoch(2026, 7, 18, 17, TZ)
        imp, exp = resolve_rate(ts, TZ, _RECORD)
        assert imp == 0.10
        assert exp == 0.04

    def test_no_sell_field_returns_zero_export(self) -> None:
        record_no_sell = {
            "energyratestructure": [[{"rate": 0.15}]],
            "energyweekdayschedule": [[0] * 24 for _ in range(12)],
            "energyweekendschedule": [[0] * 24 for _ in range(12)],
        }
        ts = _epoch(2026, 1, 5, 12, TZ)
        imp, exp = resolve_rate(ts, TZ, record_no_sell)
        assert imp == 0.15
        assert exp == 0.0

    def test_winter_vs_summer_month(self) -> None:
        ts_jan = _epoch(2026, 1, 7, 17, TZ)  # Wednesday
        ts_jul = _epoch(2026, 7, 15, 17, TZ)  # Wednesday
        imp_jan, _ = resolve_rate(ts_jan, TZ, _RECORD)
        imp_jul, _ = resolve_rate(ts_jul, TZ, _RECORD)
        assert imp_jan == 0.30
        assert imp_jul == 0.30

    def test_boundary_hour_15_is_offpeak(self) -> None:
        ts = _epoch(2026, 7, 15, 15, TZ)
        imp, _ = resolve_rate(ts, TZ, _RECORD)
        assert imp == 0.10

    def test_boundary_hour_16_is_peak(self) -> None:
        ts = _epoch(2026, 7, 15, 16, TZ)
        imp, _ = resolve_rate(ts, TZ, _RECORD)
        assert imp == 0.30

    def test_boundary_hour_21_is_offpeak(self) -> None:
        ts = _epoch(2026, 7, 15, 21, TZ)
        imp, _ = resolve_rate(ts, TZ, _RECORD)
        assert imp == 0.10
