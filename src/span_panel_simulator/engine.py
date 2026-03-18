"""Simulation engine for the standalone eBus simulator.

Orchestrates ``SimulatedCircuit`` instances, a ``SimulationClock``, and
optional ``BatteryStorageEquipment`` (BSEE) to produce
``SpanPanelSnapshot`` objects from YAML configuration.

Circuit-level logic lives in ``circuit.py``; time management in
``clock.py``; config TypedDicts in ``config_types.py``.
"""

from __future__ import annotations

import asyncio
import random
import threading
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import yaml

from span_panel_simulator.bsee import BatteryStorageEquipment
from span_panel_simulator.circuit import SimulatedCircuit
from span_panel_simulator.clock import SimulationClock
from span_panel_simulator.exceptions import SimulationConfigurationError

if TYPE_CHECKING:
    from span_panel_simulator.config_types import (
        BatteryBehavior,
        CircuitTemplateExtended,
        SimulationConfig,
        TabSynchronization,
    )
    from span_panel_simulator.recorder import RecorderDataSource

from span_panel_simulator.hvac import hvac_seasonal_factor
from span_panel_simulator.models import (
    SpanBatterySnapshot,
    SpanCircuitSnapshot,
    SpanEvseSnapshot,
    SpanPanelSnapshot,
    SpanPcsSnapshot,
    SpanPVSnapshot,
)
from span_panel_simulator.solar import daily_weather_factor, solar_production_factor
from span_panel_simulator.validation import validate_yaml_config
from span_panel_simulator.weather import get_cached_weather

