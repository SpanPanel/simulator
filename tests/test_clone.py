"""Tests for the eBus-to-YAML translation layer (clone.py)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import yaml

from span_panel_simulator.clone import (
    translate_scraped_panel,
    update_config_from_scrape,
    write_clone_config,
)
from span_panel_simulator.scraper import ScrapedPanel
from span_panel_simulator.validation import validate_yaml_config

if TYPE_CHECKING:
    from pathlib import Path

# A realistic $description fixture with circuits, BESS, PV, and EVSE nodes
_SERIAL = "nj-2316-1234"
_PREFIX = f"ebus/5/{_SERIAL}"

_DESCRIPTION: dict[str, dict[str, dict[str, str]]] = {
    "nodes": {
        "core": {"type": "energy.ebus.device.distribution-enclosure.core"},
        "upstream-lugs": {"type": "energy.ebus.device.lugs"},
        "downstream-lugs": {"type": "energy.ebus.device.lugs"},
        "aaa111": {"type": "energy.ebus.device.circuit"},  # Living Room Lights, space 1
        "bbb222": {"type": "energy.ebus.device.circuit"},  # Kitchen Outlets, space 3/5 (240V)
        "ccc333": {"type": "energy.ebus.device.circuit"},  # Solar Inverter, space 7/9 (240V)
        "ddd444": {"type": "energy.ebus.device.circuit"},  # Battery Storage, space 11/13 (240V)
        "eee555": {"type": "energy.ebus.device.circuit"},  # SPAN Drive, space 15/17 (240V)
        "bess-0": {"type": "energy.ebus.device.bess"},
        "pv-0": {"type": "energy.ebus.device.pv"},
        "evse-0": {"type": "energy.ebus.device.evse"},
        "pcs-0": {"type": "energy.ebus.device.pcs"},
        "power-flows": {"type": "energy.ebus.device.power-flows"},
    }
}


def _base_properties() -> dict[str, str]:
    """Build a minimal but complete set of scraped properties."""
    p: dict[str, str] = {}

    def _set(node: str, prop: str, val: str) -> None:
        p[f"{_PREFIX}/{node}/{prop}"] = val

    # Core
    _set("core", "serial-number", _SERIAL)
    _set("core", "breaker-rating", "200")

    # $state
    p[f"{_PREFIX}/$state"] = "ready"
    p[f"{_PREFIX}/$description"] = "{}"  # not used by translator (it gets parsed description)

    # Circuit 1: Living Room Lights — single-pole, space 1
    _set("aaa111", "name", "Living Room Lights")
    _set("aaa111", "space", "1")
    _set("aaa111", "dipole", "false")
    _set("aaa111", "breaker-rating", "15")
    _set("aaa111", "relay", "CLOSED")
    _set("aaa111", "shed-priority", "NEVER")
    _set("aaa111", "active-power", "-150.0")
    _set("aaa111", "always-on", "false")
    _set("aaa111", "imported-energy", "54321.0")
    _set("aaa111", "exported-energy", "0.0")

    # Circuit 2: Kitchen Outlets — 240V, space 3/5
    _set("bbb222", "name", "Kitchen Outlets")
    _set("bbb222", "space", "3")
    _set("bbb222", "dipole", "true")
    _set("bbb222", "breaker-rating", "20")
    _set("bbb222", "relay", "CLOSED")
    _set("bbb222", "shed-priority", "SOC_THRESHOLD")
    _set("bbb222", "active-power", "-800.0")
    _set("bbb222", "always-on", "false")

    # Circuit 3: Solar Inverter — 240V, space 7/9, fed by pv-0
    _set("ccc333", "name", "Solar Inverter")
    _set("ccc333", "space", "7")
    _set("ccc333", "dipole", "true")
    _set("ccc333", "breaker-rating", "30")
    _set("ccc333", "relay", "CLOSED")
    _set("ccc333", "shed-priority", "NEVER")
    _set("ccc333", "active-power", "3000.0")  # producing (positive on eBus = export)
    _set("ccc333", "always-on", "true")
    _set("ccc333", "imported-energy", "0.0")
    _set("ccc333", "exported-energy", "1234567.0")

    # Circuit 4: Battery Storage — 240V, space 11/13, fed by bess-0
    _set("ddd444", "name", "Battery Storage")
    _set("ddd444", "space", "11")
    _set("ddd444", "dipole", "true")
    _set("ddd444", "breaker-rating", "40")
    _set("ddd444", "relay", "CLOSED")
    _set("ddd444", "shed-priority", "NEVER")
    _set("ddd444", "active-power", "-2000.0")
    _set("ddd444", "always-on", "true")

    # Circuit 5: SPAN Drive — 240V, space 15/17, fed by evse-0
    _set("eee555", "name", "SPAN Drive")
    _set("eee555", "space", "15")
    _set("eee555", "dipole", "true")
    _set("eee555", "breaker-rating", "50")
    _set("eee555", "relay", "CLOSED")
    _set("eee555", "shed-priority", "OFF_GRID")
    _set("eee555", "active-power", "-7200.0")
    _set("eee555", "always-on", "false")

    # Device feeds
    _set("bess-0", "feed", "ddd444")
    _set("bess-0", "nameplate-capacity", "13.5")
    _set("bess-0", "soc", "85.0")
    _set("pv-0", "feed", "ccc333")
    _set("pv-0", "nameplate-capacity", "5000.0")
    _set("evse-0", "feed", "eee555")

    return p


def _make_scraped(
    props: dict[str, str] | None = None,
    desc: dict[str, dict[str, dict[str, str]]] | None = None,
) -> ScrapedPanel:
    """Build a ScrapedPanel fixture."""
    return ScrapedPanel(
        serial_number=_SERIAL,
        description=desc or _DESCRIPTION,
        properties=props or _base_properties(),
        mqtts_port=8883,
        ca_pem=b"fake-ca-pem",
    )


class TestTranslateScrapedPanel:
    """Tests for translate_scraped_panel()."""

    def test_basic_structure(self) -> None:
        """Config has all required top-level sections."""
        config = translate_scraped_panel(_make_scraped())
        assert "panel_config" in config
        assert "circuit_templates" in config
        assert "circuits" in config
        assert "unmapped_tabs" in config
        assert "simulation_params" in config

    def test_serial_suffix(self) -> None:
        """Clone serial gets sim- prefix."""
        config = translate_scraped_panel(_make_scraped())
        panel = config["panel_config"]
        assert isinstance(panel, dict)
        assert panel["serial_number"] == f"sim-{_SERIAL}-clone"

    def test_main_breaker(self) -> None:
        """Main breaker rating is extracted from core properties."""
        config = translate_scraped_panel(_make_scraped())
        panel = config["panel_config"]
        assert isinstance(panel, dict)
        assert panel["main_size"] == 200

    def test_circuit_count(self) -> None:
        """All 5 circuit nodes produce circuit definitions."""
        config = translate_scraped_panel(_make_scraped())
        circuits = config["circuits"]
        assert isinstance(circuits, list)
        assert len(circuits) == 5

    def test_single_pole_tabs(self) -> None:
        """Single-pole circuit (space 1) has one tab."""
        config = translate_scraped_panel(_make_scraped())
        circuits = config["circuits"]
        assert isinstance(circuits, list)
        # Find circuit_1
        c1 = next(c for c in circuits if isinstance(c, dict) and c["id"] == "circuit_1")
        assert c1["tabs"] == [1]

    def test_double_pole_tabs(self) -> None:
        """240V circuit (space 3, dipole) has two tabs [3, 5]."""
        config = translate_scraped_panel(_make_scraped())
        circuits = config["circuits"]
        assert isinstance(circuits, list)
        c3 = next(c for c in circuits if isinstance(c, dict) and c["id"] == "circuit_3")
        assert c3["tabs"] == [3, 5]

    def test_pv_mode(self) -> None:
        """Circuit fed by PV node gets producer mode."""
        config = translate_scraped_panel(_make_scraped())
        templates = config["circuit_templates"]
        assert isinstance(templates, dict)
        # Space 7 is the PV circuit
        t = templates["clone_7"]
        assert isinstance(t, dict)
        ep = t["energy_profile"]
        assert isinstance(ep, dict)
        assert ep["mode"] == "producer"
        assert t.get("device_type") == "pv"

    def test_bess_mode(self) -> None:
        """Circuit fed by BESS node gets bidirectional mode and battery_behavior."""
        config = translate_scraped_panel(_make_scraped())
        templates = config["circuit_templates"]
        assert isinstance(templates, dict)
        t = templates["clone_11"]
        assert isinstance(t, dict)
        ep = t["energy_profile"]
        assert isinstance(ep, dict)
        assert ep["mode"] == "bidirectional"
        assert "battery_behavior" in t
        bb = t["battery_behavior"]
        assert isinstance(bb, dict)
        assert bb["enabled"] is True
        assert bb["nameplate_capacity_kwh"] == 13.5

    def test_evse_mode(self) -> None:
        """Circuit fed by EVSE node gets bidirectional mode and evse device type."""
        config = translate_scraped_panel(_make_scraped())
        templates = config["circuit_templates"]
        assert isinstance(templates, dict)
        t = templates["clone_15"]
        assert isinstance(t, dict)
        assert t.get("device_type") == "evse"
        ep = t["energy_profile"]
        assert isinstance(ep, dict)
        assert ep["mode"] == "bidirectional"
        assert "time_of_day_profile" in t

    def test_consumer_mode(self) -> None:
        """Regular circuit gets consumer mode."""
        config = translate_scraped_panel(_make_scraped())
        templates = config["circuit_templates"]
        assert isinstance(templates, dict)
        t = templates["clone_1"]
        assert isinstance(t, dict)
        ep = t["energy_profile"]
        assert isinstance(ep, dict)
        assert ep["mode"] == "consumer"

    def test_always_on_non_controllable(self) -> None:
        """Circuit with always-on=true gets non_controllable relay behavior."""
        config = translate_scraped_panel(_make_scraped())
        templates = config["circuit_templates"]
        assert isinstance(templates, dict)
        # Solar inverter (space 7) has always_on=true
        t = templates["clone_7"]
        assert isinstance(t, dict)
        assert t["relay_behavior"] == "non_controllable"

    def test_controllable_relay(self) -> None:
        """Circuit with always-on=false gets controllable relay behavior."""
        config = translate_scraped_panel(_make_scraped())
        templates = config["circuit_templates"]
        assert isinstance(templates, dict)
        t = templates["clone_1"]
        assert isinstance(t, dict)
        assert t["relay_behavior"] == "controllable"

    def test_priority_passthrough(self) -> None:
        """Shed priority passes through from eBus to template."""
        config = translate_scraped_panel(_make_scraped())
        templates = config["circuit_templates"]
        assert isinstance(templates, dict)
        t3 = templates["clone_3"]
        assert isinstance(t3, dict)
        assert t3["priority"] == "SOC_THRESHOLD"
        t15 = templates["clone_15"]
        assert isinstance(t15, dict)
        assert t15["priority"] == "OFF_GRID"

    def test_panel_size_derivation(self) -> None:
        """Panel size rounds up to standard size from max space+companion."""
        config = translate_scraped_panel(_make_scraped())
        panel = config["panel_config"]
        assert isinstance(panel, dict)
        # Max space is 15 (dipole), companion is 17 → round up to 24
        assert panel["total_tabs"] == 24

    def test_config_validates(self) -> None:
        """Produced config passes validate_yaml_config() without error."""
        config = translate_scraped_panel(_make_scraped())
        validate_yaml_config(config)

    def test_pv_nameplate_enrichment(self) -> None:
        """PV template gets nameplate_capacity_w and adjusted power range."""
        config = translate_scraped_panel(_make_scraped())
        templates = config["circuit_templates"]
        assert isinstance(templates, dict)
        t = templates["clone_7"]
        assert isinstance(t, dict)
        ep = t["energy_profile"]
        assert isinstance(ep, dict)
        assert ep["nameplate_capacity_w"] == 5000.0
        assert ep["power_range"] == [-5000.0, 0.0]
        assert ep["typical_power"] == -3000.0


class TestWriteCloneConfig:
    """Tests for write_clone_config()."""

    def test_writes_yaml_file(self, tmp_path: Path) -> None:
        """Config is written as valid YAML to the config directory."""
        config = translate_scraped_panel(_make_scraped())
        output = write_clone_config(config, tmp_path, _SERIAL)
        assert output.exists()
        assert output.name == f"{_SERIAL}-clone.yaml"

        loaded = yaml.safe_load(output.read_text())
        assert loaded["panel_config"]["serial_number"] == f"sim-{_SERIAL}-clone"

    def test_overwrites_existing(self, tmp_path: Path) -> None:
        """Re-clone overwrites existing file."""
        config = translate_scraped_panel(_make_scraped())
        write_clone_config(config, tmp_path, _SERIAL)
        # Write again — should not raise
        output = write_clone_config(config, tmp_path, _SERIAL)
        assert output.exists()

    def test_roundtrip_validates(self, tmp_path: Path) -> None:
        """Written config can be loaded back and passes validation."""
        config = translate_scraped_panel(_make_scraped())
        output = write_clone_config(config, tmp_path, _SERIAL)
        loaded = yaml.safe_load(output.read_text())
        validate_yaml_config(loaded)


class TestEnergySeeding:
    """Tests for initial energy accumulator seeding from scraped data."""

    def test_consumer_imported_energy_seeded(self) -> None:
        """Consumer circuit gets initial_consumed_energy_wh from imported-energy."""
        config = translate_scraped_panel(_make_scraped())
        templates = config["circuit_templates"]
        assert isinstance(templates, dict)
        t = templates["clone_1"]
        assert isinstance(t, dict)
        ep = t["energy_profile"]
        assert isinstance(ep, dict)
        assert ep["initial_consumed_energy_wh"] == 54321.0

    def test_zero_energy_not_seeded(self) -> None:
        """Zero-valued energy is not written (avoids overriding annual estimate)."""
        config = translate_scraped_panel(_make_scraped())
        templates = config["circuit_templates"]
        assert isinstance(templates, dict)
        t = templates["clone_1"]
        assert isinstance(t, dict)
        ep = t["energy_profile"]
        assert isinstance(ep, dict)
        assert "initial_produced_energy_wh" not in ep

    def test_producer_exported_energy_seeded(self) -> None:
        """Producer circuit gets initial_produced_energy_wh from exported-energy."""
        config = translate_scraped_panel(_make_scraped())
        templates = config["circuit_templates"]
        assert isinstance(templates, dict)
        t = templates["clone_7"]
        assert isinstance(t, dict)
        ep = t["energy_profile"]
        assert isinstance(ep, dict)
        assert ep["initial_produced_energy_wh"] == 1234567.0

    def test_missing_energy_no_seed(self) -> None:
        """Circuits without energy topics get no initial energy seeds."""
        config = translate_scraped_panel(_make_scraped())
        templates = config["circuit_templates"]
        assert isinstance(templates, dict)
        # Kitchen Outlets (space 3) has no energy topics
        t = templates["clone_3"]
        assert isinstance(t, dict)
        ep = t["energy_profile"]
        assert isinstance(ep, dict)
        assert "initial_consumed_energy_wh" not in ep
        assert "initial_produced_energy_wh" not in ep


class TestPanelSource:
    """Tests for panel_source credential persistence."""

    def test_panel_source_written_when_host_provided(self) -> None:
        """panel_source block is written when host is passed to translate."""
        config = translate_scraped_panel(
            _make_scraped(), host="192.168.1.100", passphrase="secret"
        )
        ps = config.get("panel_source")
        assert isinstance(ps, dict)
        assert ps["origin_serial"] == _SERIAL
        assert ps["host"] == "192.168.1.100"
        assert ps["passphrase"] == "secret"
        assert "last_synced" in ps

    def test_no_panel_source_without_host(self) -> None:
        """panel_source is omitted when host is not provided."""
        config = translate_scraped_panel(_make_scraped())
        assert "panel_source" not in config

    def test_panel_source_null_passphrase(self) -> None:
        """panel_source supports null passphrase (door-bypass)."""
        config = translate_scraped_panel(_make_scraped(), host="192.168.1.100", passphrase=None)
        ps = config.get("panel_source")
        assert isinstance(ps, dict)
        assert ps["passphrase"] is None

    def test_panel_source_validates(self) -> None:
        """Config with panel_source passes validation."""
        config = translate_scraped_panel(
            _make_scraped(), host="192.168.1.100", passphrase="secret"
        )
        validate_yaml_config(config)

    def test_panel_source_roundtrip(self, tmp_path: Path) -> None:
        """panel_source survives YAML write/load roundtrip."""
        config = translate_scraped_panel(
            _make_scraped(), host="192.168.1.100", passphrase="secret"
        )
        output = write_clone_config(config, tmp_path, _SERIAL)
        loaded = yaml.safe_load(output.read_text())
        validate_yaml_config(loaded)
        ps = loaded["panel_source"]
        assert ps["origin_serial"] == _SERIAL
        assert ps["host"] == "192.168.1.100"


class TestUpdateConfigFromScrape:
    """Tests for the lightweight startup refresh (update_config_from_scrape)."""

    def test_typical_power_updated(self) -> None:
        """Active power changes are reflected in typical_power."""
        config = translate_scraped_panel(_make_scraped(), host="192.168.1.100", passphrase=None)

        # Modify scraped data to simulate changed power
        props = _base_properties()
        props[f"{_PREFIX}/aaa111/active-power"] = "-250.0"
        updated_scraped = _make_scraped(props=props)

        changed = update_config_from_scrape(config, updated_scraped)
        assert changed is True

        templates = config["circuit_templates"]
        assert isinstance(templates, dict)
        t = templates["clone_1"]
        assert isinstance(t, dict)
        ep = t["energy_profile"]
        assert isinstance(ep, dict)
        assert ep["typical_power"] == 250.0

    def test_energy_seeds_updated(self) -> None:
        """Energy accumulators are updated from new scrape."""
        config = translate_scraped_panel(_make_scraped(), host="192.168.1.100", passphrase=None)

        props = _base_properties()
        props[f"{_PREFIX}/aaa111/imported-energy"] = "99999.0"
        updated_scraped = _make_scraped(props=props)

        changed = update_config_from_scrape(config, updated_scraped)
        assert changed is True

        templates = config["circuit_templates"]
        assert isinstance(templates, dict)
        t = templates["clone_1"]
        assert isinstance(t, dict)
        ep = t["energy_profile"]
        assert isinstance(ep, dict)
        assert ep["initial_consumed_energy_wh"] == 99999.0

    def test_last_synced_updated(self) -> None:
        """panel_source.last_synced is updated on refresh."""
        config = translate_scraped_panel(_make_scraped(), host="192.168.1.100", passphrase=None)
        ps = config.get("panel_source")
        assert isinstance(ps, dict)
        old_synced = ps["last_synced"]

        import time

        time.sleep(0.01)  # ensure timestamp difference
        update_config_from_scrape(config, _make_scraped())

        assert ps["last_synced"] != old_synced

    def test_no_change_returns_false(self) -> None:
        """Returns False when scrape data matches existing config."""
        config = translate_scraped_panel(_make_scraped())
        # No panel_source → last_synced never updated → only data comparison
        # Remove panel_source to test pure data path
        changed = update_config_from_scrape(config, _make_scraped())
        assert changed is False
