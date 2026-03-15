"""In-memory configuration state manager.

Holds the full simulator config as a mutable dict tree (matching the
YAML schema).  Changes only persist when the user explicitly saves.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

import yaml

from span_panel_simulator.dashboard.defaults import make_defaults
from span_panel_simulator.dashboard.presets import (
    EVSE_PRESETS,
    evse_schedule_factors,
    get_battery_preset,
    get_evse_preset,
    get_preset,
)
from span_panel_simulator.solar import compute_solar_curve
from span_panel_simulator.validation import validate_yaml_config
from span_panel_simulator.weather import get_cached_weather


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
    hvac_type: str | None = None
    breaker_rating: int | None = None
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
                "latitude": 37.7,
                "longitude": -122.4,
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
                if key == "total_tabs" and value % 2 != 0:
                    raise ValueError("Total tabs must be an even number")
                cfg[key] = value
        for key in ("latitude", "longitude", "soc_shed_threshold"):
            if key in data:
                cfg[key] = float(data[key])

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
        result: dict[str, Any] = self._state.setdefault("circuit_templates", {})
        return result

    def _circuits(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = self._state.setdefault("circuits", [])
        return result

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
            hvac_type=template.get("hvac_type"),
            breaker_rating=circuit.get("breaker_rating") or template.get("breaker_rating"),
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

        # PV nameplate: update the template directly and derive power_range
        if "nameplate_capacity_w" in data:
            nameplate = abs(float(data["nameplate_capacity_w"]))
            ep["nameplate_capacity_w"] = nameplate
            ep["power_range"] = [-nameplate, 0.0]
            ep["typical_power"] = -nameplate * 0.6
            overrides.pop("typical_power", None)
            overrides.pop("power_range", None)
        else:
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

        if "nameplate_capacity_kwh" in data or "backup_reserve_pct" in data:
            bb: dict[str, Any] = template.setdefault("battery_behavior", {})
            if "nameplate_capacity_kwh" in data:
                bb["nameplate_capacity_kwh"] = float(data["nameplate_capacity_kwh"])
            if "backup_reserve_pct" in data:
                bb["backup_reserve_pct"] = float(data["backup_reserve_pct"])

        if "breaker_rating" in data:
            br_val = str(data["breaker_rating"]).strip()
            if br_val:
                circuit["breaker_rating"] = int(br_val)
            else:
                circuit.pop("breaker_rating", None)

        if "hvac_type" in data:
            hvac_val = str(data["hvac_type"])
            if hvac_val and hvac_val != "none":
                template["hvac_type"] = hvac_val
            else:
                template.pop("hvac_type", None)

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
                    f"Double-pole tabs {tabs} must have the same parity (both odd or both even)"
                )
            if b - a != 2:
                raise ValueError(f"Double-pole tabs {tabs} must be exactly 2 apart")

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
        """Return resolved 24-hour multipliers for an entity.

        For PV entities the profile is computed from the geographic solar
        curve using the panel's latitude.  For EVSE entities the profile
        comes from ``hour_factors`` in the charging schedule.
        """
        entity = self.get_entity(entity_id)

        if entity.entity_type == "pv":
            lat = self._state.get("panel_config", {}).get("latitude", 37.7)
            return compute_solar_curve(6, 21, latitude=lat)

        tod = entity.time_of_day_profile
        if not tod or not tod.get("enabled"):
            return {h: 1.0 for h in range(24)}

        # Explicit hour factors (EVSE schedules, custom profiles)
        hf = tod.get("hour_factors", {})
        if hf:
            return {h: float(hf.get(h, hf.get(str(h), 0.0))) for h in range(24)}

        # Legacy hourly_multipliers key
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
        lat = self._state.get("panel_config", {}).get("latitude", 37.7)
        multipliers = get_preset(
            preset_name,
            month=month,
            day=day,
            start_hour=start_hour,
            end_hour=end_hour,
            latitude=lat,
        )
        self.update_entity_profile(entity_id, multipliers)
        return multipliers

    # -- Battery charge mode --

    def get_battery_charge_mode(self, entity_id: str) -> str:
        """Return the charge mode for a battery entity (default ``"custom"``)."""
        entity = self.get_entity(entity_id)
        bb = entity.battery_behavior or {}
        return str(bb.get("charge_mode", "custom"))

    def update_battery_charge_mode(self, entity_id: str, mode: str) -> None:
        """Set the charge mode on a battery entity's template."""
        valid_modes = ("custom", "solar-gen", "solar-excess")
        if mode not in valid_modes:
            raise ValueError(f"Invalid charge mode: {mode!r}")

        circuit = self._find_circuit(entity_id)
        if circuit is None:
            raise KeyError(f"Entity not found: {entity_id}")

        template_name = circuit["template"]
        template = self._templates().get(template_name, {})
        bb: dict[str, Any] = template.setdefault("battery_behavior", {"enabled": True})
        bb["charge_mode"] = mode

    # -- Battery profile --

    def get_battery_profile(self, entity_id: str) -> dict[int, str]:
        """Return the 24-hour battery schedule as hour → mode mapping.

        Mode is one of ``"charge"``, ``"discharge"``, or ``"idle"``.
        """
        entity = self.get_entity(entity_id)
        bb = entity.battery_behavior or {}
        charge_hours = set(bb.get("charge_hours", []))
        discharge_hours = set(bb.get("discharge_hours", []))

        profile: dict[int, str] = {}
        for h in range(24):
            if h in charge_hours:
                profile[h] = "charge"
            elif h in discharge_hours:
                profile[h] = "discharge"
            else:
                profile[h] = "idle"
        return profile

    def update_battery_profile(self, entity_id: str, hour_modes: dict[int, str]) -> None:
        """Write per-hour charge/discharge/idle schedule into battery_behavior."""
        circuit = self._find_circuit(entity_id)
        if circuit is None:
            raise KeyError(f"Entity not found: {entity_id}")

        template_name = circuit["template"]
        template = self._templates().get(template_name, {})
        bb = template.setdefault("battery_behavior", {"enabled": True})

        bb["charge_hours"] = sorted(h for h, m in hour_modes.items() if m == "charge")
        bb["discharge_hours"] = sorted(h for h, m in hour_modes.items() if m == "discharge")
        bb["idle_hours"] = sorted(h for h, m in hour_modes.items() if m == "idle")

    def apply_battery_preset(self, entity_id: str, preset_name: str) -> dict[int, str]:
        """Apply a named battery preset and return the schedule."""
        hour_modes = get_battery_preset(preset_name)
        self.update_battery_profile(entity_id, hour_modes)
        return hour_modes

    # -- EVSE schedule --

    def get_evse_schedule(self, entity_id: str) -> dict[str, Any]:
        """Return EVSE charging schedule info.

        Returns a dict with keys: start, duration, preset, profile.
        """
        entity = self.get_entity(entity_id)
        tod = entity.time_of_day_profile or {}

        if not tod.get("enabled", False):
            profile = {h: 1.0 for h in range(24)}
            return {"start": 0, "duration": 24, "preset": None, "profile": profile}

        hf = tod.get("hour_factors", {})
        if not hf:
            profile = {h: 1.0 for h in range(24)}
            return {"start": 0, "duration": 24, "preset": None, "profile": profile}

        profile = {h: float(hf.get(h, hf.get(str(h), 0.0))) for h in range(24)}

        # Derive start and duration, handling midnight wrap-around
        charging = sorted(h for h in range(24) if profile.get(h, 0.0) > 0)
        if not charging:
            start, duration = 0, 0
        elif len(charging) == 24:
            start, duration = 0, 24
        else:
            # Find the largest gap — start is the hour after that gap
            max_gap = 0
            start = charging[0]
            for i in range(len(charging)):
                cur = charging[i]
                nxt = charging[(i + 1) % len(charging)]
                gap = (nxt - cur) % 24
                if gap > max_gap:
                    max_gap = gap
                    start = nxt
            duration = len(charging)

        # Try to match a known preset
        active_preset: str | None = None
        for name, (ps, pd) in EVSE_PRESETS.items():
            expected = evse_schedule_factors(ps, pd)
            if all(profile.get(h, 0.0) == expected.get(h, 0.0) for h in range(24)):
                active_preset = name
                break

        return {
            "start": start,
            "duration": duration,
            "preset": active_preset,
            "profile": profile,
        }

    def update_evse_schedule(self, entity_id: str, start_hour: int, duration_hours: int) -> None:
        """Update EVSE charging schedule from start hour and duration."""
        circuit = self._find_circuit(entity_id)
        if circuit is None:
            raise KeyError(f"Entity not found: {entity_id}")

        template_name = circuit["template"]
        template = self._templates().get(template_name, {})
        tod: dict[str, Any] = template.setdefault("time_of_day_profile", {"enabled": True})
        tod["enabled"] = True
        tod["hour_factors"] = evse_schedule_factors(start_hour, duration_hours)

    def apply_evse_preset(self, entity_id: str, preset_name: str) -> dict[int, float]:
        """Apply an EVSE charging preset and return the schedule factors."""
        factors = get_evse_preset(preset_name)

        circuit = self._find_circuit(entity_id)
        if circuit is None:
            raise KeyError(f"Entity not found: {entity_id}")

        template_name = circuit["template"]
        template = self._templates().get(template_name, {})
        tod: dict[str, Any] = template.setdefault("time_of_day_profile", {"enabled": True})
        tod["enabled"] = True
        tod["hour_factors"] = factors
        return factors

    # -- Energy projection --

    def compute_energy_projection(self, period: str = "year") -> list[dict[str, float | str]]:
        """Compute daily energy summaries for system sizing.

        Args:
            period: "week", "month", or "year".

        Returns:
            List of daily dicts with keys: date, consumption_kwh,
            pv_kwh, battery_kwh, grid_kwh.
        """
        panel = self.get_panel_config()
        lat = panel.get("latitude", 37.7)
        lon = panel.get("longitude", -122.4)

        cached = get_cached_weather(lat, lon)
        monthly_factors: dict[int, float] = {}
        if cached is not None:
            monthly_factors = cached.monthly_factors

        entities = self.list_entities()

        # Pre-compute per-entity hourly profiles (for consumer circuits)
        consumer_profiles: list[tuple[float, dict[int, float]]] = []
        pv_specs: list[tuple[float, float]] = []  # (nameplate, efficiency)
        battery_specs: list[tuple[float, float, list[int], list[int]]] = []

        for entity in entities:
            ep = entity.energy_profile
            if entity.entity_type == "pv":
                raw_np = ep.get("nameplate_capacity_w")
                nameplate = (
                    float(raw_np) if raw_np is not None else abs(float(ep["power_range"][0]))
                )
                raw_eff = ep.get("efficiency")
                efficiency = float(raw_eff) if raw_eff is not None else 0.85
                pv_specs.append((nameplate, efficiency))
            elif entity.entity_type == "battery":
                bb: dict[str, Any] = entity.battery_behavior or {}
                charge_p = abs(float(bb.get("charge_power") or 3500))
                discharge_p = abs(float(bb.get("discharge_power") or 3500))
                charge_hrs: list[int] = bb.get("charge_hours") or []
                discharge_hrs: list[int] = bb.get("discharge_hours") or []
                battery_specs.append((charge_p, discharge_p, charge_hrs, discharge_hrs))
            else:
                profile = self.get_entity_profile(entity.id)
                typical = float(ep["typical_power"])

                # Apply cycling duty cycle if explicitly configured
                cycling = entity.cycling_pattern
                duty = 1.0
                if cycling:
                    on_dur = cycling.get("on_duration", 900)
                    off_dur = cycling.get("off_duration", 1800)
                    duty = on_dur / (on_dur + off_dur) if (on_dur + off_dur) > 0 else 1.0

                consumer_profiles.append((typical * duty, profile))

        # Determine date range
        days_in_month = [0, 31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
        if period == "week":
            months_days = [(6, list(range(15, 22)))]
        elif period == "month":
            months_days = [(6, list(range(1, 31)))]
        else:  # year
            months_days = [(m, list(range(1, days_in_month[m] + 1))) for m in range(1, 13)]

        results: list[dict[str, float | str]] = []
        for month, days in months_days:
            solar_curve = compute_solar_curve(month, 15, latitude=lat)
            weather = monthly_factors.get(month, 0.85)

            for day in days:
                # Consumer consumption
                consumption_wh = 0.0
                for typical, profile in consumer_profiles:
                    for h in range(24):
                        consumption_wh += abs(typical) * profile.get(h, 0.0)

                # PV production
                pv_wh = 0.0
                for nameplate, eff in pv_specs:
                    for h in range(24):
                        pv_wh += nameplate * solar_curve.get(h, 0.0) * eff * weather

                # Battery net
                battery_wh = 0.0
                for charge_p, discharge_p, charge_hrs, discharge_hrs in battery_specs:
                    battery_wh -= charge_p * len(charge_hrs)
                    battery_wh += discharge_p * len(discharge_hrs)

                grid_wh = consumption_wh - pv_wh - battery_wh

                results.append(
                    {
                        "date": f"2025-{month:02d}-{day:02d}",
                        "consumption_kwh": round(consumption_wh / 1000, 2),
                        "pv_kwh": round(pv_wh / 1000, 2),
                        "battery_kwh": round(battery_wh / 1000, 2),
                        "grid_kwh": round(grid_wh / 1000, 2),
                    }
                )

        return results
