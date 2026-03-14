"""Battery Storage Energy Equipment (BSEE) — encapsulates all BESS state.

The BSEE determines the Grid Frequency Entity (GFE) values that drive
the HA integration's grid state display.  When the battery is discharging,
the panel reports OFF_GRID / BATTERY; otherwise ON_GRID / GRID.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any


# Time-of-day SOE profile: hour → base percentage
_SOE_PROFILE: dict[int, float] = {
    0: 45.0,
    1: 40.0,
    2: 38.0,
    3: 35.0,
    4: 33.0,
    5: 30.0,
    6: 32.0,
    7: 35.0,
    8: 40.0,
    9: 45.0,
    10: 55.0,
    11: 65.0,
    12: 75.0,
    13: 80.0,
    14: 85.0,
    15: 88.0,
    16: 90.0,
    17: 85.0,
    18: 80.0,
    19: 70.0,
    20: 60.0,
    21: 50.0,
    22: 48.0,
    23: 46.0,
}


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
    ) -> None:
        self._battery_behavior = battery_behavior
        self._panel_serial = panel_serial
        self._feed_circuit_id = feed_circuit_id
        self._nameplate_capacity_kwh = nameplate_capacity_kwh

        # Mutable state refreshed by update()
        self._battery_state: str = "idle"
        self._soe_percentage: float = 75.0
        self._battery_power_w: float = 0.0

        # Grid control overrides (set by dashboard)
        self._forced_offline: bool = False
        self._islandable: bool = True

    # ------------------------------------------------------------------
    # Public update — called each snapshot tick
    # ------------------------------------------------------------------

    def update(self, current_time: float, battery_power_w: float) -> None:
        """Refresh BSEE state for the current tick.

        Args:
            current_time: Simulation timestamp (seconds since epoch).
            battery_power_w: Instantaneous battery circuit power (watts).
        """
        self._battery_power_w = battery_power_w
        self._battery_state = self._resolve_battery_state(current_time)
        self._soe_percentage = self._calculate_soe(current_time)

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
        """``ON_GRID`` or ``OFF_GRID`` — published on ``bess-0/grid-state``."""
        if self._forced_offline:
            return "OFF_GRID"
        if self._battery_state == "discharging":
            return "OFF_GRID"
        return "ON_GRID"

    @property
    def dominant_power_source(self) -> str:
        """``BATTERY`` or ``GRID`` — published on ``core/dominant-power-source``."""
        if self._forced_offline:
            return "BATTERY"
        if self._battery_state == "discharging":
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
        return self._soe_percentage

    @property
    def soe_kwh(self) -> float:
        return self._nameplate_capacity_kwh * self._soe_percentage / 100.0

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
        return "SIM-BESS-13.5"

    @property
    def software_version(self) -> str:
        return "1.0.0-sim"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_battery_state(self, current_time: float) -> str:
        """Determine battery state from ``charge_hours`` / ``discharge_hours`` / ``idle_hours``."""
        current_hour = datetime.fromtimestamp(current_time).hour

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

    def _calculate_soe(self, current_time: float) -> float:
        """Calculate SOE percentage based on time-of-day profile and battery activity."""
        current_hour = datetime.fromtimestamp(current_time).hour
        base_soe = _SOE_PROFILE.get(current_hour, 50.0)

        power = self._battery_power_w
        if self._battery_state == "charging":
            if power > 1000:
                return min(95.0, base_soe + 15.0)
            if power > 500:
                return min(90.0, base_soe + 8.0)
        elif self._battery_state == "discharging":
            if power > 1000:
                return max(15.0, base_soe - 20.0)
            if power > 500:
                return max(20.0, base_soe - 10.0)

        return base_soe
