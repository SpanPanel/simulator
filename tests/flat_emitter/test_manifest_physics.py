import pytest

from span_panel_simulator.flat_emitter.conventions.tab_legs import Leg
from span_panel_simulator.flat_emitter.exceptions import ManifestValidationError
from span_panel_simulator.flat_emitter.manifest import DeviceInstance, DeviceManifest
from span_panel_simulator.flat_emitter.manifest_physics import ManifestPhysicsView


def _panel(**md: str) -> DeviceInstance:
    base = {
        "serial-number": "abc-123",
        "vendor-name": "Span",
        "firmware-version": "sim/v0.1.0",
        "hardware-version": "rev2",
        "panel-size": "40",
        "main-breaker-rating-a": "200",
        "panel-model": "MAIN_40",
        "postal-code": "94103",
        "time-zone": "America/Los_Angeles",
    }
    base.update(md)
    return DeviceInstance(
        entity_class="panel",
        instance_id="abc-123",
        display_name="Panel",
        metadata=base,
    )


def _circuit(instance_id: str = "kitchen", **md: str) -> DeviceInstance:
    base = {
        "tab-numbers": "1",
        "breaker-rating-a": "20",
        "default-priority": "NICE_TO_HAVE",
        "relay-behavior": "controllable",
        "placement": "downstream-of-lugs",
    }
    base.update(md)
    return DeviceInstance(
        entity_class="circuit",
        instance_id=instance_id,
        display_name=instance_id,
        metadata=base,
    )


def test_panel_view_with_defaults() -> None:
    view = ManifestPhysicsView(DeviceManifest(instances=(_panel(),)))
    assert view.panel.serial_number == "abc-123"
    assert view.panel.service_voltage_v == 240.0
    assert view.panel.line_voltage_v == 120.0
    assert view.panel.islandable is False


def test_panel_voltage_overrides_apply() -> None:
    view = ManifestPhysicsView(
        DeviceManifest(
            instances=(
                _panel(
                    **{"service-voltage-v": "400", "line-voltage-v": "230", "islandable": "true"}
                ),
            )
        )
    )
    assert view.panel.service_voltage_v == 400.0
    assert view.panel.line_voltage_v == 230.0
    assert view.panel.islandable is True


def test_missing_panel_raises() -> None:
    with pytest.raises(ManifestValidationError, match="no panel instance"):
        ManifestPhysicsView(DeviceManifest(instances=()))


def test_missing_required_panel_key_raises() -> None:
    bad = DeviceInstance(
        entity_class="panel",
        instance_id="x",
        display_name="x",
        metadata={},
    )
    with pytest.raises(ManifestValidationError, match="serial-number"):
        ManifestPhysicsView(DeviceManifest(instances=(bad,)))


def test_circuit_single_tab_l1() -> None:
    view = ManifestPhysicsView(DeviceManifest(instances=(_panel(), _circuit())))
    c = view.circuit("kitchen")
    assert c.tabs == (1,)
    assert c.legs == (Leg.L1,)
    assert c.dipole is False
    assert c.always_on is False


def test_circuit_dipole_spans_legs() -> None:
    view = ManifestPhysicsView(
        DeviceManifest(
            instances=(
                _panel(),
                _circuit("hvac", **{"tab-numbers": "1,2", "breaker-rating-a": "40"}),
            )
        )
    )
    c = view.circuit("hvac")
    assert c.tabs == (1, 2)
    assert c.legs == (Leg.L1, Leg.L2)
    assert c.dipole is True


def test_circuit_dipole_on_same_leg_is_allowed() -> None:
    """Real SPAN panels gang two adjacent same-leg tabs as dipole feeds, so we
    don't enforce the spans-both-legs rule on the dipole flag. The legs tuple
    just reports the truth."""
    inst = _circuit("hvac", **{"tab-numbers": "1,3", "dipole": "true"})
    view = ManifestPhysicsView(DeviceManifest(instances=(_panel(), inst)))
    c = view.circuit("hvac")
    assert c.tabs == (1, 3)
    assert c.dipole is True
    assert c.legs == (Leg.L1, Leg.L1)


def test_circuit_multi_tab_without_dipole_flag_raises() -> None:
    bad = _circuit("hvac", **{"tab-numbers": "1,2", "dipole": "false"})
    with pytest.raises(ManifestValidationError, match="single-tab circuits only"):
        ManifestPhysicsView(DeviceManifest(instances=(_panel(), bad)))


def test_circuit_invalid_priority_raises() -> None:
    bad = _circuit(**{"default-priority": "BOGUS"})
    with pytest.raises(ManifestValidationError, match="default-priority"):
        ManifestPhysicsView(DeviceManifest(instances=(_panel(), bad)))


def test_circuit_invalid_placement_raises() -> None:
    bad = _circuit(**{"placement": "side-of-lugs"})
    with pytest.raises(ManifestValidationError, match="placement"):
        ManifestPhysicsView(DeviceManifest(instances=(_panel(), bad)))


def test_circuit_always_on_default_from_relay_behavior() -> None:
    view = ManifestPhysicsView(
        DeviceManifest(
            instances=(
                _panel(),
                _circuit("dryer", **{"relay-behavior": "always-on"}),
            )
        )
    )
    assert view.circuit("dryer").always_on is True


def test_circuit_never_backup_defaults_false_even_at_never_priority() -> None:
    """``never-backup`` is a commissioning input of its own, so nothing derives
    it — least of all the priority value ``NEVER``, which means "never shed"."""
    view = ManifestPhysicsView(
        DeviceManifest(
            instances=(
                _panel(),
                _circuit("lights", **{"default-priority": "NEVER"}),
            )
        )
    )
    assert view.circuit("lights").never_backup is False


