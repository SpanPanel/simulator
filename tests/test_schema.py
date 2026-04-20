"""Tests for schema registry and schema-driven validation."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from span_panel_simulator.publisher import HomiePublisher
from span_panel_simulator.schema import SchemaProperty, load_schema, validate_value

if TYPE_CHECKING:
    from span_panel_simulator.models import SpanPanelSnapshot


def _bundled_schema_path() -> Path:
    """Locate the bundled homie_schema.json."""
    return (
        Path(__file__).parent.parent
        / "src"
        / "span_panel_simulator"
        / "data"
        / "homie_schema.json"
    )


class TestSchemaRegistry:
    """Schema parser produces correct typed structures."""

    def test_loads_bundled_schema(self) -> None:
        registry = load_schema(_bundled_schema_path())
        assert registry.firmware_version != ""
        assert registry.schema_hash != ""
        assert len(registry.node_types) > 0

    def test_all_expected_node_types(self) -> None:
        registry = load_schema(_bundled_schema_path())
        expected = {
            "energy.ebus.device.distribution-enclosure.core",
            "energy.ebus.device.lugs",
            "energy.ebus.device.circuit",
            "energy.ebus.device.bess",
            "energy.ebus.device.pv",
            "energy.ebus.device.evse",
            "energy.ebus.device.pcs",
            "energy.ebus.device.power-flows",
        }
        assert expected == set(registry.node_types)

    def test_circuit_properties(self) -> None:
        registry = load_schema(_bundled_schema_path())
        circuit = registry.get_node_type("energy.ebus.device.circuit")
        assert circuit is not None
        assert "relay" in circuit.properties
        assert circuit.properties["relay"].settable is True
        assert circuit.properties["relay"].datatype == "enum"
        assert "OPEN" in (circuit.properties["relay"].format or "")

    def test_settable_properties(self) -> None:
        registry = load_schema(_bundled_schema_path())
        circuit = registry.get_node_type("energy.ebus.device.circuit")
        assert circuit is not None
        settable = circuit.settable_properties
        assert "relay" in settable
        assert "shed-priority" in settable
        assert "name" not in settable

    def test_core_settable(self) -> None:
        registry = load_schema(_bundled_schema_path())
        core = registry.get_node_type("energy.ebus.device.distribution-enclosure.core")
        assert core is not None
        assert "dominant-power-source" in core.settable_properties

    def test_raw_json_preserved(self) -> None:
        registry = load_schema(_bundled_schema_path())
        parsed = json.loads(registry.raw_json)
        assert "types" in parsed
        assert "firmwareVersion" in parsed

    def test_get_property(self) -> None:
        registry = load_schema(_bundled_schema_path())
        prop = registry.get_property("energy.ebus.device.lugs", "active-power")
        assert prop is not None
        assert prop.datatype == "float"
        assert prop.unit == "W"

    def test_get_property_missing_type(self) -> None:
        registry = load_schema(_bundled_schema_path())
        assert registry.get_property("nonexistent.type", "foo") is None

    def test_get_property_missing_prop(self) -> None:
        registry = load_schema(_bundled_schema_path())
        assert registry.get_property("energy.ebus.device.lugs", "nonexistent") is None


class TestSchemaValidation:
    """Publisher validates published properties against schema at startup."""

    @pytest.mark.asyncio
    async def test_no_missing_properties_with_schema(
        self,
        sample_snapshot: SpanPanelSnapshot,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Publishing a full snapshot should produce no missing-property warnings."""
        registry = load_schema(_bundled_schema_path())
        publish_mock = AsyncMock()
        publisher = HomiePublisher(
            serial_number="SPAN-TEST-001",
            publish_fn=publish_mock,
            schema=registry,
        )

        with caplog.at_level(logging.WARNING, logger="span_panel_simulator.publisher"):
            await publisher.publish_init(sample_snapshot)

        missing_warnings = [r for r in caplog.records if "missing properties" in r.message]
        if missing_warnings:
            details = "\n".join(f"  {r.message}" for r in missing_warnings)
            pytest.fail(f"Schema validation found missing properties:\n{details}")

    @pytest.mark.asyncio
    async def test_schema_driven_set_topics(
        self,
        sample_snapshot: SpanPanelSnapshot,
    ) -> None:
        """Schema-driven /set topics should match what the schema declares settable."""
        registry = load_schema(_bundled_schema_path())
        publish_mock = AsyncMock()
        publisher = HomiePublisher(
            serial_number="SPAN-TEST-001",
            publish_fn=publish_mock,
            schema=registry,
        )
        await publisher.publish_init(sample_snapshot)

        set_topics = publisher.get_set_topics()

        # Should include core/dominant-power-source
        assert any("core/dominant-power-source/set" in t for t in set_topics)

        # Should include circuit relay and shed-priority
        assert any("relay/set" in t for t in set_topics)
        assert any("shed-priority/set" in t for t in set_topics)

    @pytest.mark.asyncio
    async def test_no_set_topics_without_description(self) -> None:
        """get_set_topics before publish_init should return empty (no description yet)."""
        registry = load_schema(_bundled_schema_path())
        publish_mock = AsyncMock()
        publisher = HomiePublisher(
            serial_number="SPAN-TEST-001",
            publish_fn=publish_mock,
            schema=registry,
        )
        # No publish_init called — _description_nodes is empty
        # Falls back to hardcoded
        topics = publisher.get_set_topics()
        # Hardcoded fallback has at least core settable
        assert any("dominant-power-source" in t for t in topics)

    @pytest.mark.asyncio
    async def test_no_type_validation_warnings(
        self,
        sample_snapshot: SpanPanelSnapshot,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """All published values should pass type validation against the schema."""
        registry = load_schema(_bundled_schema_path())
        publish_mock = AsyncMock()
        publisher = HomiePublisher(
            serial_number="SPAN-TEST-001",
            publish_fn=publish_mock,
            schema=registry,
        )

        with caplog.at_level(logging.DEBUG, logger="span_panel_simulator.publisher"):
            await publisher.publish_init(sample_snapshot)

        type_warnings = [r for r in caplog.records if "Type validation" in r.message]
        if type_warnings:
            details = "\n".join(f"  {r.message}" for r in type_warnings)
            pytest.fail(f"Type validation found errors:\n{details}")


class TestValidateValue:
    """Unit tests for the validate_value function."""

    def _prop(self, datatype: str, fmt: str | None = None) -> SchemaProperty:
        return SchemaProperty(key="test", name="Test", datatype=datatype, format=fmt)

    def test_boolean_valid(self) -> None:
        assert validate_value(self._prop("boolean"), "true") is None
        assert validate_value(self._prop("boolean"), "false") is None

    def test_boolean_invalid(self) -> None:
        assert validate_value(self._prop("boolean"), "True") is not None
        assert validate_value(self._prop("boolean"), "1") is not None
        assert validate_value(self._prop("boolean"), "yes") is not None

    def test_integer_valid(self) -> None:
        assert validate_value(self._prop("integer"), "0") is None
        assert validate_value(self._prop("integer"), "42") is None
        assert validate_value(self._prop("integer"), "-7") is None

    def test_integer_invalid(self) -> None:
        assert validate_value(self._prop("integer"), "3.14") is not None
        assert validate_value(self._prop("integer"), "abc") is not None

    def test_float_valid(self) -> None:
        assert validate_value(self._prop("float"), "0.00") is None
        assert validate_value(self._prop("float"), "-950.00") is None
        assert validate_value(self._prop("float"), "121.3") is None

    def test_float_invalid(self) -> None:
        assert validate_value(self._prop("float"), "abc") is not None
        assert validate_value(self._prop("float"), "") is not None

    def test_enum_valid(self) -> None:
        prop = self._prop("enum", "OPEN,CLOSED,UNKNOWN")
        assert validate_value(prop, "OPEN") is None
        assert validate_value(prop, "CLOSED") is None

    def test_enum_invalid(self) -> None:
        prop = self._prop("enum", "OPEN,CLOSED,UNKNOWN")
        assert validate_value(prop, "HALF_OPEN") is not None

    def test_enum_no_format_always_valid(self) -> None:
        prop = self._prop("enum")
        assert validate_value(prop, "anything") is None

    def test_string_always_valid(self) -> None:
        assert validate_value(self._prop("string"), "") is None
        assert validate_value(self._prop("string"), "hello world") is None


class TestRenderForPanel:
    """render_for_panel produces size-specific schema registries."""

    def test_patches_space_format_for_40_tabs(self) -> None:
        from span_panel_simulator.schema import render_for_panel

        template = load_schema(_bundled_schema_path())
        rendered = render_for_panel(template, 40)
        space = rendered.get_property("energy.ebus.device.circuit", "space")
        assert space is not None
        assert space.format == "1:40:1"

    def test_patches_space_format_for_48_tabs(self) -> None:
        from span_panel_simulator.schema import render_for_panel

        template = load_schema(_bundled_schema_path())
        rendered = render_for_panel(template, 48)
        space = rendered.get_property("energy.ebus.device.circuit", "space")
        assert space is not None
        assert space.format == "1:48:1"

    def test_raw_json_reflects_patched_format(self) -> None:
        from span_panel_simulator.schema import render_for_panel

        template = load_schema(_bundled_schema_path())
        rendered = render_for_panel(template, 40)
        parsed = json.loads(rendered.raw_json)
        assert parsed["types"]["energy.ebus.device.circuit"]["space"]["format"] == "1:40:1"

    def test_hash_recomputed_for_size(self) -> None:
        """Hash is derived from types content — different size → different hash."""
        from span_panel_simulator.schema import render_for_panel

        template = load_schema(_bundled_schema_path())
        r32 = render_for_panel(template, 32)
        r40 = render_for_panel(template, 40)
        assert r32.schema_hash != r40.schema_hash
        assert r32.schema_hash.startswith("sha256:")
        assert r40.schema_hash.startswith("sha256:")

    def test_hash_uses_content_derived_algorithm(self) -> None:
        """Hash is derived from sorted-keys JSON of the ``types`` dict.

        Algorithm mirrors span-panel-api/src/span_panel_api/auth.py:206-207
        (``"sha256:" + sha256(json.dumps(data["types"], sort_keys=True)).hexdigest()[:16]``)
        so simulator and consumer see matching hashes for identical content.
        """
        import hashlib

        from span_panel_simulator.schema import render_for_panel

        template = load_schema(_bundled_schema_path())
        rendered = render_for_panel(template, 40)
        types = json.loads(rendered.raw_json)["types"]
        expected = (
            "sha256:" + hashlib.sha256(json.dumps(types, sort_keys=True).encode()).hexdigest()[:16]
        )
        assert rendered.schema_hash == expected

    def test_stamped_hash_matches_derived(self) -> None:
        """typesSchemaHash in raw_json agrees with schema_hash field."""
        from span_panel_simulator.schema import render_for_panel

        template = load_schema(_bundled_schema_path())
        rendered = render_for_panel(template, 40)
        parsed = json.loads(rendered.raw_json)
        assert parsed["typesSchemaHash"] == rendered.schema_hash

    def test_deterministic(self) -> None:
        """Same inputs produce byte-identical raw_json."""
        from span_panel_simulator.schema import render_for_panel

        template = load_schema(_bundled_schema_path())
        a = render_for_panel(template, 40)
        b = render_for_panel(template, 40)
        assert a.raw_json == b.raw_json
        assert a.schema_hash == b.schema_hash

    def test_input_registry_not_mutated(self) -> None:
        """render_for_panel does not modify the template registry."""
        from span_panel_simulator.schema import render_for_panel

        template = load_schema(_bundled_schema_path())
        original_json = template.raw_json
        original_hash = template.schema_hash
        _ = render_for_panel(template, 40)
        assert template.raw_json == original_json
        assert template.schema_hash == original_hash
        space = template.get_property("energy.ebus.device.circuit", "space")
        assert space is not None
        assert space.format == "1:32:1"

    def test_rejects_zero(self) -> None:
        from span_panel_simulator.schema import render_for_panel

        template = load_schema(_bundled_schema_path())
        with pytest.raises(ValueError):
            render_for_panel(template, 0)

    def test_rejects_negative(self) -> None:
        from span_panel_simulator.schema import render_for_panel

        template = load_schema(_bundled_schema_path())
        with pytest.raises(ValueError):
            render_for_panel(template, -4)

    def test_rejects_odd(self) -> None:
        from span_panel_simulator.schema import render_for_panel

        template = load_schema(_bundled_schema_path())
        with pytest.raises(ValueError):
            render_for_panel(template, 33)

    def test_round_trip_through_build_registry(self) -> None:
        """Rendered raw_json parses back into a valid registry."""
        from span_panel_simulator.schema import render_for_panel

        template = load_schema(_bundled_schema_path())
        rendered = render_for_panel(template, 40)
        # node_types must reflect the patched format
        assert (
            rendered.node_types["energy.ebus.device.circuit"].properties["space"].format
            == "1:40:1"
        )
        # Other properties should survive the round-trip
        assert "relay" in rendered.node_types["energy.ebus.device.circuit"].properties
