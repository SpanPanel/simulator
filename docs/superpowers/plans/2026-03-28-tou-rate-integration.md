# ToU Rate Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Integrate OpenEI URDB rate plans into the simulator so users can select current and proposed ToU rates, and the modeling view displays cost alongside energy for Before/After comparisons.

**Architecture:** A new `rates/` package handles all rate concerns: API client (`openei.py`), rate lookup (`resolver.py`), and cost calculation (`cost_engine.py`). Rate data is cached simulator-wide in `data/rates_cache.yaml`. The engine's `compute_modeling_data` delegates cost math to the cost engine. The modeling view gains a rate selection UI above the charts with an OpenEI configuration dialog.

**Tech Stack:** Python 3.14, aiohttp (existing), dataclasses, pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-03-28-tou-rate-integration-design.md`

**AGENTS.md Rules:** The `rates/` package is a peer to `energy/` — it consumes the power arrays produced by `compute_modeling_data` but does not participate in energy dispatch. The engine passes power arrays to the cost engine; it never resolves rates or computes costs inline.

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `src/span_panel_simulator/rates/__init__.py` | Create | Public API re-exports |
| `src/span_panel_simulator/rates/types.py` | Create | URDBRecord, RateCacheEntry, AttributionMeta, CostLedger, OpenEIConfig, UtilitySummary, RatePlanSummary |
| `src/span_panel_simulator/rates/resolver.py` | Create | `resolve_rate(timestamp, tz, record)` -> (import, export) rate per kWh |
| `src/span_panel_simulator/rates/cost_engine.py` | Create | `compute_costs(timestamps, power_kw, record, tz)` -> CostLedger |
| `src/span_panel_simulator/rates/openei.py` | Create | URDB API client: fetch utilities, plans, detail |
| `src/span_panel_simulator/rates/cache.py` | Create | Rate cache manager: load/save/get/set rates_cache.yaml + openei config |
| `tests/test_rates/__init__.py` | Create | Test package init |
| `tests/test_rates/test_resolver.py` | Create | Rate resolution unit tests |
| `tests/test_rates/test_cost_engine.py` | Create | Cost calculation unit tests |
| `tests/test_rates/test_openei.py` | Create | API client tests with mocked HTTP |
| `tests/test_rates/test_cache.py` | Create | Cache read/write tests |
| `src/span_panel_simulator/dashboard/routes.py` | Modify | Add rate API endpoints |
| `src/span_panel_simulator/dashboard/templates/partials/modeling_view.html` | Modify | Rate selection UI, cost display, OpenEI dialog |
| `src/span_panel_simulator/engine.py` | Modify | Wire cost engine into `compute_modeling_data` response |

---

## Phase 1: Rate Types and Resolution

### Task 1: Types Module

**Files:**
- Create: `src/span_panel_simulator/rates/__init__.py`
- Create: `src/span_panel_simulator/rates/types.py`
- Create: `tests/test_rates/__init__.py`

- [ ] **Step 1: Create package structure**

```bash
mkdir -p src/span_panel_simulator/rates tests/test_rates
```

- [ ] **Step 2: Write types module**

Create `src/span_panel_simulator/rates/__init__.py`:

```python
"""ToU rate integration — OpenEI URDB rate plans and cost calculation."""
```

Create `tests/test_rates/__init__.py`:

```python
```

Create `src/span_panel_simulator/rates/types.py`:

```python
"""Core types for ToU rate integration.

URDBRecord mirrors the OpenEI URDB API response schema.  Records are
stored verbatim — never modified after fetch.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypedDict


# -- URDB schema types (read-only mirrors of API response) ---------------

class URDBRateTier(TypedDict, total=False):
    """Single tier within a rate period."""

    rate: float       # $/kWh
    max: float        # kWh ceiling for this tier (absent on last tier)
    unit: str         # e.g. "kWh"
    adj: float        # adjustment factor


class URDBRecord(TypedDict, total=False):
    """Subset of the OpenEI URDB v3 rate record we consume.

    Stored verbatim from the API — all fields are optional because
    different rate plans populate different subsets.
    """

    label: str
    utility: str
    name: str
    uri: str
    startdate: int           # epoch seconds
    enddate: int             # epoch seconds
    sector: str
    description: str
    source: str

    # Energy charges
    energyratestructure: list[list[URDBRateTier]]
    energyweekdayschedule: list[list[int]]     # 12 x 24 (month x hour)
    energyweekendschedule: list[list[int]]     # 12 x 24

    # Export / sell rates
    sell: list[list[URDBRateTier]]             # same shape as energyratestructure
    usenetmetering: bool

    # Fixed charges
    fixedmonthlycharge: float
    minmonthlycharge: float
    annualmincharge: float

    # Demand charges (flat)
    flatdemandstructure: list[list[URDBRateTier]]
    flatdemandmonths: list[list[int]]          # 12-element, period per month

    # Demand charges (time-based) — stored but not used in v1
    demandratestructure: list[list[URDBRateTier]]
    demandweekdayschedule: list[list[int]]
    demandweekendschedule: list[list[int]]


# -- Metadata and cache types -------------------------------------------

@dataclass(frozen=True)
class AttributionMeta:
    """Provenance metadata for a cached rate record."""

    provider: str
    url: str
    license: str
    api_version: int


@dataclass(frozen=True)
class RateCacheEntry:
    """A cached URDB record with its metadata envelope."""

    source: str
    retrieved_at: str            # ISO 8601
    attribution: AttributionMeta
    record: dict[str, Any]       # raw URDB JSON — typed access via URDBRecord


@dataclass(frozen=True)
class OpenEIConfig:
    """User-configurable OpenEI API settings."""

    api_url: str = "https://api.openei.org/utility_rates"
    api_key: str = ""


# -- Cost calculation result --------------------------------------------

@dataclass(frozen=True)
class CostLedger:
    """Result of applying a rate schedule to a power time-series."""

    import_cost: float       # $ over horizon
    export_credit: float     # $ over horizon
    fixed_charges: float     # $ over horizon (monthly fixed + flat demand)
    net_cost: float          # import_cost - export_credit + fixed_charges


# -- API response summaries ---------------------------------------------

@dataclass(frozen=True)
class UtilitySummary:
    """Minimal info for a utility returned by URDB search."""

    utility_name: str
    eia_id: str


@dataclass(frozen=True)
class RatePlanSummary:
    """Minimal info for a rate plan in a utility's offerings."""

    label: str
    name: str
    startdate: int           # epoch seconds
    enddate: int | None      # epoch seconds or None if open-ended
    description: str
```

- [ ] **Step 3: Commit**

```bash
git add src/span_panel_simulator/rates/ tests/test_rates/
git commit -m "Add rates package with core types for URDB integration"
```

---

### Task 2: Rate Resolver

**Files:**
- Create: `src/span_panel_simulator/rates/resolver.py`
- Create: `tests/test_rates/test_resolver.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_rates/test_resolver.py`:

```python
"""Tests for ToU rate resolution from URDB schedule matrices."""

from __future__ import annotations

