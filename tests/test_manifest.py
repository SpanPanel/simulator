"""Tests for the circuit manifest module."""

from __future__ import annotations

from span_panel_simulator.ha_api.manifest import (
    CircuitManifestEntry,
    PanelManifest,
    _parse_panel,
)


class TestCircuitManifestEntry:
    """Basic dataclass behaviour."""

    def test_frozen(self) -> None:
        entry = CircuitManifestEntry(
            entity_id="sensor.span_panel_kitchen_power",
            template="clone_1",
            device_type="consumer",
            tabs=[1],
        )
        assert entry.entity_id == "sensor.span_panel_kitchen_power"
        assert entry.template == "clone_1"


class TestPanelManifest:
    """Filtering and mapping helpers."""

    @staticmethod
    def _sample_manifest() -> PanelManifest:
        return PanelManifest(
            serial="PAN-001",
            host="192.168.1.100",
            circuits=[
                CircuitManifestEntry("sensor.kitchen_power", "clone_1", "consumer", [1]),
                CircuitManifestEntry("sensor.bedroom_power", "clone_2", "consumer", [2]),
                CircuitManifestEntry("sensor.solar_power", "clone_3", "pv", [3, 4]),
                CircuitManifestEntry("sensor.battery_power", "clone_4", "battery", [5, 6]),
                CircuitManifestEntry("sensor.evse_power", "clone_5", "evse", [7]),
            ],
        )

    def test_profile_circuits_excludes_non_profile_types(self) -> None:
        m = self._sample_manifest()
        eligible = m.profile_circuits()
        assert len(eligible) == 2
        assert all(e.device_type == "consumer" for e in eligible)

    def test_profile_entity_ids(self) -> None:
        m = self._sample_manifest()
        ids = m.profile_entity_ids()
        assert ids == ["sensor.kitchen_power", "sensor.bedroom_power"]

    def test_entity_to_template_maps_all_circuits(self) -> None:
        m = self._sample_manifest()
        mapping = m.entity_to_template()
        assert len(mapping) == 5
        assert mapping["sensor.kitchen_power"] == "clone_1"
        assert mapping["sensor.solar_power"] == "clone_3"

    def test_empty_circuits(self) -> None:
        m = PanelManifest(serial="PAN-EMPTY", host="", circuits=[])
        assert m.profile_circuits() == []
        assert m.profile_entity_ids() == []
        assert m.entity_to_template() == {}


class TestParsePanel:
    """Tests for _parse_panel raw dict parsing."""

    def test_valid_panel(self) -> None:
        raw = {
            "serial": "PAN-123",
            "host": "192.168.1.50",
            "circuits": [
                {
                    "entity_id": "sensor.span_panel_kitchen_power",
                    "template": "clone_1",
                    "device_type": "consumer",
                    "tabs": [1],
                },
                {
                    "entity_id": "sensor.span_panel_solar_power",
                    "template": "clone_2",
                    "device_type": "pv",
                    "tabs": [3, 4],
                },
            ],
        }
        result = _parse_panel(raw)
        assert result is not None
        assert result.serial == "PAN-123"
        assert result.host == "192.168.1.50"
        assert len(result.circuits) == 2
        assert result.circuits[0].entity_id == "sensor.span_panel_kitchen_power"
        assert result.circuits[1].device_type == "pv"
        assert result.circuits[1].tabs == [3, 4]

    def test_missing_serial_returns_none(self) -> None:
        assert _parse_panel({"circuits": []}) is None

    def test_missing_circuits_gives_empty_list(self) -> None:
        result = _parse_panel({"serial": "PAN-X"})
        assert result is not None
        assert result.circuits == []
        assert result.host == ""

    def test_host_parsed_from_raw(self) -> None:
        result = _parse_panel({"serial": "PAN-X", "host": "10.0.0.1", "circuits": []})
        assert result is not None
        assert result.host == "10.0.0.1"

    def test_host_defaults_to_empty_when_absent(self) -> None:
        result = _parse_panel({"serial": "PAN-X", "circuits": []})
        assert result is not None
        assert result.host == ""

    def test_non_string_host_defaults_to_empty(self) -> None:
        result = _parse_panel({"serial": "PAN-X", "host": 12345, "circuits": []})
        assert result is not None
        assert result.host == ""

    def test_skips_entries_without_entity_id(self) -> None:
        raw = {
            "serial": "PAN-X",
            "circuits": [
                {"template": "clone_1", "device_type": "consumer", "tabs": [1]},
                {
                    "entity_id": "sensor.ok",
                    "template": "clone_2",
                    "device_type": "consumer",
                    "tabs": [2],
                },
            ],
        }
        result = _parse_panel(raw)
        assert result is not None
        assert len(result.circuits) == 1

    def test_defaults_device_type_to_consumer(self) -> None:
        raw = {
            "serial": "PAN-X",
            "circuits": [
                {"entity_id": "sensor.x", "template": "clone_1", "tabs": [1]},
            ],
        }
        result = _parse_panel(raw)
        assert result is not None
        assert result.circuits[0].device_type == "consumer"

    def test_non_dict_circuit_entries_skipped(self) -> None:
        raw = {
            "serial": "PAN-X",
            "circuits": ["not-a-dict", 42, None],
        }
        result = _parse_panel(raw)
        assert result is not None
        assert result.circuits == []