# Panel size → Homie model enum (from 202609 schema)
_PANEL_SIZE_TO_MODEL: dict[int, str] = {
    16: "MAIN_16",
    24: "MLO_24",
    32: "MAIN_32",
    40: "MAIN_40",
    48: "MLO_48",
}

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

    _DEFAULT_TZ = "America/Los_Angeles"

    def __init__(
        self,
        simulation_start_time: float,
        config: SimulationConfig,
        recorder: RecorderDataSource | None = None,
    ) -> None:
        self._start_time = simulation_start_time
        self._config = config
        self._recorder = recorder
        self._circuit_cycle_states: dict[str, dict[str, Any]] = {}
        self._last_battery_direction: str = "idle"
        self._solar_excess_w: float = 0.0
        self._grid_offline: bool = False
        self._tz = self._resolve_timezone(config)

    # ------------------------------------------------------------------
    # Timezone helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_timezone(config: SimulationConfig) -> ZoneInfo:
        """Resolve panel timezone: explicit config > lat/lon lookup > fallback."""
        panel = config["panel_config"]
        explicit = panel.get("time_zone")
        if explicit:
            try:
                return ZoneInfo(str(explicit))
            except (KeyError, ValueError):
                pass

        lat = panel.get("latitude")
        lon = panel.get("longitude")
        if lat is not None and lon is not None:
            from timezonefinder import TimezoneFinder

            tz_name = TimezoneFinder().timezone_at(lat=float(lat), lng=float(lon))
            if tz_name is not None:
                return ZoneInfo(tz_name)

        return ZoneInfo(RealisticBehaviorEngine._DEFAULT_TZ)

    @property
    def panel_timezone(self) -> ZoneInfo:
        """The resolved IANA timezone for the simulated panel."""
        return self._tz

    def local_hour(self, timestamp: float) -> int:
        """Return the hour-of-day at the panel's location."""
        return datetime.fromtimestamp(timestamp, tz=self._tz).hour

    def local_weekday(self, timestamp: float) -> int:
        """Return the day-of-week at the panel's location (0=Mon..6=Sun)."""
        return datetime.fromtimestamp(timestamp, tz=self._tz).weekday()

    def local_datetime(self, timestamp: float) -> datetime:
        """Return a timezone-aware datetime at the panel's location."""
        return datetime.fromtimestamp(timestamp, tz=self._tz)

    @property
    def last_battery_direction(self) -> str:
        """Most recent battery direction set by charge mode logic."""
        return self._last_battery_direction

    def set_solar_excess(self, excess_w: float) -> None:
        """Set the solar excess watts for solar-excess charge mode."""
        self._solar_excess_w = excess_w

    def set_grid_offline(self, offline: bool) -> None:
        """Propagate grid state so battery behaviour overrides schedules."""
        self._grid_offline = offline

    def get_circuit_power(
        self,
        circuit_id: str,
        template: CircuitTemplateExtended,
        current_time: float,
        relay_state: str = "CLOSED",
    ) -> float:
        """Get realistic power for a circuit based on its template and current conditions."""
        if relay_state == "OPEN":
            return 0.0

        # Recorder replay: if the circuit has a recorder_entity and recorded
        # data is available for this timestamp, return it directly instead
        # of running the synthetic modulation chain.
        recorder_entity = template.get("recorder_entity")
        if recorder_entity and self._recorder is not None:
            recorded = self._recorder.get_power(str(recorder_entity), current_time)
            if recorded is not None:
                return recorded

        energy_profile = template["energy_profile"]
        base_power = energy_profile["typical_power"]

        # Apply time-of-day modulation: producers always use the solar model
        if template["energy_profile"]["mode"] == "producer":
            base_power = self._apply_solar_day_night_cycle(base_power, current_time)
        elif template.get("time_of_day_profile", {}).get("enabled", False):
            base_power = self._apply_time_of_day_modulation(base_power, template, current_time)

        # Apply seasonal modulation (HA-derived monthly_factors or HVAC model)
        base_power = self._apply_seasonal_modulation(base_power, template, current_time)

        # Apply cycling behavior
        if "cycling_pattern" in template:
            base_power = self._apply_cycling_behavior(
                circuit_id, base_power, template, current_time
            )

        # Apply battery behavior
        battery_behavior = template.get("battery_behavior", {})
        if isinstance(battery_behavior, dict) and battery_behavior.get("enabled", False):
            base_power = self._apply_battery_behavior(base_power, template, current_time)

        # Apply smart behavior
        if template.get("smart_behavior", {}).get("responds_to_grid", False):
            base_power = self._apply_smart_behavior(base_power, template, current_time)

        # Add random variation.  Cap total noise so that HA-derived profiles
        # with high coefficient-of-variation (bursty loads like EV/spa where
        # std ≫ mean) don't produce ±100 % swings every tick.  The hourly
        # profile already captures the macro pattern; this jitter is just
        # tick-to-tick measurement noise.
        variation = energy_profile.get("power_variation", 0.1)
        noise_factor = self._config["simulation_params"].get("noise_factor", 0.02)
        total_variation = min(variation + noise_factor, 0.15)

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
        profile = template.get("time_of_day_profile", {})

        # Skip inactive days — return 0 power
        active_days: list[int] = profile.get("active_days", [])
        if active_days and self.local_weekday(current_time) not in active_days:
            return 0.0

        current_hour = self.local_hour(current_time)

        # Use explicit hour factors when available (EVSE schedules, custom profiles)
        hour_factors = profile.get("hour_factors", {})
        if hour_factors:
            factor = float(hour_factors.get(current_hour, 0.0))
            # Normalise so the average across all hours equals base_power.
            # hour_factors are peak-normalised (peak = 1.0), so their mean
            # is always < 1.  Without this correction, typical_power * 1.0
            # at the peak hour gives only the overall mean, not the peak.
            mean_hf = sum(float(v) for v in hour_factors.values()) / max(len(hour_factors), 1)
            if mean_hf > 0:
                return base_power / mean_hf * factor
            return 0.0

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

    def _apply_solar_day_night_cycle(self, base_power: float, current_time: float) -> float:
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

    def _apply_seasonal_modulation(
        self, base_power: float, template: CircuitTemplateExtended, current_time: float
    ) -> float:
        """Scale power by seasonal factors.

        Checks ``monthly_factors`` first (HA-derived, works for any load
        type -- pool pumps, HVAC, seasonal appliances).  Falls back to
        the latitude-based ``hvac_type`` model for hand-authored configs.
        """
        monthly = template.get("monthly_factors")
        if monthly:
            month = self.local_datetime(current_time).month
            factor = float(monthly.get(month, 1.0))
            # Same normalisation as hour_factors: the mean of the monthly
            # factors is < 1.0, so multiplying directly under-represents
            # power.  Dividing by the mean preserves the correct average.
            mean_mf = sum(float(v) for v in monthly.values()) / max(len(monthly), 1)
            if mean_mf > 0:
                return base_power / mean_mf * factor
            return base_power

        hvac_type = template.get("hvac_type")
        if not hvac_type:
            return base_power
        latitude = self._config["panel_config"].get("latitude", 37.7)
        return base_power * hvac_seasonal_factor(current_time, latitude, hvac_type, tz=self._tz)

    def _apply_cycling_behavior(
        self,
        circuit_id: str,
        base_power: float,
        template: CircuitTemplateExtended,
        current_time: float,
    ) -> float:
        """Apply cycling on/off behavior (like HVAC).

        Accepts either explicit ``on_duration``/``off_duration`` or a
        statistical ``duty_cycle`` (0.0-1.0).  When ``duty_cycle`` is
        present it takes precedence -- the engine derives on/off from it
        and an optional ``period`` (default 2700 s / 45 min).

        When HA-derived ``hour_factors`` are present alongside a
        ``duty_cycle``, cycling is skipped because the hour_factors
        already incorporate the on/off behaviour from historical
        observations.  Applying duty_cycle on top would double-count.
        """
        cycling = template.get("cycling_pattern", {})

        dc = cycling.get("duty_cycle")
        if dc is not None:
            # When HA-derived hour_factors are present, the hourly means
            # already include cycling effects (a 3800 W EV charger running
            # 20 % of an hour shows up as an 800 W hourly mean).  Applying
            # binary duty-cycle gating on top would double-count, so skip.
            profile = template.get("time_of_day_profile", {})
            if profile.get("hour_factors"):
                return base_power

            period = cycling.get("period", 2700)
            on_duration = int(float(dc) * period)
            off_duration = period - on_duration
        else:
            on_duration = cycling.get("on_duration", 900)
            off_duration = cycling.get("off_duration", 1800)

        cycle_length = on_duration + off_duration
        if cycle_length <= 0:
            return base_power
        cycle_position = (current_time - self._start_time) % cycle_length

        if circuit_id not in self._circuit_cycle_states:
            self._circuit_cycle_states[circuit_id] = {
                "last_cycle_start": self._start_time,
                "is_on": True,
            }

        is_on_phase = cycle_position < on_duration
        return base_power if is_on_phase else 0.0

    def _apply_smart_behavior(
        self, base_power: float, template: CircuitTemplateExtended, current_time: float
    ) -> float:
        """Apply smart load behavior (like EV chargers responding to grid)."""
        smart = template.get("smart_behavior", {})
        max_reduction = smart.get("max_power_reduction", 0.5)

        current_hour = self.local_hour(current_time)
        if 17 <= current_hour <= 21:
            reduction_factor = 1.0 - max_reduction
            return base_power * reduction_factor

        return base_power

    def _apply_battery_behavior(
        self, base_power: float, template: CircuitTemplateExtended, current_time: float
    ) -> float:
        """Apply battery behavior with charge mode support."""
        battery_config = template.get("battery_behavior", {})
        if not isinstance(battery_config, dict):
            return base_power

        if not battery_config.get("enabled", True):
            return base_power

        current_hour = self.local_hour(current_time)

        # Grid outage override: battery must discharge to supply loads
        if self._grid_offline:
            self._last_battery_direction = "discharging"
            return self._get_discharge_power(battery_config, current_hour)

        # Skip inactive days — return idle power
        active_days: list[int] = battery_config.get("active_days", [])
        if active_days and self.local_weekday(current_time) not in active_days:
            self._last_battery_direction = "idle"
            return self._get_idle_power(battery_config)

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

    def _get_solar_intensity_from_config(
        self, hour: int, battery_config: BatteryBehavior
    ) -> float:
        """Get solar intensity from YAML configuration."""
        solar_profile: dict[int, float] = battery_config.get("solar_intensity_profile", {})
        return solar_profile.get(hour, 0.1)

    def _get_demand_factor_from_config(self, hour: int, battery_config: BatteryBehavior) -> float:
        """Get demand factor from YAML configuration."""
        demand_profile: dict[int, float] = battery_config.get("demand_factor_profile", {})
        return demand_profile.get(hour, 0.3)

    def _get_solar_gen_charge_power(
        self, battery_config: BatteryBehavior, current_time: float
    ) -> float:
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

    # ------------------------------------------------------------------
    # Annual energy estimation (seeds initial circuit counters)
    # ------------------------------------------------------------------

    def estimate_annual_energy_wh(self, template: CircuitTemplateExtended) -> tuple[float, float]:
        """Estimate one year of accumulated energy for seeding circuit counters.

        Dispatches on ``energy_profile.mode`` to produce a realistic starting
        baseline so circuits don't begin at 0 Wh.

        Returns:
            ``(produced_wh, consumed_wh)`` estimated over ~1 year.
        """
        mode = template["energy_profile"]["mode"]

        if mode == "producer":
            solar_factor = self._estimate_solar_annual_factor()
            produced = abs(template["energy_profile"]["typical_power"]) * solar_factor * 8760
            return (produced, 0.0)

        if mode == "bidirectional":
            return self._estimate_battery_annual_wh(template)

        return (0.0, self._estimate_consumer_annual_wh(template))

    def _estimate_solar_annual_factor(self) -> float:
        """Average solar x weather capacity factor across 12 representative days.

        Samples ``solar_production_factor * daily_weather_factor`` on the 15th
        of each month, every hour (288 total samples), using the configured
        latitude, longitude, and panel serial seed.
        """
        lat = self._config["panel_config"].get("latitude", 37.7)
        lon = self._config["panel_config"].get("longitude", -122.4)
        seed = hash(self._config["panel_config"]["serial_number"])

        monthly_factors: dict[int, float] | None = None
        cached = get_cached_weather(lat, lon)
        if cached is not None:
            monthly_factors = cached.monthly_factors

        days_in_month = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
        ref_jan1 = 1704067200  # 2024-01-01 00:00:00 UTC

        total = 0.0
        for month_idx in range(12):
            doy = sum(days_in_month[:month_idx]) + 15
            for hour in range(24):
                ts = ref_jan1 + (doy - 1) * 86400 + hour * 3600
                sf = solar_production_factor(ts, lat, lon)
                wf = daily_weather_factor(ts, seed=seed, monthly_factors=monthly_factors)
                total += sf * wf

        return total / 288.0  # 12 days x 24 hours

    def _estimate_consumer_annual_wh(self, template: CircuitTemplateExtended) -> float:
        """Estimate annual consumed energy for a consumer circuit.

        Uses ``typical_power``, duty cycle, time-of-day profile, HVAC seasonal
        adjustment, and smart behaviour to produce a realistic annual total.

        Circuits without any time modulation receive a conservative daily-usage
        estimate based on load magnitude to avoid assuming 24/7 operation.
        """
        typical_power: float = template["energy_profile"]["typical_power"]

        # Duty cycle from cycling pattern
        has_cycling = False
        cycling = template.get("cycling_pattern")
        if cycling:
            dc = cycling.get("duty_cycle")
            if dc is not None:
                duty_cycle = float(dc)
            else:
                on_dur = cycling.get("on_duration", 900)
                off_dur = cycling.get("off_duration", 1800)
                duty_cycle = on_dur / (on_dur + off_dur) if (on_dur + off_dur) > 0 else 1.0
            has_cycling = True
        else:
            duty_cycle = 1.0

        # Time-of-day average
        tod_avg = 1.0
        has_tod = False
        profile = template.get("time_of_day_profile", {})
        if profile.get("enabled", False):
            hour_factors = profile.get("hour_factors", {})
            hourly_mult = profile.get("hourly_multipliers", {})
            peak_hours = profile.get("peak_hours", [])

            if hour_factors:
                has_tod = True
                tod_avg = sum(float(hour_factors.get(h, 0.0)) for h in range(24)) / 24.0
            elif hourly_mult:
                has_tod = True
                tod_avg = sum(float(hourly_mult.get(h, 0.0)) for h in range(24)) / 24.0
            elif peak_hours:
                has_tod = True
                # For annual estimation, non-peak hours should be low since the
                # circuit is mostly inactive outside its peak usage window.
                total_factor = 0.0
                for h in range(24):
                    if h in peak_hours:
                        total_factor += 1.0
                    elif 0 <= h <= 5:
                        # Deep night — minimal usage
                        total_factor += 0.05
                    else:
                        # Daytime / evening off-peak — occasional use
                        total_factor += 0.15
                tod_avg = total_factor / 24.0

        # Seasonal average: HA-derived monthly_factors or HVAC model
        seasonal_avg = 1.0
        monthly = template.get("monthly_factors")
        hvac_type = template.get("hvac_type")
        if monthly:
            seasonal_avg = sum(float(monthly.get(m, 1.0)) for m in range(1, 13)) / 12.0
        elif hvac_type:
            lat = self._config["panel_config"].get("latitude", 37.7)
            days_in_month = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
            ref_jan1 = 1704067200
            hvac_total = 0.0
            for month_idx in range(12):
                doy = sum(days_in_month[:month_idx]) + 15
                ts = ref_jan1 + (doy - 1) * 86400 + 12 * 3600
                hvac_total += hvac_seasonal_factor(ts, lat, hvac_type, tz=self._tz)
            seasonal_avg = hvac_total / 12.0

        # Smart behavior: 19 hours full + 5 hours (17-21) reduced
        smart_avg = 1.0
        smart = template.get("smart_behavior", {})
        if smart.get("responds_to_grid", False):
            max_reduction = smart.get("max_power_reduction", 0.5)
            smart_avg = (19.0 + 5.0 * (1.0 - max_reduction)) / 24.0

        # For circuits with no cycling, no TOD, and no HVAC modulation, apply
        # a default daily-operating-hours estimate.  Circuits whose power_range
        # has a non-zero minimum (always-on loads) are excluded.
        usage_factor = 1.0
        if not has_cycling and not has_tod and hvac_type is None and monthly is None:
            power_range = template["energy_profile"].get("power_range", [0, typical_power])
            min_power = float(power_range[0]) if power_range else 0.0
            if min_power <= 0:
                if typical_power <= 200:
                    daily_hours = 6.0
                elif typical_power <= 500:
                    daily_hours = 3.0
                elif typical_power <= 3000:
                    daily_hours = 1.0
                else:
                    daily_hours = 2.5
                usage_factor = daily_hours / 24.0

        avg_power = typical_power * duty_cycle * tod_avg * seasonal_avg * smart_avg * usage_factor
        return avg_power * 8760

    def _estimate_battery_annual_wh(
        self, template: CircuitTemplateExtended
    ) -> tuple[float, float]:
        """Estimate annual battery energy ``(produced_wh, consumed_wh)``."""
        battery_config = template.get("battery_behavior", {})
        if not isinstance(battery_config, dict) or not battery_config.get("enabled", False):
            return (0.0, 0.0)

        charge_mode: str = battery_config.get("charge_mode", "custom")
        max_charge = abs(float(battery_config.get("max_charge_power", 3000.0)))
        max_discharge = abs(float(battery_config.get("max_discharge_power", 2500.0)))
        discharge_hours: list[int] = battery_config.get("discharge_hours", [])

        # Discharge -> production (common to all charge modes)
        produced_wh = 0.0
        if discharge_hours:
            avg_discharge = sum(
                max_discharge * self._get_demand_factor_from_config(h, battery_config)
                for h in discharge_hours
            ) / len(discharge_hours)
            produced_wh = avg_discharge * len(discharge_hours) * 365

        consumed_wh = 0.0
        if charge_mode == "custom":
            charge_hours: list[int] = battery_config.get("charge_hours", [])
            if charge_hours:
                avg_charge = sum(
                    max_charge * self._get_solar_intensity_from_config(h, battery_config)
                    for h in charge_hours
                ) / len(charge_hours)
                consumed_wh = avg_charge * len(charge_hours) * 365

        elif charge_mode == "solar-gen":
            solar_factor = self._estimate_solar_annual_factor()
            consumed_wh = max_charge * solar_factor * 8760

        elif charge_mode == "solar-excess":
            solar_factor = self._estimate_solar_annual_factor()
            consumed_wh = 0.3 * max_charge * solar_factor * 8760

        return (produced_wh, consumed_wh)


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
        recorder: RecorderDataSource | None = None,
    ) -> None:
        self._config: SimulationConfig | None = None
        self._config_path = Path(config_path) if config_path else None
        self._config_data = config_data
        self._serial_number_override = serial_number
        self._recorder = recorder
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
            self._behavior_engine = RealisticBehaviorEngine(
                self._clock.real_start_time,
                self._config,
                recorder=self._recorder,
            )
            self._clock.initialize(
                self._config.get("simulation_params", {}),
                panel_timezone=self._behavior_engine.panel_timezone,
            )
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
            self._config = await loop.run_in_executor(
                None, self._load_yaml_config, self._config_path
            )
        else:
            raise ValueError("YAML configuration is required")

        if self._serial_number_override and self._config:
            self._config["panel_config"]["serial_number"] = self._serial_number_override

        # Ensure every simulated panel has the sim- prefix so the HA
        # integration can distinguish simulators from real hardware.
        if self._config:
            serial = self._config["panel_config"]["serial_number"]
            if not serial.lower().startswith("sim-"):
                self._config["panel_config"]["serial_number"] = f"sim-{serial}"

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
        if self._behavior_engine is not None:
            self._behavior_engine.set_grid_offline(not online)

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

    @property
    def total_tabs(self) -> int:
        """Total panel tab count from configuration."""
        if self._config:
            return int(self._config["panel_config"].get("total_tabs", 32))
        return 32

    @property
    def panel_timezone(self) -> str:
        """IANA timezone string for the simulated panel."""
        if self._behavior_engine is not None:
            return str(self._behavior_engine.panel_timezone)
        return "America/Los_Angeles"

    @property
    def soc_percentage(self) -> float | None:
        """Battery state-of-charge percentage, or None if no BESS."""
        if self._bsee is not None:
            return self._bsee.soe_percentage
        return None

    @property
    def soc_shed_threshold(self) -> float:
        """SOC percentage below which SOC_THRESHOLD circuits are shed."""
        if self._config is not None:
            return float(self._config["panel_config"].get("soc_shed_threshold", 20.0))
        return 20.0

    @property
    def recorder_time_bounds(self) -> tuple[float, float] | None:
        """Earliest/latest epoch seconds of loaded recorder data, or None."""
        if self._recorder is not None:
            return self._recorder.time_bounds()
        return None

    @property
    def has_recorder_data(self) -> bool:
        """Whether recorder replay data is loaded."""
        return self._recorder is not None and self._recorder.is_loaded

    def set_time_acceleration(self, accel: float) -> None:
        """Set the time acceleration multiplier."""
        self._clock.time_acceleration = accel

    def get_power_summary(self) -> dict[str, object]:
        """Aggregate current power flows and grid/battery state.

        Returns a dict suitable for dashboard rendering with keys:
        ``grid_w``, ``pv_w``, ``battery_w``, ``consumption_w``,
        ``simulation_time``, ``grid_online``, ``has_battery``,
        ``is_islandable``, ``soc_pct``, ``soc_threshold``, ``shed_ids``,
        ``user_open_ids``, ``all_off``, ``time_zone``.
        """
        sim_time = self.get_current_simulation_time()

        grid = 0.0
        pv = 0.0
        battery = 0.0
        total_consumption = 0.0
        total_production = 0.0
        for circuit in self._circuits.values():
            power = circuit.instant_power_w
            if circuit.energy_mode == "producer":
                pv += power
                total_production += power
            elif circuit.energy_mode == "bidirectional":
                battery = power
            else:
                total_consumption += power

        # Battery sign: positive = discharging (producer), negative = charging (consumer)
        battery_state = self._bsee.battery_state if self._bsee is not None else "idle"
        if battery_state == "discharging":
            # Battery is a producer — offsets grid
            total_production += battery
        elif battery_state == "charging":
            # Battery is a consumer — adds to grid demand
            total_consumption += battery
            battery = -battery

        # Grid = net energy demand: all consumers - all producers
        # Floors at zero (can't push to grid without net metering)
        if self.grid_online:
            grid = max(0.0, total_consumption - total_production)
        else:
            grid = 0.0
            if self.has_battery:
                battery = total_consumption - pv

        # Shedding info
        shed_ids: list[str] = []
        soc_pct = self.soc_percentage
        soc_threshold = self.soc_shed_threshold
        if not self.grid_online and self.has_battery:
            for circuit in self._circuits.values():
                if circuit.energy_mode in ("producer", "bidirectional"):
                    continue
                if circuit._priority == "OFF_GRID" or (
                    circuit._priority == "SOC_THRESHOLD"
                    and soc_pct is not None
                    and soc_pct < soc_threshold
                ):
                    shed_ids.append(circuit.circuit_id)

        # Circuits manually opened by user (via relay override)
        user_open_ids: list[str] = []
        for cid, overrides in self._dynamic_overrides.items():
            if overrides.get("relay_state") == "OPEN" and cid not in shed_ids:
                user_open_ids.append(cid)

        all_off = not self.grid_online and not self.has_battery

        return {
            "grid_w": round(grid, 1),
            "pv_w": round(pv, 1),
            "battery_w": round(battery, 1),
            "consumption_w": round(total_consumption, 1),
            "simulation_time": sim_time,
            "grid_online": self.grid_online,
            "has_battery": self.has_battery,
            "is_islandable": self.is_grid_islandable,
            "soc_pct": round(soc_pct, 1) if soc_pct is not None else None,
            "soc_threshold": soc_threshold,
            "shed_ids": shed_ids,
            "user_open_ids": user_open_ids,
            "all_off": all_off,
            "time_zone": self.panel_timezone,
            "time_acceleration": self._clock.time_acceleration,
        }

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
                soc_threshold = self._config["panel_config"].get("soc_shed_threshold", 20.0)
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
                    if circuit._priority == "OFF_GRID" or (
                        circuit._priority == "SOC_THRESHOLD" and soc < soc_threshold
                    ):
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
            # Battery power flow uses SPAN panel sign convention
            # (matches real hardware per SpanPanel/span#184):
            #   positive = charging  (panel sending power TO battery)
            #   negative = discharging (battery sending power TO panel)
            if self._forced_grid_offline:
                # Off-grid: battery covers load deficit — always discharging
                power_flow_battery = -(total_consumption - total_production)
            elif self._bsee.battery_state == "discharging":
                power_flow_battery = -battery_power_w
            else:
                power_flow_battery = battery_power_w

            # Rebuild battery circuit snapshot — the original was captured
            # before the BSEE update and off-grid deficit calculation, so it
            # has stale power.  Sync the circuit object then re-snapshot.
            if battery_circuit is not None:
                battery_circuit._instant_power_w = abs(power_flow_battery)
                cid = battery_circuit.circuit_id
                snap = battery_circuit.to_snapshot()
                if cid in shed_ids:
                    snap = replace(
                        snap,
                        relay_state="OPEN",
                        relay_requester="BACKUP",
                        instant_power_w=0.0,
                    )
                circuit_snapshots[cid] = snap
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
                    software_version="1.0.0-sim",
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
                    part_number="SPN-DRV-001",
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

        # Panel model derived from tab count
        panel_model = _PANEL_SIZE_TO_MODEL.get(total_tabs)

        # Config-driven fields with sensible defaults
        postal_code = self._config["panel_config"].get("postal_code", "94103")
        time_zone = self._config["panel_config"].get("time_zone", "America/Los_Angeles")

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
            postal_code=postal_code,
            time_zone=time_zone,
            panel_model=panel_model,
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
            pcs=SpanPcsSnapshot(),
        )

    # ------------------------------------------------------------------
    # Dynamic overrides (dispatched to SimulatedCircuit instances)
    # ------------------------------------------------------------------

    def set_dynamic_overrides(
        self,
        circuit_overrides: dict[str, dict[str, Any]] | None = None,
        global_overrides: dict[str, Any] | None = None,
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
            raise SimulationConfigurationError(
                "Simulation configuration is required for tab synchronization."
            )

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
            template: CircuitTemplateExtended | dict[str, Any] = self._config[
                "circuit_templates"
            ].get(template_name, {})
            if not isinstance(template, dict):
                continue
            battery_cfg = template.get("battery_behavior", {})
            if isinstance(battery_cfg, dict) and battery_cfg.get("enabled", False):
                battery_dict: dict[str, Any] = dict(battery_cfg)
                nameplate: float = float(battery_cfg.get("nameplate_capacity_kwh", 13.5))
                panel_tz = (
                    self._behavior_engine.panel_timezone
                    if self._behavior_engine is not None
                    else ZoneInfo(RealisticBehaviorEngine._DEFAULT_TZ)
                )
                return BatteryStorageEquipment(
                    battery_behavior=battery_dict,
                    panel_serial=self._config["panel_config"]["serial_number"],
                    feed_circuit_id=circuit_def["id"],
                    nameplate_capacity_kwh=nameplate,
                    behavior_engine=self._behavior_engine,
                    panel_timezone=panel_tz,
                )
        return None
