"""Usage profile applicator -- merges HA-derived profiles into clone YAML.

Pure function: reads a clone config file, overlays per-circuit usage
profiles into the corresponding ``circuit_templates`` entries, and writes
the result back.  The simulator engine already consumes all target
parameters (``hour_factors``, ``monthly_factors``, ``duty_cycle``, etc.)
so no engine changes are required.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from pathlib import Path

_LOGGER = logging.getLogger(__name__)

# Energy modes whose typical_power / power_variation are hardware-driven
# and should not be overwritten by usage profiles.
_SKIP_POWER_MODES = frozenset({"producer", "bidirectional"})


def apply_usage_profiles(
    config_path: Path,
    profiles: dict[str, dict[str, object]],
) -> int:
    """Merge per-circuit usage profiles into a clone YAML config.

    For each *template_name* in *profiles*, if a matching key exists in
    ``circuit_templates``:

    - ``typical_power``   → ``energy_profile.typical_power``
    - ``power_variation`` → ``energy_profile.power_variation``
    - ``hour_factors``    → ``time_of_day_profile.hour_factors`` + enabled
    - ``duty_cycle``      → ``cycling_pattern.duty_cycle``
    - ``monthly_factors`` → ``monthly_factors``

    String dict keys (``"0"``, ``"1"`` from JSON) are converted to
    ``int`` keys for YAML compatibility.

    Returns the number of templates that were updated.
    """
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        _LOGGER.warning("Invalid config format in %s", config_path)
        return 0

    templates = raw.get("circuit_templates")
    if not isinstance(templates, dict):
        _LOGGER.warning("No circuit_templates in %s", config_path)
        return 0

    updated = 0

    for template_name, profile in profiles.items():
        template = templates.get(template_name)
        if not isinstance(template, dict):
            _LOGGER.warning(
                "Template %s not found in %s, skipping",
                template_name,
                config_path.name,
            )
            continue

        if not isinstance(profile, dict) or not profile:
            continue

        changed = False

        # typical_power / power_variation — require energy_profile,
        # skip for producer/bidirectional modes.
        ep = template.get("energy_profile")
        if isinstance(ep, dict):
            mode = ep.get("mode", "consumer")
            if mode not in _SKIP_POWER_MODES:
                if "typical_power" in profile:
                    ep["typical_power"] = profile["typical_power"]
                    changed = True
                if "power_variation" in profile:
                    ep["power_variation"] = profile["power_variation"]
                    changed = True

        # hour_factors → time_of_day_profile
        if "hour_factors" in profile:
            hour_factors = _int_keys(profile["hour_factors"])
            if hour_factors:
                template["time_of_day_profile"] = {
                    "enabled": True,
                    "hour_factors": hour_factors,
                }
                changed = True

        # active_days → time_of_day_profile or battery_behavior
        if "active_days" in profile:
            raw_days = profile["active_days"]
            if isinstance(raw_days, list) and raw_days:
                days = [int(d) for d in raw_days if 0 <= int(d) <= 6]
                if days:
                    tod = template.get("time_of_day_profile")
                    if isinstance(tod, dict):
                        tod["active_days"] = days
                    bb = template.get("battery_behavior")
                    if isinstance(bb, dict) and bb.get("enabled"):
                        bb["active_days"] = days
                    changed = True

        # duty_cycle → cycling_pattern
        if "duty_cycle" in profile:
            template["cycling_pattern"] = {
                "duty_cycle": profile["duty_cycle"],
            }
            changed = True

        # monthly_factors (top-level on the template)
        if "monthly_factors" in profile:
            monthly = _int_keys(profile["monthly_factors"])
            if monthly:
                template["monthly_factors"] = monthly
                changed = True

        if changed:
            updated += 1

    if updated:
        config_path.write_text(
            yaml.dump(raw, default_flow_style=False, sort_keys=False),
            encoding="utf-8",
        )
        _LOGGER.info(
            "Applied usage profiles to %d/%d templates in %s",
            updated,
            len(profiles),
            config_path.name,
        )

    return updated


def _int_keys(mapping: object) -> dict[int, float]:
    """Convert a mapping with string or int keys to int-keyed dict.

    JSON serialisation turns ``{0: 1.0}`` into ``{"0": 1.0}``.  YAML
    and the engine expect ``int`` keys.
    """
    if not isinstance(mapping, dict):
        return {}
    result: dict[int, float] = {}
    for k, v in mapping.items():
        try:
            result[int(k)] = float(v)
        except (ValueError, TypeError):
            continue
    return result
