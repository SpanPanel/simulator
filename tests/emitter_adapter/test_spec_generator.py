from pathlib import Path

import pytest
import yaml

from span_panel_simulator.emitter_adapter.instance_ids import stable_circuit_uuid
from span_panel_simulator.emitter_adapter.spec_generator import build_manifest


def _profile() -> dict:
    """Load the default_MAIN_40 clone profile fixture."""
    return yaml.safe_load(Path("configs/default_MAIN_40.yaml").read_text())


def test_build_manifest_includes_panel_lugs_and_circuits() -> None:
    manifest = build_manifest(_profile())
    assert len(manifest.of_class("panel")) == 1
    assert len(manifest.of_class("lugs")) == 2
    assert len(manifest.of_class("circuit")) > 0


def test_build_manifest_includes_bess_when_enabled() -> None:
    profile = _profile()
    if profile.get("bess", {}).get("enabled"):
        manifest = build_manifest(profile)
        assert len(manifest.of_class("bess")) == 1
    else:
        pytest.skip("default_MAIN_40 has no enabled BESS")


def test_build_manifest_derives_pv_and_evse_from_device_type_templates() -> None:
    profile = _profile()
    # Read from the fixture rather than hardcoded: a feed uuid is scoped to the
    # owning panel, so a test that scoped with some other serial would assert a
    # value the manifest never emits.
    panel_id = profile["panel_config"]["serial_number"]
    manifest = build_manifest(profile)

    pv = manifest.of_class("pv")[0]
    assert pv.instance_id == "pv"
    assert pv.metadata["feed"] == stable_circuit_uuid(panel_id, "solar_inverter")
    assert pv.metadata["relative-position"] == "IN_PANEL"

    # The node id is the drive serial, not a positional slot. `evse` / `evse-2` were
    # invented here and no panel publishes them; firmware names the node after the
    # drive, which is what lets an EVSE keep its identity across a schema upgrade.
    # Asserting the two are equal rather than asserting a literal: the literal would
    # still pass if one of them were derived some other way that happened to match.
    evse = manifest.of_class("evse")[0]
    assert evse.instance_id == evse.metadata["serial-number"]
    assert evse.metadata["feed"] == stable_circuit_uuid(panel_id, "span_drive_garage")
    assert len(manifest.of_class("evse")) == 2
    second = manifest.of_class("evse")[1]
    assert second.instance_id == second.metadata["serial-number"]
    assert second.instance_id != evse.instance_id, "two drives must not share a node id"
    assert second.metadata["feed"] == stable_circuit_uuid(panel_id, "span_drive_driveway")


def test_build_manifest_omits_native_devices_when_disabled() -> None:
    profile = {
        "panel_config": {"serial_number": "test-001"},
        "circuits": [],
    }
    manifest = build_manifest(profile)
    assert len(manifest.of_class("bess")) == 0
    assert len(manifest.of_class("pv")) == 0
    assert len(manifest.of_class("evse")) == 0


def test_build_manifest_panel_id_matches_serial() -> None:
    profile = {
        "panel_config": {"serial_number": "abc-123", "display_name": "Test Panel"},
        "circuits": [],
    }
    manifest = build_manifest(profile)
    panel = manifest.of_class("panel")[0]
    assert panel.instance_id == "abc-123"
    assert panel.display_name == "Test Panel"


# ---- v0.3.0 physics-key emission --------------------------------------------


def test_panel_metadata_includes_physics_keys() -> None:
    profile = {
        "panel_config": {
            "serial_number": "abc-123",
            "total_tabs": 40,
            "main_size": 200,
            "postal_code": "94110",
            "time_zone": "America/Los_Angeles",
        },
        "circuits": [],
    }
    panel = build_manifest(profile).of_class("panel")[0]
    assert panel.metadata["panel-size"] == "40"
    assert panel.metadata["main-breaker-rating-a"] == "200"
    assert panel.metadata["panel-model"] == "MAIN_40"
    assert panel.metadata["postal-code"] == "94110"
    assert panel.metadata["time-zone"] == "America/Los_Angeles"
    assert panel.metadata["service-voltage-v"] == "240.0"
    assert panel.metadata["line-voltage-v"] == "120.0"
    assert panel.metadata["islandable"] == "false"


def test_circuit_metadata_includes_physics_keys() -> None:
    profile = {
        "panel_config": {"serial_number": "abc-123", "total_tabs": 40, "main_size": 200},
        "circuit_templates": {
            "lighting": {
                "priority": "NICE_TO_HAVE",
                "relay_behavior": "controllable",
                "breaker_rating_a": 15.0,
            },
        },
        "circuits": [
            {"id": "kitchen", "name": "Kitchen", "template": "lighting", "tabs": [1]},
            {"id": "hvac", "name": "HVAC", "template": "lighting", "tabs": [3, 4]},
        ],
    }
    manifest = build_manifest(profile)
    circuits = manifest.of_class("circuit")
    assert len(circuits) == 2

    by_name = {c.display_name: c for c in circuits}
    kitchen = by_name["Kitchen"]
    assert kitchen.metadata["tab-numbers"] == "1"
    assert kitchen.metadata["breaker-rating-a"] == "15.0"
    assert kitchen.metadata["default-priority"] == "NICE_TO_HAVE"
    assert kitchen.metadata["relay-behavior"] == "controllable"
    assert kitchen.metadata["placement"] == "downstream-of-lugs"
    assert kitchen.metadata["always-on"] == "false"
    assert kitchen.metadata["never-backup"] == "false"

    hvac = by_name["HVAC"]
    assert hvac.metadata["tab-numbers"] == "3,4"


