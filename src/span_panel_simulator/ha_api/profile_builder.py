"""Profile builder — compute usage profiles from power statistics.

Accepts any :class:`~span_panel_simulator.history.HistoryProvider` backend
(HA recorder, future eBus history, or a no-op stub) and computes per-circuit
profiles: ``typical_power``, ``hour_factors``, ``duty_cycle``,
``monthly_factors``.

The output format matches what ``profile_applicator.py`` expects, so the
downstream pipeline is unchanged regardless of the history source.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from span_panel_simulator.ha_api.manifest import CircuitManifestEntry
    from span_panel_simulator.history import HistoryProvider

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
    active_days: list[int]  # weekdays active (0=Mon..6=Sun); empty = all


async def build_profiles(
    history: HistoryProvider,
    entries: list[CircuitManifestEntry],
    entity_to_template: dict[str, str],
    *,
    time_zone: str | None = None,
) -> dict[str, dict[str, object]]:
    """Build usage profiles from manifest entries.

    Queries a :class:`~span_panel_simulator.history.HistoryProvider` for
    each circuit's power entity and computes the profile fields that
    ``profile_applicator`` consumes.  The provider can be backed by the
    HA recorder, a future eBus history source, or a no-op stub.

    When *time_zone* is provided (IANA name like ``"America/Los_Angeles"``),
    hourly statistics are bucketed by local hour-of-day rather than UTC.
    This is critical for correct time-of-day profiles — without it, a
    UTC-8 panel's 7 PM evening peak would appear at hour 3 AM.

    Output is keyed by **template name** (via *entity_to_template*),
    eliminating the fragile slug-based remapping step.

    Returns a dict keyed by template name::

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
    power_ids = [e.entity_id for e in entries]

    if not power_ids:
        _LOGGER.warning("No power entities to query — returning empty profiles")
        return {}

    local_tz: ZoneInfo | None = None
    if time_zone:
        try:
            local_tz = ZoneInfo(time_zone)
            _LOGGER.debug("Profile builder using timezone: %s", time_zone)
        except (KeyError, ValueError):
            _LOGGER.warning("Unknown timezone %r — falling back to UTC", time_zone)

    now = datetime.now(UTC)

    hourly_start = (now - timedelta(days=_HOURLY_LOOKBACK_DAYS)).isoformat()
    monthly_start = (now - timedelta(days=_MONTHLY_LOOKBACK_DAYS)).isoformat()

    hourly_stats = await history.async_get_statistics(
        statistic_ids=power_ids,
        period="hour",
        start_time=hourly_start,
    )

    monthly_stats = await history.async_get_statistics(
        statistic_ids=power_ids,
        period="month",
        start_time=monthly_start,
    )

    profiles: dict[str, dict[str, object]] = {}

    for entry in entries:
        hourly = hourly_stats.get(entry.entity_id, [])
        monthly = monthly_stats.get(entry.entity_id, [])

        profile = _compute_profile(hourly, monthly, local_tz=local_tz)
        if profile is None:
            continue

        template_name = entity_to_template.get(entry.entity_id)
        if template_name is None:
            _LOGGER.debug("No template mapping for entity %s", entry.entity_id)
            continue

        profile_dict: dict[str, object] = {
            "typical_power": profile.typical_power,
            "power_variation": profile.power_variation,
            "hour_factors": profile.hour_factors,
            "duty_cycle": profile.duty_cycle,
            "monthly_factors": profile.monthly_factors,
        }
        if profile.active_days:
            profile_dict["active_days"] = profile.active_days
        profiles[template_name] = profile_dict
        _LOGGER.debug(
            "Profile for %s (%s): typical=%.1fW, duty=%.2f",
            template_name,
            entry.entity_id,
            profile.typical_power,
            profile.duty_cycle,
        )

    _LOGGER.info("Built profiles for %d/%d circuits", len(profiles), len(entries))
    return profiles


