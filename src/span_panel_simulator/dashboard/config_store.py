"""In-memory configuration state manager.

Holds the full simulator config as a mutable dict tree (matching the
YAML schema).  Changes only persist when the user explicitly saves.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from span_panel_simulator.dashboard.defaults import make_defaults
from span_panel_simulator.dashboard.presets import get_preset
from span_panel_simulator.validation import validate_yaml_config


@dataclass
class EntityView:
    """Read-only projection of a circuit + its template for templates."""

    id: str
    name: str
    entity_type: str  # "circuit" | "pv" | "evse" | "battery"
    template_name: str
    tabs: list[int]
    energy_profile: dict[str, Any]
    relay_behavior: str
    priority: str
    cycling_pattern: dict[str, Any] | None = None
    time_of_day_profile: dict[str, Any] | None = None
    smart_behavior: dict[str, Any] | None = None
    battery_behavior: dict[str, Any] | None = None
    overrides: dict[str, Any] = field(default_factory=dict)


def _detect_entity_type(template: dict[str, Any]) -> str:
    """Infer entity type from template fields."""
    device_type = template.get("device_type", "")
    if device_type == "pv":
        return "pv"
    if device_type == "evse":
        return "evse"
    if template.get("battery_behavior", {}).get("enabled"):
        return "battery"
    return "circuit"


class ConfigStore:
    """In-memory config state: load, mutate, validate, export."""

    def __init__(self) -> None:
        self._state: dict[str, Any] = {
            "panel_config": {
                "serial_number": "SPAN-SIM-001",
                "total_tabs": 32,
                "main_size": 200,
            },
            "circuit_templates": {},
            "circuits": [],
            "simulation_params": {
                "update_interval": 5,
                "time_acceleration": 1.0,
                "noise_factor": 0.02,
                "enable_realistic_behaviors": True,
            },
        }

    def load_from_yaml(self, content: str) -> None:
        """Parse, validate, and replace state from YAML string."""
        data = yaml.safe_load(content)
        if not isinstance(data, dict):
            raise ValueError("YAML content must be a mapping")
        validate_yaml_config(data)
        self._state = data

    def load_from_file(self, path: Path) -> None:
        """Read a file and load its content."""
        self.load_from_yaml(path.read_text(encoding="utf-8"))

    def export_yaml(self) -> str:
        """Serialize current state to YAML."""
        return yaml.dump(
            self._state,
            default_flow_style=False,
            sort_keys=False,
            allow_unicode=True,
        )

    # -- Panel config --

    def get_panel_config(self) -> dict[str, Any]:
        return dict(self._state.get("panel_config", {}))

    def update_panel_config(self, data: dict[str, Any]) -> None:
        cfg = self._state.setdefault("panel_config", {})
        for key in ("serial_number", "total_tabs", "main_size"):
            if key in data:
                value = data[key]
                if key in ("total_tabs", "main_size"):
                    value = int(value)
                cfg[key] = value

    # -- Simulation params --

    def get_simulation_params(self) -> dict[str, Any]:
        return dict(self._state.get("simulation_params", {}))

    def update_simulation_params(self, data: dict[str, Any]) -> None:
        params = self._state.setdefault("simulation_params", {})
        for key in ("update_interval", "time_acceleration", "noise_factor"):
            if key in data:
                params[key] = float(data[key])
        if "enable_realistic_behaviors" in data:
            val = data["enable_realistic_behaviors"]
            params["enable_realistic_behaviors"] = val in (True, "true", "on", "1")

    # -- Entities --

    def _templates(self) -> dict[str, Any]:
        return self._state.setdefault("circuit_templates", {})

    def _circuits(self) -> list[dict[str, Any]]:
        return self._state.setdefault("circuits", [])

    def _find_circuit(self, entity_id: str) -> dict[str, Any] | None:
        for circ in self._circuits():
            if circ.get("id") == entity_id:
                return circ
        return None

    def _merge_entity(self, circuit: dict[str, Any]) -> EntityView:
        """Build an EntityView by merging template + circuit overrides."""
        template_name = circuit["template"]
        template = deepcopy(self._templates().get(template_name, {}))

        overrides = circuit.get("overrides", {})
        energy_profile = dict(template.get("energy_profile", {}))
        for k, v in overrides.items():
            if k == "power_range":
                energy_profile["power_range"] = v
            elif k in energy_profile:
                energy_profile[k] = v

        return EntityView(
            id=circuit["id"],
            name=circuit["name"],
            entity_type=_detect_entity_type(template),
            template_name=template_name,
            tabs=list(circuit.get("tabs", [])),
            energy_profile=energy_profile,
            relay_behavior=template.get("relay_behavior", "controllable"),
            priority=template.get("priority", "NEVER"),
            cycling_pattern=template.get("cycling_pattern"),
            time_of_day_profile=template.get("time_of_day_profile"),
            smart_behavior=template.get("smart_behavior"),
            battery_behavior=template.get("battery_behavior"),
            overrides=dict(overrides),
        )

    def list_entities(self) -> list[EntityView]:
        """Return entities with infrastructure (pv, battery, evse) first, then circuits."""
        _type_order = {"pv": 0, "battery": 1, "evse": 2, "circuit": 3}
        entities = [self._merge_entity(c) for c in self._circuits()]
        entities.sort(key=lambda e: (_type_order.get(e.entity_type, 9), e.name.lower()))
        return entities

    def get_entity(self, entity_id: str) -> EntityView:
        """Return a single entity by id."""
        circuit = self._find_circuit(entity_id)
        if circuit is None:
            raise KeyError(f"Entity not found: {entity_id}")
        return self._merge_entity(circuit)

    def update_entity(self, entity_id: str, data: dict[str, Any]) -> None:
        """Update circuit and template fields from form data."""
        circuit = self._find_circuit(entity_id)
        if circuit is None:
            raise KeyError(f"Entity not found: {entity_id}")

        template_name = circuit["template"]
        template = self._templates().get(template_name, {})

        if "name" in data:
            circuit["name"] = data["name"]

        if "tabs" in data:
            tabs_raw = data["tabs"]
            if isinstance(tabs_raw, str):
                tabs_raw = [int(t.strip()) for t in tabs_raw.split(",") if t.strip()]
            circuit["tabs"] = tabs_raw

        if "priority" in data:
            template["priority"] = data["priority"]
        if "relay_behavior" in data:
            template["relay_behavior"] = data["relay_behavior"]

        overrides: dict[str, Any] = circuit.get("overrides", {})
        ep = template.get("energy_profile", {})

        if "typical_power" in data:
            val = float(data["typical_power"])
            if val != ep.get("typical_power"):
                overrides["typical_power"] = val
            else:
                overrides.pop("typical_power", None)

        if "power_range_min" in data and "power_range_max" in data:
            pr = [float(data["power_range_min"]), float(data["power_range_max"])]
            if pr != ep.get("power_range"):
                overrides["power_range"] = pr
            else:
                overrides.pop("power_range", None)

        if overrides:
            circuit["overrides"] = overrides
        else:
            circuit.pop("overrides", None)

    def add_entity(self, entity_type: str) -> EntityView:
        """Create a new entity with type-appropriate defaults."""
        entity_id, template_name, template_dict, circuit_dict = make_defaults(entity_type)

        existing_ids = {c["id"] for c in self._circuits()}
        base_id = entity_id
        counter = 2
        while entity_id in existing_ids:
            entity_id = f"{base_id}_{counter}"
            circuit_dict["id"] = entity_id
            counter += 1

        self._templates()[template_name] = template_dict
        self._circuits().append(circuit_dict)
        return self._merge_entity(circuit_dict)

    def get_unmapped_tabs(self) -> list[int]:
        """Return tab numbers not assigned to any circuit, sorted ascending."""
        total_tabs = self._state.get("panel_config", {}).get("total_tabs", 32)
        used: set[int] = set()
        for circ in self._circuits():
            used.update(circ.get("tabs", []))
        return sorted(t for t in range(1, total_tabs + 1) if t not in used)

    def add_entity_from_tabs(self, tabs: list[int]) -> EntityView:
        """Create a new circuit entity assigned to the given tabs.

        For double-pole (2 tabs), validates same parity and exactly 2 apart.
        """
        if not tabs or len(tabs) > 2:
            raise ValueError("Select 1 or 2 tabs")

        if len(tabs) == 2:
            a, b = sorted(tabs)
            if a % 2 != b % 2:
                raise ValueError(
                    f"Double-pole tabs {tabs} must have the same parity "
                    "(both odd or both even)"
                )
            if b - a != 2:
                raise ValueError(
                    f"Double-pole tabs {tabs} must be exactly 2 apart"
                )

        unmapped = set(self.get_unmapped_tabs())
        for t in tabs:
            if t not in unmapped:
                raise ValueError(f"Tab {t} is already assigned to a circuit")

        entity_id, template_name, template_dict, circuit_dict = make_defaults("circuit")
        circuit_dict["tabs"] = sorted(tabs)

        tab_label = ", ".join(str(t) for t in sorted(tabs))
        circuit_dict["name"] = f"New Circuit (Tab {tab_label})"

        existing_ids = {c["id"] for c in self._circuits()}
        base_id = entity_id
        counter = 2
        while entity_id in existing_ids:
            entity_id = f"{base_id}_{counter}"
            circuit_dict["id"] = entity_id
            counter += 1

        self._templates()[template_name] = template_dict
        self._circuits().append(circuit_dict)
        return self._merge_entity(circuit_dict)

    def delete_entity(self, entity_id: str) -> None:
        """Remove an entity and its template if no other circuit uses it."""
        circuits = self._circuits()
        circuit = None
        for i, c in enumerate(circuits):
            if c.get("id") == entity_id:
                circuit = circuits.pop(i)
                break
        if circuit is None:
            raise KeyError(f"Entity not found: {entity_id}")

        template_name = circuit["template"]
        still_used = any(c.get("template") == template_name for c in circuits)
        if not still_used:
            self._templates().pop(template_name, None)

    # -- Profile --

    def get_entity_profile(self, entity_id: str) -> dict[int, float]:
        """Return resolved 24-hour multipliers for an entity."""
        entity = self.get_entity(entity_id)
        tod = entity.time_of_day_profile
        if not tod or not tod.get("enabled"):
            return {h: 1.0 for h in range(24)}

        multipliers = {h: 0.0 for h in range(24)}
        hourly = tod.get("hourly_multipliers", {})
        peak_hours = tod.get("peak_hours", [])
        peak_mult = tod.get("peak_multiplier", 1.0)
        off_peak_mult = tod.get("off_peak_multiplier", 0.0)

        for h in range(24):
            if h in hourly or str(h) in hourly:
                multipliers[h] = float(hourly.get(h, hourly.get(str(h), 0.0)))
            elif h in peak_hours:
                multipliers[h] = peak_mult
            else:
                multipliers[h] = off_peak_mult

        return multipliers

    def update_entity_profile(self, entity_id: str, multipliers: dict[int, float]) -> None:
        """Write 24-hour multipliers into the entity's template."""
        circuit = self._find_circuit(entity_id)
        if circuit is None:
            raise KeyError(f"Entity not found: {entity_id}")

        template_name = circuit["template"]
        template = self._templates().get(template_name, {})
        tod = template.setdefault("time_of_day_profile", {"enabled": True})
        tod["enabled"] = True
        tod["hourly_multipliers"] = {h: v for h, v in sorted(multipliers.items())}

        peak_hours = [h for h, v in multipliers.items() if v >= 0.8]
        if peak_hours:
            tod["peak_hours"] = sorted(peak_hours)

    def apply_preset(
        self,
        entity_id: str,
        preset_name: str,
        month: int,
        day: int,
        start_hour: int = 0,
        end_hour: int = 24,
    ) -> dict[int, float]:
        """Apply a named preset to an entity's profile and return the multipliers."""
        multipliers = get_preset(
            preset_name, month=month, day=day,
            start_hour=start_hour, end_hour=end_hour,
        )
        self.update_entity_profile(entity_id, multipliers)
        return multipliers