def test_circuit_never_backup_is_read_from_the_manifest() -> None:
    view = ManifestPhysicsView(
        DeviceManifest(
            instances=(
                _panel(),
                _circuit("hot_tub", **{"never-backup": "true"}),
            )
        )
    )
    assert view.circuit("hot_tub").never_backup is True


def test_circuit_invalid_never_backup_raises() -> None:
    with pytest.raises(ManifestValidationError, match="never-backup"):
        ManifestPhysicsView(
            DeviceManifest(
                instances=(_panel(), _circuit("hot_tub", **{"never-backup": "sometimes"})),
            )
        )


def test_circuit_initial_energy_seeds() -> None:
    view = ManifestPhysicsView(
        DeviceManifest(
            instances=(
                _panel(),
                _circuit(
                    "kitchen", **{"initial-consumed-wh": "12345.0", "initial-produced-wh": "67.5"}
                ),
            )
        )
    )
    c = view.circuit("kitchen")
    assert c.initial_consumed_wh == 12345.0
    assert c.initial_produced_wh == 67.5


def test_circuit_zero_tab_raises() -> None:
    bad = _circuit(**{"tab-numbers": "0"})
    with pytest.raises(ManifestValidationError, match="must be >= 1"):
        ManifestPhysicsView(DeviceManifest(instances=(_panel(), bad)))


def test_lugs_direction_validated() -> None:
    good_up = DeviceInstance(
        entity_class="lugs",
        instance_id="up",
        display_name="up",
        metadata={"direction": "upstream"},
    )
    good_dn = DeviceInstance(
        entity_class="lugs",
        instance_id="dn",
        display_name="dn",
        metadata={"direction": "downstream"},
    )
    view = ManifestPhysicsView(DeviceManifest(instances=(_panel(), good_up, good_dn)))
    assert view.lugs("up").direction == "upstream"
    assert view.lugs("dn").direction == "downstream"

    bad = DeviceInstance(
        entity_class="lugs",
        instance_id="x",
        display_name="x",
        metadata={"direction": "sideways"},
    )
    with pytest.raises(ManifestValidationError, match="direction"):
        ManifestPhysicsView(DeviceManifest(instances=(_panel(), bad)))


def test_bess_with_initial_soe() -> None:
    bess = DeviceInstance(
        entity_class="bess",
        instance_id="b1",
        display_name="Battery",
        metadata={
            "vendor-name": "Span",
            "nameplate-capacity-kwh": "13.5",
            "initial-soe-kwh": "6.75",
        },
    )
    view = ManifestPhysicsView(DeviceManifest(instances=(_panel(), bess)))
    b = view.bess("b1")
    assert b.nameplate_capacity_kwh == 13.5
    assert b.initial_soe_kwh == 6.75


def test_bess_without_initial_soe_returns_none() -> None:
    bess = DeviceInstance(
        entity_class="bess",
        instance_id="b1",
        display_name="Battery",
        metadata={"vendor-name": "Span", "nameplate-capacity-kwh": "13.5"},
    )
    view = ManifestPhysicsView(DeviceManifest(instances=(_panel(), bess)))
    assert view.bess("b1").initial_soe_kwh is None


def test_pv_inverter_type_validated() -> None:
    pv = DeviceInstance(
        entity_class="pv",
        instance_id="pv1",
        display_name="Solar",
        metadata={
            "vendor-name": "Enphase",
            "nameplate-capacity-w": "5000",
            "inverter-type": "hybrid",
        },
    )
    view = ManifestPhysicsView(DeviceManifest(instances=(_panel(), pv)))
    assert view.pv("pv1").inverter_type == "hybrid"

    bad = DeviceInstance(
        entity_class="pv",
        instance_id="pv2",
        display_name="Solar",
        metadata={
            "vendor-name": "Enphase",
            "nameplate-capacity-w": "5000",
            "inverter-type": "string",
        },
    )
    with pytest.raises(ManifestValidationError, match="inverter-type"):
        ManifestPhysicsView(DeviceManifest(instances=(_panel(), bad)))


def test_evse_required_fields() -> None:
    evse = DeviceInstance(
        entity_class="evse",
        instance_id="ev1",
        display_name="EV",
        metadata={
            "vendor-name": "SPAN",
            "product-name": "SPAN Drive",
            "part-number": "SPN-DRV-001",
            "serial-number": "SIM-EVSE-1",
            "firmware-version": "1.0",
            "max-current-a": "32",
        },
    )
    view = ManifestPhysicsView(DeviceManifest(instances=(_panel(), evse)))
    assert view.evse("ev1").max_current_a == 32.0


def test_multiple_panels_raises() -> None:
    p2 = DeviceInstance(
        entity_class="panel",
        instance_id="def-456",
        display_name="P2",
        metadata={**_panel().metadata, "serial-number": "def-456"},
    )
    with pytest.raises(ManifestValidationError, match="Multiple panel"):
        ManifestPhysicsView(DeviceManifest(instances=(_panel(), p2)))


def test_error_message_includes_offending_instance_id() -> None:
    bad = _circuit("microwave", **{"default-priority": "BOGUS"})
    with pytest.raises(ManifestValidationError, match="circuit/microwave"):
        ManifestPhysicsView(DeviceManifest(instances=(_panel(), bad)))
