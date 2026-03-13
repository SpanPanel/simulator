"""Seasonal solar curve computation.

Simplified astronomical model that produces a 24-hour production
multiplier dict keyed by hour (0-23), normalised so peak = 1.0.
"""

from __future__ import annotations

import math

DEFAULT_LATITUDE = 37.7  # US average residential


def compute_solar_curve(
    month: int,
    day: int,
    latitude: float = DEFAULT_LATITUDE,
) -> dict[int, float]:
    """Return hourly solar production multipliers for the given date.

    Args:
        month: Calendar month (1-12).
        day: Day of month (1-31).
        latitude: Observer latitude in degrees north.

    Returns:
        Dict mapping hour (0-23) to a production factor in [0.0, 1.0].
    """
    doy = _day_of_year(month, day)

    lat_rad = math.radians(latitude)
    declination = math.radians(23.45 * math.sin(math.radians(360.0 * (284 + doy) / 365.0)))

    raw: dict[int, float] = {}
    for hour in range(24):
        hour_angle = math.radians(15.0 * (hour - 12))
        sin_elevation = (
            math.sin(lat_rad) * math.sin(declination)
            + math.cos(lat_rad) * math.cos(declination) * math.cos(hour_angle)
        )
        if sin_elevation <= 0:
            raw[hour] = 0.0
        else:
            raw[hour] = sin_elevation**1.2

    peak = max(raw.values()) if raw else 1.0
    if peak <= 0:
        return {h: 0.0 for h in range(24)}

    return {h: round(v / peak, 4) for h, v in raw.items()}


def _day_of_year(month: int, day: int) -> int:
    """Convert month + day to day-of-year (1-366)."""
    days_in_month = [0, 31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    return sum(days_in_month[:month]) + day