def test_circuit_never_backup_is_per_circuit_not_per_template() -> None:
    """The installer's lock belongs to one circuit; two circuits sharing a load
    template can be commissioned differently, which is why the flag is read
    from the circuit definition and not the template."""
    profile = {
        "panel_config": {"serial_number": "abc-123", "total_tabs": 40, "main_size": 200},
        "circuit_templates": {
            "resistive": {
                "priority": "NEVER",
                "relay_behavior": "controllable",
                "breaker_rating_a": 30.0,
            },
        },
        "circuits": [
            {"id": "hot_tub", "name": "Hot Tub", "template": "resistive", "tabs": [1]},
            {
                "id": "pool_heater",
                "name": "Pool Heater",
                "template": "resistive",
                "tabs": [3],
                "never_backup": True,
            },
        ],
    }
    by_name = {c.display_name: c for c in build_manifest(profile).of_class("circuit")}
    assert by_name["Hot Tub"].metadata["never-backup"] == "false"
    assert by_name["Pool Heater"].metadata["never-backup"] == "true"
    # Both keep the template's priority in the manifest; the lock's OFF_GRID is
    # resolved at emit time, not baked into the circuit's commissioned value.
    assert by_name["Pool Heater"].metadata["default-priority"] == "NEVER"


def test_bess_metadata_includes_physics_keys() -> None:
    profile = {
        "panel_config": {"serial_number": "abc-123", "total_tabs": 40, "main_size": 200},
        "circuits": [],
        "bess": {"enabled": True, "nameplate_capacity_kwh": 13.5, "initial_soe_kwh": 6.75},
    }
    bess = build_manifest(profile).of_class("bess")[0]
    assert bess.instance_id == "bess"
    assert bess.metadata["vendor-name"] == "Span"
    assert bess.metadata["nameplate-capacity-kwh"] == "13.5"
    assert bess.metadata["relative-position"] == "UPSTREAM"
    assert bess.metadata["initial-soe-kwh"] == "6.75"


def test_pv_metadata_includes_inverter_type() -> None:
    profile = {
        "panel_config": {"serial_number": "abc-123", "total_tabs": 40, "main_size": 200},
        "circuits": [],
        "pv": {
            "enabled": True,
            "vendor": "Enphase",
            "nameplate_capacity_w": 7000.0,
            "inverter_type": "hybrid",
        },
    }
    manifest = build_manifest(profile)
    pv = manifest.of_class("pv")[0]
    assert pv.metadata["inverter-type"] == "hybrid"
    assert pv.metadata["nameplate-capacity-w"] == "7000.0"
    assert pv.metadata["relative-position"] == "UPSTREAM"
    # Hybrid PV → panel becomes islandable.
    panel = manifest.of_class("panel")[0]
    assert panel.metadata["islandable"] == "true"


def test_evse_metadata_includes_physics_keys() -> None:
    profile = {
        "panel_config": {"serial_number": "abc-123", "total_tabs": 40, "main_size": 200},
        "circuits": [],
        "evse": {"enabled": True, "max_current_a": 40.0},
    }
    evse = build_manifest(profile).of_class("evse")[0]
    assert evse.metadata["max-current-a"] == "40.0"
    assert evse.metadata["product-name"] == "SPAN Drive"


def test_circuit_relay_behavior_translates_underscore_to_hyphen() -> None:
    profile = {
        "panel_config": {"serial_number": "abc-123", "total_tabs": 40, "main_size": 200},
        "circuit_templates": {
            "always": {"priority": "MUST_HAVE", "relay_behavior": "always_on"},
        },
        "circuits": [{"id": "smoke", "name": "Smoke Alarm", "template": "always", "tabs": [1]}],
    }
    c = build_manifest(profile).of_class("circuit")[0]
    assert c.metadata["relay-behavior"] == "always-on"
    assert c.metadata["always-on"] == "true"


def test_two_panels_sharing_circuit_ids_emit_distinct_circuit_devices() -> None:
    """The same YAML circuit ids appear in every panel config, so an unscoped
    uuid gave two panels one circuit identity. PanelBench publishes circuits
    flat and collided on the wire for this reason; here the ids must simply
    match PanelBench's, so that swapping one for the other does not re-key
    every circuit."""

    def _profile_for(serial: str) -> dict:
        return {
            "panel_config": {"serial_number": serial, "total_tabs": 40, "main_size": 200},
            "circuit_templates": {"lighting": {"priority": "NICE_TO_HAVE"}},
            "circuits": [
                {"id": "kitchen", "name": "Kitchen", "template": "lighting", "tabs": [1]},
                {"id": "oven", "name": "Oven", "template": "lighting", "tabs": [3]},
            ],
        }

    first = {c.instance_id for c in build_manifest(_profile_for("sim-a")).of_class("circuit")}
    second = {c.instance_id for c in build_manifest(_profile_for("sim-b")).of_class("circuit")}
    assert len(first) == 2
    assert not first & second


def test_evse_feed_points_at_the_circuit_device_this_panel_publishes() -> None:
    """The feed is a cross-reference to a circuit's device id, so it has to be
    scoped with the same serial the circuit instance was — not left unscoped
    while the circuit moved, which would point every drive at nothing."""
    profile = {
        "panel_config": {"serial_number": "sim-a", "total_tabs": 40, "main_size": 200},
        "circuit_templates": {"span_drive": {"device_type": "evse"}},
        "circuits": [{"id": "garage", "name": "Garage", "template": "span_drive", "tabs": [1]}],
    }
    manifest = build_manifest(profile)
    circuit_ids = {c.instance_id for c in manifest.of_class("circuit")}
    assert manifest.of_class("evse")[0].metadata["feed"] in circuit_ids
