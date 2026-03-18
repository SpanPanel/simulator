"""Default entity values by type.

When a user adds a new entity via the dashboard, these defaults
populate the template and circuit definition.
"""

from __future__ import annotations

import re
from typing import Any


def _slugify(name: str) -> str:
    """Convert a human name to a YAML-safe snake_case id."""
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "_", slug)
    return slug.strip("_")


ENTITY_TYPE_DEFAULTS: dict[str, dict[str, Any]] = {
    "circuit": {
        "template": {
            "energy_profile": {
                "mode": "consumer",
                "power_range": [0.0, 1800.0],
                "typical_power": 150.0,
                "power_variation": 0.3,
            },
            "relay_behavior": "controllable",
            "priority": "NEVER",
            "breaker_rating": 15,
        },
        "circuit": {
            "tabs": [],
        },
    },
    "pv": {
        "template": {
            "energy_profile": {
                "mode": "producer",
                "power_range": [-5000.0, 0.0],
                "typical_power": -3000.0,
                "power_variation": 0.25,
                "efficiency": 0.85,
                "nameplate_capacity_w": 5000.0,
            },
            "relay_behavior": "non_controllable",
            "priority": "NEVER",
            "device_type": "pv",
            "breaker_rating": 30,
        },
        "circuit": {
            "tabs": [],
        },
    },
    "evse": {
        "template": {
            "energy_profile": {
                "mode": "consumer",
                "power_range": [0.0, 11500.0],
                "typical_power": 7200.0,
                "power_variation": 0.05,
            },
            "relay_behavior": "controllable",
            "priority": "OFF_GRID",
            "device_type": "evse",
            "breaker_rating": 50,
            "smart_behavior": {
                "responds_to_grid": True,
                "max_power_reduction": 0.6,
            },
            "time_of_day_profile": {
                "enabled": True,
                "hour_factors": {
                    0: 1.0,
                    1: 1.0,
                    2: 1.0,
                    3: 1.0,
                    4: 1.0,
                    5: 1.0,
                    6: 0.0,
                    7: 0.0,
                    8: 0.0,
                    9: 0.0,
                    10: 0.0,
                    11: 0.0,
                    12: 0.0,
                    13: 0.0,
                    14: 0.0,
                    15: 0.0,
                    16: 0.0,
                    17: 0.0,
                    18: 0.0,
                    19: 0.0,
                    20: 0.0,
                    21: 0.0,
                    22: 0.0,
                    23: 0.0,
                },
            },
        },
        "circuit": {
            "tabs": [],
        },
    },
    "battery": {
        "template": {
            "energy_profile": {
                "mode": "bidirectional",
                "power_range": [-5000.0, 5000.0],
                "typical_power": 0.0,
                "power_variation": 0.02,
                "efficiency": 0.95,
            },
            "relay_behavior": "controllable",
            "priority": "NEVER",
            "battery_behavior": {
                "enabled": True,
                "charge_mode": "custom",
                "nameplate_capacity_kwh": 13.5,
                "backup_reserve_pct": 20.0,
                "max_charge_power": 3500.0,
                "max_discharge_power": 3500.0,
            },
        },
        "circuit": {
            "tabs": [],
        },
    },
}


def default_name_for_type(entity_type: str) -> str:
    """Return a sensible default display name for a new entity."""
    return {
        "circuit": "New Circuit",
        "pv": "Solar Inverter",
        "evse": "SPAN Drive",
        "battery": "Battery Storage",
    }.get(entity_type, "New Entity")


def make_defaults(
    entity_type: str, name: str | None = None
) -> tuple[str, str, dict[str, Any], dict[str, Any]]:
    """Return ``(entity_id, template_name, template_dict, circuit_dict)``.

    The caller can insert these directly into the config store.
    """
    if entity_type not in ENTITY_TYPE_DEFAULTS:
        raise ValueError(f"Unknown entity type: {entity_type}")

    display_name = name or default_name_for_type(entity_type)
    entity_id = _slugify(display_name)
    template_name = f"{entity_id}_tpl"

    spec = ENTITY_TYPE_DEFAULTS[entity_type]
    template_dict: dict[str, Any] = dict(spec["template"])
    circuit_dict: dict[str, Any] = {
        "id": entity_id,
        "name": display_name,
        "template": template_name,
        **spec["circuit"],
    }

    return entity_id, template_name, template_dict, circuit_dict
