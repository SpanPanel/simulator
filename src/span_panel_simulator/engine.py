"""Simulation engine for the standalone eBus simulator.

Orchestrates ``SimulatedCircuit`` instances, a ``SimulationClock``, and
optional ``BatteryStorageEquipment`` (BSEE) to produce
``SpanPanelSnapshot`` objects from YAML configuration.

Circuit-level logic lives in ``circuit.py``; time management in
``clock.py``; config TypedDicts in ``config_types.py``.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime
from pathlib import Path
import random
import threading
from typing import Any

import yaml

from span_panel_simulator.bsee import BatteryStorageEquipment
from span_panel_simulator.circuit import SimulatedCircuit
from span_panel_simulator.clock import SimulationClock
from span_panel_simulator.config_types import (
    BatteryBehavior,
    CircuitTemplateExtended,
    SimulationConfig,
    TabSynchronization,
)
from span_panel_simulator.exceptions import SimulationConfigurationError
from span_panel_simulator.hvac import hvac_seasonal_factor
from span_panel_simulator.solar import daily_weather_factor, solar_production_factor
from span_panel_simulator.weather import get_cached_weather
from span_panel_simulator.models import (
    SpanBatterySnapshot,
    SpanCircuitSnapshot,
    SpanEvseSnapshot,
    SpanPanelSnapshot,
    SpanPVSnapshot,
)
from span_panel_simulator.validation import validate_yaml_config

# Constants inlined from span-panel-api (simple string values)
DSM_ON_GRID = "DSM_ON_GRID"
DSM_OFF_GRID = "DSM_OFF_GRID"
MAIN_RELAY_CLOSED = "CLOSED"
PANEL_ON_GRID = "PANEL_ON_GRID"
PANEL_OFF_GRID = "PANEL_OFF_GRID"


# ---------------------------------------------------------------------------
# RealisticBehaviorEngine (unchanged — power modulation logic)
# ---------------------------------------------------------------------------


class RealisticBehaviorEngine:
    """Engine for realistic circuit behaviors."""

    def __init__(self, simulation_start_time: float, config: SimulationConfig) -> None:
        self._start_time = simulation_start_time
        self._config = config
        self._circuit_cycle_states: dict[str, dict[str, Any]] = {}
        self._last_battery_direction: str = "idle"
        self._solar_excess_w: float = 0.0

    @property
    def last_battery_direction(self) -> str:
        """Most recent battery direction set by charge mode logic."""
        return self._last_battery_direction

    def set_solar_excess(self, excess_w: float) -> None:
        """Set the solar excess watts for solar-excess charge mode."""
        self._solar_excess_w = excess_w

    def get_circuit_power(
        self, circuit_id: str, template: CircuitTemplateExtended, current_time: float, relay_state: str = "CLOSED"
    ) -> float:
        """Get realistic power for a circuit based on its template and current conditions."""
        if relay_state == "OPEN":
            return 0.0

        energy_profile = template["energy_profile"]
        base_power = energy_profile["typical_power"]

        # Apply time-of-day modulation: producers always use the solar model
        if template["energy_profile"]["mode"] == "producer":
            base_power = self._apply_solar_day_night_cycle(base_power, current_time)
        elif template.get("time_of_day_profile", {}).get("enabled", False):
            base_power = self._apply_time_of_day_modulation(base_power, template, current_time)

        # Apply HVAC seasonal modulation
        base_power = self._apply_hvac_seasonal_modulation(base_power, template, current_time)

        # Apply cycling behavior
        if "cycling_pattern" in template:
            base_power = self._apply_cycling_behavior(circuit_id, base_power, template, current_time)

        # Apply battery behavior
        battery_behavior = template.get("battery_behavior", {})
        if isinstance(battery_behavior, dict) and battery_behavior.get("enabled", False):
            base_power = self._apply_battery_behavior(base_power, template, current_time)

        # Apply smart behavior
        if template.get("smart_behavior", {}).get("responds_to_grid", False):
            base_power = self._apply_smart_behavior(base_power, template, current_time)

        # Add random variation
        variation = energy_profile.get("power_variation", 0.1)
        noise_factor = self._config["simulation_params"].get("noise_factor", 0.02)
        total_variation = variation + noise_factor

        power_multiplier = 1.0 + random.uniform(-total_variation, total_variation)  # nosec B311
        final_power = base_power * power_multiplier

        # Clamp to template range
        min_power, max_power = energy_profile["power_range"]
        if energy_profile["mode"] == "producer":
            original_min, original_max = min_power, max_power
            min_power = 0.0
            max_power = max(abs(original_min), abs(original_max))
        final_power = max(min_power, min(max_power, final_power))

        return final_power

    def _apply_time_of_day_modulation(
        self, base_power: float, template: CircuitTemplateExtended, current_time: float
    ) -> float:
        """Apply time-of-day power modulation for consumer circuits."""
        current_hour = datetime.fromtimestamp(current_time).hour

        profile = template.get("time_of_day_profile", {})

        # Use explicit hour factors when available (EVSE schedules, custom profiles)
        hour_factors = profile.get("hour_factors", {})
        if hour_factors:
            return base_power * float(hour_factors.get(current_hour, 0.0))

        # Check hourly_multipliers (used by profile editor)
        hourly_mult = profile.get("hourly_multipliers", {})
        if hourly_mult:
            return base_power * float(hourly_mult.get(current_hour, 0.0))

        # Fallback: peak_hours based modulation
        peak_hours = profile.get("peak_hours", [])
        if current_hour in peak_hours:
            return base_power * 1.3
        if current_hour >= 22 or current_hour <= 6:
            return base_power * 0.3
        return base_power

    def _apply_solar_day_night_cycle(
        self, base_power: float, current_time: float
    ) -> float:
        """Apply solar day/night cycle using geographic sine model + weather."""
        lat = self._config["panel_config"].get("latitude", 37.7)
        lon = self._config["panel_config"].get("longitude", -122.4)
        factor = solar_production_factor(current_time, lat, lon)

        # Use historical monthly weather data when available
        monthly_factors: dict[int, float] | None = None
        cached = get_cached_weather(lat, lon)
        if cached is not None:
            monthly_factors = cached.monthly_factors

        weather = daily_weather_factor(
            current_time,
            seed=hash(self._config["panel_config"]["serial_number"]),
            monthly_factors=monthly_factors,
        )
        return abs(base_power) * factor * weather

    def _apply_hvac_seasonal_modulation(
        self, base_power: float, template: CircuitTemplateExtended, current_time: float
    ) -> float:
        """Scale power by seasonal HVAC factor when hvac_type is configured."""
        hvac_type = template.get("hvac_type")
        if not hvac_type:
            return base_power
        latitude = self._config["panel_config"].get("latitude", 37.7)
        return base_power * hvac_seasonal_factor(current_time, latitude, hvac_type)

    def _apply_cycling_behavior(
        self, circuit_id: str, base_power: float, template: CircuitTemplateExtended, current_time: float
    ) -> float:
        """Apply cycling on/off behavior (like HVAC)."""
        cycling = template.get("cycling_pattern", {})
        on_duration = cycling.get("on_duration", 900)
        off_duration = cycling.get("off_duration", 1800)

        cycle_length = on_duration + off_duration
        cycle_position = (current_time - self._start_time) % cycle_length

        if circuit_id not in self._circuit_cycle_states:
            self._circuit_cycle_states[circuit_id] = {"last_cycle_start": self._start_time, "is_on": True}

        is_on_phase = cycle_position < on_duration
        return base_power if is_on_phase else 0.0

    def _apply_smart_behavior(self, base_power: float, template: CircuitTemplateExtended, current_time: float) -> float:
        """Apply smart load behavior (like EV chargers responding to grid)."""
        smart = template.get("smart_behavior", {})
        max_reduction = smart.get("max_power_reduction", 0.5)

        current_hour = int((current_time % 86400) / 3600)
        if 17 <= current_hour <= 21:
            reduction_factor = 1.0 - max_reduction
            return base_power * reduction_factor

        return base_power

    def _apply_battery_behavior(self, base_power: float, template: CircuitTemplateExtended, current_time: float) -> float:
        """Apply battery behavior with charge mode support."""
        dt = datetime.fromtimestamp(current_time)
        current_hour = dt.hour

        battery_config = template.get("battery_behavior", {})
        if not isinstance(battery_config, dict):
            return base_power

        if not battery_config.get("enabled", True):
            return base_power

        discharge_hours: list[int] = battery_config.get("discharge_hours", [])
        idle_hours: list[int] = battery_config.get("idle_hours", [])

        # Discharge hours always take precedence regardless of charge mode
        if current_hour in discharge_hours:
            self._last_battery_direction = "discharging"
            return self._get_discharge_power(battery_config, current_hour)

        if current_hour in idle_hours:
            self._last_battery_direction = "idle"
            return self._get_idle_power(battery_config)

        charge_mode: str = battery_config.get("charge_mode", "custom")

        if charge_mode == "solar-gen":
            return self._get_solar_gen_charge_power(battery_config, current_time)

        if charge_mode == "solar-excess":
            return self._get_solar_excess_charge_power(battery_config)

        # "custom" — original schedule-based logic
        custom_charge_hours: list[int] = battery_config.get("charge_hours", [])
        if current_hour in custom_charge_hours:
            self._last_battery_direction = "charging"
            return self._get_charge_power(battery_config, current_hour)

        self._last_battery_direction = "idle"
        return base_power * 0.1

    def _get_charge_power(self, battery_config: BatteryBehavior, current_hour: int) -> float:
        """Get charging power for the current hour."""
        max_charge_power: float = battery_config.get("max_charge_power", -3000.0)
        solar_intensity = self._get_solar_intensity_from_config(current_hour, battery_config)
        return abs(max_charge_power) * solar_intensity

    def _get_discharge_power(self, battery_config: BatteryBehavior, current_hour: int) -> float:
        """Get discharging power for the current hour."""
        max_discharge_power: float = battery_config.get("max_discharge_power", 2500.0)
        demand_factor = self._get_demand_factor_from_config(current_hour, battery_config)
        return abs(max_discharge_power) * demand_factor

    def _get_idle_power(self, battery_config: BatteryBehavior) -> float:
        """Get idle power (minimal power flow during low activity hours)."""
        idle_range: list[float] = battery_config.get("idle_power_range", [-100.0, 100.0])
        min_val, max_val = idle_range[0], idle_range[1]
        if min_val < 0 and max_val < 0:
            min_idle, max_idle = abs(max_val), abs(min_val)
        elif min_val < 0:
            min_idle, max_idle = 0.0, abs(max_val)
        else:
            min_idle, max_idle = min_val, max_val

        return random.uniform(min_idle, max_idle)  # nosec B311

    def _get_solar_intensity_from_config(self, hour: int, battery_config: BatteryBehavior) -> float:
        """Get solar intensity from YAML configuration."""
        solar_profile: dict[int, float] = battery_config.get("solar_intensity_profile", {})
        return solar_profile.get(hour, 0.1)

    def _get_demand_factor_from_config(self, hour: int, battery_config: BatteryBehavior) -> float:
        """Get demand factor from YAML configuration."""
        demand_profile: dict[int, float] = battery_config.get("demand_factor_profile", {})
        return demand_profile.get(hour, 0.3)

    def _get_solar_gen_charge_power(self, battery_config: BatteryBehavior, current_time: float) -> float:
        """Charge at max_charge_power * solar_factor * weather_factor."""
        lat = self._config["panel_config"].get("latitude", 37.7)
        lon = self._config["panel_config"].get("longitude", -122.4)
        factor = solar_production_factor(current_time, lat, lon)

        if factor <= 0.0:
            self._last_battery_direction = "idle"
            return self._get_idle_power(battery_config)

        monthly_factors: dict[int, float] | None = None
        cached = get_cached_weather(lat, lon)
        if cached is not None:
            monthly_factors = cached.monthly_factors

        weather = daily_weather_factor(
            current_time,
            seed=hash(self._config["panel_config"]["serial_number"]),
            monthly_factors=monthly_factors,
        )

        max_charge: float = battery_config.get("max_charge_power", 3000.0)
        self._last_battery_direction = "charging"
        return abs(max_charge) * factor * weather

    def _get_solar_excess_charge_power(self, battery_config: BatteryBehavior) -> float:
        """Charge from surplus solar after loads are met."""
        if self._solar_excess_w <= 0.0:
            self._last_battery_direction = "idle"
            return self._get_idle_power(battery_config)

        max_charge: float = battery_config.get("max_charge_power", 3000.0)
        self._last_battery_direction = "charging"
        return min(self._solar_excess_w, abs(max_charge))


# ---------------------------------------------------------------------------
# DynamicSimulationEngine (orchestrator)
# ---------------------------------------------------------------------------


class DynamicSimulationEngine:
    """Enhanced simulation engine with YAML configuration support.

    After the modular refactoring, this class is a thin orchestrator:
    it owns the clock, behaviour engine, circuits, and BSEE, and
    coordinates them each tick to produce ``SpanPanelSnapshot``.
    """

    def __init__(
        self,
        serial_number: str | None = None,
        config_path: Path | str | None = None,
        config_data: SimulationConfig | None = None,
    ) -> None:
        self._config: SimulationConfig | None = None
        self._config_path = Path(config_path) if config_path else None
        self._config_data = config_data
        self._serial_number_override = serial_number
        self._fixture_loading_lock: asyncio.Lock | None = None
        self._lock_init_lock = threading.Lock()

        # Sub-components
        self._clock = SimulationClock()
        self._behavior_engine: RealisticBehaviorEngine | None = None
        self._circuits: dict[str, SimulatedCircuit] = {}
        self._bsee: BatteryStorageEquipment | None = None

        # Dynamic overrides (dispatched to circuits)
        self._dynamic_overrides: dict[str, dict[str, Any]] = {}
        self._global_overrides: dict[str, Any] = {}

        # Grid control state
        self._forced_grid_offline: bool = False

        # Tab synchronization tracking
        self._tab_sync_groups: dict[int, str] = {}  # tab_number -> sync_group_id
        self._sync_group_power: dict[str, float] = {}  # sync_group_id -> total_power

        self._initialized = False

    # ------------------------------------------------------------------
    # Initialization
    # ------------------------------------------------------------------

    async def initialize_async(self) -> None:
        """Initialize the simulation engine asynchronously."""
        if self._initialized:
            return

        if self._fixture_loading_lock is None:
            with self._lock_init_lock:
                if self._fixture_loading_lock is None:
                    self._fixture_loading_lock = asyncio.Lock()

        async with self._fixture_loading_lock:
            if self._initialized:
                return

            await self._load_config_async()

            if not self._config:
                raise ValueError("YAML configuration is required")

            self._initialize_tab_synchronizations()
            self._clock.initialize(self._config.get("simulation_params", {}))
            self._behavior_engine = RealisticBehaviorEngine(self._clock.real_start_time, self._config)
            self._build_circuits()
            self._bsee = self._create_bsee()
            self._initialized = True

    async def _load_config_async(self) -> None:
        """Load simulation configuration asynchronously."""
        if self._config_data:
            self._validate_yaml_config(self._config_data)
            self._config = self._config_data
        elif self._config_path and self._config_path.exists():
            loop = asyncio.get_event_loop()
            self._config = await loop.run_in_executor(None, self._load_yaml_config, self._config_path)
        else:
            raise ValueError("YAML configuration is required")

        if self._serial_number_override and self._config:
            self._config["panel_config"]["serial_number"] = self._serial_number_override

    def _load_yaml_config(self, config_path: Path) -> SimulationConfig:
        """Load YAML configuration file synchronously."""
        with config_path.open() as f:
            config_data = yaml.safe_load(f)
            self._validate_yaml_config(config_data)
            return config_data  # type: ignore[no-any-return]

    def _validate_yaml_config(self, config_data: dict[str, Any] | SimulationConfig) -> None:
        """Validate YAML configuration structure and required fields."""
        if not isinstance(config_data, dict):
            raise ValueError("YAML configuration must be a dictionary")
        validate_yaml_config(config_data)

    def _build_circuits(self) -> None:
        """Construct SimulatedCircuit instances from config."""
        if not self._config or not self._behavior_engine:
            raise SimulationConfigurationError("Configuration and behavior engine required")

        self._circuits = {}
        for circuit_def in self._config["circuits"]:
            template_name = circuit_def["template"]
            template = self._config["circuit_templates"][template_name]
            self._circuits[circuit_def["id"]] = SimulatedCircuit(
                circuit_def, template, self._behavior_engine
            )

    # ------------------------------------------------------------------
    # Clock delegation (preserves public API)
    # ------------------------------------------------------------------

    def get_current_simulation_time(self) -> float:
        """Get current time for simulation (either real time or simulation time)."""
        return self._clock.current_time

    def override_simulation_start_time(self, start_time_str: str) -> None:
        """Override the simulation start time after initialization."""
        self._clock.set_time(start_time_str)

    # ------------------------------------------------------------------
    # Grid control
    # ------------------------------------------------------------------

    @property
    def grid_online(self) -> bool:
        """Whether the utility grid is connected."""
        return not self._forced_grid_offline

    def set_grid_online(self, online: bool) -> None:
        """Force the grid online or offline."""
        self._forced_grid_offline = not online
        if self._bsee is not None:
            self._bsee.set_forced_offline(not online)

    @property
    def is_grid_islandable(self) -> bool:
        """Whether PV can operate when grid is disconnected."""
        if self._bsee is not None:
            return self._bsee.grid_islandable
        return False

    def set_grid_islandable(self, islandable: bool) -> None:
        """Set whether PV can operate when grid is disconnected."""
        if self._bsee is not None:
            self._bsee.set_islandable(islandable)

    @property
    def has_battery(self) -> bool:
        """Whether a BESS is configured."""
        return self._bsee is not None

    # ------------------------------------------------------------------
    # Public properties & accessors
    # ------------------------------------------------------------------

    @property
    def serial_number(self) -> str:
        """Get the simulated panel serial number."""
        if self._config:
            return self._config["panel_config"]["serial_number"]
        if self._serial_number_override:
            return self._serial_number_override
        raise ValueError("No configuration loaded - serial number not available")

    # ------------------------------------------------------------------
    # Snapshot generation
    # ------------------------------------------------------------------

    async def get_snapshot(self) -> SpanPanelSnapshot:
        """Build a transport-agnostic snapshot from current simulation state."""
        if not self._config:
            raise SimulationConfigurationError("Configuration not loaded")

        current_time = self._clock.current_time

        # 1. Identify solar-excess battery circuits for two-pass tick
        solar_excess_ids: set[str] = set()
        for cid, circuit in self._circuits.items():
            battery_cfg = circuit.template.get("battery_behavior", {})
            if (
                isinstance(battery_cfg, dict)
                and battery_cfg.get("enabled", False)
                and battery_cfg.get("charge_mode") == "solar-excess"
            ):
                solar_excess_ids.add(cid)

        # Pass 1: tick all circuits except solar-excess batteries
        for cid, circuit in self._circuits.items():
            if cid in solar_excess_ids:
                continue
            sync_override = self._get_sync_power_override(circuit)
            circuit.tick(current_time, power_override=sync_override)

        # Pass 2: compute excess and tick solar-excess batteries
        if solar_excess_ids and self._behavior_engine is not None:
            pv_total = 0.0
            load_total = 0.0
            for circuit in self._circuits.values():
                if circuit.circuit_id in solar_excess_ids:
                    continue
                if circuit.energy_mode == "producer":
                    pv_total += circuit.instant_power_w
                elif circuit.energy_mode != "bidirectional":
                    load_total += circuit.instant_power_w
            self._behavior_engine.set_solar_excess(max(0.0, pv_total - load_total))
            for cid in solar_excess_ids:
                circuit = self._circuits[cid]
                sync_override = self._get_sync_power_override(circuit)
                circuit.tick(current_time, power_override=sync_override)

        # 2. Apply global overrides
        self._apply_global_overrides()

        # 2b. Handle forced grid offline + load shedding
        shed_ids: set[str] = set()
        if self._forced_grid_offline:
            if self._bsee is None:
                # No battery: panel is dead — zero all circuits
                for circuit in self._circuits.values():
                    circuit._instant_power_w = 0.0
            else:
                soc = self._bsee.soe_percentage
                soc_threshold = self._config["panel_config"].get(
                    "soc_shed_threshold", 20.0
                )
                for circuit in self._circuits.values():
                    # PV: shed if not islandable
                    if circuit.energy_mode == "producer":
                        if not self._bsee.grid_islandable:
                            circuit._instant_power_w = 0.0
                        continue
                    # Battery: never shed
                    if circuit.energy_mode == "bidirectional":
                        continue
                    # User relay override takes precedence over shedding
                    cid_overrides = self._dynamic_overrides.get(circuit.circuit_id, {})
                    if "relay_state" in cid_overrides:
                        continue
                    # Consumer shedding by priority
                    if circuit._priority == "OFF_GRID":
                        circuit._instant_power_w = 0.0
                        shed_ids.add(circuit.circuit_id)
                    elif circuit._priority == "SOC_THRESHOLD" and soc < soc_threshold:
                        circuit._instant_power_w = 0.0
                        shed_ids.add(circuit.circuit_id)

        # 3. Collect circuit snapshots (apply shedding overlay)
        circuit_snapshots: dict[str, SpanCircuitSnapshot] = {}
        for cid, circuit in self._circuits.items():
            snap = circuit.to_snapshot()
            if cid in shed_ids:
                snap = replace(
                    snap,
                    relay_state="OPEN",
                    relay_requester="BACKUP",
                    instant_power_w=0.0,
                )
            circuit_snapshots[cid] = snap

        # 4. Add unmapped tabs
        self._add_unmapped_tabs(circuit_snapshots)

        # 5. Aggregate totals
        total_consumption = 0.0
        total_production = 0.0
        total_produced_energy = 0.0
        total_consumed_energy = 0.0

        for circuit in self._circuits.values():
            power = circuit.instant_power_w
            if circuit.energy_mode == "producer":
                total_production += power
            elif circuit.energy_mode == "bidirectional":
                battery_dir = circuit._resolve_battery_direction(current_time)
                if battery_dir == "discharging":
                    total_production += power
                else:
                    total_consumption += power
            else:
                total_consumption += power
            total_produced_energy += circuit.produced_energy_wh
            total_consumed_energy += circuit.consumed_energy_wh

        grid_power = total_consumption - total_production

        # Disconnected from grid → no grid power flow
        if self._forced_grid_offline:
            grid_power = 0.0

        # 6. Battery / BSEE
        battery_circuit = self._find_battery_circuit()
        battery_power_w = battery_circuit.instant_power_w if battery_circuit else 0.0
        if self._bsee is not None:
            self._bsee.update(current_time, battery_power_w)

            # Reflect effective power back — BSEE may have zeroed it
            # (e.g. SOE hit backup reserve or full charge)
            effective_power = self._bsee.battery_power_w
            if battery_circuit is not None and effective_power != battery_power_w:
                battery_circuit._instant_power_w = effective_power
                battery_power_w = effective_power

            battery_snapshot = SpanBatterySnapshot(
                soe_percentage=self._bsee.soe_percentage,
                soe_kwh=self._bsee.soe_kwh,
                vendor_name=self._bsee.vendor_name,
                product_name=self._bsee.product_name,
                model=self._bsee.model,
                serial_number=self._bsee.serial_number,
                software_version=self._bsee.software_version,
                nameplate_capacity_kwh=self._bsee.nameplate_capacity_kwh,
                connected=self._bsee.connected,
                feed_circuit_id=self._bsee.feed_circuit_id,
            )
            dominant_power_source = self._bsee.dominant_power_source
            grid_state = self._bsee.grid_state
            grid_islandable = self._bsee.grid_islandable
            dsm_state = DSM_OFF_GRID if grid_state == "OFF_GRID" else DSM_ON_GRID
            current_run_config = PANEL_OFF_GRID if grid_state == "OFF_GRID" else PANEL_ON_GRID
            # Off-grid: battery covers load deficit (consumption minus PV)
            if self._forced_grid_offline:
                power_flow_battery = total_consumption - total_production
            else:
                power_flow_battery = battery_power_w
        else:
            battery_snapshot = SpanBatterySnapshot()
            if self._forced_grid_offline:
                dominant_power_source = None
                grid_state = "OFF_GRID"
                grid_islandable = False
                dsm_state = DSM_OFF_GRID
                current_run_config = PANEL_OFF_GRID
            else:
                dominant_power_source = "GRID"
                grid_state = None
                grid_islandable = False
                dsm_state = DSM_ON_GRID
                current_run_config = PANEL_ON_GRID
            power_flow_battery = 0.0

        # 7. PV
        pv_snapshot = SpanPVSnapshot()
        pv_power = 0.0
        for cid, circ in circuit_snapshots.items():
            if circ.device_type == "pv":
                pv_snapshot = SpanPVSnapshot(
                    node_id=f"sim_pv_{cid}",
                    feed_circuit_id=cid,
                    vendor_name="Simulated",
                    product_name="Virtual PV Inverter",
                    nameplate_capacity_w=5000.0,
                )
                pv_power = circ.instant_power_w
                break

        # 8. EVSE
        evse_devices: dict[str, SpanEvseSnapshot] = {}
        for cid, circ in circuit_snapshots.items():
            if circ.device_type == "evse":
                evse_devices[f"sim_evse_{cid}"] = SpanEvseSnapshot(
                    node_id=f"sim_evse_{cid}",
                    feed_circuit_id=cid,
                    status="CHARGING" if circ.instant_power_w > 100 else "AVAILABLE",
                    lock_state="LOCKED" if circ.instant_power_w > 100 else "UNLOCKED",
                    advertised_current_a=32.0,
                    vendor_name="SPAN",
                    product_name="SPAN Drive",
                    serial_number=f"SIM-EVSE-{cid.upper()}",
                    software_version="1.0.0-sim",
                )

        # 9. Build panel snapshot
        total_tabs = self._config["panel_config"].get("total_tabs", 32)
        main_size = self._config["panel_config"].get("main_size", 200)
        feedthrough_power = 0.0

        # Main relay is open when grid is disconnected
        main_relay = "OPEN" if self._forced_grid_offline else MAIN_RELAY_CLOSED
        # Voltage drops to 0 when offline without battery
        line_voltage = 0.0 if (self._forced_grid_offline and self._bsee is None) else 120.0

        return SpanPanelSnapshot(
            serial_number=self._config["panel_config"]["serial_number"],
            firmware_version="sim/v1.0.0",
            main_relay_state=main_relay,
            instant_grid_power_w=grid_power,
            feedthrough_power_w=feedthrough_power,
            main_meter_energy_consumed_wh=total_consumed_energy,
            main_meter_energy_produced_wh=total_produced_energy,
            feedthrough_energy_consumed_wh=0.0,
            feedthrough_energy_produced_wh=0.0,
            dsm_state=dsm_state,
            current_run_config=current_run_config,
            door_state="CLOSED",
            proximity_proven=True,
            uptime_s=3600000,
            eth0_link=True,
            wlan_link=True,
            wwan_link=False,
            dominant_power_source=dominant_power_source,
            grid_state=grid_state,
            grid_islandable=grid_islandable,
            l1_voltage=line_voltage,
            l2_voltage=line_voltage,
            main_breaker_rating_a=main_size,
            wifi_ssid="SimulatedNetwork",
            vendor_cloud="CONNECTED",
            panel_size=total_tabs,
            power_flow_battery=power_flow_battery,
            power_flow_site=grid_power,
            power_flow_grid=grid_power,
            power_flow_pv=pv_power,
            upstream_l1_current_a=abs(grid_power / 240.0),
            upstream_l2_current_a=abs(grid_power / 240.0),
            downstream_l1_current_a=abs(feedthrough_power / 240.0),
            downstream_l2_current_a=abs(feedthrough_power / 240.0),
            circuits=circuit_snapshots,
            battery=battery_snapshot,
            pv=pv_snapshot,
            evse=evse_devices,
        )

    # ------------------------------------------------------------------
    # Dynamic overrides (dispatched to SimulatedCircuit instances)
    # ------------------------------------------------------------------

    def set_dynamic_overrides(
        self, circuit_overrides: dict[str, dict[str, Any]] | None = None, global_overrides: dict[str, Any] | None = None
    ) -> None:
        """Set dynamic overrides for circuits and global parameters."""
        if circuit_overrides:
            self._dynamic_overrides.update(circuit_overrides)
            for cid, overrides in circuit_overrides.items():
                if cid in self._circuits:
                    self._circuits[cid].apply_override(overrides)

        if global_overrides:
            self._global_overrides.update(global_overrides)

    def clear_dynamic_overrides(self) -> None:
        """Clear all dynamic overrides."""
        self._dynamic_overrides.clear()
        self._global_overrides.clear()
        for circuit in self._circuits.values():
            circuit.clear_overrides()

    # ------------------------------------------------------------------
    # Tab synchronization
    # ------------------------------------------------------------------

    def _initialize_tab_synchronizations(self) -> None:
        """Initialize tab synchronization groups from configuration."""
        if not self._config:
            return

        tab_syncs = self._config.get("tab_synchronizations", [])

        for sync_config in tab_syncs:
            sync_group_id = f"sync_{sync_config['behavior']}_{hash(tuple(sync_config['tabs']))}"
            for tab_num in sync_config["tabs"]:
                self._tab_sync_groups[tab_num] = sync_group_id

        for sync_group_id in set(self._tab_sync_groups.values()):
            self._sync_group_power[sync_group_id] = 0.0

    def _get_sync_power_override(self, circuit: SimulatedCircuit) -> float | None:
        """Return a power override if the circuit belongs to a sync group, else None."""
        circuit_tabs = circuit.tabs
        sync_config = None
        for tab_num in circuit_tabs:
            sync_config = self._get_tab_sync_config(tab_num)
            if sync_config:
                break

        if not sync_config or len(circuit_tabs) <= 1:
            return None

        # Multi-tab synced circuits: return None to use normal power calculation
        # but store sync group power for unmapped tab reference
        return None

    def _get_tab_sync_config(self, tab_num: int) -> TabSynchronization | None:
        """Get synchronization configuration for a specific tab."""
        if not self._config:
            raise SimulationConfigurationError("Simulation configuration is required for tab synchronization.")

        tab_syncs = self._config.get("tab_synchronizations", [])
        for sync_config in tab_syncs:
            if tab_num in sync_config["tabs"]:
                return sync_config
        return None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _apply_global_overrides(self) -> None:
        """Apply global power multiplier to all circuits after tick."""
        if "power_multiplier" not in self._global_overrides:
            return
        multiplier = float(self._global_overrides["power_multiplier"])
        for circuit in self._circuits.values():
            circuit._instant_power_w *= multiplier

    def _add_unmapped_tabs(self, circuit_snapshots: dict[str, SpanCircuitSnapshot]) -> None:
        """Fill in unmapped tabs as zero-power placeholder circuits."""
        if not self._config:
            return

        occupied_tabs: set[int] = set()
        for circuit in circuit_snapshots.values():
            occupied_tabs.update(circuit.tabs)

        if not occupied_tabs:
            return

        total_tabs = self._config["panel_config"].get("total_tabs", 32)
        panel_size = max(*occupied_tabs, total_tabs)
        for tab in range(1, panel_size + 1):
            if tab not in occupied_tabs:
                cid = f"unmapped_tab_{tab}"
                circuit_snapshots[cid] = SpanCircuitSnapshot(
                    circuit_id=cid,
                    name=f"Unmapped Tab {tab}",
                    relay_state="CLOSED",
                    instant_power_w=0.0,
                    produced_energy_wh=0.0,
                    consumed_energy_wh=0.0,
                    tabs=[tab],
                    priority="UNKNOWN",
                    is_user_controllable=False,
                    is_sheddable=False,
                    is_never_backup=False,
                )

    def _find_battery_circuit(self) -> SimulatedCircuit | None:
        """Find the battery circuit instance, if any."""
        for circuit in self._circuits.values():
            battery_cfg = circuit.template.get("battery_behavior", {})
            if isinstance(battery_cfg, dict) and battery_cfg.get("enabled", False):
                return circuit
        return None

    def _create_bsee(self) -> BatteryStorageEquipment | None:
        """Create a BSEE if the config contains a battery circuit."""
        if not self._config:
            return None
        for circuit_def in self._config["circuits"]:
            template_name = circuit_def.get("template", "")
            template = self._config["circuit_templates"].get(template_name, {})
            if not isinstance(template, dict):
                continue
            battery_cfg = template.get("battery_behavior", {})
            if isinstance(battery_cfg, dict) and battery_cfg.get("enabled", False):
                battery_dict: dict[str, Any] = dict(battery_cfg)
                nameplate: float = float(battery_cfg.get("nameplate_capacity_kwh", 13.5))
                return BatteryStorageEquipment(
                    battery_behavior=battery_dict,
                    panel_serial=self._config["panel_config"]["serial_number"],
                    feed_circuit_id=circuit_def["id"],
                    nameplate_capacity_kwh=nameplate,
                    behavior_engine=self._behavior_engine,
                )
        return None
