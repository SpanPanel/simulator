"""EnergySystem — the top-level energy balance resolver.

Constructed from an ``EnergySystemConfig``, ticked with ``PowerInputs``,
and returns a ``SystemState``.  Pure value object — no external dependencies,
no shared mutable state.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from span_panel_simulator.energy.bus import PanelBus
from span_panel_simulator.energy.components import BESSUnit, GridMeter, LoadGroup, PVSource
from span_panel_simulator.energy.types import (
    EnergySystemConfig,
    PowerInputs,
    SystemState,
)

if TYPE_CHECKING:
    from span_panel_simulator.energy.components import Component
    from span_panel_simulator.energy.types import BESSConfig


class EnergySystem:
    """Component-based energy balance resolver.

    Instantiate via ``from_config()``.  Call ``tick()`` each simulation
    step to resolve power flows across the bus.
    """

    def __init__(
        self,
        bus: PanelBus,
        grid: GridMeter,
        pv: PVSource | None,
        bess: BESSUnit | None,
        load: LoadGroup,
    ) -> None:
        self.bus = bus
        self.grid = grid
        self.pv = pv
        self.bess = bess
        self.load = load
        self.islandable: bool = True

    @property
    def grid_state(self) -> str:
        """``ON_GRID`` or ``OFF_GRID`` based on grid connection status."""
        return "OFF_GRID" if not self.grid.connected else "ON_GRID"

    @property
    def dominant_power_source(self) -> str:
        """``BATTERY`` or ``GRID`` based on grid connection status."""
        return "BATTERY" if not self.grid.connected else "GRID"

    @staticmethod
    def from_config(config: EnergySystemConfig) -> EnergySystem:
        grid = GridMeter(connected=config.grid.connected)

        pv: PVSource | None = None
        if config.pv is not None:
            pv = PVSource(available_power_w=0.0, online=True)

        bess: BESSUnit | None = None
        if config.bess is not None:
            bc: BESSConfig = config.bess
            initial_soe = bc.initial_soe_kwh
            if initial_soe is None:
                initial_soe = bc.nameplate_kwh * 0.5
            from zoneinfo import ZoneInfo

            bess = BESSUnit(
                nameplate_capacity_kwh=bc.nameplate_kwh,
                max_charge_w=bc.max_charge_w,
                max_discharge_w=bc.max_discharge_w,
                charge_efficiency=bc.charge_efficiency,
                discharge_efficiency=bc.discharge_efficiency,
                backup_reserve_pct=bc.backup_reserve_pct,
                hard_min_pct=bc.hard_min_pct,
                hybrid=bc.hybrid,
                pv_source=pv,
                soe_kwh=initial_soe,
                panel_serial=bc.panel_serial,
                feed_circuit_id=bc.feed_circuit_id,
                charge_hours=bc.charge_hours,
                discharge_hours=bc.discharge_hours,
                panel_timezone=ZoneInfo(bc.panel_timezone),
                charge_mode=bc.charge_mode,
            )

        total_demand = sum(lc.demand_w for lc in config.loads)
        load = LoadGroup(demand_w=total_demand)

        components: list[Component] = [load]
        if pv is not None:
            components.append(pv)
        if bess is not None:
            components.append(bess)
        components.append(grid)

        bus = PanelBus(components=components)
        return EnergySystem(bus=bus, grid=grid, pv=pv, bess=bess, load=load)

    def tick(self, ts: float, inputs: PowerInputs) -> SystemState:
        # 1. Apply topology
        self.grid.connected = inputs.grid_connected
        if self.bess is not None:
            self.bess.update_pv_online_status(inputs.grid_connected)
        elif self.pv is not None and not inputs.grid_connected:
            self.pv.online = False

        # 2. Set component inputs
        self.load.demand_w = inputs.load_demand_w
        if self.pv is not None:
            self.pv.available_power_w = inputs.pv_available_w
        if self.bess is not None:
            if self.bess.charge_mode == "self-consumption":
                # Real-time response: determine direction from load vs PV.
                # Discharge: covers grid import up to inverter rate (GFE
                #   throttle limits to actual deficit).
                # Charge: absorbs only the PV excess — never pulls from
                #   grid.  Clamped to inverter rate.
                preliminary_deficit = inputs.load_demand_w - inputs.pv_available_w
                if preliminary_deficit > 0:
                    self.bess.scheduled_state = "discharging"
                    self.bess.requested_power_w = self.bess.max_discharge_w
                elif preliminary_deficit < 0:
                    excess = -preliminary_deficit
                    self.bess.scheduled_state = "charging"
                    self.bess.requested_power_w = min(excess, self.bess.max_charge_w)
                else:
                    self.bess.scheduled_state = "idle"
                    self.bess.requested_power_w = 0.0

            elif self.bess.charge_mode == "backup-only":
                if not inputs.grid_connected:
                    self.bess.scheduled_state = "discharging"
                    self.bess.requested_power_w = self.bess.max_discharge_w
                elif self.bess.soe_percentage < 100.0:
                    self.bess.scheduled_state = "charging"
                    self.bess.requested_power_w = self.bess.max_charge_w
                else:
                    self.bess.scheduled_state = "idle"
                    self.bess.requested_power_w = 0.0

            else:  # custom (TOU): use schedule
                self.bess.scheduled_state = inputs.bess_scheduled_state
                if self.bess.scheduled_state == "discharging":
                    self.bess.requested_power_w = self.bess.max_discharge_w
                elif self.bess.scheduled_state == "charging":
                    self.bess.requested_power_w = self.bess.max_charge_w
                else:
                    self.bess.requested_power_w = 0.0

            # Non-hybrid islanding override (applies to ALL modes):
            # if grid disconnected and PV is offline, BESS must discharge
            if not inputs.grid_connected and not self.bess.hybrid:
                self.bess.scheduled_state = "discharging"
                self.bess.requested_power_w = self.bess.max_discharge_w

        # 3. Resolve bus
        bus_state = self.bus.resolve()

        # 4. Integrate BESS energy
        if self.bess is not None:
            self.bess.integrate_energy(ts)

        # 5. Return resolved state
        pv_power = 0.0
        if self.pv is not None and self.pv.online:
            pv_power = self.pv.available_power_w

        bess_power = 0.0
        bess_state = "idle"
        soe_kwh = 0.0
        soe_pct = 0.0
        if self.bess is not None:
            bess_power = self.bess.effective_power_w
            bess_state = self.bess.effective_state
            soe_kwh = self.bess.soe_kwh
            soe_pct = self.bess.soe_percentage

        return SystemState(
            grid_power_w=bus_state.grid_power_w,
            pv_power_w=pv_power,
            bess_power_w=bess_power,
            bess_state=bess_state,
            load_power_w=inputs.load_demand_w,
            soe_kwh=soe_kwh,
            soe_percentage=soe_pct,
            balanced=bus_state.is_balanced(),
        )
