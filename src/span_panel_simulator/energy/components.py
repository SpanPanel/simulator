"""Physical energy components for the panel bus.

Each component has a role (LOAD, SOURCE, STORAGE, SLACK) and implements
``resolve()`` which returns a ``PowerContribution`` given the current
``BusState``.  All power values are non-negative magnitudes; direction
is expressed by which field (``demand_w`` vs ``supply_w``) is populated.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from span_panel_simulator.const import DEFAULT_FIRMWARE_VERSION
from span_panel_simulator.energy.types import (
    BusState,
    ComponentRole,
    PowerContribution,
)


class Component:
    """Base class for all bus components."""

    role: ComponentRole

    def resolve(self, bus_state: BusState) -> PowerContribution:
        raise NotImplementedError


class LoadGroup(Component):
    """Consumer load — declares demand on the bus."""

    role = ComponentRole.LOAD

    def __init__(self, demand_w: float = 0.0) -> None:
        self.demand_w = demand_w

    def resolve(self, bus_state: BusState) -> PowerContribution:
        return PowerContribution(demand_w=self.demand_w)


class GridMeter(Component):
    """Utility grid connection — slack bus that absorbs residual."""

    role = ComponentRole.SLACK

    def __init__(self, connected: bool = True) -> None:
        self.connected = connected

    def resolve(self, bus_state: BusState) -> PowerContribution:
        if not self.connected:
            return PowerContribution()
        deficit = bus_state.net_deficit_w
        if deficit > 0:
            return PowerContribution(supply_w=deficit)
        elif deficit < 0:
            return PowerContribution(demand_w=-deficit)
        return PowerContribution()


class PVSource(Component):
    """Solar PV inverter — declares available production."""

    role = ComponentRole.SOURCE

    def __init__(self, available_power_w: float = 0.0, online: bool = True) -> None:
        self.available_power_w = available_power_w
        self.online = online

    def resolve(self, bus_state: BusState) -> PowerContribution:
        if not self.online:
            return PowerContribution()
        return PowerContribution(supply_w=self.available_power_w)


_SOE_MAX_PCT = 100.0
_MAX_INTEGRATION_DELTA_S = 300.0
_SOE_TOLERANCE_KWH = 0.001  # 1 Wh — below this, treat SOE as at the boundary


class BESSUnit(Component):
    """Battery Energy Storage System — GFE-aware storage component.

    When discharging, the BESS only sources what the bus actually demands
    (GFE constraint).  Enforces SOE bounds, max charge/discharge rates,
    and efficiency losses.  Controls co-located PV online status when
    configured as a hybrid inverter.
    """

    role = ComponentRole.STORAGE

    def __init__(
        self,
        *,
        nameplate_capacity_kwh: float,
        max_charge_w: float,
        max_discharge_w: float,
        charge_efficiency: float,
        discharge_efficiency: float,
        backup_reserve_pct: float,
        hard_min_pct: float,
        hybrid: bool,
        pv_source: PVSource | None,
        soe_kwh: float,
        scheduled_state: str = "idle",
        requested_power_w: float = 0.0,
        panel_serial: str = "",
        charge_hours: tuple[int, ...] = (),
        discharge_hours: tuple[int, ...] = (),
        panel_timezone: ZoneInfo | None = None,
        charge_mode: str = "self-consumption",
        rate_record: dict[str, Any] | None = None,
    ) -> None:
        self.nameplate_capacity_kwh = nameplate_capacity_kwh
        self.max_charge_w = max_charge_w
        self.max_discharge_w = max_discharge_w
        self.charge_efficiency = charge_efficiency
        self.discharge_efficiency = discharge_efficiency
        self.backup_reserve_pct = backup_reserve_pct
        self.hard_min_pct = hard_min_pct
        self.hybrid = hybrid
        self.pv_source = pv_source
        self.soe_kwh = soe_kwh
        self.scheduled_state = scheduled_state
        self.requested_power_w = requested_power_w

        self.charge_mode = charge_mode

        # Identity / schedule
        self.panel_serial = panel_serial
        self._charge_hours = charge_hours
        self._discharge_hours = discharge_hours
        self._panel_timezone: ZoneInfo = panel_timezone or ZoneInfo("America/Los_Angeles")
        self._rate_record: dict[str, Any] | None = rate_record

        # Output — set by resolve()
        self.effective_power_w: float = 0.0
        self.effective_state: str = "idle"

        # Timestamp tracking for energy integration
        self._last_ts: float | None = None

    @property
    def panel_timezone(self) -> ZoneInfo:
        """Timezone used for schedule resolution."""
        return self._panel_timezone

    @property
    def rate_record(self) -> dict[str, Any] | None:
        """URDB rate record for TOU dispatch, if configured."""
        return self._rate_record

    @property
    def soe_percentage(self) -> float:
        if self.nameplate_capacity_kwh <= 0:
            return 0.0
        return self.soe_kwh / self.nameplate_capacity_kwh * 100.0

    # ------------------------------------------------------------------
    # Identity properties
    # ------------------------------------------------------------------

    @property
    def serial_number(self) -> str:
        return f"SIM-BESS-{self.panel_serial}"

    @property
    def vendor_name(self) -> str:
        return "Simulated BESS"

    @property
    def product_name(self) -> str:
        return "Battery Storage"

    @property
    def model(self) -> str:
        return f"SIM-BESS-{self.nameplate_capacity_kwh:.1f}"

    @property
    def software_version(self) -> str:
        return DEFAULT_FIRMWARE_VERSION

    @property
    def connected(self) -> bool:
        return True

    # ------------------------------------------------------------------
    # Schedule resolution
    # ------------------------------------------------------------------

    def resolve_scheduled_state(self, ts: float, *, forced_offline: bool = False) -> str:
        """Determine battery state from schedule or grid status."""
        if forced_offline:
            return "discharging"
        current_hour = datetime.fromtimestamp(ts, tz=self._panel_timezone).hour
        if self._discharge_hours and current_hour in self._discharge_hours:
            return "discharging"
        if self._charge_hours and current_hour in self._charge_hours:
            return "charging"
        return "idle"

    def resolve(self, bus_state: BusState) -> PowerContribution:
        if self.scheduled_state == "idle":
            self.effective_power_w = 0.0
            self.effective_state = "idle"
            return PowerContribution()
        if self.scheduled_state == "discharging":
            return self._resolve_discharge(bus_state)
        if self.scheduled_state == "charging":
            return self._resolve_charge(bus_state)
        self.effective_power_w = 0.0
        self.effective_state = "idle"
        return PowerContribution()

    def _resolve_discharge(self, bus_state: BusState) -> PowerContribution:
        deficit = bus_state.net_deficit_w
        if deficit <= 0:
            self.effective_power_w = 0.0
            self.effective_state = "idle"
            return PowerContribution()
        max_for_soe = self._max_discharge_for_soe()
        if max_for_soe <= 0:
            self.effective_power_w = 0.0
            self.effective_state = "idle"
            return PowerContribution()
        power = min(self.requested_power_w, deficit, self.max_discharge_w, max_for_soe)
        self.effective_power_w = power
        self.effective_state = "discharging"
        return PowerContribution(supply_w=power)

    def _resolve_charge(self, bus_state: BusState) -> PowerContribution:
        max_for_soe = self._max_charge_for_soe()
        if max_for_soe <= 0:
            self.effective_power_w = 0.0
            self.effective_state = "idle"
            return PowerContribution()
        power = min(self.requested_power_w, self.max_charge_w, max_for_soe)
        self.effective_power_w = power
        self.effective_state = "charging"
        return PowerContribution(demand_w=power)

    def _max_discharge_for_soe(self) -> float:
        """Max discharge power (W) before hitting SOE floor (backup reserve).

        Computes a conservative power limit such that discharging at this
        rate for the maximum integration interval will not drive SOE below
        the backup reserve.
        """
        min_kwh = self.nameplate_capacity_kwh * self.backup_reserve_pct / 100.0
        available_kwh = self.soe_kwh - min_kwh
        if available_kwh <= _SOE_TOLERANCE_KWH:
            return 0.0
        if _MAX_INTEGRATION_DELTA_S <= 0 or self.discharge_efficiency <= 0:
            return 0.0
        delta_hours = _MAX_INTEGRATION_DELTA_S / 3600.0
        # From integrate_energy: energy_kwh = (P/1000) * delta_hours / discharge_efficiency
        # Constrain energy_kwh <= available_kwh  =>  P <= available * 1000 * eff / delta_h
        return available_kwh * 1000.0 * self.discharge_efficiency / delta_hours

    def _max_charge_for_soe(self) -> float:
        """Max charge power (W) before hitting SOE ceiling (100%).

        Computes a conservative power limit such that charging at this
        rate for the maximum integration interval will not drive SOE above
        the ceiling.
        """
        max_kwh = self.nameplate_capacity_kwh * _SOE_MAX_PCT / 100.0
        headroom_kwh = max_kwh - self.soe_kwh
        if headroom_kwh <= _SOE_TOLERANCE_KWH:
            return 0.0
        if _MAX_INTEGRATION_DELTA_S <= 0 or self.charge_efficiency <= 0:
            return 0.0
        delta_hours = _MAX_INTEGRATION_DELTA_S / 3600.0
        # From integrate_energy: energy_kwh = (P/1000) * delta_hours * charge_efficiency
        # Constrain energy_kwh <= headroom_kwh  =>  P <= headroom * 1000 / (delta_h * eff)
        return headroom_kwh * 1000.0 / (delta_hours * self.charge_efficiency)

    def integrate_energy(self, ts: float) -> None:
        """Integrate effective power over elapsed time to update SOE.

        For numerical stability the integration is performed in sub-steps
        of at most ``_MAX_INTEGRATION_DELTA_S`` seconds, but the *full*
        elapsed interval is always consumed so that sparse tick intervals
        (e.g. 3600 s modelling steps) integrate correctly.
        """
        if self._last_ts is None:
            self._last_ts = ts
            return
        total_delta_s = ts - self._last_ts
        self._last_ts = ts
        if total_delta_s <= 0:
            return

        mag = abs(self.effective_power_w)
        max_kwh = self.nameplate_capacity_kwh * _SOE_MAX_PCT / 100.0
        min_kwh = self.nameplate_capacity_kwh * self.hard_min_pct / 100.0
        remaining = total_delta_s

        while remaining > 0:
            step_s = min(remaining, _MAX_INTEGRATION_DELTA_S)
            delta_hours = step_s / 3600.0
            if self.effective_state == "charging" and mag > 0:
                energy_kwh = (mag / 1000.0) * delta_hours * self.charge_efficiency
                self.soe_kwh += energy_kwh
            elif self.effective_state == "discharging" and mag > 0:
                energy_kwh = (mag / 1000.0) * delta_hours / self.discharge_efficiency
                self.soe_kwh -= energy_kwh
            self.soe_kwh = max(min_kwh, min(max_kwh, self.soe_kwh))
            remaining -= step_s

    def update_pv_online_status(self, pv_allowed: bool) -> None:
        """Control co-located PV online status.

        *pv_allowed* is True when PV should be permitted to produce
        (e.g. grid connected, or system is islandable with hybrid inverter).
        Hybrid inverters always keep PV online; non-hybrid respect the flag.
        """
        if self.pv_source is None:
            return
        if self.hybrid:
            self.pv_source.online = True
        else:
            self.pv_source.online = pv_allowed
