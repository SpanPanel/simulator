"""Simulation clock — manages simulated time, acceleration, and overrides.

Extracted from DynamicSimulationEngine to enable dashboard time controls
(speed slider, "present time" picker) and improve testability.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from zoneinfo import ZoneInfo

    from span_panel_simulator.config_types import SimulationParams


class SimulationClock:
    """Manages the mapping between wall-clock time and simulation time.

    Supports a configurable start time, time acceleration, and runtime
    overrides via ``set_time`` (for dashboard controls).
    """

    def __init__(self) -> None:
        self._real_start_time = time.time()
        self._time_offset = 0.0  # Offset between real time and simulation time
        self._use_simulation_time = False
        self._time_acceleration = 1.0
        self._pending_override: str | None = None
        self._panel_tz: ZoneInfo | None = None

    # ------------------------------------------------------------------
    # Initialization
    # ------------------------------------------------------------------

    def initialize(
        self,
        sim_params: SimulationParams,
        *,
        panel_timezone: ZoneInfo | None = None,
    ) -> None:
        """Configure the clock from simulation parameters.

        Must be called once during engine startup, after config is loaded.
        """
        self._panel_tz = panel_timezone
        self._use_simulation_time = sim_params.get("use_simulation_time", False)
        self._time_acceleration = sim_params.get("time_acceleration", 1.0)

        if self._use_simulation_time:
            start_time_str = sim_params.get("simulation_start_time")
            if start_time_str:
                self._apply_start_time(start_time_str)

        # Apply any override that was set before initialization
        if self._pending_override:
            self.set_time(self._pending_override)
            self._pending_override = None

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def current_time(self) -> float:
        """Current simulation timestamp (seconds since epoch)."""
        current_real_time = time.time()

        if self._use_simulation_time:
            elapsed_real_time = current_real_time - self._real_start_time
            elapsed_sim_time = elapsed_real_time * self._time_acceleration
            return self._real_start_time + self._time_offset + elapsed_sim_time

        return current_real_time

    @property
    def time_acceleration(self) -> float:
        """Current time acceleration multiplier."""
        return self._time_acceleration

    @time_acceleration.setter
    def time_acceleration(self, value: float) -> None:
        if not self._use_simulation_time:
            self._enable_simulation_time()
        else:
            # Re-anchor: freeze current sim time so the new multiplier
            # only applies to real time elapsed from this moment forward.
            self._time_offset = self.current_time - time.time()
            self._real_start_time = time.time()
        self._time_acceleration = value

    @property
    def real_start_time(self) -> float:
        """Wall-clock time when the simulation was created."""
        return self._real_start_time

    # ------------------------------------------------------------------
    # Runtime control
    # ------------------------------------------------------------------

    def set_time(self, iso_str: str) -> None:
        """Override the simulation start time (e.g. from dashboard controls).

        If called before ``initialize``, the value is stored and applied later.
        Automatically enables simulation time mode.  Always re-anchors the
        clock so the new time takes effect from this real-time moment.
        """
        self._use_simulation_time = True
        self._real_start_time = time.time()

        try:
            self._apply_start_time(iso_str)
        except (ValueError, TypeError):
            self._use_simulation_time = False

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _enable_simulation_time(self) -> None:
        """Switch to simulation time, anchored at the current wall-clock moment."""
        self._use_simulation_time = True
        self._real_start_time = time.time()
        self._time_offset = 0.0

    def _apply_start_time(self, start_time_str: str) -> None:
        """Parse an ISO datetime string and set the time offset.

        Naive datetimes (no timezone suffix) are interpreted as panel-local
        time when ``_panel_tz`` is set, so the dashboard slider sends
        panel-local values regardless of the host system's timezone.
        """
        if start_time_str.endswith("Z"):
            start_time_str = start_time_str[:-1]

        sim_start_dt = datetime.fromisoformat(start_time_str)
        if sim_start_dt.tzinfo is None and self._panel_tz is not None:
            sim_start_dt = sim_start_dt.replace(tzinfo=self._panel_tz)
        sim_start_timestamp = sim_start_dt.timestamp()

        self._time_offset = sim_start_timestamp - self._real_start_time
