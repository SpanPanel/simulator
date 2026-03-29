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
    months_seen: set[tuple[int, int]] = set()

    for ts, pwr in zip(timestamps, power_kw, strict=False):
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
    return float(period_tiers[0].get("rate", 0.0))
