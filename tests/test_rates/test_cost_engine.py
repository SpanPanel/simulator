"""Tests for the cost engine — applies rates to power time-series."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from span_panel_simulator.rates.cost_engine import compute_costs

TZ = "America/Los_Angeles"


def _epoch(year: int, month: int, day: int, hour: int) -> int:
    dt = datetime(year, month, day, hour, tzinfo=ZoneInfo(TZ))
    return int(dt.timestamp())


# Flat rate: single period, $0.20 import, $0.05 export, all hours
_FLAT_RECORD: dict = {
    "energyratestructure": [[{"rate": 0.20}]],
    "energyweekdayschedule": [[0] * 24 for _ in range(12)],
    "energyweekendschedule": [[0] * 24 for _ in range(12)],
    "sell": [[{"rate": 0.05}]],
    "fixedmonthlycharge": 10.0,
}


class TestComputeCosts:
    """Cost engine applies rates to hourly power arrays."""

    def test_pure_import(self) -> None:
        ts = [_epoch(2026, 3, 15, h) for h in range(10, 13)]
        power = [1.0, 1.0, 1.0]
        result = compute_costs(ts, power, _FLAT_RECORD, TZ)
        assert result.import_cost == pytest.approx(0.60)
        assert result.export_credit == pytest.approx(0.0)

    def test_pure_export(self) -> None:
        ts = [_epoch(2026, 3, 15, h) for h in range(10, 12)]
        power = [-2.0, -2.0]
        result = compute_costs(ts, power, _FLAT_RECORD, TZ)
        assert result.import_cost == pytest.approx(0.0)
        assert result.export_credit == pytest.approx(0.20)

    def test_mixed_import_export(self) -> None:
        ts = [_epoch(2026, 3, 15, 10), _epoch(2026, 3, 15, 11)]
        power = [1.0, -1.0]
        result = compute_costs(ts, power, _FLAT_RECORD, TZ)
        assert result.import_cost == pytest.approx(0.20)
        assert result.export_credit == pytest.approx(0.05)

    def test_net_cost_includes_fixed(self) -> None:
        ts = [_epoch(2026, 3, 15, 10)]
        power = [1.0]
        result = compute_costs(ts, power, _FLAT_RECORD, TZ)
        assert result.fixed_charges == pytest.approx(10.0)
        assert result.net_cost == pytest.approx(0.20 - 0.0 + 10.0)

    def test_multi_month_fixed_charges(self) -> None:
        ts = [_epoch(2026, 1, 31, 23), _epoch(2026, 2, 1, 0)]
        power = [0.0, 0.0]
        result = compute_costs(ts, power, _FLAT_RECORD, TZ)
        assert result.fixed_charges == pytest.approx(20.0)

    def test_no_fixed_charge_field(self) -> None:
        record_no_fixed: dict = {
            "energyratestructure": [[{"rate": 0.10}]],
            "energyweekdayschedule": [[0] * 24 for _ in range(12)],
            "energyweekendschedule": [[0] * 24 for _ in range(12)],
        }
        ts = [_epoch(2026, 3, 15, 10)]
        power = [1.0]
        result = compute_costs(ts, power, record_no_fixed, TZ)
        assert result.fixed_charges == pytest.approx(0.0)

    def test_zero_power(self) -> None:
        ts = [_epoch(2026, 3, 15, h) for h in range(10, 14)]
        power = [0.0, 0.0, 0.0, 0.0]
        result = compute_costs(ts, power, _FLAT_RECORD, TZ)
        assert result.import_cost == pytest.approx(0.0)
        assert result.export_credit == pytest.approx(0.0)

    def test_empty_arrays(self) -> None:
        result = compute_costs([], [], _FLAT_RECORD, TZ)
        assert result.import_cost == pytest.approx(0.0)
        assert result.export_credit == pytest.approx(0.0)
        assert result.fixed_charges == pytest.approx(0.0)
        assert result.net_cost == pytest.approx(0.0)

    def test_flat_demand_included_in_fixed(self) -> None:
        record_with_demand: dict = {
            "energyratestructure": [[{"rate": 0.10}]],
            "energyweekdayschedule": [[0] * 24 for _ in range(12)],
            "energyweekendschedule": [[0] * 24 for _ in range(12)],
            "fixedmonthlycharge": 5.0,
            "flatdemandstructure": [[{"rate": 8.0}]],
            "flatdemandmonths": [[0] * 12],
        }
        ts = [_epoch(2026, 3, 15, 10)]
        power = [0.0]
        result = compute_costs(ts, power, record_with_demand, TZ)
        assert result.fixed_charges == pytest.approx(13.0)