import calendar
from datetime import datetime, timezone

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
        [{"rate": 0.10}],   # period 0: off-peak $0.10/kWh
        [{"rate": 0.30}],   # period 1: peak $0.30/kWh
    ],
    "energyweekdayschedule": _WEEKDAY_SCHEDULE,
    "energyweekendschedule": _WEEKEND_SCHEDULE,
    "sell": [
        [{"rate": 0.04}],   # period 0: off-peak export $0.04/kWh
        [{"rate": 0.08}],   # period 1: peak export $0.08/kWh
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
        # Same hour, different months — verify month index is used
        ts_jan = _epoch(2026, 1, 7, 17, TZ)  # Wednesday
        ts_jul = _epoch(2026, 7, 15, 17, TZ)  # Wednesday
        imp_jan, _ = resolve_rate(ts_jan, TZ, _RECORD)
        imp_jul, _ = resolve_rate(ts_jul, TZ, _RECORD)
        # Both should be peak since our test schedule uses peak 16-21 all months
        assert imp_jan == 0.30
        assert imp_jul == 0.30

    def test_boundary_hour_15_is_offpeak(self) -> None:
        # Hour 15 is off-peak (peak starts at 16)
        ts = _epoch(2026, 7, 15, 15, TZ)
        imp, _ = resolve_rate(ts, TZ, _RECORD)
        assert imp == 0.10

    def test_boundary_hour_16_is_peak(self) -> None:
        # Hour 16 is peak
        ts = _epoch(2026, 7, 15, 16, TZ)
        imp, _ = resolve_rate(ts, TZ, _RECORD)
        assert imp == 0.30

    def test_boundary_hour_21_is_offpeak(self) -> None:
        # Hour 21 is off-peak (peak ends at 20 inclusive)
        ts = _epoch(2026, 7, 15, 21, TZ)
        imp, _ = resolve_rate(ts, TZ, _RECORD)
        assert imp == 0.10
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_rates/test_resolver.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'span_panel_simulator.rates.resolver'`

- [ ] **Step 3: Write the resolver**

Create `src/span_panel_simulator/rates/resolver.py`:

```python
"""Resolve import/export rates from URDB schedule matrices.

Given a UNIX timestamp and timezone, looks up the rate for that hour
using the URDB energyweekday/weekendschedule (12x24 month x hour
matrices) and energyratestructure (period -> tier list).

Uses tier 1 (index 0) only in v1.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from zoneinfo import ZoneInfo


def resolve_rate(
    timestamp: int,
    tz: str,
    record: dict[str, Any],
) -> tuple[float, float]:
    """Return (import_rate_per_kwh, export_rate_per_kwh) for a timestamp.

    Parameters
    ----------
    timestamp:
        UNIX epoch seconds.
    tz:
        IANA timezone string (e.g. "America/Los_Angeles").
    record:
        URDB record dict containing energyratestructure and schedule matrices.
    """
    dt = datetime.fromtimestamp(timestamp, tz=ZoneInfo(tz))
    month_idx = dt.month - 1   # 0-based (Jan=0)
    hour_idx = dt.hour         # 0-based (0-23)

    # Weekday: Mon=0 .. Sun=6; URDB weekday schedule covers Mon-Fri
    is_weekend = dt.weekday() >= 5
    if is_weekend:
        schedule = record.get("energyweekendschedule", [])
    else:
        schedule = record.get("energyweekdayschedule", [])

    # Look up period index from schedule matrix
    if schedule and month_idx < len(schedule) and hour_idx < len(schedule[month_idx]):
        period_idx = schedule[month_idx][hour_idx]
    else:
        period_idx = 0

    # Import rate: tier 1 of the period
    import_rate = 0.0
    rate_structure = record.get("energyratestructure", [])
    if rate_structure and period_idx < len(rate_structure):
        tiers = rate_structure[period_idx]
        if tiers:
            import_rate = tiers[0].get("rate", 0.0)

    # Export rate: tier 1 of the sell structure, same period
    export_rate = 0.0
    sell_structure = record.get("sell", [])
    if sell_structure and period_idx < len(sell_structure):
        sell_tiers = sell_structure[period_idx]
        if sell_tiers:
            export_rate = sell_tiers[0].get("rate", 0.0)

    return (import_rate, export_rate)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_rates/test_resolver.py -v`
Expected: All 8 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/span_panel_simulator/rates/resolver.py tests/test_rates/test_resolver.py
git commit -m "Add rate resolver for URDB schedule matrix lookup"
```

---

### Task 3: Cost Engine

**Files:**
- Create: `src/span_panel_simulator/rates/cost_engine.py`
- Create: `tests/test_rates/test_cost_engine.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_rates/test_cost_engine.py`:

```python
"""Tests for the cost engine — applies rates to power time-series."""

from __future__ import annotations

from datetime import datetime

import pytest
from zoneinfo import ZoneInfo

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
        # 3 hours at 1.0 kW import = 3 kWh * $0.20 = $0.60
        ts = [_epoch(2026, 3, 15, h) for h in range(10, 13)]
        power = [1.0, 1.0, 1.0]
        result = compute_costs(ts, power, _FLAT_RECORD, TZ)
        assert result.import_cost == pytest.approx(0.60)
        assert result.export_credit == pytest.approx(0.0)

    def test_pure_export(self) -> None:
        # 2 hours at -2.0 kW (export) = 4 kWh * $0.05 = $0.20
        ts = [_epoch(2026, 3, 15, h) for h in range(10, 12)]
        power = [-2.0, -2.0]
        result = compute_costs(ts, power, _FLAT_RECORD, TZ)
        assert result.import_cost == pytest.approx(0.0)
        assert result.export_credit == pytest.approx(0.20)

    def test_mixed_import_export(self) -> None:
        # Hour 1: 1.0 kW import = $0.20, Hour 2: -1.0 kW export = $0.05
        ts = [_epoch(2026, 3, 15, 10), _epoch(2026, 3, 15, 11)]
        power = [1.0, -1.0]
        result = compute_costs(ts, power, _FLAT_RECORD, TZ)
        assert result.import_cost == pytest.approx(0.20)
        assert result.export_credit == pytest.approx(0.05)

    def test_net_cost_includes_fixed(self) -> None:
        # 1 hour in March -> 1 month -> $10 fixed
        ts = [_epoch(2026, 3, 15, 10)]
        power = [1.0]  # $0.20 import
        result = compute_costs(ts, power, _FLAT_RECORD, TZ)
        assert result.fixed_charges == pytest.approx(10.0)
        assert result.net_cost == pytest.approx(0.20 - 0.0 + 10.0)

    def test_multi_month_fixed_charges(self) -> None:
        # Hours spanning Jan and Feb -> 2 months -> $20 fixed
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
        # 1 month -> $5 fixed + $8 flat demand = $13
        ts = [_epoch(2026, 3, 15, 10)]
        power = [0.0]
        result = compute_costs(ts, power, record_with_demand, TZ)
        assert result.fixed_charges == pytest.approx(13.0)
```

Note: Add `import pytest` at the top of the test file (for `pytest.approx`).

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_rates/test_cost_engine.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'span_panel_simulator.rates.cost_engine'`

- [ ] **Step 3: Write the cost engine**

Create `src/span_panel_simulator/rates/cost_engine.py`:

```python
"""Batch cost calculation over a modeling horizon.

Applies a URDB rate record to hourly power arrays, producing a
CostLedger with import cost, export credit, fixed charges, and net cost.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from zoneinfo import ZoneInfo

from span_panel_simulator.rates.resolver import resolve_rate
from span_panel_simulator.rates.types import CostLedger


def compute_costs(
    timestamps: list[int],
    power_kw: list[float],
    record: dict[str, Any],
    tz: str,
    resolution_s: int = 3600,
) -> CostLedger:
    """Compute total costs over a horizon.

    Parameters
    ----------
    timestamps:
        UNIX epoch seconds, one per interval (hourly).
    power_kw:
        Grid power per interval in kW.  Positive = import, negative = export.
    record:
        URDB record dict.
    tz:
        IANA timezone string.
    resolution_s:
        Interval length in seconds (default 3600 = 1 hour).
    """
    import_cost = 0.0
    export_credit = 0.0
    months_seen: set[tuple[int, int]] = set()   # (year, month)

    for ts, pwr in zip(timestamps, power_kw):
        import_rate, export_rate = resolve_rate(ts, tz, record)
        energy_kwh = pwr * resolution_s / 3600

        if energy_kwh > 0:
            import_cost += energy_kwh * import_rate
        elif energy_kwh < 0:
            export_credit += abs(energy_kwh) * export_rate

        dt = datetime.fromtimestamp(ts, tz=ZoneInfo(tz))
        months_seen.add((dt.year, dt.month))

    num_months = len(months_seen)
    fixed_monthly = record.get("fixedmonthlycharge", 0.0) or 0.0
    flat_demand = _flat_demand_per_month(record)
    fixed_charges = num_months * (fixed_monthly + flat_demand)

    net_cost = import_cost - export_credit + fixed_charges

    return CostLedger(
        import_cost=import_cost,
        export_credit=export_credit,
        fixed_charges=fixed_charges,
        net_cost=net_cost,
    )


def _flat_demand_per_month(record: dict[str, Any]) -> float:
    """Extract flat demand charge per month from URDB record.

    Uses tier 1 of period 0 from flatdemandstructure.
    """
    structure = record.get("flatdemandstructure", [])
    if not structure:
        return 0.0
    period_tiers = structure[0]
    if not period_tiers:
        return 0.0
    return period_tiers[0].get("rate", 0.0)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_rates/test_cost_engine.py -v`
Expected: All 9 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/span_panel_simulator/rates/cost_engine.py tests/test_rates/test_cost_engine.py
git commit -m "Add cost engine for hourly power-to-cost calculation"
```

---

## Phase 2: Rate Cache and OpenEI Client

### Task 4: Rate Cache Manager

**Files:**
- Create: `src/span_panel_simulator/rates/cache.py`
- Create: `tests/test_rates/test_cache.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_rates/test_cache.py`:

```python
"""Tests for the rate cache manager."""

from __future__ import annotations

from pathlib import Path

from span_panel_simulator.rates.cache import RateCache
from span_panel_simulator.rates.types import OpenEIConfig


SAMPLE_URDB_RECORD: dict = {
    "label": "abc123",
    "utility": "Pacific Gas & Electric Co",
    "name": "E-TOU-C",
    "energyratestructure": [[{"rate": 0.25}]],
    "energyweekdayschedule": [[0] * 24 for _ in range(12)],
    "energyweekendschedule": [[0] * 24 for _ in range(12)],
}


class TestRateCache:
    """Rate cache load/save/get/set operations."""

    def test_empty_cache_returns_none(self, tmp_path: Path) -> None:
        cache = RateCache(tmp_path / "rates_cache.yaml")
        assert cache.get_cached_rate("nonexistent") is None

    def test_cache_and_retrieve(self, tmp_path: Path) -> None:
        cache = RateCache(tmp_path / "rates_cache.yaml")
        cache.cache_rate("abc123", SAMPLE_URDB_RECORD)
        entry = cache.get_cached_rate("abc123")
        assert entry is not None
        assert entry.record["label"] == "abc123"
        assert entry.record["utility"] == "Pacific Gas & Electric Co"
        assert entry.source == "openei_urdb"
        assert entry.attribution.license == "CC0"

    def test_persistence_across_instances(self, tmp_path: Path) -> None:
        path = tmp_path / "rates_cache.yaml"
        cache1 = RateCache(path)
        cache1.cache_rate("abc123", SAMPLE_URDB_RECORD)

        cache2 = RateCache(path)
        entry = cache2.get_cached_rate("abc123")
        assert entry is not None
        assert entry.record["name"] == "E-TOU-C"

    def test_current_rate_label(self, tmp_path: Path) -> None:
        cache = RateCache(tmp_path / "rates_cache.yaml")
        assert cache.get_current_rate_label() is None
        cache.set_current_rate_label("abc123")
        assert cache.get_current_rate_label() == "abc123"

    def test_current_rate_label_persists(self, tmp_path: Path) -> None:
        path = tmp_path / "rates_cache.yaml"
        cache1 = RateCache(path)
        cache1.set_current_rate_label("abc123")

        cache2 = RateCache(path)
        assert cache2.get_current_rate_label() == "abc123"

    def test_list_cached_rates(self, tmp_path: Path) -> None:
        cache = RateCache(tmp_path / "rates_cache.yaml")
        cache.cache_rate("abc123", SAMPLE_URDB_RECORD)
        cache.cache_rate("def456", {
            "label": "def456",
            "utility": "SoCal Edison",
            "name": "TOU-D-PRIME",
        })
        summaries = cache.list_cached_rates()
        assert len(summaries) == 2
        labels = {s["label"] for s in summaries}
        assert labels == {"abc123", "def456"}

    def test_openei_config_defaults(self, tmp_path: Path) -> None:
        cache = RateCache(tmp_path / "rates_cache.yaml")
        config = cache.get_openei_config()
        assert config.api_url == "https://api.openei.org/utility_rates"
        assert config.api_key == ""

    def test_openei_config_set_and_get(self, tmp_path: Path) -> None:
        cache = RateCache(tmp_path / "rates_cache.yaml")
        cache.set_openei_config("https://custom.api/rates", "my-key-123")
        config = cache.get_openei_config()
        assert config.api_url == "https://custom.api/rates"
        assert config.api_key == "my-key-123"

    def test_openei_config_persists(self, tmp_path: Path) -> None:
        path = tmp_path / "rates_cache.yaml"
        cache1 = RateCache(path)
        cache1.set_openei_config("https://custom.api/rates", "my-key-123")

        cache2 = RateCache(path)
        config = cache2.get_openei_config()
        assert config.api_url == "https://custom.api/rates"
        assert config.api_key == "my-key-123"

    def test_delete_cached_rate(self, tmp_path: Path) -> None:
        cache = RateCache(tmp_path / "rates_cache.yaml")
        cache.cache_rate("abc123", SAMPLE_URDB_RECORD)
        assert cache.get_cached_rate("abc123") is not None
        cache.delete_cached_rate("abc123")
        assert cache.get_cached_rate("abc123") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_rates/test_cache.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'span_panel_simulator.rates.cache'`

- [ ] **Step 3: Write the cache manager**

Create `src/span_panel_simulator/rates/cache.py`:

```python
"""Simulator-wide rate cache backed by a YAML file.

Stores URDB records verbatim, keyed by their label.  Also manages
the current rate selection and OpenEI API configuration.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from span_panel_simulator.rates.types import (
    AttributionMeta,
    OpenEIConfig,
    RateCacheEntry,
)

_DEFAULT_ATTRIBUTION = AttributionMeta(
    provider="OpenEI Utility Rate Database",
    url="https://openei.org/wiki/Utility_Rate_Database",
    license="CC0",
    api_version=3,
)


class RateCache:
    """Manages the simulator-wide rate cache YAML file."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._data = self._load()

    # -- Cache operations ------------------------------------------------

    def get_cached_rate(self, label: str) -> RateCacheEntry | None:
        """Return a cached rate entry by URDB label, or None."""
        rates = self._data.get("rates", {})
        entry = rates.get(label)
        if entry is None:
            return None
        attr_data = entry.get("attribution", {})
        return RateCacheEntry(
            source=entry.get("source", "openei_urdb"),
            retrieved_at=entry.get("retrieved_at", ""),
            attribution=AttributionMeta(
                provider=attr_data.get("provider", _DEFAULT_ATTRIBUTION.provider),
                url=attr_data.get("url", _DEFAULT_ATTRIBUTION.url),
                license=attr_data.get("license", _DEFAULT_ATTRIBUTION.license),
                api_version=attr_data.get("api_version", _DEFAULT_ATTRIBUTION.api_version),
            ),
            record=entry.get("record", {}),
        )

    def cache_rate(self, label: str, urdb_record: dict[str, Any]) -> None:
        """Store a URDB record in the cache."""
        if "rates" not in self._data:
            self._data["rates"] = {}
        self._data["rates"][label] = {
            "source": "openei_urdb",
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "attribution": {
                "provider": _DEFAULT_ATTRIBUTION.provider,
                "url": _DEFAULT_ATTRIBUTION.url,
                "license": _DEFAULT_ATTRIBUTION.license,
                "api_version": _DEFAULT_ATTRIBUTION.api_version,
            },
            "record": urdb_record,
        }
        self._save()

    def delete_cached_rate(self, label: str) -> None:
        """Remove a rate from the cache."""
        rates = self._data.get("rates", {})
        rates.pop(label, None)
        self._save()

    def list_cached_rates(self) -> list[dict[str, Any]]:
        """Return summary dicts for all cached rates."""
        rates = self._data.get("rates", {})
        summaries = []
        for label, entry in rates.items():
            record = entry.get("record", {})
            summaries.append({
                "label": label,
                "utility": record.get("utility", ""),
                "name": record.get("name", ""),
                "retrieved_at": entry.get("retrieved_at", ""),
            })
        return summaries

    # -- Current rate selection ------------------------------------------

    def get_current_rate_label(self) -> str | None:
        """Return the simulator-wide current rate label, or None."""
        label = self._data.get("current_rate_label")
        return label if label else None

    def set_current_rate_label(self, label: str) -> None:
        """Set the simulator-wide current rate selection."""
        self._data["current_rate_label"] = label
        self._save()

    # -- OpenEI configuration -------------------------------------------

    def get_openei_config(self) -> OpenEIConfig:
        """Return the stored OpenEI API settings."""
        cfg = self._data.get("openei", {})
        return OpenEIConfig(
            api_url=cfg.get("api_url", OpenEIConfig.api_url),
            api_key=cfg.get("api_key", OpenEIConfig.api_key),
        )

    def set_openei_config(self, api_url: str, api_key: str) -> None:
        """Update the OpenEI API settings."""
        self._data["openei"] = {
            "api_url": api_url,
            "api_key": api_key,
        }
        self._save()

    # -- Persistence -----------------------------------------------------

    def _load(self) -> dict[str, Any]:
        if self._path.exists():
            with open(self._path) as f:
                data = yaml.safe_load(f)
            return data if isinstance(data, dict) else {}
        return {}

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "w") as f:
            yaml.dump(
                self._data,
                f,
                default_flow_style=False,
                sort_keys=False,
                allow_unicode=True,
            )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_rates/test_cache.py -v`
Expected: All 10 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/span_panel_simulator/rates/cache.py tests/test_rates/test_cache.py
git commit -m "Add rate cache manager for URDB record persistence"
```

---

### Task 5: OpenEI API Client

**Files:**
- Create: `src/span_panel_simulator/rates/openei.py`
- Create: `tests/test_rates/test_openei.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_rates/test_openei.py`:

```python
"""Tests for the OpenEI URDB API client (mocked HTTP)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from span_panel_simulator.rates.openei import (
    OpenEIError,
    fetch_rate_detail,
    fetch_rate_plans,
    fetch_utilities,
)

API_URL = "https://api.openei.org/utility_rates"
API_KEY = "test-key"


def _mock_response(json_data: dict, status: int = 200) -> AsyncMock:
    """Create a mock aiohttp response."""
    resp = AsyncMock()
    resp.status = status
    resp.json = AsyncMock(return_value=json_data)
    resp.text = AsyncMock(return_value=str(json_data))
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


class TestFetchUtilities:
    """Fetch utilities by lat/lon."""

    @pytest.mark.asyncio
    async def test_returns_utility_summaries(self) -> None:
        response_data = {
            "items": [
                {"utility_name": "Pacific Gas & Electric Co", "eia": "14328"},
                {"utility_name": "City of Palo Alto", "eia": "14328"},
            ]
        }
        with patch("span_panel_simulator.rates.openei._get_json", return_value=response_data):
            result = await fetch_utilities(37.7, -122.4, API_URL, API_KEY)
        assert len(result) >= 1
        assert result[0].utility_name == "Pacific Gas & Electric Co"

    @pytest.mark.asyncio
    async def test_empty_result(self) -> None:
        with patch("span_panel_simulator.rates.openei._get_json", return_value={"items": []}):
            result = await fetch_utilities(0.0, 0.0, API_URL, API_KEY)
        assert result == []


class TestFetchRatePlans:
    """Fetch rate plans for a utility."""

    @pytest.mark.asyncio
    async def test_returns_plan_summaries(self) -> None:
        response_data = {
            "items": [
                {
                    "label": "abc123",
                    "name": "E-TOU-C",
                    "startdate": 1672531200,
                    "enddate": None,
                    "description": "Time of use residential",
                },
                {
                    "label": "def456",
                    "name": "E-TOU-D",
                    "startdate": 1672531200,
                    "enddate": 1704067200,
                    "description": "Legacy TOU",
                },
            ]
        }
        with patch("span_panel_simulator.rates.openei._get_json", return_value=response_data):
            result = await fetch_rate_plans("Pacific Gas & Electric Co", API_URL, API_KEY)
        assert len(result) == 2
        assert result[0].label == "abc123"
        assert result[0].name == "E-TOU-C"
        assert result[1].enddate == 1704067200


class TestFetchRateDetail:
    """Fetch full rate detail by label."""

    @pytest.mark.asyncio
    async def test_returns_full_record(self) -> None:
        response_data = {
            "items": [
                {
                    "label": "abc123",
                    "utility": "PG&E",
                    "name": "E-TOU-C",
                    "energyratestructure": [[{"rate": 0.25}]],
                    "energyweekdayschedule": [[0] * 24 for _ in range(12)],
                    "energyweekendschedule": [[0] * 24 for _ in range(12)],
                }
            ]
        }
        with patch("span_panel_simulator.rates.openei._get_json", return_value=response_data):
            result = await fetch_rate_detail("abc123", API_URL, API_KEY)
        assert result["label"] == "abc123"
        assert result["energyratestructure"] == [[{"rate": 0.25}]]

    @pytest.mark.asyncio
    async def test_label_not_found_raises(self) -> None:
        with patch("span_panel_simulator.rates.openei._get_json", return_value={"items": []}):
            with pytest.raises(OpenEIError, match="not found"):
                await fetch_rate_detail("nonexistent", API_URL, API_KEY)

    @pytest.mark.asyncio
    async def test_api_error_raises(self) -> None:
        with patch(
            "span_panel_simulator.rates.openei._get_json",
            side_effect=OpenEIError("HTTP 401: Unauthorized"),
        ):
            with pytest.raises(OpenEIError, match="401"):
                await fetch_rate_detail("abc123", API_URL, API_KEY)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_rates/test_openei.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'span_panel_simulator.rates.openei'`

- [ ] **Step 3: Write the OpenEI client**

Create `src/span_panel_simulator/rates/openei.py`:

```python
"""OpenEI URDB API client.

Fetches utility and rate plan data from the OpenEI Utility Rate
Database.  All functions accept api_url and api_key so the base URL
and credentials are caller-configurable.
"""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

from span_panel_simulator.rates.types import RatePlanSummary, UtilitySummary

_LOG = logging.getLogger(__name__)


class OpenEIError(Exception):
    """Raised when the URDB API returns an error or unexpected response."""


async def _get_json(url: str, params: dict[str, str]) -> dict[str, Any]:
    """Issue a GET request and return the parsed JSON response."""
    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise OpenEIError(f"HTTP {resp.status}: {body[:200]}")
            data: dict[str, Any] = await resp.json(content_type=None)
            return data


async def fetch_utilities(
    lat: float,
    lon: float,
    api_url: str,
    api_key: str,
) -> list[UtilitySummary]:
    """Fetch utilities near a lat/lon from URDB.

    Returns de-duplicated utilities sorted by name.
    """
    params = {
        "version": "3",
        "format": "json",
        "api_key": api_key,
        "lat": str(lat),
        "lon": str(lon),
        "sector": "Residential",
        "detail": "minimal",
    }
    data = await _get_json(api_url, params)
    items = data.get("items", [])

    seen: set[str] = set()
    utilities: list[UtilitySummary] = []
    for item in items:
        name = item.get("utility_name", item.get("utility", ""))
        if not name or name in seen:
            continue
        seen.add(name)
        utilities.append(UtilitySummary(
            utility_name=name,
            eia_id=str(item.get("eia", "")),
        ))
    utilities.sort(key=lambda u: u.utility_name)
    return utilities


async def fetch_rate_plans(
    utility: str,
    api_url: str,
    api_key: str,
    sector: str = "Residential",
) -> list[RatePlanSummary]:
    """Fetch available rate plans for a utility."""
    params = {
        "version": "3",
        "format": "json",
        "api_key": api_key,
        "ratesforutility": utility,
        "sector": sector,
        "detail": "minimal",
    }
    data = await _get_json(api_url, params)
    items = data.get("items", [])

    plans: list[RatePlanSummary] = []
    for item in items:
        plans.append(RatePlanSummary(
            label=item.get("label", ""),
            name=item.get("name", ""),
            startdate=item.get("startdate", 0),
            enddate=item.get("enddate"),
            description=item.get("description", ""),
        ))
    return plans


async def fetch_rate_detail(
    label: str,
    api_url: str,
    api_key: str,
) -> dict[str, Any]:
    """Fetch the full rate record for a URDB label.

    Returns the raw URDB record dict (to be stored verbatim).
    Raises OpenEIError if the label is not found.
    """
    params = {
        "version": "3",
        "format": "json",
        "api_key": api_key,
        "getpage": label,
        "detail": "full",
    }
    data = await _get_json(api_url, params)
    items = data.get("items", [])
    if not items:
        raise OpenEIError(f"Rate plan '{label}' not found in URDB")
    record: dict[str, Any] = items[0]
    return record
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_rates/test_openei.py -v`
Expected: All 5 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/span_panel_simulator/rates/openei.py tests/test_rates/test_openei.py
git commit -m "Add OpenEI URDB API client for rate plan discovery"
```

---

## Phase 3: API Endpoints

### Task 6: Rate API Routes

**Files:**
- Modify: `src/span_panel_simulator/dashboard/routes.py`

This task adds all rate-related HTTP endpoints. The routes follow the existing pattern: thin handlers that parse the request, call the rates package, and return JSON.

- [ ] **Step 1: Add RateCache initialization**

The `RateCache` instance needs to be accessible from route handlers. Add it to the dashboard app keys.

Modify `src/span_panel_simulator/dashboard/keys.py` — add a new app key:

```python
APP_KEY_RATE_CACHE = web.AppKey("rate_cache", default=None)
```

Note: Read the file first to see the exact pattern for existing keys, then add the new key following the same pattern.

- [ ] **Step 2: Initialize RateCache in the app factory**

Find where the dashboard app is created (likely `dashboard/app.py` or similar) and add:

```python
from span_panel_simulator.rates.cache import RateCache

# In the app setup function, after config_dir is available:
rate_cache = RateCache(config_dir / "rates_cache.yaml")
app[APP_KEY_RATE_CACHE] = rate_cache
```

Note: Read the app factory file to find the exact location and follow the existing pattern for setting app keys.

- [ ] **Step 3: Add rate route handlers to routes.py**

Add to `src/span_panel_simulator/dashboard/routes.py`:

```python
from span_panel_simulator.rates.cache import RateCache
from span_panel_simulator.rates.openei import OpenEIError, fetch_rate_detail, fetch_rate_plans, fetch_utilities

def _rate_cache(request: web.Request) -> RateCache:
    return request.app[APP_KEY_RATE_CACHE]


# -- Rate endpoints --


async def handle_get_openei_config(request: web.Request) -> web.Response:
    """GET /rates/openei-config — return current API URL and key."""
    config = _rate_cache(request).get_openei_config()
    return web.json_response({
        "api_url": config.api_url,
        "api_key": config.api_key,
    })


async def handle_put_openei_config(request: web.Request) -> web.Response:
    """PUT /rates/openei-config — update API URL and key."""
    body = await request.json()
    api_url = body.get("api_url", "").strip()
    api_key = body.get("api_key", "").strip()
    if not api_url or not api_key:
        return web.json_response({"error": "api_url and api_key are required"}, status=400)
    _rate_cache(request).set_openei_config(api_url, api_key)
    return web.json_response({"ok": True})


async def handle_get_utilities(request: web.Request) -> web.Response:
    """GET /rates/utilities?lat=&lon= — utilities near location."""
    lat = request.query.get("lat")
    lon = request.query.get("lon")
    if lat is None or lon is None:
        return web.json_response({"error": "lat and lon are required"}, status=400)
    config = _rate_cache(request).get_openei_config()
    if not config.api_key:
        return web.json_response({"error": "OpenEI API key not configured"}, status=400)
    try:
        results = await fetch_utilities(float(lat), float(lon), config.api_url, config.api_key)
    except OpenEIError as e:
        return web.json_response({"error": str(e)}, status=502)
    return web.json_response([
        {"utility_name": u.utility_name, "eia_id": u.eia_id}
        for u in results
    ])


async def handle_get_rate_plans(request: web.Request) -> web.Response:
    """GET /rates/plans?utility=&sector= — rate plans for a utility."""
    utility = request.query.get("utility")
    if not utility:
        return web.json_response({"error": "utility is required"}, status=400)
    sector = request.query.get("sector", "Residential")
    config = _rate_cache(request).get_openei_config()
    if not config.api_key:
        return web.json_response({"error": "OpenEI API key not configured"}, status=400)
    try:
        plans = await fetch_rate_plans(utility, config.api_url, config.api_key, sector)
    except OpenEIError as e:
        return web.json_response({"error": str(e)}, status=502)
    return web.json_response([
        {
            "label": p.label,
            "name": p.name,
            "startdate": p.startdate,
            "enddate": p.enddate,
            "description": p.description,
        }
        for p in plans
    ])


async def handle_fetch_rate(request: web.Request) -> web.Response:
    """POST /rates/fetch {label} — fetch from URDB and cache."""
    body = await request.json()
    label = body.get("label", "").strip()
    if not label:
        return web.json_response({"error": "label is required"}, status=400)
    config = _rate_cache(request).get_openei_config()
    if not config.api_key:
        return web.json_response({"error": "OpenEI API key not configured"}, status=400)
    try:
        record = await fetch_rate_detail(label, config.api_url, config.api_key)
    except OpenEIError as e:
        return web.json_response({"error": str(e)}, status=502)
    cache = _rate_cache(request)
    cache.cache_rate(label, record)
    return web.json_response({
        "label": label,
        "utility": record.get("utility", ""),
        "name": record.get("name", ""),
    })


async def handle_refresh_rate(request: web.Request) -> web.Response:
    """POST /rates/refresh {label} — re-fetch a cached rate."""
    body = await request.json()
    label = body.get("label", "").strip()
    if not label:
        return web.json_response({"error": "label is required"}, status=400)
    config = _rate_cache(request).get_openei_config()
    if not config.api_key:
        return web.json_response({"error": "OpenEI API key not configured"}, status=400)
    try:
        record = await fetch_rate_detail(label, config.api_url, config.api_key)
    except OpenEIError as e:
        return web.json_response({"error": str(e)}, status=502)
    _rate_cache(request).cache_rate(label, record)
    return web.json_response({"ok": True, "label": label})


async def handle_get_rates_cache(request: web.Request) -> web.Response:
    """GET /rates/cache — list all cached rate summaries."""
    return web.json_response(_rate_cache(request).list_cached_rates())


async def handle_get_current_rate(request: web.Request) -> web.Response:
    """GET /rates/current — current rate label and summary."""
    cache = _rate_cache(request)
    label = cache.get_current_rate_label()
    if label is None:
        return web.json_response({"label": None})
    entry = cache.get_cached_rate(label)
    if entry is None:
        return web.json_response({"label": label, "error": "cached record missing"})
    return web.json_response({
        "label": label,
        "utility": entry.record.get("utility", ""),
        "name": entry.record.get("name", ""),
        "retrieved_at": entry.retrieved_at,
    })


async def handle_put_current_rate(request: web.Request) -> web.Response:
    """PUT /rates/current {label} — set simulator-wide current rate."""
    body = await request.json()
    label = body.get("label", "").strip()
    if not label:
        return web.json_response({"error": "label is required"}, status=400)
    _rate_cache(request).set_current_rate_label(label)
    return web.json_response({"ok": True})


async def handle_get_rate_detail(request: web.Request) -> web.Response:
    """GET /rates/detail/{label} — full cached record."""
    label = request.match_info["label"]
    entry = _rate_cache(request).get_cached_rate(label)
    if entry is None:
        return web.json_response({"error": "not found"}, status=404)
    return web.json_response(entry.record)


async def handle_get_rate_attribution(request: web.Request) -> web.Response:
    """GET /rates/attribution/{label} — attribution metadata."""
    label = request.match_info["label"]
    entry = _rate_cache(request).get_cached_rate(label)
    if entry is None:
        return web.json_response({"error": "not found"}, status=404)
    return web.json_response({
        "provider": entry.attribution.provider,
        "url": entry.attribution.url,
        "license": entry.attribution.license,
        "api_version": entry.attribution.api_version,
        "retrieved_at": entry.retrieved_at,
    })
```

- [ ] **Step 4: Register the routes in setup_routes**

Add to the `setup_routes` function in `routes.py`, after the existing modeling-data route:

```python
    # Rate plan management
    app.router.add_get("/rates/openei-config", handle_get_openei_config)
    app.router.add_put("/rates/openei-config", handle_put_openei_config)
    app.router.add_get("/rates/utilities", handle_get_utilities)
    app.router.add_get("/rates/plans", handle_get_rate_plans)
    app.router.add_post("/rates/fetch", handle_fetch_rate)
    app.router.add_post("/rates/refresh", handle_refresh_rate)
    app.router.add_get("/rates/cache", handle_get_rates_cache)
    app.router.add_get("/rates/current", handle_get_current_rate)
    app.router.add_put("/rates/current", handle_put_current_rate)
    app.router.add_get("/rates/detail/{label}", handle_get_rate_detail)
    app.router.add_get("/rates/attribution/{label}", handle_get_rate_attribution)
```

- [ ] **Step 5: Run the existing test suite to verify no regressions**

Run: `pytest tests/ -v --timeout=30`
Expected: All existing tests PASS

- [ ] **Step 6: Commit**

```bash
git add src/span_panel_simulator/dashboard/routes.py src/span_panel_simulator/dashboard/keys.py
git commit -m "Add rate API endpoints for URDB discovery, caching, and selection"
```

Note: Also `git add` the app factory file if it was modified in step 2.

---

## Phase 4: Engine Integration

### Task 7: Wire Cost Engine into compute_modeling_data

**Files:**
- Modify: `src/span_panel_simulator/engine.py`
- Modify: `src/span_panel_simulator/dashboard/routes.py` (pass rate cache to engine)

The cost engine is called **after** the Before/After power arrays are computed. The engine does not resolve rates or compute costs — it delegates to `compute_costs`.

- [ ] **Step 1: Add cost calculation to compute_modeling_data**

Read `engine.py` and find the return dict at the end of `compute_modeling_data` (around line 1562). Add cost calculation just before the return:

```python
from span_panel_simulator.rates.cache import RateCache
from span_panel_simulator.rates.cost_engine import compute_costs
```

Add a `rate_cache` parameter to `compute_modeling_data` and an optional `proposed_rate_label`:

```python
async def compute_modeling_data(
    self,
    horizon_hours: int,
    rate_cache: RateCache | None = None,
    proposed_rate_label: str | None = None,
) -> dict[str, Any]:
```

Just before the `return` statement, add:

```python
        # -- Cost calculation (optional, requires rate cache) --------
        result: dict[str, Any] = {
            "horizon_start": int(horizon_start),
            "horizon_end": int(horizon_end),
            "resolution_s": 3600,
            "time_zone": tz_str,
            "timestamps": [int(t) for t in timestamps],
            "site_power": site_power_arr,
            "grid_power": grid_power_arr,
            "pv_power_before": pv_before_arr,
            "pv_power_after": pv_after_arr,
            "pv_power": pv_after_arr,
            "battery_power": battery_power_arr,
            "battery_power_before": battery_before_arr,
            "circuits": circuits_response,
        }

        if rate_cache is not None:
            current_label = rate_cache.get_current_rate_label()
            if current_label is not None:
                current_entry = rate_cache.get_cached_rate(current_label)
                if current_entry is not None:
                    ts_list = result["timestamps"]
                    before_costs = compute_costs(
                        ts_list, site_power_arr, current_entry.record, tz_str,
                    )
                    result["before_costs"] = {
                        "import_cost": round(before_costs.import_cost, 2),
                        "export_credit": round(before_costs.export_credit, 2),
                        "fixed_charges": round(before_costs.fixed_charges, 2),
                        "net_cost": round(before_costs.net_cost, 2),
                    }

                    # After: use proposed rate if set, otherwise current
                    after_record = current_entry.record
                    if proposed_rate_label:
                        proposed_entry = rate_cache.get_cached_rate(proposed_rate_label)
                        if proposed_entry is not None:
                            after_record = proposed_entry.record
                    after_costs = compute_costs(
                        ts_list, grid_power_arr, after_record, tz_str,
                    )
                    result["after_costs"] = {
                        "import_cost": round(after_costs.import_cost, 2),
                        "export_credit": round(after_costs.export_credit, 2),
                        "fixed_charges": round(after_costs.fixed_charges, 2),
                        "net_cost": round(after_costs.net_cost, 2),
                    }

        return result
```

- [ ] **Step 2: Update the DashboardContext to pass rate_cache**

Modify `src/span_panel_simulator/dashboard/context.py` to extend the `get_modeling_data` callable signature, or alternatively update `handle_modeling_data` in `routes.py` to call the cost engine itself after receiving the result.

The cleaner approach: keep the engine method signature unchanged, and compute costs in the route handler. This avoids changing the DashboardContext interface. Update `handle_modeling_data`:

```python
async def handle_modeling_data(request: web.Request) -> web.Response:
    """Return time-series for Before/After energy comparison."""
    ctx = _ctx(request)
    horizon_key = request.query.get("horizon", "1mo")
    horizon_hours = _HORIZON_MAP.get(horizon_key, 730)

    config_file = resolve_modeling_config_filename(ctx, request.query.get("config"))
    result = await ctx.get_modeling_data(horizon_hours, config_file)
    if result is None:
        return web.json_response({"error": "No running simulation"}, status=503)
    if "error" in result:
        return web.json_response(result, status=400)

    # Attach cost data if rate cache is available
    cache = _rate_cache(request)
    if cache is not None:
        proposed_label = request.query.get("proposed_rate_label")
        _attach_costs(result, cache, proposed_label)

    return web.json_response(result)
```

Add the helper function:

```python
def _attach_costs(
    result: dict[str, Any],
    cache: RateCache,
    proposed_rate_label: str | None,
) -> None:
    """Add before_costs and after_costs to a modeling result dict."""
    current_label = cache.get_current_rate_label()
    if current_label is None:
        return
    current_entry = cache.get_cached_rate(current_label)
    if current_entry is None:
        return

    tz_str = result["time_zone"]
    ts_list = result["timestamps"]

    before_costs = compute_costs(ts_list, result["site_power"], current_entry.record, tz_str)
    result["before_costs"] = {
        "import_cost": round(before_costs.import_cost, 2),
        "export_credit": round(before_costs.export_credit, 2),
        "fixed_charges": round(before_costs.fixed_charges, 2),
        "net_cost": round(before_costs.net_cost, 2),
    }

    after_record = current_entry.record
    if proposed_rate_label:
        proposed_entry = cache.get_cached_rate(proposed_rate_label)
        if proposed_entry is not None:
            after_record = proposed_entry.record
    after_costs = compute_costs(ts_list, result["grid_power"], after_record, tz_str)
    result["after_costs"] = {
        "import_cost": round(after_costs.import_cost, 2),
        "export_credit": round(after_costs.export_credit, 2),
        "fixed_charges": round(after_costs.fixed_charges, 2),
        "net_cost": round(after_costs.net_cost, 2),
    }
```

This approach is better because:
- `engine.py` stays focused on energy calculation (respects AGENTS.md boundary)
- No changes to `DashboardContext` interface
- Cost calculation is a route-level concern — decorating the response

- [ ] **Step 3: Run full test suite**

Run: `pytest tests/ -v --timeout=30`
Expected: All tests PASS (no engine signature changes)

- [ ] **Step 4: Commit**

```bash
git add src/span_panel_simulator/dashboard/routes.py
git commit -m "Wire cost engine into modeling-data response via route handler"
```

---

## Phase 5: Modeling View UI

### Task 8: Rate Plan Selection UI

**Files:**
- Modify: `src/span_panel_simulator/dashboard/templates/partials/modeling_view.html`

This task adds the rate plan selection section and OpenEI dialog to the modeling view HTML. The JavaScript interacts with the rate API endpoints from Task 6.

- [ ] **Step 1: Add rate plan section HTML**

Insert after the `<!-- Loading spinner -->` div (line 34) and before `<!-- Energy summary table -->` (line 36) in `modeling_view.html`:

```html
  <!-- Rate plan selection -->
  <div id="modeling-rate-section" style="display:none; margin-bottom:1rem">
    <div class="modeling-rate-row" style="display:flex; gap:1.5rem; align-items:flex-start; flex-wrap:wrap">
      <!-- Current rate -->
      <div class="modeling-rate-slot" style="flex:1; min-width:220px">
        <div style="font-weight:500; font-size:0.85rem; margin-bottom:0.25rem">
          Current Rate
          <button type="button" class="btn btn-xs" id="btn-rate-attribution"
                  style="display:none; margin-left:0.25rem; font-size:0.65rem"
                  title="Data source attribution">&#9432;</button>
        </div>
        <div id="rate-current-display" class="text-muted" style="font-size:0.8rem">
          No rate plan selected
        </div>
        <div style="margin-top:0.25rem">
          <button type="button" class="btn btn-xs" id="btn-rate-current-configure">Configure</button>
          <button type="button" class="btn btn-xs" id="btn-rate-current-refresh" style="display:none">Refresh</button>
        </div>
      </div>
      <!-- Proposed rate -->
      <div class="modeling-rate-slot" style="flex:1; min-width:220px">
        <div style="font-weight:500; font-size:0.85rem; margin-bottom:0.25rem">Proposed Rate</div>
        <div id="rate-proposed-display" class="text-muted" style="font-size:0.8rem">
          Using current rate for comparison
        </div>
        <div style="margin-top:0.25rem">
          <button type="button" class="btn btn-xs" id="btn-rate-proposed-set">Set Proposed Rate</button>
          <button type="button" class="btn btn-xs" id="btn-rate-proposed-clear" style="display:none">Clear</button>
        </div>
      </div>
    </div>
  </div>

  <!-- OpenEI rate selection dialog (modal) -->
  <div id="rate-dialog-overlay" style="display:none; position:fixed; inset:0; background:rgba(0,0,0,0.5); z-index:1000; justify-content:center; align-items:center">
    <div class="card" style="width:min(480px,90vw); max-height:80vh; overflow-y:auto; margin:2rem">
      <h3 style="margin-top:0">OpenEI Rate Plan</h3>

      <!-- Settings section -->
      <div style="margin-bottom:1rem; padding-bottom:0.75rem; border-bottom:1px solid var(--border)">
        <div style="font-weight:500; font-size:0.85rem; margin-bottom:0.5rem">API Settings</div>
        <label style="font-size:0.8rem; display:block; margin-bottom:0.25rem">
          API URL
          <input type="text" id="rate-api-url" class="input-sm" style="width:100%"
                 placeholder="https://api.openei.org/utility_rates">
        </label>
        <label style="font-size:0.8rem; display:block; margin-bottom:0.25rem">
          API Key
          <input type="text" id="rate-api-key" class="input-sm" style="width:100%"
                 placeholder="Enter your OpenEI API key">
        </label>
        <div style="display:flex; gap:0.5rem; align-items:center; margin-top:0.25rem">
          <button type="button" class="btn btn-xs" id="btn-rate-save-config">Save</button>
          <a href="https://api.openei.org/signup/" target="_blank" rel="noopener"
             style="font-size:0.7rem">Get a free API key</a>
        </div>
      </div>

      <!-- Rate selection section -->
      <div style="margin-bottom:1rem">
        <div style="font-weight:500; font-size:0.85rem; margin-bottom:0.5rem">Select Rate Plan</div>
        <label style="font-size:0.8rem; display:block; margin-bottom:0.25rem">
          Utility
          <select id="rate-utility-select" class="input-sm" style="width:100%">
            <option value="">Loading utilities...</option>
          </select>
        </label>
        <label style="font-size:0.8rem; display:block; margin-bottom:0.25rem">
          Rate Plan
          <select id="rate-plan-select" class="input-sm" style="width:100%" disabled>
            <option value="">Select a utility first</option>
          </select>
        </label>
        <div id="rate-dialog-error" class="text-muted" style="font-size:0.75rem; color:#ef4444; display:none"></div>
      </div>

      <div style="display:flex; gap:0.5rem; justify-content:flex-end">
        <button type="button" class="btn btn-xs" id="btn-rate-dialog-cancel">Cancel</button>
        <button type="button" class="btn btn-xs btn-primary" id="btn-rate-dialog-use" disabled>Use This Rate</button>
      </div>
    </div>
  </div>

  <!-- Attribution popup -->
  <div id="rate-attribution-overlay" style="display:none; position:fixed; inset:0; background:rgba(0,0,0,0.5); z-index:1001; justify-content:center; align-items:center">
    <div class="card" style="width:min(360px,85vw); margin:2rem">
      <h3 style="margin-top:0">Rate Data Source</h3>
      <div id="rate-attribution-content" style="font-size:0.8rem"></div>
      <div style="text-align:right; margin-top:0.75rem">
        <button type="button" class="btn btn-xs" id="btn-attribution-close">Close</button>
      </div>
    </div>
  </div>
```

- [ ] **Step 2: Add rate plan JavaScript**

Add to the `<script>` section in `modeling_view.html`, after the existing state variables (around line 131):

```javascript
  // -- Rate plan state --
  var currentRateLabel = null;
  var proposedRateLabel = null;
  var rateDialogTarget = 'current';  // 'current' or 'proposed'

  // Rate DOM refs
  var rateSectionEl = document.getElementById('modeling-rate-section');
  var currentDisplayEl = document.getElementById('rate-current-display');
  var proposedDisplayEl = document.getElementById('rate-proposed-display');
  var attributionBtn = document.getElementById('btn-rate-attribution');
  var refreshCurrentBtn = document.getElementById('btn-rate-current-refresh');
  var clearProposedBtn = document.getElementById('btn-rate-proposed-clear');
  var dialogOverlay = document.getElementById('rate-dialog-overlay');
  var attrOverlay = document.getElementById('rate-attribution-overlay');
  var utilitySelect = document.getElementById('rate-utility-select');
  var planSelect = document.getElementById('rate-plan-select');
  var dialogError = document.getElementById('rate-dialog-error');
  var useRateBtn = document.getElementById('btn-rate-dialog-use');

  // -- Rate API helpers --
  function loadOpenEIConfig() {
    fetch('rates/openei-config').then(function(r) { return r.json(); }).then(function(cfg) {
      document.getElementById('rate-api-url').value = cfg.api_url || '';
      document.getElementById('rate-api-key').value = cfg.api_key || '';
    });
  }

  function loadCurrentRate() {
    fetch('rates/current').then(function(r) { return r.json(); }).then(function(data) {
      if (data.label) {
        currentRateLabel = data.label;
        currentDisplayEl.textContent = (data.utility || '') + ' \u2014 ' + (data.name || data.label);
        attributionBtn.style.display = '';
        refreshCurrentBtn.style.display = '';
        document.getElementById('btn-rate-current-configure').textContent = 'Change';
      } else {
        currentRateLabel = null;
        currentDisplayEl.textContent = 'No rate plan selected';
        attributionBtn.style.display = 'none';
        refreshCurrentBtn.style.display = 'none';
        document.getElementById('btn-rate-current-configure').textContent = 'Configure';
      }
    });
  }

  function openRateDialog(target) {
    rateDialogTarget = target;
    dialogError.style.display = 'none';
    dialogOverlay.style.display = 'flex';
    loadOpenEIConfig();
    loadUtilities();
  }

  function closeRateDialog() {
    dialogOverlay.style.display = 'none';
  }

  function loadUtilities() {
    var store = _store_ref();
    if (!store) return;
    var lat = store.lat || 37.7;
    var lon = store.lon || -122.4;
    utilitySelect.innerHTML = '<option value="">Loading...</option>';
    planSelect.innerHTML = '<option value="">Select a utility first</option>';
    planSelect.disabled = true;
    useRateBtn.disabled = true;

    fetch('rates/utilities?lat=' + lat + '&lon=' + lon)
      .then(function(r) { return r.json(); })
      .then(function(data) {
        if (data.error) {
          utilitySelect.innerHTML = '<option value="">Error: ' + data.error + '</option>';
          return;
        }
        utilitySelect.innerHTML = '<option value="">Select a utility...</option>';
        for (var i = 0; i < data.length; i++) {
          var opt = document.createElement('option');
          opt.value = data[i].utility_name;
          opt.textContent = data[i].utility_name;
          utilitySelect.appendChild(opt);
        }
      })
      .catch(function(err) {
        utilitySelect.innerHTML = '<option value="">Error loading utilities</option>';
      });
  }

  function loadRatePlans(utility) {
    planSelect.innerHTML = '<option value="">Loading plans...</option>';
    planSelect.disabled = true;
    useRateBtn.disabled = true;

    fetch('rates/plans?utility=' + encodeURIComponent(utility))
      .then(function(r) { return r.json(); })
      .then(function(data) {
        if (data.error) {
          planSelect.innerHTML = '<option value="">Error: ' + data.error + '</option>';
          return;
        }
        planSelect.innerHTML = '<option value="">Select a rate plan...</option>';
        planSelect.disabled = false;
        for (var i = 0; i < data.length; i++) {
          var opt = document.createElement('option');
          opt.value = data[i].label;
          var dateStr = data[i].startdate ? ' (' + new Date(data[i].startdate * 1000).getFullYear() + ')' : '';
          opt.textContent = data[i].name + dateStr;
          planSelect.appendChild(opt);
        }
      })
      .catch(function(err) {
        planSelect.innerHTML = '<option value="">Error loading plans</option>';
      });
  }

  function useSelectedRate() {
    var label = planSelect.value;
    if (!label) return;
    useRateBtn.disabled = true;
    dialogError.style.display = 'none';

    fetch('rates/fetch', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({label: label}),
    })
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.error) {
        dialogError.textContent = data.error;
        dialogError.style.display = '';
        useRateBtn.disabled = false;
        return;
      }
      if (rateDialogTarget === 'current') {
        fetch('rates/current', {
          method: 'PUT',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({label: label}),
        }).then(function() {
          loadCurrentRate();
          closeRateDialog();
          fetchModelingData(horizonSelect.value);
        });
      } else {
        proposedRateLabel = label;
        proposedDisplayEl.textContent = (data.utility || '') + ' \u2014 ' + (data.name || label);
        clearProposedBtn.style.display = '';
        document.getElementById('btn-rate-proposed-set').textContent = 'Change';
        closeRateDialog();
        fetchModelingData(horizonSelect.value);
      }
    })
    .catch(function(err) {
      dialogError.textContent = 'Network error: ' + err.message;
      dialogError.style.display = '';
      useRateBtn.disabled = false;
    });
  }

  // -- Store ref helper (panel config lat/lon) --
  function _store_ref() {
    // Try to read lat/lon from the panel config form if available
    var latEl = document.querySelector('[name="latitude"]');
    var lonEl = document.querySelector('[name="longitude"]');
    if (latEl && lonEl) {
      return { lat: parseFloat(latEl.value) || 37.7, lon: parseFloat(lonEl.value) || -122.4 };
    }
    return { lat: 37.7, lon: -122.4 };
  }

  // -- Event listeners --
  document.getElementById('btn-rate-current-configure').addEventListener('click', function() {
    openRateDialog('current');
  });
  document.getElementById('btn-rate-proposed-set').addEventListener('click', function() {
    openRateDialog('proposed');
  });
  refreshCurrentBtn.addEventListener('click', function() {
    if (!currentRateLabel) return;
    refreshCurrentBtn.disabled = true;
    fetch('rates/refresh', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({label: currentRateLabel}),
    })
    .then(function(r) { return r.json(); })
    .then(function() {
      refreshCurrentBtn.disabled = false;
      fetchModelingData(horizonSelect.value);
    })
    .catch(function() { refreshCurrentBtn.disabled = false; });
  });
  clearProposedBtn.addEventListener('click', function() {
    proposedRateLabel = null;
    proposedDisplayEl.textContent = 'Using current rate for comparison';
    clearProposedBtn.style.display = 'none';
    document.getElementById('btn-rate-proposed-set').textContent = 'Set Proposed Rate';
    fetchModelingData(horizonSelect.value);
  });
  document.getElementById('btn-rate-dialog-cancel').addEventListener('click', closeRateDialog);
  document.getElementById('btn-rate-save-config').addEventListener('click', function() {
    var url = document.getElementById('rate-api-url').value.trim();
    var key = document.getElementById('rate-api-key').value.trim();
    fetch('rates/openei-config', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({api_url: url, api_key: key}),
    }).then(function() { loadUtilities(); });
  });
  utilitySelect.addEventListener('change', function() {
    if (utilitySelect.value) loadRatePlans(utilitySelect.value);
  });
  planSelect.addEventListener('change', function() {
    useRateBtn.disabled = !planSelect.value;
  });
  document.getElementById('btn-rate-dialog-use').addEventListener('click', useSelectedRate);

  // Attribution popup
  attributionBtn.addEventListener('click', function() {
    if (!currentRateLabel) return;
    fetch('rates/attribution/' + encodeURIComponent(currentRateLabel))
      .then(function(r) { return r.json(); })
      .then(function(data) {
        var html = '<p><strong>Provider:</strong> ' + (data.provider || 'Unknown') + '</p>'
          + '<p><strong>License:</strong> ' + (data.license || 'Unknown') + '</p>'
          + '<p><strong>URDB Label:</strong> ' + currentRateLabel + '</p>'
          + '<p><strong>Retrieved:</strong> ' + (data.retrieved_at || 'Unknown') + '</p>'
          + '<p><a href="' + (data.url || '#') + '" target="_blank" rel="noopener">View on OpenEI</a></p>';
        document.getElementById('rate-attribution-content').innerHTML = html;
        attrOverlay.style.display = 'flex';
      });
  });
  document.getElementById('btn-attribution-close').addEventListener('click', function() {
    attrOverlay.style.display = 'none';
  });
```

- [ ] **Step 3: Update modelingDataQuery to include proposed_rate_label**

Find the `modelingDataQuery` function and update it:

```javascript
  function modelingDataQuery(horizon) {
    var q = 'modeling-data?horizon=' + encodeURIComponent(horizon);
    if (modelingConfigFile) {
      q += '&config=' + encodeURIComponent(modelingConfigFile);
    }
    if (proposedRateLabel) {
      q += '&proposed_rate_label=' + encodeURIComponent(proposedRateLabel);
    }
    return q;
  }
```

- [ ] **Step 4: Show rate section when entering modeling mode**

Find `enterModelingMode` function and add after the existing setup:

```javascript
    // Load rate state
    rateSectionEl.style.display = '';
    loadCurrentRate();
```

Find `exitModelingMode` function and add:

```javascript
    rateSectionEl.style.display = 'none';
    proposedRateLabel = null;
```

- [ ] **Step 5: Commit**

```bash
git add src/span_panel_simulator/dashboard/templates/partials/modeling_view.html
git commit -m "Add rate plan selection UI and OpenEI dialog to modeling view"
```

---

### Task 9: Cost Display on Summary Cards

**Files:**
- Modify: `src/span_panel_simulator/dashboard/templates/partials/modeling_view.html`

- [ ] **Step 1: Add cost columns to summary tables**

Update the Before summary table `<thead>` (around line 46):

```html
      <table class="modeling-summary-table">
        <thead>
          <tr>
            <th></th>
            <th>Energy (kWh)</th>
            <th class="modeling-col-cost" style="display:none">Cost</th>
          </tr>
        </thead>
        <tbody>
          <tr class="modeling-row-horizon">
            <td class="modeling-row-label">Full Horizon</td>
            <td id="modeling-before-horizon" class="modeling-col-before"></td>
            <td id="modeling-before-cost-horizon" class="modeling-col-cost" style="display:none"></td>
          </tr>
          <tr class="modeling-row-visible">
            <td class="modeling-row-label">Visible Range</td>
            <td id="modeling-before-visible" class="modeling-col-before"></td>
            <td id="modeling-before-cost-visible" class="modeling-col-cost" style="display:none"></td>
          </tr>
        </tbody>
      </table>
```

Update the After summary table similarly, adding cost and cost-diff columns:

```html
      <table class="modeling-summary-table">
        <thead>
          <tr>
            <th></th>
            <th>Energy (kWh)</th>
            <th>Difference</th>
            <th class="modeling-col-cost" style="display:none">Cost</th>
            <th class="modeling-col-cost" style="display:none">Savings</th>
          </tr>
        </thead>
        <tbody>
          <tr class="modeling-row-horizon">
            <td class="modeling-row-label">Full Horizon</td>
            <td id="modeling-after-horizon" class="modeling-col-after"></td>
            <td id="modeling-diff-horizon" class="modeling-col-diff"></td>
            <td id="modeling-after-cost-horizon" class="modeling-col-cost" style="display:none"></td>
            <td id="modeling-cost-diff-horizon" class="modeling-col-cost" style="display:none"></td>
          </tr>
          <tr class="modeling-row-visible">
            <td class="modeling-row-label">Visible Range</td>
            <td id="modeling-after-visible" class="modeling-col-after"></td>
            <td id="modeling-diff-visible" class="modeling-col-diff"></td>
            <td id="modeling-after-cost-visible" class="modeling-col-cost" style="display:none"></td>
            <td id="modeling-cost-diff-visible" class="modeling-col-cost" style="display:none"></td>
          </tr>
        </tbody>
      </table>
```

- [ ] **Step 2: Add cost display JavaScript**

Add helper functions and update `renderCharts`:

```javascript
  function formatDollar(amount) {
    return '$' + Math.abs(amount).toFixed(2);
  }

  function populateCostCell(el, costs) {
    if (!costs) { el.textContent = ''; return; }
    el.textContent = formatDollar(costs.import_cost) + ' imp, ' + formatDollar(costs.export_credit)
      + ' exp \u2014 Net: ' + formatDollar(costs.net_cost);
  }

  function populateCostDiffCell(el, beforeCosts, afterCosts) {
    if (!beforeCosts || !afterCosts) { el.textContent = ''; return; }
    var diff = beforeCosts.net_cost - afterCosts.net_cost;
    var pct = beforeCosts.net_cost !== 0 ? (diff / Math.abs(beforeCosts.net_cost) * 100) : 0;
    var sign = diff > 0 ? '\u2212' : '+';
    el.textContent = sign + formatDollar(diff) + ' (' + Math.abs(pct).toFixed(1) + '%)';
    el.className = 'modeling-col-cost ' + (diff > 0 ? 'diff-positive' : diff < 0 ? 'diff-negative' : '');
  }

  function showCostColumns(visible) {
    var els = document.querySelectorAll('.modeling-col-cost');
    for (var i = 0; i < els.length; i++) {
      els[i].style.display = visible ? '' : 'none';
    }
  }
```

In the `renderCharts` function, after the existing energy summary population (around line 288), add:

```javascript
    // Cost summaries — full horizon (from server response)
    var hasCosts = !!d.before_costs;
    showCostColumns(hasCosts);
    if (hasCosts) {
      populateCostCell(document.getElementById('modeling-before-cost-horizon'), d.before_costs);
      populateCostCell(document.getElementById('modeling-after-cost-horizon'), d.after_costs);
      populateCostDiffCell(document.getElementById('modeling-cost-diff-horizon'), d.before_costs, d.after_costs);
      // Visible range costs are not computed server-side (would need per-hour cost arrays).
      // Show full-horizon costs only for now; visible-range cost cells left blank.
      document.getElementById('modeling-before-cost-visible').textContent = '\u2014';
      document.getElementById('modeling-after-cost-visible').textContent = '\u2014';
      document.getElementById('modeling-cost-diff-visible').textContent = '\u2014';
    }
```

- [ ] **Step 3: Commit**

```bash
git add src/span_panel_simulator/dashboard/templates/partials/modeling_view.html
git commit -m "Add cost columns to modeling summary cards"
```

---

## Phase 6: Package Init and Final Integration

### Task 10: Package Re-exports and Final Wiring

**Files:**
- Modify: `src/span_panel_simulator/rates/__init__.py`

- [ ] **Step 1: Update package init with public API**

```python
"""ToU rate integration — OpenEI URDB rate plans and cost calculation."""

from span_panel_simulator.rates.cache import RateCache
from span_panel_simulator.rates.cost_engine import compute_costs
from span_panel_simulator.rates.openei import OpenEIError, fetch_rate_detail, fetch_rate_plans, fetch_utilities
from span_panel_simulator.rates.resolver import resolve_rate
from span_panel_simulator.rates.types import (
    AttributionMeta,
    CostLedger,
    OpenEIConfig,
    RateCacheEntry,
    RatePlanSummary,
    URDBRateTier,
    URDBRecord,
    UtilitySummary,
)

__all__ = [
    "AttributionMeta",
    "CostLedger",
    "OpenEIConfig",
    "OpenEIError",
    "RateCache",
    "RateCacheEntry",
    "RatePlanSummary",
    "URDBRateTier",
    "URDBRecord",
    "UtilitySummary",
    "compute_costs",
    "fetch_rate_detail",
    "fetch_rate_plans",
    "fetch_utilities",
    "resolve_rate",
]
```

- [ ] **Step 2: Run full test suite**

Run: `pytest tests/ -v --timeout=30`
Expected: All tests PASS

- [ ] **Step 3: Run type checker**

Run: `mypy src/span_panel_simulator/rates/`
Expected: No errors

- [ ] **Step 4: Commit**

```bash
git add src/span_panel_simulator/rates/__init__.py
git commit -m "Finalize rates package public API"
```

---

## Summary

| Phase | Tasks | What it delivers |
|-------|-------|-----------------|
| 1. Rate Types and Resolution | 1-3 | Types, resolver, cost engine — all testable in isolation |
| 2. Cache and Client | 4-5 | Persistent rate cache + OpenEI API client |
| 3. API Endpoints | 6 | HTTP routes for rate management |
| 4. Engine Integration | 7 | Cost data in modeling response |
| 5. UI | 8-9 | Rate selection dialog + cost display on summary cards |
| 6. Final | 10 | Package init, type checking, integration test |
