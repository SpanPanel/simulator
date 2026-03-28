"""PanelBus — role-ordered power flow resolution with conservation enforcement."""

from __future__ import annotations

from typing import TYPE_CHECKING

from span_panel_simulator.energy.types import BusState, ComponentRole

if TYPE_CHECKING:
    from span_panel_simulator.energy.components import Component


class PanelBus:
    """Resolves components in role order and enforces conservation of energy.

    Components are evaluated in order: LOAD -> SOURCE -> STORAGE -> SLACK.
    SLACK (grid) contributions are tracked in ``grid_power_w`` only — they
    are NOT folded into ``total_demand_w``/``total_supply_w`` so that the
    conservation check ``total_demand = total_supply + grid_power`` holds
    without double-counting.
    """

    def __init__(self, components: list[Component]) -> None:
        self._components = components

    def resolve(self) -> BusState:
        state = BusState()
        for role in (
            ComponentRole.LOAD,
            ComponentRole.SOURCE,
            ComponentRole.STORAGE,
            ComponentRole.SLACK,
        ):
            for component in self._components:
                if component.role != role:
                    continue
                contribution = component.resolve(state)
                if role == ComponentRole.SLACK:
                    # Grid tracked separately — not in demand/supply totals
                    state.grid_power_w += contribution.supply_w - contribution.demand_w
                else:
                    state.total_demand_w += contribution.demand_w
                    state.total_supply_w += contribution.supply_w
                    if role == ComponentRole.STORAGE:
                        state.storage_contribution_w += (
                            contribution.supply_w - contribution.demand_w
                        )
        return state
