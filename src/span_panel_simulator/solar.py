"""Solar production model: geographic sine curve + weather degradation.

Provides a single authoritative solar model used by both the simulation
engine and the configuration dashboard.  The curve is parameterised by
latitude, longitude, and timestamp — no hourly lookup tables required.

The ``daily_weather_factor`` function generates deterministic, smooth
multi-day weather patterns from a seed, suitable for reproducible
simulation without an external weather API.
"""

from __future__ import annotations

import math

DEFAULT_LATITUDE = 37.7  # San Francisco
DEFAULT_LONGITUDE = -122.4


def _day_of_year(month: int, day: int) -> int:
    """Convert month + day to day-of-year (1-366)."""
    days_in_month = [0, 31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    return sum(days_in_month[:month]) + day


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
        sin_elevation = math.sin(lat_rad) * math.sin(declination) + math.cos(lat_rad) * math.cos(
            declination
        ) * math.cos(hour_angle)
        if sin_elevation <= 0:
            raw[hour] = 0.0
        else:
            raw[hour] = sin_elevation**1.2

    peak = max(raw.values()) if raw else 1.0
    if peak <= 0:
        return {h: 0.0 for h in range(24)}

    return {h: round(v / peak, 4) for h, v in raw.items()}


def solar_production_factor(
    timestamp: float,
    latitude: float = DEFAULT_LATITUDE,
    longitude: float = DEFAULT_LONGITUDE,
) -> float:
    """Return instantaneous solar production factor [0.0, 1.0].

    Uses solar declination and hour angle derived from the Unix
    *timestamp*.  Longitude shifts the hour angle so that solar noon
    aligns with local noon.  The result is ``sin(elevation)^1.2``
    normalised so that the day's peak equals 1.0.

    Args:
        timestamp: Unix epoch seconds (UTC).
        latitude: Observer latitude in degrees north.
        longitude: Observer longitude in degrees east (negative for west).

    Returns:
        Production factor in ``[0.0, 1.0]``.
    """
    # Seconds since midnight UTC, shifted by longitude to get local solar time
    seconds_of_day = timestamp % 86400
    solar_offset_seconds = (longitude / 360.0) * 86400
    local_solar_seconds = (seconds_of_day + solar_offset_seconds) % 86400
    solar_hour = local_solar_seconds / 3600.0

    # Day of year from timestamp
    days_since_epoch = timestamp / 86400.0
    # Jan 1 1970 was day-of-year 1 — approximate doy
    doy = int(days_since_epoch % 365.25) + 1
    if doy > 365:
        doy -= 365

    lat_rad = math.radians(latitude)
    declination = math.radians(23.45 * math.sin(math.radians(360.0 * (284 + doy) / 365.0)))

    # Hour angle: 0 at solar noon (hour 12)
    hour_angle = math.radians(15.0 * (solar_hour - 12.0))
    sin_elevation = math.sin(lat_rad) * math.sin(declination) + math.cos(lat_rad) * math.cos(
        declination
    ) * math.cos(hour_angle)

    if sin_elevation <= 0:
        return 0.0

    # Compute the day's peak for normalisation.
    # Peak occurs at solar noon (hour_angle=0).
    sin_peak = math.sin(lat_rad) * math.sin(declination) + math.cos(lat_rad) * math.cos(
        declination
    )
    if sin_peak <= 0:
        return 0.0

    raw: float = float(sin_elevation**1.2) / float(sin_peak**1.2)
    return float(min(1.0, max(0.0, raw)))


def daily_weather_factor(
    timestamp: float,
    *,
    seed: int = 0,
    monthly_factors: dict[int, float] | None = None,
) -> float:
    """Return a weather degradation multiplier [0.3, 1.0] for the given day.

    When *monthly_factors* is provided (from historical cloud cover
    data), the base degradation comes from the month's average with
    day-to-day noise layered on top.  Otherwise falls back to a purely
    deterministic model.

    Args:
        timestamp: Unix epoch seconds (UTC).
        seed: Panel-specific seed for reproducibility.
        monthly_factors: Optional month (1-12) → factor mapping from
            historical weather data.  Overrides the seasonal bias.

    Returns:
        Degradation factor in ``[0.3, 1.0]``.
    """
    days_since_epoch = int(timestamp / 86400)
    doy = int((timestamp / 86400) % 365.25) + 1
    if doy > 365:
        doy -= 365

    # Day-to-day noise via anchor interpolation
    anchor_days = 5
    anchor_index = days_since_epoch // anchor_days
    frac = (days_since_epoch % anchor_days) / anchor_days

    val_a = _anchor_value(anchor_index, seed)
    val_b = _anchor_value(anchor_index + 1, seed)

    t = (1.0 - math.cos(frac * math.pi)) / 2.0
    noise = val_a * (1.0 - t) + val_b * t  # 0.0-1.0

    if monthly_factors:
        # Derive month from day-of-year
        month = _month_from_doy(doy)
        base_factor = monthly_factors.get(month, 0.75)
        # Layer ±15% noise around the monthly average
        noise_offset = (noise - 0.5) * 0.3  # ±0.15
        result = base_factor + noise_offset
    else:
        # Deterministic seasonal model (original behaviour)
        seasonal = 0.5 + 0.5 * math.cos(math.radians((doy - 172) * (360.0 / 365.0)))
        combined = noise * 0.8 + seasonal * 0.2
        result = 0.3 + combined * 0.7

    return min(1.0, max(0.3, result))


def _month_from_doy(doy: int) -> int:
    """Approximate calendar month (1-12) from day-of-year."""
    days_in_month = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    cumulative = 0
    for month_idx, days in enumerate(days_in_month):
        cumulative += days
        if doy <= cumulative:
            return month_idx + 1
    return 12


def _anchor_value(index: int, seed: int) -> float:
    """Deterministic pseudo-random value in [0.0, 1.0] for an anchor point.

    Uses a simple hash-based approach to avoid needing ``random.Random``
    state management.
    """
    # Mix seed and index via a large prime multiply + xor
    h = ((index * 2654435761) ^ (seed * 40503)) & 0xFFFFFFFF
    h = ((h >> 16) ^ h) * 0x45D9F3B
    h = ((h >> 16) ^ h) & 0xFFFFFFFF
    return (h % 10000) / 10000.0
