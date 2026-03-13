"""Named daily profile presets.

Each preset is a dict mapping hour (0-23) to a production/consumption
multiplier.  The special "solar_curve" preset is computed dynamically
via ``solar.compute_solar_curve()``.
"""

from __future__ import annotations

import random

from span_panel_simulator.dashboard.solar import compute_solar_curve

EVENING_LIGHTING: dict[int, float] = {
    0: 0.0, 1: 0.0, 2: 0.0, 3: 0.0, 4: 0.0, 5: 0.0,
    6: 0.05, 7: 0.1, 8: 0.05, 9: 0.0, 10: 0.0, 11: 0.0,
    12: 0.0, 13: 0.0, 14: 0.0, 15: 0.1, 16: 0.3, 17: 0.6,
    18: 0.9, 19: 1.0, 20: 1.0, 21: 1.0, 22: 0.7, 23: 0.3,
}

ALWAYS_ON: dict[int, float] = {h: 1.0 for h in range(24)}

HVAC_CYCLING: dict[int, float] = {
    0: 0.3, 1: 0.3, 2: 0.3, 3: 0.3, 4: 0.3, 5: 0.3,
    6: 0.4, 7: 0.5, 8: 0.5, 9: 0.5, 10: 0.6, 11: 0.6,
    12: 0.7, 13: 0.7, 14: 0.8, 15: 0.9, 16: 1.0, 17: 1.0,
    18: 1.0, 19: 1.0, 20: 1.0, 21: 1.0, 22: 0.7, 23: 0.4,
}

DAYTIME_APPLIANCE: dict[int, float] = {
    0: 0.0, 1: 0.0, 2: 0.0, 3: 0.0, 4: 0.0, 5: 0.0,
    6: 0.0, 7: 0.1, 8: 0.3, 9: 0.7, 10: 1.0, 11: 1.0,
    12: 1.0, 13: 1.0, 14: 1.0, 15: 0.8, 16: 0.5, 17: 0.3,
    18: 0.1, 19: 0.0, 20: 0.0, 21: 0.0, 22: 0.0, 23: 0.0,
}

BASELOAD: dict[int, float] = {h: 0.1 for h in range(24)}


PRESET_REGISTRY: dict[str, dict[int, float] | None] = {
    "evening_lighting": EVENING_LIGHTING,
    "always_on": ALWAYS_ON,
    "solar_curve": None,  # computed dynamically
    "random": None,  # computed dynamically
    "hvac_cycling": HVAC_CYCLING,
    "daytime_appliance": DAYTIME_APPLIANCE,
    "baseload": BASELOAD,
}

PRESET_LABELS: dict[str, str] = {
    "evening_lighting": "Evening Lighting",
    "always_on": "Always On",
    "solar_curve": "Solar Curve",
    "random": "Random",
    "hvac_cycling": "HVAC Cycling",
    "daytime_appliance": "Daytime Appliance",
    "baseload": "Baseload",
}

# Presets appropriate for each entity type.
# Battery and EVSE have no time-of-day profile — they use battery_behavior
# and smart_behavior respectively.
PRESETS_BY_TYPE: dict[str, dict[str, str]] = {
    "circuit": {
        "evening_lighting": "Evening Lighting",
        "always_on": "Always On",
        "hvac_cycling": "HVAC Cycling",
        "daytime_appliance": "Daytime Appliance",
        "baseload": "Baseload",
        "random": "Random",
    },
    "pv": {
        "solar_curve": "Solar Curve",
        "always_on": "Always On",
    },
}


def _compute_random_profile(start_hour: int, end_hour: int) -> dict[int, float]:
    """Generate a random on/off profile between start_hour and end_hour.

    Hours outside the range are always 0.0.  Hours inside are randomly
    either 1.0 (on) or 0.0 (off).
    """
    result: dict[int, float] = {}
    for h in range(24):
        if start_hour <= end_hour:
            active = start_hour <= h < end_hour
        else:
            # Wraps midnight, e.g. start=22, end=6
            active = h >= start_hour or h < end_hour
        result[h] = float(random.randint(0, 1)) if active else 0.0
    return result


def get_preset(
    name: str,
    month: int = 6,
    day: int = 21,
    start_hour: int = 0,
    end_hour: int = 24,
) -> dict[int, float]:
    """Return the multiplier dict for the named preset.

    For ``solar_curve``, the curve is computed from *month* and *day*.
    For ``random``, values are randomised between *start_hour* and *end_hour*.
    """
    if name == "solar_curve":
        return compute_solar_curve(month, day)

    if name == "random":
        return _compute_random_profile(start_hour, end_hour)

    preset = PRESET_REGISTRY.get(name)
    if preset is None:
        raise ValueError(f"Unknown preset: {name}")
    return dict(preset)
