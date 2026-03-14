"""Historical weather data from Open-Meteo.

Fetches daily cloud cover from the Open-Meteo Archive API (no API key
required), averages it into 12 monthly values, and converts to solar
degradation factors.

The cache is keyed by ``(lat_rounded, lon_rounded)`` so that nearby
coordinates share the same data without redundant fetches.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import aiohttp

_LOGGER = logging.getLogger(__name__)

_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
_YEARS_BACK = 3
_COORD_PRECISION = 1  # round to 0.1 degree for cache key


@dataclass
class WeatherData:
    """Cached monthly cloud cover averages for a location."""

    latitude: float
    longitude: float
    monthly_cloud_cover: dict[int, float]  # month (1-12) → avg cloud % (0-100)
    monthly_factors: dict[int, float]  # month (1-12) → degradation factor (0.3-1.0)
    years_averaged: int
    fetched_at: float  # epoch timestamp
    source: str = "Open-Meteo Historical Archive"

    @property
    def display_summary(self) -> str:
        """Human-readable summary for the UI."""
        return f"Avg cloud cover from {self.years_averaged} years of data ({self.source})"


class WeatherCache:
    """In-memory cache of historical weather data by location."""

    def __init__(self) -> None:
        self._cache: dict[tuple[float, float], WeatherData] = {}

    def _key(self, lat: float, lon: float) -> tuple[float, float]:
        return (
            round(lat, _COORD_PRECISION),
            round(lon, _COORD_PRECISION),
        )

    def get(self, lat: float, lon: float) -> WeatherData | None:
        return self._cache.get(self._key(lat, lon))

    def put(self, data: WeatherData) -> None:
        self._cache[self._key(data.latitude, data.longitude)] = data


# Module-level singleton
_weather_cache = WeatherCache()


def get_cached_weather(lat: float, lon: float) -> WeatherData | None:
    """Return cached weather data for the location, if available."""
    return _weather_cache.get(lat, lon)


def cloud_cover_to_factor(cloud_pct: float) -> float:
    """Convert cloud cover percentage (0-100) to solar degradation factor.

    Uses a quadratic mapping: moderate cloud cover (which often includes
    morning fog or high thin clouds) has a modest effect on daily
    production, while only heavy persistent overcast causes significant
    degradation.  This matches real-world PV data where diffuse
    radiation still contributes substantially.

    Returns a value in [0.3, 1.0]:
        0% cloud  → 1.0  (clear sky)
        40% cloud → ~0.89  (typical Bay Area summer w/ marine layer)
        60% cloud → ~0.75
        80% cloud → ~0.55
        100% cloud → 0.3  (heavy overcast)
    """
    fraction = max(0.0, min(1.0, cloud_pct / 100.0))
    return 1.0 - 0.7 * fraction**2


async def fetch_historical_weather(lat: float, lon: float) -> WeatherData:
    """Fetch daily cloud cover from Open-Meteo Archive and average by month.

    Queries the last ``_YEARS_BACK`` calendar years of daily
    ``cloud_cover_mean``.  Returns a ``WeatherData`` with monthly
    averages and corresponding solar degradation factors.

    Raises ``RuntimeError`` on API failure.
    """
    now = time.time()
    current_year = int(time.strftime("%Y"))
    start_year = current_year - _YEARS_BACK
    start_date = f"{start_year}-01-01"
    end_date = f"{current_year - 1}-12-31"

    params = {
        "latitude": str(lat),
        "longitude": str(lon),
        "start_date": start_date,
        "end_date": end_date,
        "daily": "cloud_cover_mean",
        "timezone": "UTC",
    }

    async with (
        aiohttp.ClientSession() as session,
        session.get(
            _ARCHIVE_URL,
            params=params,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp,
    ):
        if resp.status != 200:
            body = await resp.text()
            raise RuntimeError(f"Open-Meteo returned {resp.status}: {body[:200]}")
        data = await resp.json()

    daily = data.get("daily", {})
    dates: list[str] = daily.get("time", [])
    cloud_values: list[float | None] = daily.get("cloud_cover_mean", [])

    if not dates or not cloud_values:
        raise RuntimeError("Open-Meteo returned no daily data")

    # Aggregate by month
    month_totals: dict[int, float] = {m: 0.0 for m in range(1, 13)}
    month_counts: dict[int, int] = {m: 0 for m in range(1, 13)}

    for date_str, cloud_val in zip(dates, cloud_values, strict=False):
        if cloud_val is None:
            continue
        month = int(date_str[5:7])
        month_totals[month] += cloud_val
        month_counts[month] += 1

    monthly_cloud: dict[int, float] = {}
    monthly_factors: dict[int, float] = {}
    for m in range(1, 13):
        count = month_counts[m]
        avg = month_totals[m] / count if count > 0 else 50.0
        monthly_cloud[m] = round(avg, 1)
        monthly_factors[m] = round(cloud_cover_to_factor(avg), 4)

    years_counted = len({d[:4] for d in dates})

    weather_data = WeatherData(
        latitude=lat,
        longitude=lon,
        monthly_cloud_cover=monthly_cloud,
        monthly_factors=monthly_factors,
        years_averaged=years_counted,
        fetched_at=now,
    )

    _weather_cache.put(weather_data)
    _LOGGER.info(
        "Fetched %d days of cloud cover for (%.1f, %.1f) from Open-Meteo",
        len(dates),
        lat,
        lon,
    )

    return weather_data
