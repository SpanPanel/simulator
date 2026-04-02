"""Resolve import/export rates from URDB schedule matrices.

Given a UNIX timestamp and timezone, looks up the rate for that hour
using the URDB energyweekday/weekendschedule (12x24 month x hour
matrices) and energyratestructure (period -> tier list).

Uses tier 1 (index 0) only in v1.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo


def derive_bess_tou_schedule(
    record: dict[str, Any],
    month: int = 6,
) -> dict[int, str]:
    """Derive a 24-hour BESS schedule from URDB rate periods.

    Examines the weekday energy schedule for the given month to find
    which hours are cheapest (charge) and most expensive (discharge).
    Hours at intermediate rates are set to idle.

    Parameters
    ----------
    record:
        URDB record dict containing energyratestructure and schedule matrices.
    month:
        1-based month to derive the schedule for (default: June).

    Returns
    -------
    dict mapping hour (0-23) to ``"charge"``, ``"discharge"``, or ``"idle"``.
    """
    month_idx = month - 1
    schedule = record.get("energyweekdayschedule", [])
    rate_structure = record.get("energyratestructure", [])

    if not schedule or not rate_structure:
        return {h: "idle" for h in range(24)}

    if month_idx >= len(schedule):
        month_idx = 0

    # Resolve the rate for each hour
    hour_rates: dict[int, float] = {}
    for hour in range(24):
        period_idx = schedule[month_idx][hour] if hour < len(schedule[month_idx]) else 0
        rate = 0.0
        if period_idx < len(rate_structure):
            tiers = rate_structure[period_idx]
            if tiers:
                rate = tiers[0].get("rate", 0.0)
        hour_rates[hour] = rate

    if not hour_rates:
        return {h: "idle" for h in range(24)}

    # Identify distinct rate levels
    distinct_rates = sorted(set(hour_rates.values()))

    if len(distinct_rates) <= 1:
        # Flat rate — no TOU benefit
        return {h: "idle" for h in range(24)}

    min_rate = distinct_rates[0]
    max_rate = distinct_rates[-1]

    result: dict[int, str] = {}
    for hour in range(24):
        rate = hour_rates[hour]
        if rate == min_rate:
            result[hour] = "charge"
        elif rate == max_rate:
            result[hour] = "discharge"
        else:
            result[hour] = "idle"

    return result


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
    month_idx = dt.month - 1  # 0-based (Jan=0)
    hour_idx = dt.hour  # 0-based (0-23)

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
