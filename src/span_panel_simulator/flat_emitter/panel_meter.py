"""Panel-level aggregator. Pure function: takes resolved per-circuit gated powers
+ battery dispatch + grid online + manifest physics, returns the panel-level
fields that go into ``EbusPanelSnapshot``.

Stateless — all integration / accumulation lives in ``EnergyIntegrator``. This
module is just arithmetic over the current tick's inputs.

Two sign frames live on this reading, and they are not the same frame.

METER frame — positive = consumption, negative = production. Every field below
is a reading taken by one meter, about itself:
- Per-circuit ``power_w``: positive = consume, negative = produce (PV/V2G).
- ``battery_w``: positive = discharging (battery → panel), negative = charging.
- ``instant_grid_power_w``: positive = importing from grid, negative = exporting.
- ``upstream_active_power_w``: net power through the panel-side upstream lugs,
  before any upstream BESS contribution is removed.
- ``feedthrough_power_w``: net power flowing through the lugs to downstream
  loads (panel-side meter perspective).

NODE frame — positive = power LEAVING the panel node, negative = power ENTERING
it. The four ``power_flow_*`` fields are not four meters; they are the four
terms of one balance at one node, and a balance only closes if every term is
in the same frame. So PV (injecting) is negative, loads (drawing) are positive,
export (leaving) is positive, and a charging battery (drawing) is positive.

The two frames disagree about the same instant, on purpose. A live panel
exporting 2.5 kW publishes ``lugs-upstream/active-power`` negative and
``power-flows/grid`` positive simultaneously; both are correct, because they
answer different questions. Do not "make them consistent".

The balance is what makes the node frame checkable: a real panel's four flows
sum to zero to the last digit it publishes. ``test_power_flows_sum_to_zero``
holds this emitter to the same identity, which is why ``power_flow_grid`` is
derived from the physics below rather than back-solved from the other three —
a residual would satisfy the test by construction and detect nothing.

Off-grid: when ``grid_online`` is False, ``instant_grid_power_w`` is 0 by
definition (grid is electrically disconnected); battery and PV cover load."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from span_panel_simulator.flat_emitter.conventions.tab_legs import Leg

if TYPE_CHECKING:
    from span_panel_simulator.flat_emitter.manifest_physics import CircuitPhysics, PanelPhysics


# ---------------------------------------------------------------------------
# Per-tick output bundle
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class PanelMeterReading:
    """Resolved panel-level values for a single tick."""

    instant_grid_power_w: float
    upstream_active_power_w: float
    feedthrough_power_w: float
    upstream_l1_current_a: float
    upstream_l2_current_a: float
    downstream_l1_current_a: float
    downstream_l2_current_a: float
    line_voltage_v: float
    main_relay_state: str  # "OPEN" | "CLOSED"
    grid_state: str | None  # "ON_GRID" | "OFF_GRID"
    dsm_state: str
    current_run_config: str
    dominant_power_source: str | None  # "GRID" | "BATTERY" | None
    grid_islandable: bool
    power_flow_pv: float
    power_flow_battery: float
    power_flow_grid: float
    power_flow_site: float


# ---------------------------------------------------------------------------
# Per-circuit derived current — published per circuit, also folded into panel
# upstream/downstream legs.
# ---------------------------------------------------------------------------


def circuit_current_a(power_w: float, dipole: bool, line_voltage_v: float) -> float:
    """Magnitude of current draw. Dipole circuits use line-to-line voltage
    (2 * line_voltage_v); single-tab circuits use line-to-neutral."""
    if line_voltage_v <= 0:
        return 0.0
    voltage = 2 * line_voltage_v if dipole else line_voltage_v
    return abs(power_w) / voltage


# ---------------------------------------------------------------------------
# Panel state strings — derived from grid + battery presence
# ---------------------------------------------------------------------------


_DSM_ON = "DSM_ON_GRID"
_DSM_OFF = "DSM_OFF_GRID"
_RUN_ON = "PANEL_ON_GRID"
_RUN_OFF = "PANEL_OFF_GRID"


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


def resolve(
    *,
    panel: PanelPhysics,
    circuits: dict[str, CircuitPhysics],
    gated_powers: dict[str, float],  # circuit_id -> post-relay-gate signed power
    battery_w: float,  # signed; positive = discharging
    grid_online: bool,
    has_battery: bool,
) -> PanelMeterReading:
    """Build the panel-level reading from per-circuit gated powers + battery + grid."""

    load_demand_w = sum(p for p in gated_powers.values() if p > 0)
    pv_available_w = -sum(p for p in gated_powers.values() if p < 0)  # positive magnitude

    upstream_active_w = load_demand_w - pv_available_w

    if grid_online:
        # Upstream lugs see the panel-side net flow. Utility grid flow is on the
        # other side of an upstream BESS, so remove the BESS contribution to get
        # what the utility is actually supplying or absorbing.
        grid_w = _grid_power_from_lugs_and_bess(upstream_active_w, battery_w)
        grid_state: str | None = "ON_GRID"
        dsm_state = _DSM_ON
        current_run_config = _RUN_ON
        main_relay_state = "CLOSED"
        line_voltage_v = panel.line_voltage_v
        dominant_power_source: str | None = "GRID"
    else:
        grid_w = 0.0
        grid_state = "OFF_GRID"
        dsm_state = _DSM_OFF
        current_run_config = _RUN_OFF
        main_relay_state = "OPEN"
        line_voltage_v = panel.line_voltage_v if has_battery else 0.0
        dominant_power_source = "BATTERY" if has_battery else None

    # Per-leg upstream current = panel-side current routed through the lugs.
    # With an upstream BESS, this can differ from utility grid current.
    if line_voltage_v > 0:
        upstream_l1_a, upstream_l2_a = _per_leg_current(
            gated_powers,
            circuits,
            line_voltage_v=line_voltage_v,
        )
    else:
        upstream_l1_a = upstream_l2_a = 0.0

    # Feedthrough = signed power across downstream-of-lugs circuits only.
    feedthrough_w = sum(
        p for cid, p in gated_powers.items() if circuits[cid].placement == "downstream-of-lugs"
    )
    if line_voltage_v > 0:
        downstream_l1_a, downstream_l2_a = _per_leg_current(
            {
                cid: p
                for cid, p in gated_powers.items()
                if circuits[cid].placement == "downstream-of-lugs"
            },
            circuits,
            line_voltage_v=line_voltage_v,
        )
    else:
        downstream_l1_a = downstream_l2_a = 0.0

    return PanelMeterReading(
        instant_grid_power_w=grid_w,
        upstream_active_power_w=upstream_active_w if grid_online else 0.0,
        feedthrough_power_w=feedthrough_w,
        upstream_l1_current_a=upstream_l1_a,
        upstream_l2_current_a=upstream_l2_a,
        downstream_l1_current_a=downstream_l1_a,
        downstream_l2_current_a=downstream_l2_a,
        line_voltage_v=line_voltage_v,
        main_relay_state=main_relay_state,
        grid_state=grid_state,
        dsm_state=dsm_state,
        current_run_config=current_run_config,
        dominant_power_source=dominant_power_source,
        grid_islandable=panel.islandable,
        # Node frame — see the module docstring. Each of these is the meter-frame
        # quantity above it, restated as "power leaving the panel node", which is
        # what makes the four sum to zero.
        power_flow_pv=-pv_available_w,
        power_flow_battery=-battery_w,
        power_flow_grid=-grid_w,
        power_flow_site=load_demand_w,
    )


def _grid_power_from_lugs_and_bess(upstream_active_w: float, battery_w: float) -> float:
    """Return utility-side grid power from panel-side lugs and BESS power.

    BESS sign convention is positive=discharging, negative=charging. The BESS sits
    upstream of the lugs, so whatever it supplies the utility does not have to, and
    whatever it absorbs the utility must: one subtraction, in both directions.

    Charging used to be credited only against the PV surplus visible at the lugs, so
    that a charging BESS "never creates extra grid import". That clamp is gone. It
    was a dispatch policy enforced in the wrong module, and it enforced it against
    the one mode that does not want it: ``self-consumption`` already charges from
    ``pv_surplus_w`` alone (``native_devices/bess.py``), so the clamp never bound
    there, while ``backup-only`` deliberately charges from the utility -- and the
    clamp silently deleted exactly that import from the reading. The energy did not
    stop arriving; the meter stopped saying where it came from, which is the one
    thing a meter is for. It also put the node balance out by the amount hidden.
    """
    return upstream_active_w - battery_w


def _per_leg_current(
    powers: dict[str, float],
    circuits: dict[str, CircuitPhysics],
    *,
    line_voltage_v: float,
) -> tuple[float, float]:
    """Sum per-circuit current contribution onto L1 and L2.

    Single-tab circuit on tab N → all current to legs_for_tabs((N,))[0].
    Dipole circuit (one tab per leg) → equal current on both legs at line-to-line
    voltage (2 * line_voltage_v)."""
    l1_a = 0.0
    l2_a = 0.0
    for cid, power in powers.items():
        cphys = circuits[cid]
        if cphys.dipole:
            i = abs(power) / (2 * line_voltage_v)
            # Dipole spans both legs — same current on each leg.
            l1_a += i
            l2_a += i
        else:
            i = abs(power) / line_voltage_v
            leg = cphys.legs[0]
            if leg == Leg.L1:
                l1_a += i
            else:
                l2_a += i
    return l1_a, l2_a
