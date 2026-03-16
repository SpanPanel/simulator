"""Profile builder — compute usage profiles from HA recorder statistics.

This is the logic that previously lived in the HA integration.  The
integration would query ``recorder/statistics_during_period``, compute
per-circuit profiles (``typical_power``, ``hour_factors``, ``duty_cycle``,
``monthly_factors``), and push them to the simulator via Socket.IO.

Now the add-on queries the recorder directly and computes profiles itself.
The output format matches what ``profile_applicator.py`` expects, so the
downstream pipeline is unchanged.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from span_panel_simulator.ha_api.client import HAClient
    from span_panel_simulator.ha_api.entity_discovery import CircuitEntityMapping

_LOGGER = logging.getLogger(__name__)

# Default lookback windows for statistics queries.
_HOURLY_LOOKBACK_DAYS = 30
_MONTHLY_LOOKBACK_DAYS = 365


def _float_val(stat: dict[str, object], key: str) -> float | None:
    """Safely extract a numeric value from a stat dict entry."""
    val = stat.get(key)
    if isinstance(val, int | float):
        return float(val)
    return None


def _parse_start_timestamp(stat: dict[str, object]) -> datetime | None:
    """Parse the ``start`` field from a recorder statistic entry.

    HA's WebSocket API returns ``start`` as a Unix timestamp in
    milliseconds (integer).  The REST API (if ever used) may return
    ISO 8601 strings.  Handle both.
    """
    start = stat.get("start")
    if start is None:
        return None
    try:
        if isinstance(start, int | float):
            ts = float(start)
            # Heuristic: timestamps > 1e12 are milliseconds
            if ts > 1e12:
                ts /= 1000.0
            return datetime.fromtimestamp(ts, tz=UTC)
        if isinstance(start, str):
            return datetime.fromisoformat(start)
    except (ValueError, OSError, OverflowError):
        pass
    return None


@dataclass(frozen=True, slots=True)
class CircuitProfile:
    """Computed usage profile for a single circuit.

    Fields match the keys expected by ``profile_applicator.apply_usage_profiles``.
    """

    typical_power: float  # watts — mean power over the observation window
    power_variation: float  # coefficient of variation (std/mean), 0.0-1.0
    hour_factors: dict[int, float]  # hour (0-23) -> normalised factor (peak = 1.0)
    duty_cycle: float  # mean/max ratio, 0.0-1.0
    monthly_factors: dict[int, float]  # month (1-12) -> normalised factor (peak = 1.0)


async def build_profiles(
    client: HAClient,
    mappings: list[CircuitEntityMapping],
) -> dict[str, dict[str, object]]:
    """Build usage profiles for all mapped circuits.

    Queries HA recorder statistics for each circuit's power entity and
    computes the profile fields that ``profile_applicator`` consumes.

    Returns a dict keyed by template name, matching the shape expected
    by ``apply_usage_profiles``::

        {
            "clone_1": {
                "typical_power": 145.3,
                "power_variation": 0.45,
                "hour_factors": {0: 0.15, 1: 0.12, ...},
                "duty_cycle": 0.4,
                "monthly_factors": {1: 0.6, 2: 0.65, ...},
            },
            ...
        }
    """
    power_ids = [m.power_entity_id for m in mappings if m.power_entity_id is not None]

    if not power_ids:
        _LOGGER.warning("No power entities to query — returning empty profiles")
        return {}

    now = datetime.now(UTC)

    # Fetch hourly stats (30 days) and monthly stats (12 months) in parallel
    # by issuing both queries.  The recorder endpoint accepts ISO timestamps.
    hourly_start = (now - timedelta(days=_HOURLY_LOOKBACK_DAYS)).isoformat()
    monthly_start = (now - timedelta(days=_MONTHLY_LOOKBACK_DAYS)).isoformat()

    hourly_stats = await client.async_get_statistics(
        statistic_ids=power_ids,
        period="hour",
        start_time=hourly_start,
    )

    monthly_stats = await client.async_get_statistics(
        statistic_ids=power_ids,
        period="month",
        start_time=monthly_start,
    )

    # Build a profile for each mapping
    profiles: dict[str, dict[str, object]] = {}

    for mapping in mappings:
        if mapping.power_entity_id is None:
            continue

        hourly = hourly_stats.get(mapping.power_entity_id, [])
        monthly = monthly_stats.get(mapping.power_entity_id, [])

        profile = _compute_profile(hourly, monthly)
        if profile is not None:
            profiles[mapping.circuit_key] = {
                "typical_power": profile.typical_power,
                "power_variation": profile.power_variation,
                "hour_factors": profile.hour_factors,
                "duty_cycle": profile.duty_cycle,
                "monthly_factors": profile.monthly_factors,
            }
            _LOGGER.debug(
                "Profile for %s (%s): typical=%.1fW, duty=%.2f",
                mapping.circuit_key,
                mapping.circuit_name,
                profile.typical_power,
                profile.duty_cycle,
            )

    _LOGGER.info("Built profiles for %d/%d circuits", len(profiles), len(mappings))
    return profiles


def _compute_profile(
    hourly_stats: list[dict[str, object]],
    monthly_stats: list[dict[str, object]],
) -> CircuitProfile | None:
    """Derive a circuit profile from recorder statistics.

    Args:
        hourly_stats: Hourly statistic records with ``start``, ``mean``,
            ``min``, ``max`` fields.
        monthly_stats: Monthly statistic records with ``mean``, ``min``,
            ``max`` fields.

    Returns ``None`` if insufficient data.
    """
    if not hourly_stats:
        return None

    # ------------------------------------------------------------------
    # typical_power: overall mean across all hourly observations
    # ------------------------------------------------------------------
    means = [v for s in hourly_stats if (v := _float_val(s, "mean")) is not None]
    if not means:
        return None

    typical_power = sum(means) / len(means)
    if typical_power <= 0:
        return None

    # ------------------------------------------------------------------
    # power_variation: coefficient of variation (std / mean)
    # ------------------------------------------------------------------
    variance = sum((m - typical_power) ** 2 for m in means) / len(means)
    std_dev = variance**0.5
    power_variation = min(std_dev / typical_power, 1.0) if typical_power > 0 else 0.0

    # ------------------------------------------------------------------
    # hour_factors: mean power per hour-of-day, normalised to peak = 1.0
    # ------------------------------------------------------------------
    hour_factors = _compute_hour_factors(hourly_stats)

    # ------------------------------------------------------------------
    # duty_cycle: mean / max ratio across all observations
    # ------------------------------------------------------------------
    maxes = [v for s in hourly_stats if (v := _float_val(s, "max")) is not None and v > 0]
    overall_max = max(maxes) if maxes else typical_power
    duty_cycle = min(typical_power / overall_max, 1.0) if overall_max > 0 else 1.0

    # ------------------------------------------------------------------
    # monthly_factors: mean power per calendar month, normalised to peak
    # ------------------------------------------------------------------
    monthly_factors = _compute_monthly_factors(monthly_stats)

    return CircuitProfile(
        typical_power=round(typical_power, 1),
        power_variation=round(power_variation, 3),
        hour_factors=hour_factors,
        duty_cycle=round(duty_cycle, 3),
        monthly_factors=monthly_factors,
    )


def _compute_hour_factors(hourly_stats: list[dict[str, object]]) -> dict[int, float]:
    """Compute normalised hourly load shape from hourly statistics.

    Groups statistic records by hour-of-day, averages the ``mean`` within
    each group, and normalises so the peak hour is 1.0.
    """
    hour_sums: dict[int, float] = {}
    hour_counts: dict[int, int] = {}

    for stat in hourly_stats:
        mean_val = _float_val(stat, "mean")
        dt = _parse_start_timestamp(stat)
        if mean_val is None or dt is None:
            continue

        hour = dt.hour
        hour_sums[hour] = hour_sums.get(hour, 0.0) + abs(mean_val)
        hour_counts[hour] = hour_counts.get(hour, 0) + 1

    if not hour_sums:
        return {h: 1.0 for h in range(24)}

    hour_avgs = {h: hour_sums[h] / hour_counts[h] for h in hour_sums}

    # Fill any missing hours by interpolating from neighbors (circular).
    if 0 < len(hour_avgs) < 24:
        hour_avgs = _interpolate_hourly_gaps(hour_avgs)

    peak = max(hour_avgs.values()) if hour_avgs else 1.0
    if peak <= 0:
        return {h: 1.0 for h in range(24)}

    return {h: round(hour_avgs.get(h, 0.0) / peak, 3) for h in range(24)}


def _compute_monthly_factors(monthly_stats: list[dict[str, object]]) -> dict[int, float]:
    """Compute normalised monthly load shape from monthly statistics.

    Groups by calendar month, averages the ``mean``, and normalises so
    the peak month is 1.0.
    """
    month_sums: dict[int, float] = {}
    month_counts: dict[int, int] = {}

    for stat in monthly_stats:
        mean_val = _float_val(stat, "mean")
        dt = _parse_start_timestamp(stat)
        if mean_val is None or dt is None:
            continue

        month = dt.month
        month_sums[month] = month_sums.get(month, 0.0) + abs(mean_val)
        month_counts[month] = month_counts.get(month, 0) + 1

    if not month_sums:
        return {m: 1.0 for m in range(1, 13)}

    month_avgs = {m: month_sums[m] / month_counts[m] for m in month_sums}

    # Fill gaps by circular interpolation from neighboring months.
    # Months with no observations inherit from the nearest months
    # that do have data, wrapping around Dec→Jan.
    month_avgs = _interpolate_monthly_gaps(month_avgs)

    peak = max(month_avgs.values()) if month_avgs else 1.0
    if peak <= 0:
        return {m: 1.0 for m in range(1, 13)}

    return {m: round(month_avgs.get(m, 0.0) / peak, 3) for m in range(1, 13)}


def _interpolate_hourly_gaps(hour_avgs: dict[int, float]) -> dict[int, float]:
    """Fill missing hours by linearly interpolating from neighbors (circular 0-23)."""
    observed = sorted(hour_avgs)
    if len(observed) >= 24:
        return hour_avgs

    if len(observed) <= 1:
        val = next(iter(hour_avgs.values())) if hour_avgs else 0.0
        return {h: val for h in range(24)}

    filled = dict(hour_avgs)
    for h in range(24):
        if h in filled:
            continue
        before_h, before_val = _nearest_in_cycle(h, hour_avgs, 24, -1)
        after_h, after_val = _nearest_in_cycle(h, hour_avgs, 24, 1)
        dist_before = (h - before_h) % 24 or 24
        dist_after = (after_h - h) % 24 or 24
        total = dist_before + dist_after
        filled[h] = (after_val * dist_before + before_val * dist_after) / total
    return filled


def _interpolate_monthly_gaps(month_avgs: dict[int, float]) -> dict[int, float]:
    """Fill missing months by linearly interpolating from neighbors.

    Months wrap circularly (Dec neighbors Jan).  If only one month has
    data, all months get that value.  If no months have data the caller
    handles it.
    """
    observed = sorted(month_avgs)  # months with data (1-based)
    if len(observed) >= 12:
        return month_avgs  # no gaps

    if len(observed) <= 1:
        # Single observation — use it everywhere
        val = next(iter(month_avgs.values())) if month_avgs else 0.0
        return {m: val for m in range(1, 13)}

    filled = dict(month_avgs)

    for m in range(1, 13):
        if m in filled:
            continue

        # Find nearest observed month before and after (circularly)
        before_m, before_val = _nearest_in_cycle(m, month_avgs, 12, -1, offset=1)
        after_m, after_val = _nearest_in_cycle(m, month_avgs, 12, 1, offset=1)

        # Circular distance
        dist_before = (m - before_m) % 12 or 12
        dist_after = (after_m - m) % 12 or 12
        total = dist_before + dist_after

        # Linear interpolation weighted by distance
        filled[m] = (after_val * dist_before + before_val * dist_after) / total

    return filled


def _nearest_in_cycle(
    pos: int,
    values: dict[int, float],
    cycle_len: int,
    direction: int,
    *,
    offset: int = 0,
) -> tuple[int, float]:
    """Find the nearest observed position in a circular sequence.

    Args:
        pos: Current position to search from.
        values: Observed positions and their values.
        cycle_len: Length of the cycle (24 for hours, 12 for months).
        direction: -1 for backward, 1 for forward.
        offset: Starting value of the cycle (0 for hours, 1 for months).
    """
    for step in range(1, cycle_len + 1):
        candidate = ((pos - offset + direction * step) % cycle_len) + offset
        if candidate in values:
            return candidate, values[candidate]
    return pos, 0.0
