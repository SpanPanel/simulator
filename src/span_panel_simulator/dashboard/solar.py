"""Re-export solar model from the top-level module.

All solar computation now lives in ``span_panel_simulator.solar``.
This shim preserves backward-compatible imports for dashboard code.
"""

from span_panel_simulator.solar import (  # noqa: F401
    DEFAULT_LATITUDE,
    DEFAULT_LONGITUDE,
    compute_solar_curve,
    daily_weather_factor,
    solar_production_factor,
)

__all__ = [
    "DEFAULT_LATITUDE",
    "DEFAULT_LONGITUDE",
    "compute_solar_curve",
    "daily_weather_factor",
    "solar_production_factor",
]
