"""Named daily profile presets.

Each preset is a dict mapping hour (0-23) to a production/consumption
multiplier.  The special "solar_curve" preset is computed dynamically
via ``solar.compute_solar_curve()``.
"""

from __future__ import annotations

import random

from span_panel_simulator.dashboard.solar import compute_solar_curve

EVENING_LIGHTING: dict[int, float] = {
    0: 0.0,
    1: 0.0,
    2: 0.0,
    3: 0.0,
    4: 0.0,
    5: 0.0,
    6: 0.05,
    7: 0.1,
    8: 0.05,
    9: 0.0,
    10: 0.0,
    11: 0.0,
    12: 0.0,
    13: 0.0,
    14: 0.0,
    15: 0.1,
    16: 0.3,
    17: 0.6,
    18: 0.9,
    19: 1.0,
    20: 1.0,
    21: 1.0,
    22: 0.7,
    23: 0.3,
}

ALWAYS_ON: dict[int, float] = {h: 1.0 for h in range(24)}

HVAC_CYCLING: dict[int, float] = {
    0: 0.3,
    1: 0.3,
    2: 0.3,
    3: 0.3,
    4: 0.3,
    5: 0.3,
    6: 0.4,
    7: 0.5,
    8: 0.5,
    9: 0.5,
    10: 0.6,
    11: 0.6,
    12: 0.7,
    13: 0.7,
    14: 0.8,
    15: 0.9,
    16: 1.0,
    17: 1.0,
    18: 1.0,
    19: 1.0,
    20: 1.0,
    21: 1.0,
    22: 0.7,
    23: 0.4,
}

DAYTIME_APPLIANCE: dict[int, float] = {
    0: 0.0,
    1: 0.0,
    2: 0.0,
    3: 0.0,
    4: 0.0,
    5: 0.0,
    6: 0.0,
    7: 0.1,
    8: 0.3,
    9: 0.7,
    10: 1.0,
    11: 1.0,
    12: 1.0,
    13: 1.0,
    14: 1.0,
    15: 0.8,
    16: 0.5,
    17: 0.3,
    18: 0.1,
    19: 0.0,
    20: 0.0,
    21: 0.0,
    22: 0.0,
    23: 0.0,
}

BASELOAD: dict[int, float] = {h: 0.1 for h in range(24)}

# -- Battery schedule presets ------------------------------------------------
# Each maps hour (0-23) to a mode string: "charge", "discharge", or "idle".

BATTERY_POST_SOLAR_DISCHARGE: dict[int, str] = {
    0: "idle",
    1: "idle",
    2: "idle",
    3: "idle",
    4: "idle",
    5: "idle",
    6: "idle",
    7: "idle",
    8: "charge",
    9: "charge",
    10: "charge",
    11: "charge",
    12: "charge",
    13: "charge",
    14: "charge",
    15: "charge",
    16: "discharge",
    17: "discharge",
    18: "discharge",
    19: "discharge",
    20: "discharge",
    21: "discharge",
    22: "discharge",
    23: "idle",
}

BATTERY_GRID_DISCONNECT_DISCHARGE: dict[int, str] = {h: "discharge" for h in range(24)}

BATTERY_CUSTOM: dict[int, str] = {h: "idle" for h in range(24)}

# -- EVSE charging schedule presets -------------------------------------------
# Each is a (start_hour, duration_hours) tuple.

EVSE_PRESETS: dict[str, tuple[int, int]] = {
    "peak_solar": (10, 6),  # 10:00-16:00
    "evening": (18, 6),  # 18:00-00:00
    "night": (0, 6),  # 00:00-06:00
}

EVSE_PRESET_LABELS: dict[str, str] = {
    "peak_solar": "Peak Solar",
    "evening": "Evening",
    "night": "Night",
}


def evse_schedule_factors(start_hour: int, duration_hours: int) -> dict[int, float]:
    """Return hour_factors for an EVSE charging window."""
    factors: dict[int, float] = {}
    for h in range(24):
        offset = (h - start_hour) % 24
        factors[h] = 1.0 if offset < duration_hours else 0.0
    return factors


def get_evse_preset(name: str) -> dict[int, float]:
    """Return hour_factors for a named EVSE charging preset."""
    preset = EVSE_PRESETS.get(name)
    if preset is None:
        raise ValueError(f"Unknown EVSE preset: {name}")
    return evse_schedule_factors(preset[0], preset[1])


BATTERY_PRESET_REGISTRY: dict[str, dict[int, str]] = {
    "post_solar_discharge": BATTERY_POST_SOLAR_DISCHARGE,
    "grid_disconnect_discharge": BATTERY_GRID_DISCONNECT_DISCHARGE,
    "custom": BATTERY_CUSTOM,
}

BATTERY_PRESET_LABELS: dict[str, str] = {
    "post_solar_discharge": "Post-Solar Discharge",
    "grid_disconnect_discharge": "Grid-Disconnect Discharge",
    "custom": "Custom",
}


def get_battery_preset(name: str) -> dict[int, str]:
    """Return the hour-mode mapping for a named battery preset."""
    preset = BATTERY_PRESET_REGISTRY.get(name)
    if preset is None:
        raise ValueError(f"Unknown battery preset: {name}")
    return dict(preset)


def match_battery_preset(profile: dict[int, str]) -> str | None:
    """Return the preset key matching the current battery profile, or None."""
    for key, preset in BATTERY_PRESET_REGISTRY.items():
        if all(profile.get(h, "idle") == preset.get(h, "idle") for h in range(24)):
            return key
    return None


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
    "battery": BATTERY_PRESET_LABELS,
    "evse": EVSE_PRESET_LABELS,
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
    latitude: float = 37.7,
) -> dict[int, float]:
    """Return the multiplier dict for the named preset.

    For ``solar_curve``, the curve is computed from *month*, *day*, and
    *latitude*.  For ``random``, values are randomised between
    *start_hour* and *end_hour*.
    """
    if name == "solar_curve":
        return compute_solar_curve(month, day, latitude=latitude)

    if name == "random":
        return _compute_random_profile(start_hour, end_hour)

    preset = PRESET_REGISTRY.get(name)
    if preset is None:
        raise ValueError(f"Unknown preset: {name}")
    return dict(preset)
