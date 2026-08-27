import pytest

from span_panel_simulator.flat_emitter.conventions.tab_legs import Leg
from span_panel_simulator.flat_emitter.manifest_physics import CircuitPhysics, PanelPhysics
from span_panel_simulator.flat_emitter.panel_meter import (
    PanelMeterReading,
    circuit_current_a,
    resolve,
)


def _panel(**overrides: object) -> PanelPhysics:
    base = dict(
        serial_number="abc-123",
        vendor_name="Span",
        firmware_version="sim/v0.1.0",
        hardware_version="rev2",
        panel_size=40,
        main_breaker_rating_a=200,
        panel_model="MAIN_40",
        postal_code="94103",
        time_zone="America/Los_Angeles",
        service_voltage_v=240.0,
        line_voltage_v=120.0,
        islandable=False,
    )
    base.update(overrides)
    return PanelPhysics(**base)  # type: ignore[arg-type]


def _circuit(
    *,
    tabs: tuple[int, ...] = (1,),
    legs: tuple[Leg, ...] | None = None,
    placement: str = "downstream-of-lugs",
    always_on: bool = False,
    never_backup: bool = False,
) -> CircuitPhysics:
    if legs is None:
        from span_panel_simulator.flat_emitter.conventions.tab_legs import legs_for_tabs

        legs = legs_for_tabs(tabs)
    return CircuitPhysics(
        tabs=tabs,
        legs=legs,
        dipole=len(tabs) > 1,
        breaker_rating_a=20.0,
        default_priority="NICE_TO_HAVE",
        relay_behavior="always-on" if always_on else "controllable",
        placement=placement,
        always_on=always_on,
        never_backup=never_backup,
        initial_consumed_wh=0.0,
        initial_produced_wh=0.0,
    )


# -- circuit_current_a -------------------------------------------------------


def test_single_tab_current_uses_line_voltage() -> None:
    assert circuit_current_a(1200.0, dipole=False, line_voltage_v=120.0) == pytest.approx(10.0)


def test_dipole_current_uses_line_to_line() -> None:
    assert circuit_current_a(2400.0, dipole=True, line_voltage_v=120.0) == pytest.approx(10.0)


def test_negative_power_returns_positive_current() -> None:
    assert circuit_current_a(-1200.0, dipole=False, line_voltage_v=120.0) == pytest.approx(10.0)


def test_zero_voltage_returns_zero_current() -> None:
    assert circuit_current_a(1200.0, dipole=False, line_voltage_v=0.0) == 0.0


# -- resolve: grid online, simple consumer -----------------------------------


def test_on_grid_consumer_only() -> None:
    panel = _panel()
    circuits = {"kitchen": _circuit(tabs=(1,))}
    powers = {"kitchen": 1000.0}
    r = resolve(
        panel=panel,
        circuits=circuits,
        gated_powers=powers,
        battery_w=0.0,
        grid_online=True,
        has_battery=False,
    )
    assert r.instant_grid_power_w == 1000.0
    assert r.power_flow_pv == 0.0
    assert r.power_flow_battery == 0.0
    # Node frame: the grid is feeding the panel, so power enters -- negative.
    # ``instant_grid_power_w`` is the meter frame and stays positive for import.
    assert r.power_flow_grid == -1000.0
    assert r.power_flow_site == 1000.0
    assert r.grid_state == "ON_GRID"
    assert r.dominant_power_source == "GRID"
    assert r.main_relay_state == "CLOSED"
    assert r.line_voltage_v == 120.0


def test_on_grid_with_pv_export() -> None:
    panel = _panel()
    circuits = {
        "kitchen": _circuit(tabs=(1,)),
        "solar": _circuit(tabs=(3,)),
    }
    powers = {"kitchen": 500.0, "solar": -2000.0}
    r = resolve(
        panel=panel,
        circuits=circuits,
        gated_powers=powers,
        battery_w=0.0,
        grid_online=True,
        has_battery=False,
    )
    # load - pv - battery = 500 - 2000 - 0 = -1500 (exporting to grid)
    assert r.instant_grid_power_w == -1500.0
    # Node frame inverts both: the array feeds the node, the export leaves it.
    assert r.power_flow_pv == -2000.0
    assert r.power_flow_grid == 1500.0


def test_on_grid_with_battery_discharging() -> None:
    panel = _panel()
    circuits = {"kitchen": _circuit(tabs=(1,))}
    powers = {"kitchen": 3000.0}
    r = resolve(
        panel=panel,
        circuits=circuits,
        gated_powers=powers,
        battery_w=2000.0,
        grid_online=True,
        has_battery=True,
    )
    # grid = load - pv - battery_supply = 3000 - 0 - 2000 = 1000
    assert r.instant_grid_power_w == 1000.0
    assert r.upstream_active_power_w == 3000.0
    # Node frame: a discharging battery feeds the node, like the array does.
    assert r.power_flow_battery == -2000.0


