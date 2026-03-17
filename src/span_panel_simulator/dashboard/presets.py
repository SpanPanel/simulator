"""Named daily profile presets backed by YAML configuration.

Presets are loaded from ``configs/presets/presets.yaml`` when available, falling
back to built-in defaults.  The special ``solar_curve`` and ``random``
presets are computed dynamically at apply time.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    from pathlib import Path

from span_panel_simulator.solar import compute_solar_curve

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CircuitPreset:
    label: str
    profile: dict[int, float] = field(default_factory=dict)
    dynamic: str | None = None
    random_days: bool = False


@dataclass(frozen=True)
class BatteryPreset:
    label: str
    schedule: dict[int, str] = field(default_factory=dict)


@dataclass(frozen=True)
class EvsePreset:
    label: str
    start_hour: int = 0
    duration_hours: int = 6


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class PresetRegistry:
    """Central store for all preset definitions."""

    def __init__(
        self,
        circuit: dict[str, CircuitPreset],
        pv: dict[str, CircuitPreset],
        battery: dict[str, BatteryPreset],
        evse: dict[str, EvsePreset],
    ) -> None:
        self.circuit = circuit
        self.pv = pv
        self.battery = battery
        self.evse = evse

    # -- label dicts for templates -------------------------------------------

    @property
    def circuit_labels(self) -> dict[str, str]:
        return {k: v.label for k, v in self.circuit.items()}

    @property
    def pv_labels(self) -> dict[str, str]:
        return {k: v.label for k, v in self.pv.items()}

    @property
    def battery_labels(self) -> dict[str, str]:
        return {k: v.label for k, v in self.battery.items()}

    @property
    def evse_labels(self) -> dict[str, str]:
        return {k: v.label for k, v in self.evse.items()}

    def presets_for_type(self, entity_type: str) -> dict[str, str]:
        """Return the label dict appropriate for the given entity type."""
        mapping: dict[str, dict[str, str]] = {
            "circuit": self.circuit_labels,
            "pv": self.pv_labels,
            "battery": self.battery_labels,
            "evse": self.evse_labels,
        }
        return mapping.get(entity_type, {})

    # -- loaders -------------------------------------------------------------

    @classmethod
    def load(cls, config_dir: Path) -> PresetRegistry:
        """Load from ``config_dir / presets.yaml``, falling back to builtins."""
        path = config_dir / "presets" / "presets.yaml"
        if path.exists():
            try:
                raw = yaml.safe_load(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    return cls._from_dict(raw)
            except Exception:
                _LOGGER.warning("Failed to load %s, using builtins", path, exc_info=True)
        return cls.builtin()

    @classmethod
    def builtin(cls) -> PresetRegistry:
        """Return the hardcoded default presets."""
        return cls(
            circuit={
                "evening_lighting": CircuitPreset(
                    label="Evening Lighting",
                    profile={
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
                    },
                ),
                "always_on": CircuitPreset(
                    label="Always On",
                    profile={h: 1.0 for h in range(24)},
                ),
                "hvac_cycling": CircuitPreset(
                    label="HVAC Cycling",
                    profile={
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
                    },
                ),
                "daytime_appliance": CircuitPreset(
                    label="Daytime Appliance",
                    profile={
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
                    },
                ),
                "baseload": CircuitPreset(
                    label="Baseload",
                    profile={h: 0.1 for h in range(24)},
                ),
                "random": CircuitPreset(
                    label="Random",
                    dynamic="random",
                    random_days=True,
                ),
            },
            pv={
                "solar_curve": CircuitPreset(label="Solar Curve", dynamic="solar_curve"),
                "always_on": CircuitPreset(
                    label="Always On",
                    profile={h: 1.0 for h in range(24)},
                ),
            },
            battery={
                "post_solar_discharge": BatteryPreset(
                    label="Post-Solar Discharge",
                    schedule={
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
                    },
                ),
                "grid_disconnect_discharge": BatteryPreset(
                    label="Grid-Disconnect Discharge",
                    schedule={h: "discharge" for h in range(24)},
                ),
                "custom": BatteryPreset(
                    label="Custom",
                    schedule={h: "idle" for h in range(24)},
                ),
            },
            evse={
                "peak_solar": EvsePreset(label="Peak Solar", start_hour=10, duration_hours=6),
                "evening": EvsePreset(label="Evening", start_hour=18, duration_hours=6),
                "night": EvsePreset(label="Night", start_hour=0, duration_hours=6),
            },
        )

    @classmethod
    def _from_dict(cls, raw: dict[str, Any]) -> PresetRegistry:
        """Parse a raw YAML dict into a PresetRegistry."""
        circuit: dict[str, CircuitPreset] = {}
        for key, val in (raw.get("circuit") or {}).items():
            circuit[key] = CircuitPreset(
                label=val.get("label", key),
                profile=_parse_profile(val.get("profile")),
                dynamic=val.get("dynamic"),
                random_days=bool(val.get("random_days", False)),
            )

        pv: dict[str, CircuitPreset] = {}
        for key, val in (raw.get("pv") or {}).items():
            pv[key] = CircuitPreset(
                label=val.get("label", key),
                profile=_parse_profile(val.get("profile")),
                dynamic=val.get("dynamic"),
            )

        battery: dict[str, BatteryPreset] = {}
        for key, val in (raw.get("battery") or {}).items():
            schedule: dict[int, str] = {}
            for h, mode in (val.get("schedule") or {}).items():
                schedule[int(h)] = str(mode)
            battery[key] = BatteryPreset(label=val.get("label", key), schedule=schedule)

        evse: dict[str, EvsePreset] = {}
        for key, val in (raw.get("evse") or {}).items():
            evse[key] = EvsePreset(
                label=val.get("label", key),
                start_hour=int(val.get("start_hour", 0)),
                duration_hours=int(val.get("duration_hours", 6)),
            )

        return cls(circuit=circuit, pv=pv, battery=battery, evse=evse)


def _parse_profile(raw: dict[Any, Any] | None) -> dict[int, float]:
    if not raw:
        return {}
    return {int(k): float(v) for k, v in raw.items()}


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_registry: PresetRegistry | None = None


def init_presets(config_dir: Path) -> PresetRegistry:
    """Initialise the global preset registry from *config_dir*."""
    global _registry
    _registry = PresetRegistry.load(config_dir)
    return _registry


def _get_registry() -> PresetRegistry:
    """Return the global registry, falling back to builtins."""
    if _registry is None:
        return PresetRegistry.builtin()
    return _registry


# ---------------------------------------------------------------------------
# Public API — kept compatible with the old module-level interface
# ---------------------------------------------------------------------------


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

    reg = _get_registry()
    # Look up in circuit presets first, then pv
    preset = reg.circuit.get(name) or reg.pv.get(name)
    if preset is None or not preset.profile:
        raise ValueError(f"Unknown preset: {name}")
    return dict(preset.profile)


def is_random_days_preset(name: str) -> bool:
    """Return True if the named preset wants random active days."""
    reg = _get_registry()
    preset = reg.circuit.get(name)
    if preset is not None:
        return preset.random_days
    return False


def get_battery_preset(name: str) -> dict[int, str]:
    """Return the hour-mode mapping for a named battery preset."""
    reg = _get_registry()
    preset = reg.battery.get(name)
    if preset is None:
        raise ValueError(f"Unknown battery preset: {name}")
    return dict(preset.schedule)


def match_battery_preset(profile: dict[int, str]) -> str | None:
    """Return the preset key matching the current battery profile, or None."""
    reg = _get_registry()
    for key, preset in reg.battery.items():
        if all(profile.get(h, "idle") == preset.schedule.get(h, "idle") for h in range(24)):
            return key
    return None


def evse_schedule_factors(start_hour: int, duration_hours: int) -> dict[int, float]:
    """Return hour_factors for an EVSE charging window."""
    factors: dict[int, float] = {}
    for h in range(24):
        offset = (h - start_hour) % 24
        factors[h] = 1.0 if offset < duration_hours else 0.0
    return factors


def get_evse_preset(name: str) -> dict[int, float]:
    """Return hour_factors for a named EVSE charging preset."""
    reg = _get_registry()
    preset = reg.evse.get(name)
    if preset is None:
        raise ValueError(f"Unknown EVSE preset: {name}")
    return evse_schedule_factors(preset.start_hour, preset.duration_hours)


def get_evse_tuples() -> dict[str, tuple[int, int]]:
    """Return ``{name: (start_hour, duration_hours)}`` for all EVSE presets."""
    reg = _get_registry()
    return {k: (v.start_hour, v.duration_hours) for k, v in reg.evse.items()}
