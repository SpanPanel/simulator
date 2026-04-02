"""SimulatedCircuit — per-circuit simulation state and snapshot generation.

Each instance is constructed once (at engine init / reload) with its
circuit definition, resolved template, and a shared RealisticBehaviorEngine
reference.  The engine calls ``tick()`` each cycle, then reads properties
or calls ``to_snapshot()`` to produce the transport-agnostic dataclass.
"""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, cast

from span_panel_simulator.models import SpanCircuitSnapshot

if TYPE_CHECKING:
    from span_panel_simulator.config_types import (
        CircuitDefinitionExtended,
        CircuitTemplateExtended,
    )
    from span_panel_simulator.engine import RealisticBehaviorEngine


_STANDARD_BREAKER_SIZES = (15, 20, 25, 30, 40, 50, 60, 70, 80, 100, 125, 150, 200)


class SimulatedCircuit:
    """Encapsulates the state and logic for a single simulated circuit."""

    def __init__(
        self,
        circuit_def: CircuitDefinitionExtended,
        template: CircuitTemplateExtended,
        behavior_engine: RealisticBehaviorEngine,
    ) -> None:
        self._circuit_def = circuit_def
        self._template = deepcopy(template)
        self._behavior_engine = behavior_engine

        # Per-circuit recorder link lives on the circuit definition in YAML;
        # merge it so behaviour engine / modeling see ``recorder_entity``.
        rec_entity = circuit_def.get("recorder_entity")
        if rec_entity is not None:
            self._template["recorder_entity"] = rec_entity

        # Apply circuit-level overrides to the template, routing
        # energy-profile keys into the nested ``energy_profile`` dict.
        if "overrides" in circuit_def:
            _ENERGY_PROFILE_KEYS = {
                "mode",
                "power_range",
                "typical_power",
                "power_variation",
                "efficiency",
                "nameplate_capacity_w",
            }
            for key, value in circuit_def["overrides"].items():
                if key in _ENERGY_PROFILE_KEYS:
                    self._template["energy_profile"][key] = value  # type: ignore[literal-required]
                else:
                    self._template[key] = value  # type: ignore[literal-required]

        # Circuit-level breaker_rating overrides template
        if "breaker_rating" in circuit_def:
            self._template["breaker_rating"] = circuit_def["breaker_rating"]

        # Derived from template (stable across ticks)
        self._energy_mode: str = self._template["energy_profile"]["mode"]
        self._device_type_str: str = self._derive_device_type()

        # Mutable per-tick state
        self._instant_power_w = 0.0
        self._relay_state = "CLOSED"
        self._priority = self._template["priority"]
        self._produced_energy_wh = 0.0
        self._consumed_energy_wh = 0.0
        self._last_energy_update: float | None = None
        self._last_tick_time = 0

        # Dynamic overrides (set by dashboard / API)
        self._overrides: dict[str, object] = {}

        # Seed energy counters: prefer explicit seeds from clone, fall back to estimate
        ep = self._template["energy_profile"]
        initial_consumed = ep.get("initial_consumed_energy_wh")
        initial_produced = ep.get("initial_produced_energy_wh")
        if initial_consumed is not None or initial_produced is not None:
            self._consumed_energy_wh = float(initial_consumed) if initial_consumed else 0.0
            self._produced_energy_wh = float(initial_produced) if initial_produced else 0.0
        else:
            produced, consumed = behavior_engine.estimate_annual_energy_wh(self._template)
            self._produced_energy_wh, self._consumed_energy_wh = produced, consumed

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def tick(self, current_time: float, *, power_override: float | None = None) -> None:
        """Advance the circuit by one simulation step.

        Args:
            current_time: Simulation timestamp (seconds since epoch).
            power_override: If set, use this power instead of behaviour engine
                            (used for tab-sync groups where the engine computes
                            the split externally).
        """
        # Apply state overrides (relay, priority) before power computation
        # so get_circuit_power sees the current relay state immediately.
        self._apply_state_overrides()

        # Compute base power
        if power_override is not None:
            base_power = power_override
        else:
            base_power = self._behavior_engine.get_circuit_power(
                self._circuit_def["id"],
                self._template,
                current_time,
                self._relay_state,
            )

        self._instant_power_w = base_power

        # Apply power overrides after computation
        self._apply_power_overrides()

        # Accumulate energy
        self._accumulate_energy(current_time)

        self._last_tick_time = int(current_time)

    def to_snapshot(self) -> SpanCircuitSnapshot:
        """Produce a frozen snapshot of the current circuit state."""
        tabs = self._circuit_def.get("tabs", [])
        controllable = self._template["relay_behavior"] == "controllable"
        is_240v = len(tabs) == 2
        voltage = 240.0 if is_240v else 120.0

        breaker_rating = self._template.get("breaker_rating")
        if breaker_rating is None:
            max_power = max(abs(x) for x in self._template["energy_profile"]["power_range"])
            breaker_rating = self._derive_breaker_rating(max_power, voltage)

        return SpanCircuitSnapshot(
            circuit_id=self._circuit_def["id"],
            name=self._circuit_def["name"],
            relay_state=self._relay_state,
            instant_power_w=self._instant_power_w,
            produced_energy_wh=self._produced_energy_wh,
            consumed_energy_wh=self._consumed_energy_wh,
            tabs=tabs,
            priority=self._priority,
            is_user_controllable=controllable,
            is_sheddable=self._priority in ("OFF_GRID", "SOC_THRESHOLD"),
            is_never_backup=False,
            always_on=not controllable,
            device_type=self._device_type_str,
            is_240v=is_240v,
            current_a=abs(self._instant_power_w) / voltage,
            breaker_rating_a=float(breaker_rating),
            energy_accum_update_time_s=self._last_tick_time,
            instant_power_update_time_s=self._last_tick_time,
        )

    def apply_override(self, overrides: dict[str, object]) -> None:
        """Set dynamic overrides (from dashboard / REST API)."""
        self._overrides.update(overrides)

    def clear_overrides(self) -> None:
        """Remove all dynamic overrides."""
        self._overrides.clear()

    # ------------------------------------------------------------------
    # Properties (for engine aggregation)
    # ------------------------------------------------------------------

    @property
    def circuit_id(self) -> str:
        return self._circuit_def["id"]

    @property
    def name(self) -> str:
        return str(self._circuit_def["name"])

    @property
    def instant_power_w(self) -> float:
        return self._instant_power_w

    @property
    def device_type(self) -> str:
        return self._device_type_str

    @property
    def energy_mode(self) -> str:
        return self._energy_mode

    @property
    def produced_energy_wh(self) -> float:
        return self._produced_energy_wh

    @property
    def consumed_energy_wh(self) -> float:
        return self._consumed_energy_wh

    @property
    def tabs(self) -> list[int]:
        return self._circuit_def.get("tabs", [])

    @property
    def template_name(self) -> str:
        return str(self._circuit_def["template"])

    @property
    def template(self) -> CircuitTemplateExtended:
        return self._template

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _derive_breaker_rating(max_power_w: float, voltage: float) -> int:
        """Derive a standard breaker rating from max power and voltage."""
        amps = max_power_w / voltage
        for size in _STANDARD_BREAKER_SIZES:
            if amps <= size:
                return size
        return _STANDARD_BREAKER_SIZES[-1]

    def _derive_device_type(self) -> str:
        """Derive device_type from the template.

        Checks for an explicit ``device_type`` field first, then falls back
        to mode-based detection.  Bidirectional circuits with
        ``battery_behavior.enabled`` are batteries, not EVSE.
        """
        explicit = self._template.get("device_type")
        if explicit:
            return explicit
        mode = self._template.get("energy_profile", {}).get("mode", "consumer")
        if mode == "producer":
            return "pv"
        if mode == "bidirectional":
            battery = self._template.get("battery_behavior", {})
            if isinstance(battery, dict) and battery.get("enabled", False):
                return "circuit"
            return "evse"
        return "circuit"

    def _apply_state_overrides(self) -> None:
        """Apply relay and priority overrides before power computation."""
        if not self._overrides:
            return
        if "relay_state" in self._overrides:
            self._relay_state = str(self._overrides["relay_state"])
        if "priority" in self._overrides:
            self._priority = str(self._overrides["priority"])

    def _apply_power_overrides(self) -> None:
        """Apply power overrides after power computation."""
        if not self._overrides:
            return
        if "power_override" in self._overrides:
            self._instant_power_w = float(self._overrides["power_override"])  # type: ignore[arg-type]
        elif "power_multiplier" in self._overrides:
            self._instant_power_w *= float(self._overrides["power_multiplier"])  # type: ignore[arg-type]
        if self._relay_state == "OPEN":
            self._instant_power_w = 0.0

    def _accumulate_energy(self, current_time: float) -> None:
        """Unified energy accumulation — replaces three separate methods."""
        if self._last_energy_update is None:
            self._last_energy_update = current_time
            return

        time_elapsed_hours = (current_time - self._last_energy_update) / 3600.0
        self._last_energy_update = current_time

        if self._instant_power_w <= 0:
            return

        energy_increment = self._instant_power_w * time_elapsed_hours

        if self._energy_mode == "producer":
            self._produced_energy_wh += energy_increment
        elif self._energy_mode == "bidirectional":
            direction = self._resolve_battery_direction(current_time)
            if direction == "discharging":
                self._produced_energy_wh += energy_increment
            else:
                # charging, idle, unknown → consumption
                self._consumed_energy_wh += energy_increment
        else:
            # consumer
            self._consumed_energy_wh += energy_increment

    def _resolve_battery_direction(self, current_time: float) -> str:
        """Determine battery direction from template config or engine state."""
        battery_config = self._template.get("battery_behavior", {})
        if not isinstance(battery_config, dict):
            return "unknown"
        if not battery_config.get("enabled", True):
            return "unknown"

        charge_mode = cast("str", battery_config.get("charge_mode", "custom"))
        if charge_mode != "custom":
            return self._behavior_engine.last_battery_direction

        current_hour = self._behavior_engine.local_hour(current_time)
        charge_hours = cast("list[int]", battery_config.get("charge_hours", []))
        discharge_hours = cast("list[int]", battery_config.get("discharge_hours", []))
        idle_hours = cast("list[int]", battery_config.get("idle_hours", []))

        if current_hour in charge_hours:
            return "charging"
        if current_hour in discharge_hours:
            return "discharging"
        if current_hour in idle_hours:
            return "idle"
        return "unknown"
