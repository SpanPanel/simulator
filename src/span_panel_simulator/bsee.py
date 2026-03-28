"""Battery Storage Energy Equipment (BSEE) — identity and grid-state facade.

Holds BESS identity properties (serial, vendor, model) and GFE grid-state
logic.  Power-flow resolution and SOE integration are delegated to the
``EnergySystem``; the engine syncs results back each tick.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from span_panel_simulator.const import DEFAULT_FIRMWARE_VERSION

# SOE bounds (percentage of nameplate)
_SOE_INITIAL_PCT = 50.0  # Starting SOE when no prior state


class BatteryStorageEquipment:
    """Encapsulates BESS identity and grid-state properties.

    Created once per engine initialisation (when a battery circuit exists).
    Power-flow and SOE bookkeeping are handled by the ``EnergySystem``;
    the engine syncs the results back to this object each tick so the
    snapshot builder can read identity and grid properties from one place.
    """

    def __init__(
        self,
        battery_behavior: dict[str, Any],
        panel_serial: str,
        feed_circuit_id: str,
        *,
        nameplate_capacity_kwh: float = 13.5,
        panel_timezone: ZoneInfo | None = None,
    ) -> None:
        self._battery_behavior = battery_behavior
        self._panel_serial = panel_serial
        self._feed_circuit_id = feed_circuit_id
        self._nameplate_capacity_kwh = nameplate_capacity_kwh
        self._tz: ZoneInfo = panel_timezone or ZoneInfo("America/Los_Angeles")

        # Mutable state refreshed by the energy system each tick
        self._battery_state: str = "idle"
        self._soe_kwh: float = nameplate_capacity_kwh * _SOE_INITIAL_PCT / 100.0
        self._battery_power_w: float = 0.0

        # Grid control overrides (set by dashboard)
        self._forced_offline: bool = False
        self._islandable: bool = True

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
        """Determine battery state from grid status or schedule.

        The schedule (charge/discharge/idle hours) is always authoritative
        for state resolution.  The charge mode (solar-gen, solar-excess,
        custom) affects power *magnitude* via the behavior engine's
        ``_apply_battery_behavior``, not the state.  This separation
        ensures correct behavior in both the live simulation (where the
        behavior engine is active) and the modeling pass (where the
        battery circuit may be replayed from recorder data, leaving
        ``last_battery_direction`` stale).
        """
        if self._forced_offline:
            return "discharging"

        current_hour = datetime.fromtimestamp(current_time, tz=self._tz).hour

        charge_hours: list[int] = self._battery_behavior.get("charge_hours", [])
        discharge_hours: list[int] = self._battery_behavior.get("discharge_hours", [])

        if current_hour in discharge_hours:
            return "discharging"
        if current_hour in charge_hours:
            return "charging"
        return "idle"
