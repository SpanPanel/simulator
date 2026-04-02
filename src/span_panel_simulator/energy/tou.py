"""Rate-aware TOU dispatch resolution for BESS.

Resolves charge/discharge/idle state at each tick by consulting the
URDB rate record directly.  Replaces the static hour-list approach
with real-time rate-period classification.

Classification rules (mirrors real BESS TOU behaviour):
- **Off-peak** (current rate == day's minimum): charge from grid/PV
- **Peak** (current rate == day's maximum): discharge to cover load
- **Partial-peak** (between min and max): self-consumption only
- **Flat rate** (single period): self-consumption fallback
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class TouDispatch:
    """Dispatch decision for a single simulation tick."""

    state: str  # "charging", "discharging", "idle"
    requested_power_w: float
    reason: str


def resolve_tou_dispatch(
    ts: float,
    tz: ZoneInfo,
    rate_record: dict[str, Any],
    soe_pct: float,
    backup_reserve_pct: float,
    max_charge_w: float,
    max_discharge_w: float,
    pv_excess_w: float,
    load_deficit_w: float,
) -> TouDispatch:
    """Resolve BESS dispatch from the rate schedule at timestamp *ts*."""
    dt = datetime.fromtimestamp(ts, tz=tz)
    current_rate = _rate_at(dt, rate_record)
    day_rates = all_rates_for_day(dt, rate_record)

    if not day_rates:
        return TouDispatch("idle", 0.0, "no_rate_data")

    min_rate = min(day_rates.values())
    max_rate = max(day_rates.values())

    if min_rate == max_rate:
        # Flat rate — no TOU arbitrage value, fall back to self-consumption
        return _self_consumption_dispatch(
            pv_excess_w,
            load_deficit_w,
            soe_pct,
            backup_reserve_pct,
            max_charge_w,
            max_discharge_w,
        )

    if current_rate <= min_rate:
        return _off_peak_dispatch(pv_excess_w, soe_pct, max_charge_w)

    if current_rate >= max_rate:
        return _peak_dispatch(load_deficit_w, soe_pct, backup_reserve_pct, max_discharge_w)

    # Partial-peak — self-consumption only (no grid charge, no grid export)
    return _self_consumption_dispatch(
        pv_excess_w,
        load_deficit_w,
        soe_pct,
        backup_reserve_pct,
        max_charge_w,
        max_discharge_w,
    )


# ------------------------------------------------------------------
# Rate lookup helpers
# ------------------------------------------------------------------


def _rate_at(dt: datetime, record: dict[str, Any]) -> float:
    """Look up the energy rate for a specific datetime."""
    month_idx = dt.month - 1
    hour = dt.hour
    is_weekend = dt.weekday() >= 5

    schedule = record.get("energyweekendschedule" if is_weekend else "energyweekdayschedule", [])
    if not schedule or month_idx >= len(schedule):
        return 0.0

    row = schedule[month_idx]
    period_idx = row[hour] if hour < len(row) else 0

    rate_structure = record.get("energyratestructure", [])
    if not rate_structure or period_idx >= len(rate_structure):
        return 0.0

    tiers = rate_structure[period_idx]
    return float(tiers[0].get("rate", 0.0)) if tiers else 0.0


def all_rates_for_day(dt: datetime, record: dict[str, Any]) -> dict[int, float]:
    """Return {hour: rate} for every hour of the given day-type and month."""
    month_idx = dt.month - 1
    is_weekend = dt.weekday() >= 5

    schedule = record.get("energyweekendschedule" if is_weekend else "energyweekdayschedule", [])
    if not schedule or month_idx >= len(schedule):
        return {}

    row = schedule[month_idx]
    rate_structure = record.get("energyratestructure", [])
    if not rate_structure:
        return {}

    rates: dict[int, float] = {}
    for hour in range(24):
        period_idx = row[hour] if hour < len(row) else 0
        if period_idx < len(rate_structure):
            tiers = rate_structure[period_idx]
            rates[hour] = float(tiers[0].get("rate", 0.0)) if tiers else 0.0
        else:
            rates[hour] = 0.0
    return rates


# ------------------------------------------------------------------
# Dispatch strategy functions
# ------------------------------------------------------------------


def _off_peak_dispatch(
    pv_excess_w: float,
    soe_pct: float,
    max_charge_w: float,
) -> TouDispatch:
    """Off-peak: charge from PV excess first, then grid."""
    if soe_pct >= 100.0:
        return TouDispatch("idle", 0.0, "offpeak_full")
    if pv_excess_w > 0:
        power = min(pv_excess_w, max_charge_w)
        return TouDispatch("charging", power, "offpeak_solar_charge")
    return TouDispatch("charging", max_charge_w, "offpeak_grid_charge")


def _peak_dispatch(
    load_deficit_w: float,
    soe_pct: float,
    backup_reserve_pct: float,
    max_discharge_w: float,
) -> TouDispatch:
    """Peak: discharge to cover load (GFE limits actual output on bus)."""
    if soe_pct <= backup_reserve_pct:
        return TouDispatch("idle", 0.0, "peak_reserve_hold")
    if load_deficit_w <= 0:
        return TouDispatch("idle", 0.0, "peak_no_deficit")
    return TouDispatch("discharging", max_discharge_w, "peak_discharge")


def _self_consumption_dispatch(
    pv_excess_w: float,
    load_deficit_w: float,
    soe_pct: float,
    backup_reserve_pct: float,
    max_charge_w: float,
    max_discharge_w: float,
) -> TouDispatch:
    """Self-consumption: discharge for deficit, charge from PV excess."""
    if load_deficit_w > 0 and soe_pct > backup_reserve_pct:
        return TouDispatch("discharging", max_discharge_w, "selfcon_discharge")
    if pv_excess_w > 0 and soe_pct < 100.0:
        power = min(pv_excess_w, max_charge_w)
        return TouDispatch("charging", power, "selfcon_solar_charge")
    return TouDispatch("idle", 0.0, "selfcon_idle")