def test_on_grid_with_battery_charging_from_pv_surplus() -> None:
    panel = _panel()
    circuits = {
        "kitchen": _circuit(tabs=(1,)),
        "solar": _circuit(tabs=(3,)),
    }
    powers = {"kitchen": 500.0, "solar": -2000.0}
    r = resolve(
        panel=panel,
        circuits=circuits,
        gated_powers=powers,
        battery_w=-1500.0,
        grid_online=True,
        has_battery=True,
    )
    # PV surplus charges the BESS without creating utility grid import -- not
    # because anything clamps it, but because 500 - 2000 - (-1500) is 0.
    assert r.instant_grid_power_w == 0.0
    assert r.upstream_active_power_w == -1500.0
    # Node frame: a charging battery draws from the node, like a load.
    assert r.power_flow_battery == 1500.0


def test_pv_surplus_exports_when_battery_charges_less_than_surplus() -> None:
    panel = _panel()
    circuits = {
        "kitchen": _circuit(tabs=(1,)),
        "solar": _circuit(tabs=(3,)),
    }
    powers = {"kitchen": 500.0, "solar": -2000.0}
    r = resolve(
        panel=panel,
        circuits=circuits,
        gated_powers=powers,
        battery_w=-500.0,
        grid_online=True,
        has_battery=True,
    )
    assert r.upstream_active_power_w == -1500.0
    assert r.instant_grid_power_w == -1000.0


def test_battery_charging_with_no_pv_imports_from_the_grid() -> None:
    """500 W of load and a BESS pulling 1.5 kW with no array: the utility
    supplies all 2 kW.

    This asserted 500 W until the lugs-vs-BESS clamp came out. The clamp existed
    to keep a charging BESS from "adding grid import", but with no PV there is
    nowhere else for 1.5 kW to come from, so what it really did was hide the
    import that ``backup-only`` charging deliberately creates -- and put the node
    balance out by the same 1.5 kW.
    """
    panel = _panel()
    circuits = {"kitchen": _circuit(tabs=(1,))}
    powers = {"kitchen": 500.0}
    r = resolve(
        panel=panel,
        circuits=circuits,
        gated_powers=powers,
        battery_w=-1500.0,
        grid_online=True,
        has_battery=True,
    )
    assert r.upstream_active_power_w == 500.0
    assert r.instant_grid_power_w == 2000.0
    assert r.power_flow_grid == -2000.0


def test_without_bess_upstream_lug_power_is_grid_power() -> None:
    panel = _panel()
    circuits = {"kitchen": _circuit(tabs=(1,))}
    powers = {"kitchen": 1000.0}
    r = resolve(
        panel=panel,
        circuits=circuits,
        gated_powers=powers,
        battery_w=0.0,
        grid_online=True,
        has_battery=False,
    )
    assert r.upstream_active_power_w == r.instant_grid_power_w


# -- resolve: off-grid -------------------------------------------------------


def test_off_grid_no_battery_zeros_voltage() -> None:
    panel = _panel()
    circuits = {"kitchen": _circuit(tabs=(1,))}
    powers = {"kitchen": 1000.0}
    r = resolve(
        panel=panel,
        circuits=circuits,
        gated_powers=powers,
        battery_w=0.0,
        grid_online=False,
        has_battery=False,
    )
    assert r.instant_grid_power_w == 0.0
    assert r.line_voltage_v == 0.0
    assert r.main_relay_state == "OPEN"
    assert r.grid_state == "OFF_GRID"
    assert r.dominant_power_source is None


def test_off_grid_with_battery_keeps_voltage() -> None:
    panel = _panel()
    circuits = {"kitchen": _circuit(tabs=(1,))}
    powers = {"kitchen": 1000.0}
    r = resolve(
        panel=panel,
        circuits=circuits,
        gated_powers=powers,
        battery_w=1000.0,
        grid_online=False,
        has_battery=True,
    )
    assert r.instant_grid_power_w == 0.0
    assert r.line_voltage_v == 120.0
    assert r.main_relay_state == "OPEN"
    assert r.dominant_power_source == "BATTERY"


# -- per-leg currents --------------------------------------------------------


