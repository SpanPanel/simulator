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
