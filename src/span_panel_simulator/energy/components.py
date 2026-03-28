"""Physical energy components for the panel bus.

Each component has a role (LOAD, SOURCE, STORAGE, SLACK) and implements
``resolve()`` which returns a ``PowerContribution`` given the current
``BusState``.  All power values are non-negative magnitudes; direction
is expressed by which field (``demand_w`` vs ``supply_w``) is populated.
"""

from __future__ import annotations

from datetime import datetime
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
        feed_circuit_id: str = "",
        charge_hours: tuple[int, ...] = (),
        discharge_hours: tuple[int, ...] = (),
        panel_timezone: ZoneInfo | None = None,
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

        # Identity / schedule
        self.panel_serial = panel_serial
        self.feed_circuit_id = feed_circuit_id
        self._charge_hours = charge_hours
        self._discharge_hours = discharge_hours
        self._panel_timezone: ZoneInfo = panel_timezone or ZoneInfo("America/Los_Angeles")

        # Output — set by resolve()
        self.effective_power_w: float = 0.0
        self.effective_state: str = "idle"

        # Timestamp tracking for energy integration
        self._last_ts: float | None = None

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
        """Max discharge power before hitting SOE floor (backup reserve)."""
        min_kwh = self.nameplate_capacity_kwh * self.backup_reserve_pct / 100.0
        available_kwh = self.soe_kwh - min_kwh
        if available_kwh <= 0:
            return 0.0
        return available_kwh * 1000.0 * 3600.0

    def _max_charge_for_soe(self) -> float:
        """Max charge power before hitting SOE ceiling (100%)."""
        max_kwh = self.nameplate_capacity_kwh * _SOE_MAX_PCT / 100.0
        headroom_kwh = max_kwh - self.soe_kwh
        if headroom_kwh <= 0:
            return 0.0
        return headroom_kwh * 1000.0 * 3600.0

    def integrate_energy(self, ts: float) -> None:
        """Integrate effective power over elapsed time to update SOE."""
        if self._last_ts is None:
            self._last_ts = ts
            return
        delta_s = ts - self._last_ts
        self._last_ts = ts
        if delta_s <= 0:
            return
        delta_s = min(delta_s, _MAX_INTEGRATION_DELTA_S)
        delta_hours = delta_s / 3600.0
        mag = abs(self.effective_power_w)
        if self.effective_state == "charging" and mag > 0:
            energy_kwh = (mag / 1000.0) * delta_hours * self.charge_efficiency
            self.soe_kwh += energy_kwh
        elif self.effective_state == "discharging" and mag > 0:
            energy_kwh = (mag / 1000.0) * delta_hours / self.discharge_efficiency
            self.soe_kwh -= energy_kwh
        max_kwh = self.nameplate_capacity_kwh * _SOE_MAX_PCT / 100.0
        min_kwh = self.nameplate_capacity_kwh * self.hard_min_pct / 100.0
        self.soe_kwh = max(min_kwh, min(max_kwh, self.soe_kwh))

    def update_pv_online_status(self, grid_connected: bool) -> None:
        """Control co-located PV based on hybrid inverter capability."""
        if self.pv_source is None:
            return
        if self.hybrid:
            self.pv_source.online = True
        elif not grid_connected:
            self.pv_source.online = False
        else:
            self.pv_source.online = True
