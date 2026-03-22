"""Battery Storage Energy Equipment (BSEE) — encapsulates all BESS state.

The BSEE determines the Grid Frequency Entity (GFE) values that drive
the HA integration's grid state display.  When the battery is discharging,
the panel reports OFF_GRID / BATTERY; otherwise ON_GRID / GRID.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from span_panel_simulator.const import DEFAULT_FIRMWARE_VERSION

if TYPE_CHECKING:
    from span_panel_simulator.engine import RealisticBehaviorEngine


# SOE bounds (percentage of nameplate)
_SOE_HARD_MIN_PCT = 5.0  # Absolute floor — even in grid-disconnect emergencies
_SOE_MAX_PCT = 100.0  # Fully charged ceiling
_SOE_INITIAL_PCT = 50.0  # Starting SOE when no prior state
_DEFAULT_BACKUP_RESERVE_PCT = 20.0  # Normal discharge stops here; outages go deeper
_MAX_INTEGRATION_DELTA_S = 300.0  # Cap per-tick delta to 5 minutes of sim time


class BatteryStorageEquipment:
    """Encapsulates BESS state and controls GFE-driven grid state.

    Created once per engine initialisation (when a battery circuit exists)
    and updated every snapshot tick.
    """

    def __init__(
        self,
        battery_behavior: dict[str, Any],
        panel_serial: str,
        feed_circuit_id: str,
        *,
        nameplate_capacity_kwh: float = 13.5,
        behavior_engine: RealisticBehaviorEngine | None = None,
        panel_timezone: ZoneInfo | None = None,
    ) -> None:
        self._battery_behavior = battery_behavior
        self._panel_serial = panel_serial
        self._feed_circuit_id = feed_circuit_id
        self._nameplate_capacity_kwh = nameplate_capacity_kwh
        self._behavior_engine = behavior_engine
        self._tz: ZoneInfo = panel_timezone or ZoneInfo("America/Los_Angeles")

        self._charge_efficiency: float = float(battery_behavior.get("charge_efficiency", 0.95))
        self._discharge_efficiency: float = float(
            battery_behavior.get("discharge_efficiency", 0.95)
        )
        self._backup_reserve_pct: float = float(
            battery_behavior.get("backup_reserve_pct", _DEFAULT_BACKUP_RESERVE_PCT)
        )

        # Mutable state refreshed by update()
        self._battery_state: str = "idle"
        self._soe_kwh: float = nameplate_capacity_kwh * _SOE_INITIAL_PCT / 100.0
        self._battery_power_w: float = 0.0
        self._last_update_time: float | None = None

        # Grid control overrides (set by dashboard)
        self._forced_offline: bool = False
        self._islandable: bool = True

    # ------------------------------------------------------------------
    # Public update — called each snapshot tick
    # ------------------------------------------------------------------

    def update(self, current_time: float, battery_power_w: float) -> None:
        """Refresh BSEE state for the current tick.

        Resolves the scheduled battery state, enforces SOE bounds (the
        battery transitions to idle when it hits the reserve or full
        charge), then integrates the effective power over time.

        Args:
            current_time: Simulation timestamp (seconds since epoch).
            battery_power_w: Instantaneous battery circuit power (watts).
                Positive = charging/discharging magnitude from the engine.
        """
        self._battery_state = self._resolve_battery_state(current_time)

        # Enforce SOE bounds — stop discharge at reserve, stop charge at max
        effective_min_pct = _SOE_HARD_MIN_PCT if self._forced_offline else self._backup_reserve_pct
        if (self._battery_state == "discharging" and self.soe_percentage <= effective_min_pct) or (
            self._battery_state == "charging" and self.soe_percentage >= _SOE_MAX_PCT
        ):
            self._battery_state = "idle"
            battery_power_w = 0.0

        self._battery_power_w = battery_power_w
        self._integrate_energy(current_time, battery_power_w)
        self._last_update_time = current_time

    # ------------------------------------------------------------------
    # Grid control overrides
    # ------------------------------------------------------------------

    def set_forced_offline(self, offline: bool) -> None:
        """Force the grid offline (or back online)."""
        self._forced_offline = offline

    def set_islandable(self, islandable: bool) -> None:
        """Set whether PV can operate when grid is disconnected."""
        self._islandable = islandable

    # ------------------------------------------------------------------
    # GFE / grid properties
    # ------------------------------------------------------------------

    @property
    def grid_state(self) -> str:
        """``ON_GRID`` or ``OFF_GRID`` — published on ``bess-0/grid-state``.

        Reflects the physical grid connection, not the battery schedule.
        The panel is only OFF_GRID when the grid is actually disconnected
        (forced offline via dashboard or real outage).
        """
        return "OFF_GRID" if self._forced_offline else "ON_GRID"

    @property
    def dominant_power_source(self) -> str:
        """``BATTERY`` or ``GRID`` — published on ``core/dominant-power-source``."""
        if self._forced_offline:
            return "BATTERY"
        return "GRID"

    @property
    def grid_islandable(self) -> bool:
        """Whether PV can operate during grid disconnection."""
        return self._islandable

    # ------------------------------------------------------------------
    # Battery state properties
    # ------------------------------------------------------------------

    @property
    def battery_state(self) -> str:
        """``charging``, ``discharging``, or ``idle``."""
        return self._battery_state

    @property
    def soe_percentage(self) -> float:
        if self._nameplate_capacity_kwh <= 0:
            return 0.0
        return self._soe_kwh / self._nameplate_capacity_kwh * 100.0

    @property
    def soe_kwh(self) -> float:
        return self._soe_kwh

    @property
    def battery_power_w(self) -> float:
        return self._battery_power_w

    @property
    def connected(self) -> bool:
        return True

    @property
    def nameplate_capacity_kwh(self) -> float:
        return self._nameplate_capacity_kwh

    @property
    def backup_reserve_pct(self) -> float:
        return self._backup_reserve_pct

    @property
    def feed_circuit_id(self) -> str:
        return self._feed_circuit_id

    # ------------------------------------------------------------------
    # Identity properties
    # ------------------------------------------------------------------

    @property
    def serial_number(self) -> str:
        return f"SIM-BESS-{self._panel_serial}"

    @property
    def vendor_name(self) -> str:
        return "Simulated BESS"

    @property
    def product_name(self) -> str:
        return "Battery Storage"

    @property
    def model(self) -> str:
        cap = self._nameplate_capacity_kwh
        return f"SIM-BESS-{cap:.1f}"

    @property
    def software_version(self) -> str:
        return DEFAULT_FIRMWARE_VERSION

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_battery_state(self, current_time: float) -> str:
        """Determine battery state from grid status, charge mode, or schedule.

        Grid-forced-offline always overrides the schedule: the battery
        must discharge to supply loads during an outage.
        """
        if self._forced_offline:
            return "discharging"

        charge_mode: str = self._battery_behavior.get("charge_mode", "custom")
        if charge_mode != "custom" and self._behavior_engine is not None:
            return self._behavior_engine.last_battery_direction

        current_hour = datetime.fromtimestamp(current_time, tz=self._tz).hour

        charge_hours: list[int] = self._battery_behavior.get("charge_hours", [])
        discharge_hours: list[int] = self._battery_behavior.get("discharge_hours", [])
        idle_hours: list[int] = self._battery_behavior.get("idle_hours", [])

        if current_hour in charge_hours:
            return "charging"
        if current_hour in discharge_hours:
            return "discharging"
        if current_hour in idle_hours:
            return "idle"
        return "idle"

    def _integrate_energy(self, current_time: float, power_w: float) -> None:
        """Integrate power over elapsed time to update stored energy.

        Applies charge/discharge efficiency and clamps to capacity bounds.
        """
        if self._last_update_time is None:
            # First tick — no delta to integrate
            return

        delta_s = current_time - self._last_update_time
        if delta_s <= 0:
            return

        # Cap delta to prevent runaway integration on time jumps
        delta_s = min(delta_s, _MAX_INTEGRATION_DELTA_S)
        delta_hours = delta_s / 3600.0

        if self._battery_state == "charging" and power_w > 0:
            energy_kwh = (power_w / 1000.0) * delta_hours * self._charge_efficiency
            self._soe_kwh += energy_kwh
        elif self._battery_state == "discharging" and power_w > 0:
            # Discharge: power delivered = stored energy * discharge_efficiency
            # So stored energy consumed = power / efficiency
            energy_kwh = (power_w / 1000.0) * delta_hours / self._discharge_efficiency
            self._soe_kwh -= energy_kwh

        # Clamp to bounds — use backup reserve for normal discharge,
        # hard minimum only during grid-disconnect emergencies
        max_kwh = self._nameplate_capacity_kwh * _SOE_MAX_PCT / 100.0
        min_pct = _SOE_HARD_MIN_PCT if self._forced_offline else self._backup_reserve_pct
        min_kwh = self._nameplate_capacity_kwh * min_pct / 100.0
        self._soe_kwh = max(min_kwh, min(max_kwh, self._soe_kwh))
