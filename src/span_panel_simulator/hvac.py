"""Seasonal HVAC power modulation.

Provides a latitude-aware seasonal power multiplier so HVAC circuits
automatically adjust electrical draw month-by-month based on system type
and estimated outdoor temperature.
"""

from __future__ import annotations

import math
from datetime import datetime


VALID_HVAC_TYPES: tuple[str, ...] = ("central_ac", "heat_pump", "heat_pump_aux")

HVAC_TYPE_LABELS: dict[str, str] = {
    "central_ac": "Central AC / Gas Furnace",
    "heat_pump": "Heat Pump",
    "heat_pump_aux": "Heat Pump + Aux Strips",
}

# Electrical draw during heating relative to peak cooling draw.
_HEATING_RATIOS: dict[str, float] = {
    "central_ac": 0.15,    # Gas furnace heats; circuit powers blower fan only
    "heat_pump": 0.45,     # COP ~3 reduces electrical input per unit of thermal output
    "heat_pump_aux": 1.4,  # Below ~35 F, resistive aux strips exceed compressor draw
}

_BALANCE_POINT_C = 18.0  # Standard HVAC thermostat balance point (65 F)
_STANDBY_MIN = 0.05      # Minimum factor: standby / fan circulation


def _estimated_temperature(month_frac: float, latitude: float) -> float:
    """Sinusoidal temperature estimate from latitude and month.

    Args:
        month_frac: Fractional month (1.0 = Jan 1, 7.5 = mid-July).
        latitude: Degrees north (negative for southern hemisphere).

    Returns:
        Estimated outdoor temperature in Celsius.
    """
    abs_lat = abs(latitude)
    annual_mean = 27.0 - 0.35 * abs_lat
    amplitude = 2.0 + 0.25 * abs_lat

    # Phase: peak in July (month 7) for northern hemisphere
    # Shift by 6 months for southern hemisphere
    if latitude >= 0:
        phase = 2.0 * math.pi * (month_frac - 7.0) / 12.0
    else:
        phase = 2.0 * math.pi * (month_frac - 1.0) / 12.0

    return annual_mean + amplitude * math.cos(phase)


def hvac_seasonal_factor(timestamp: float, latitude: float, hvac_type: str) -> float:
    """Compute seasonal power multiplier for an HVAC circuit.

    Args:
        timestamp: Unix epoch seconds (simulation time).
        latitude: Panel latitude in degrees north.
        hvac_type: One of ``VALID_HVAC_TYPES``.

    Returns:
        Multiplier in [``_STANDBY_MIN``, ``_HEATING_RATIOS[hvac_type]``]
        (or up to 1.0 for cooling).
    """
    if hvac_type not in _HEATING_RATIOS:
        return 1.0

    dt = datetime.fromtimestamp(timestamp)
    # Fractional month: 1-based, day/30 gives rough intra-month position
    month_frac = dt.month + (dt.day - 1) / 30.0

    temp = _estimated_temperature(month_frac, latitude)

    # Peak summer and winter temperatures for normalization
    peak_summer = _estimated_temperature(7.0 if latitude >= 0 else 1.0, latitude)
    peak_winter = _estimated_temperature(1.0 if latitude >= 0 else 7.0, latitude)

    heating_ratio = _HEATING_RATIOS[hvac_type]

    # Cooling factor: ramps 0→1 as temperature rises above balance point
    if temp > _BALANCE_POINT_C:
        denominator = peak_summer - _BALANCE_POINT_C
        if denominator > 0:
            cooling = min((temp - _BALANCE_POINT_C) / denominator, 1.0)
        else:
            cooling = 1.0
        return max(cooling, _STANDBY_MIN)

    # Heating factor: ramps 0→heating_ratio as temperature drops below balance point
    denominator = _BALANCE_POINT_C - peak_winter
    if denominator > 0:
        heating = min((_BALANCE_POINT_C - temp) / denominator, 1.0) * heating_ratio
    else:
        heating = heating_ratio
    return max(heating, _STANDBY_MIN)