def test_per_leg_currents_single_tab_split_l1_l2() -> None:
    panel = _panel()
    circuits = {
        "a": _circuit(tabs=(1,)),  # L1
        "b": _circuit(tabs=(2,)),  # L2
    }
    powers = {"a": 1200.0, "b": 2400.0}  # 10 A on L1, 20 A on L2
    r = resolve(
        panel=panel,
        circuits=circuits,
        gated_powers=powers,
        battery_w=0.0,
        grid_online=True,
        has_battery=False,
    )
    assert r.upstream_l1_current_a == pytest.approx(10.0)
    assert r.upstream_l2_current_a == pytest.approx(20.0)


def test_per_leg_currents_dipole_appears_on_both() -> None:
    panel = _panel()
    circuits = {"hvac": _circuit(tabs=(1, 2))}
    powers = {"hvac": 4800.0}  # 4800 W / 240 V = 20 A on each leg
    r = resolve(
        panel=panel,
        circuits=circuits,
        gated_powers=powers,
        battery_w=0.0,
        grid_online=True,
        has_battery=False,
    )
    assert r.upstream_l1_current_a == pytest.approx(20.0)
    assert r.upstream_l2_current_a == pytest.approx(20.0)


# -- feedthrough -------------------------------------------------------------


def test_feedthrough_is_downstream_only() -> None:
    panel = _panel()
    circuits = {
        "main_breaker_load": _circuit(tabs=(1,), placement="upstream-of-lugs"),
        "subpanel_a": _circuit(tabs=(3,), placement="downstream-of-lugs"),
        "subpanel_b": _circuit(tabs=(5,), placement="downstream-of-lugs"),
    }
    powers = {"main_breaker_load": 500.0, "subpanel_a": 1000.0, "subpanel_b": 1500.0}
    r = resolve(
        panel=panel,
        circuits=circuits,
        gated_powers=powers,
        battery_w=0.0,
        grid_online=True,
        has_battery=False,
    )
    # Feedthrough only counts downstream circuits.
    assert r.feedthrough_power_w == 2500.0
    # Site / grid use ALL circuits.
    assert r.power_flow_site == 3000.0
    assert r.power_flow_grid == -3000.0


def test_feedthrough_per_leg_currents() -> None:
    panel = _panel()
    circuits = {
        "upstream": _circuit(tabs=(1,), placement="upstream-of-lugs"),
        "down_l1": _circuit(tabs=(3,), placement="downstream-of-lugs"),
        "down_l2": _circuit(tabs=(4,), placement="downstream-of-lugs"),
    }
    powers = {"upstream": 1200.0, "down_l1": 600.0, "down_l2": 1200.0}
    r = resolve(
        panel=panel,
        circuits=circuits,
        gated_powers=powers,
        battery_w=0.0,
        grid_online=True,
        has_battery=False,
    )
    # downstream L1: 600/120 = 5; downstream L2: 1200/120 = 10
    assert r.downstream_l1_current_a == pytest.approx(5.0)
    assert r.downstream_l2_current_a == pytest.approx(10.0)
    # upstream sees ALL circuits: L1 = (1200 + 600)/120 = 15; L2 = 1200/120 = 10
    assert r.upstream_l1_current_a == pytest.approx(15.0)
    assert r.upstream_l2_current_a == pytest.approx(10.0)


def test_islandable_passed_through_from_panel() -> None:
    panel = _panel(islandable=True)
    r = resolve(
        panel=panel,
        circuits={},
        gated_powers={},
        battery_w=0.0,
        grid_online=True,
        has_battery=False,
    )
    assert r.grid_islandable is True


def test_dsm_and_run_config_track_grid_state() -> None:
    panel = _panel()
    on = resolve(
        panel=panel,
        circuits={},
        gated_powers={},
        battery_w=0.0,
        grid_online=True,
        has_battery=False,
    )
    off = resolve(
        panel=panel,
        circuits={},
        gated_powers={},
        battery_w=0.0,
        grid_online=False,
        has_battery=True,
    )
    assert on.dsm_state == "DSM_ON_GRID"
    assert on.current_run_config == "PANEL_ON_GRID"
    assert off.dsm_state == "DSM_OFF_GRID"
    assert off.current_run_config == "PANEL_OFF_GRID"


# -- power-flows node balance ------------------------------------------------
#
# The four ``power-flows`` values are one balance at one node, so they sum to
# zero. This is not a style rule; it is the identity a shipping panel satisfies
# to the last digit it publishes, and it follows from the signs SPAN documents:
# grid positive while exporting, pv negative while producing, battery positive
# while charging, site positive while consuming -- every term stated as power
# leaving the panel node. See SPAN-API-Client-Docs,
# ``docs/public/power-and-energy-conventions.md``, "The ``power-flows``
# capability is an exception", which also records that these signs are
# longstanding and did not change in the parent/child migration.
#
# The emitter used to publish pv, grid and battery in the meter frame instead,
# which put the sum out by twice the site load and made a producing array read
# as negative in Home Assistant.
#
# ``power_flow_grid`` is computed from the lugs and the BESS, not back-solved
# from the other three -- these assertions have teeth only because grid arrives
# independently.