def _compute_profile(
    hourly_stats: list[dict[str, object]],
    monthly_stats: list[dict[str, object]],
    *,
    local_tz: ZoneInfo | None = None,
) -> CircuitProfile | None:
    """Derive a circuit profile from recorder statistics.

    Args:
        hourly_stats: Hourly statistic records with ``start``, ``mean``,
            ``min``, ``max`` fields.
        monthly_stats: Monthly statistic records with ``mean``, ``min``,
            ``max`` fields.
        local_tz: When provided, timestamps are converted to this timezone
            before bucketing by hour-of-day or month.

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
    hour_factors = _compute_hour_factors(hourly_stats, local_tz=local_tz)

    # ------------------------------------------------------------------
    # duty_cycle: mean / max ratio across all observations
    # ------------------------------------------------------------------
    maxes = [v for s in hourly_stats if (v := _float_val(s, "max")) is not None and v > 0]
    overall_max = max(maxes) if maxes else typical_power
    duty_cycle = min(typical_power / overall_max, 1.0) if overall_max > 0 else 1.0

    # ------------------------------------------------------------------
    # monthly_factors: mean power per calendar month, normalised to peak
    # ------------------------------------------------------------------
    monthly_factors = _compute_monthly_factors(monthly_stats, local_tz=local_tz)

    # ------------------------------------------------------------------
    # active_days: detect which weekdays the circuit is actually used
    # ------------------------------------------------------------------
    active_days = _compute_active_days(hourly_stats, local_tz=local_tz)

    return CircuitProfile(
        typical_power=round(typical_power, 1),
        power_variation=round(power_variation, 3),
        hour_factors=hour_factors,
        duty_cycle=round(duty_cycle, 3),
        monthly_factors=monthly_factors,
        active_days=active_days,
    )


def _compute_hour_factors(
    hourly_stats: list[dict[str, object]],
    *,
    local_tz: ZoneInfo | None = None,
) -> dict[int, float]:
    """Compute normalised hourly load shape from hourly statistics.

    Groups statistic records by hour-of-day, averages the ``mean`` within
    each group, and normalises so the peak hour is 1.0.

    When *local_tz* is provided, UTC timestamps are converted to local
    time before bucketing — essential for correct time-of-day profiles.
    """
    hour_sums: dict[int, float] = {}
    hour_counts: dict[int, int] = {}

    for stat in hourly_stats:
        mean_val = _float_val(stat, "mean")
        dt = _parse_start_timestamp(stat)
        if mean_val is None or dt is None:
            continue

        if local_tz is not None:
            dt = dt.astimezone(local_tz)
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


def _compute_monthly_factors(
    monthly_stats: list[dict[str, object]],
    *,
    local_tz: ZoneInfo | None = None,
) -> dict[int, float]:
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

        if local_tz is not None:
            dt = dt.astimezone(local_tz)
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


def _compute_active_days(
    hourly_stats: list[dict[str, object]],
    *,
    local_tz: ZoneInfo | None = None,
) -> list[int]:
    """Detect which weekdays a circuit is actively used.

    Groups hourly statistics by ``weekday()`` (0=Mon..6=Sun), computes
    mean power per weekday, and considers a day "active" if its mean
    exceeds 10% of the peak weekday mean.

    Returns an empty list when all 7 days are active or when there are
    fewer than 14 data points (not enough to draw conclusions).
    """
    day_sums: dict[int, float] = {}
    day_counts: dict[int, int] = {}

    for stat in hourly_stats:
        mean_val = _float_val(stat, "mean")
        dt = _parse_start_timestamp(stat)
        if mean_val is None or dt is None:
            continue

        if local_tz is not None:
            dt = dt.astimezone(local_tz)
        wd = dt.weekday()
        day_sums[wd] = day_sums.get(wd, 0.0) + abs(mean_val)
        day_counts[wd] = day_counts.get(wd, 0) + 1

    total_points = sum(day_counts.values())
    if total_points < 14:
        return []

    if not day_sums:
        return []

    day_avgs = {wd: day_sums[wd] / day_counts[wd] for wd in day_sums}
    peak = max(day_avgs.values())
    if peak <= 0:
        return []

    threshold = peak * 0.1
    active = sorted(wd for wd, avg in day_avgs.items() if avg >= threshold)

    if len(active) >= 7:
        return []

    return active


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