def _flow_sum(r: PanelMeterReading) -> float:
    return r.power_flow_pv + r.power_flow_grid + r.power_flow_site + r.power_flow_battery


@pytest.mark.parametrize(
    ("label", "powers", "battery_w", "grid_online", "has_battery"),
    [
        ("consumer only", {"kitchen": 1000.0}, 0.0, True, False),
        ("pv exporting", {"kitchen": 500.0, "solar": -2000.0}, 0.0, True, False),
        ("pv covering load exactly", {"kitchen": 2000.0, "solar": -2000.0}, 0.0, True, False),
        ("battery discharging", {"kitchen": 3000.0}, 2000.0, True, True),
        (
            "battery charging from pv surplus",
            {"kitchen": 500.0, "solar": -2000.0},
            -1500.0,
            True,
            True,
        ),
        (
            "battery charging beyond pv surplus",
            {"kitchen": 500.0, "solar": -2000.0},
            -2500.0,
            True,
            True,
        ),
        ("battery charging with no pv at all", {"kitchen": 500.0}, -1500.0, True, True),
        ("off grid, battery covers load", {"kitchen": 1000.0}, 1000.0, False, True),
        (
            "off grid, battery covers load net of pv",
            {"kitchen": 1800.0, "solar": -800.0},
            1000.0,
            False,
            True,
        ),
    ],
)
def test_power_flows_sum_to_zero(
    label: str,
    powers: dict[str, float],
    battery_w: float,
    grid_online: bool,
    has_battery: bool,
) -> None:
    circuits = {cid: _circuit(tabs=(i * 2 + 1,)) for i, cid in enumerate(powers)}
    r = resolve(
        panel=_panel(),
        circuits=circuits,
        gated_powers=powers,
        battery_w=battery_w,
        grid_online=grid_online,
        has_battery=has_battery,
    )
    assert _flow_sum(r) == pytest.approx(0.0, abs=1e-9), label


def test_power_flow_signs_match_a_producing_panel() -> None:
    """Exporting solar, no battery -- the case the hardware check covered.

    PV is negative because it feeds the node, grid is positive because power
    leaves through it, site is positive because loads draw from it.
    """
    r = resolve(
        panel=_panel(),
        circuits={"kitchen": _circuit(tabs=(1,)), "solar": _circuit(tabs=(3,))},
        gated_powers={"kitchen": 500.0, "solar": -2000.0},
        battery_w=0.0,
        grid_online=True,
        has_battery=False,
    )
    assert r.power_flow_pv == -2000.0
    assert r.power_flow_site == 500.0
    assert r.power_flow_grid == 1500.0
    assert r.power_flow_battery == 0.0
    # The meter frame disagrees at the same instant, and that is correct: the
    # upstream lugs read negative while exporting.
    assert r.upstream_active_power_w == -1500.0


def test_charging_battery_is_positive_and_discharging_is_negative() -> None:
    """The sign the live panel could not settle -- it has no BESS.

    Fixed by the balance rather than by observation: a charging battery draws
    from the node exactly as a load does, so it carries a load's sign.
    """
    charging = resolve(
        panel=_panel(),
        circuits={"kitchen": _circuit(tabs=(1,))},
        gated_powers={"kitchen": 500.0},
        battery_w=-1500.0,
        grid_online=True,
        has_battery=True,
    )
    discharging = resolve(
        panel=_panel(),
        circuits={"kitchen": _circuit(tabs=(1,))},
        gated_powers={"kitchen": 3000.0},
        battery_w=2000.0,
        grid_online=True,
        has_battery=True,
    )
    assert charging.power_flow_battery == 1500.0
    assert discharging.power_flow_battery == -2000.0


def test_charging_beyond_pv_surplus_shows_the_grid_import_it_causes() -> None:
    """500 W of load, 2 kW of PV, a BESS pulling 2.5 kW: 1 kW has to come from
    the utility. The old lugs-vs-BESS clamp reported that as zero import."""
    r = resolve(
        panel=_panel(),
        circuits={"kitchen": _circuit(tabs=(1,)), "solar": _circuit(tabs=(3,))},
        gated_powers={"kitchen": 500.0, "solar": -2000.0},
        battery_w=-2500.0,
        grid_online=True,
        has_battery=True,
    )
    assert r.instant_grid_power_w == 1000.0
    assert r.power_flow_grid == -1000.0
