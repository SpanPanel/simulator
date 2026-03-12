"""Tests for HomiePublisher snapshot-to-MQTT mapping."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from span_panel_simulator.publisher import (
    HomiePublisher,
    _stable_circuit_uuid,
)

if TYPE_CHECKING:
    from span_panel_simulator.models import SpanPanelSnapshot


class TestStableUUID:
    """UUID generation must be deterministic and dashless."""

    def test_deterministic(self) -> None:
        a = _stable_circuit_uuid("living_room_lights")
        b = _stable_circuit_uuid("living_room_lights")
        assert a == b

    def test_different_inputs(self) -> None:
        a = _stable_circuit_uuid("living_room_lights")
        b = _stable_circuit_uuid("kitchen_outlets")
        assert a != b

    def test_dashless(self) -> None:
        result = _stable_circuit_uuid("test_circuit")
        assert "-" not in result
        assert len(result) == 32


class TestPublishInit:
    """publish_init should emit $state, $description, all properties, then $state=ready."""

    @pytest.mark.asyncio
    async def test_publishes_state_transitions(
        self,
        publisher: HomiePublisher,
        publish_mock: AsyncMock,
        sample_snapshot: SpanPanelSnapshot,
    ) -> None:
        await publisher.publish_init(sample_snapshot)

        calls = publish_mock.call_args_list
        topics = [c.args[0] for c in calls]

        # First call: $state = init
        assert calls[0].args == ("ebus/5/SPAN-TEST-001/$state", "init", True)

        # Second call: $description
        assert calls[1].args[0] == "ebus/5/SPAN-TEST-001/$description"

        # Last call: $state = ready
        assert calls[-1].args == ("ebus/5/SPAN-TEST-001/$state", "ready", True)

    @pytest.mark.asyncio
    async def test_description_contains_circuit_nodes(
        self,
        publisher: HomiePublisher,
        publish_mock: AsyncMock,
        sample_snapshot: SpanPanelSnapshot,
    ) -> None:
        await publisher.publish_init(sample_snapshot)

        import json

        desc_call = publish_mock.call_args_list[1]
        desc = json.loads(desc_call.args[1])

        # Should have core, upstream-lugs, downstream-lugs, power-flows,
        # plus 2 circuit nodes (unmapped_tab_2 excluded)
        assert "core" in desc["nodes"]
        assert "upstream-lugs" in desc["nodes"]
        assert "downstream-lugs" in desc["nodes"]
        assert "power-flows" in desc["nodes"]

        circuit_nodes = [
            nid
            for nid, ndef in desc["nodes"].items()
            if ndef["type"] == "energy.ebus.device.circuit"
        ]
        assert len(circuit_nodes) == 2

    @pytest.mark.asyncio
    async def test_no_bess_when_no_battery(
        self,
        publisher: HomiePublisher,
        publish_mock: AsyncMock,
        sample_snapshot: SpanPanelSnapshot,
    ) -> None:
        await publisher.publish_init(sample_snapshot)

        import json

        desc = json.loads(publish_mock.call_args_list[1].args[1])
        assert "bess-0" not in desc["nodes"]


class TestPropertyMapping:
    """Verify correct field-to-topic mapping and unit conversions."""

    @pytest.mark.asyncio
    async def test_core_properties(
        self,
        publisher: HomiePublisher,
        publish_mock: AsyncMock,
        sample_snapshot: SpanPanelSnapshot,
    ) -> None:
        await publisher.publish_init(sample_snapshot)

        published = {c.args[0]: c.args[1] for c in publish_mock.call_args_list}
        prefix = "ebus/5/SPAN-TEST-001/core"

        assert published[f"{prefix}/vendor-name"] == "SPAN"
        assert published[f"{prefix}/serial-number"] == "SPAN-TEST-001"
        assert published[f"{prefix}/relay"] == "CLOSED"
        assert published[f"{prefix}/l1-voltage"] == "121.3"
        assert published[f"{prefix}/l2-voltage"] == "119.8"
        assert published[f"{prefix}/breaker-rating"] == "200"
        assert published[f"{prefix}/ethernet"] == "true"
        assert published[f"{prefix}/wifi"] == "false"
        assert published[f"{prefix}/grid-islandable"] == "false"

    @pytest.mark.asyncio
    async def test_circuit_power_conversion(
        self,
        publisher: HomiePublisher,
        publish_mock: AsyncMock,
        sample_snapshot: SpanPanelSnapshot,
    ) -> None:
        """Circuit power must be negated and converted to kW."""
        await publisher.publish_init(sample_snapshot)

        published = {c.args[0]: c.args[1] for c in publish_mock.call_args_list}

        # Find the living room lights UUID
        uuid = _stable_circuit_uuid("living_room_lights")
        topic = f"ebus/5/SPAN-TEST-001/{uuid}/active-power"

        # 150W consumption → -0.1500 kW on wire (consumer negates on read)
        assert topic in published
        assert float(published[topic]) == pytest.approx(-0.15, abs=0.001)

    @pytest.mark.asyncio
    async def test_circuit_energy_swap(
        self,
        publisher: HomiePublisher,
        publish_mock: AsyncMock,
        sample_snapshot: SpanPanelSnapshot,
    ) -> None:
        """exported-energy on wire = consumed_energy_wh in snapshot."""
        await publisher.publish_init(sample_snapshot)

        published = {c.args[0]: c.args[1] for c in publish_mock.call_args_list}

        uuid = _stable_circuit_uuid("kitchen_outlets")
        assert float(published[f"ebus/5/SPAN-TEST-001/{uuid}/exported-energy"]) == 25000.0
        assert float(published[f"ebus/5/SPAN-TEST-001/{uuid}/imported-energy"]) == 0.0

    @pytest.mark.asyncio
    async def test_priority_mapping_v1_to_v2(
        self,
        publisher: HomiePublisher,
        publish_mock: AsyncMock,
        sample_snapshot: SpanPanelSnapshot,
    ) -> None:
        """MUST_HAVE → NEVER, NICE_TO_HAVE → SOC_THRESHOLD."""
        await publisher.publish_init(sample_snapshot)

        published = {c.args[0]: c.args[1] for c in publish_mock.call_args_list}

        lr_uuid = _stable_circuit_uuid("living_room_lights")
        k_uuid = _stable_circuit_uuid("kitchen_outlets")

        assert published[f"ebus/5/SPAN-TEST-001/{lr_uuid}/shed-priority"] == "NEVER"
        assert published[f"ebus/5/SPAN-TEST-001/{k_uuid}/shed-priority"] == "SOC_THRESHOLD"

    @pytest.mark.asyncio
    async def test_upstream_lugs_power_negation(
        self,
        publisher: HomiePublisher,
        publish_mock: AsyncMock,
        sample_snapshot: SpanPanelSnapshot,
    ) -> None:
        """Upstream lugs active-power negates grid power (consumer negates on read)."""
        await publisher.publish_init(sample_snapshot)

        published = {c.args[0]: c.args[1] for c in publish_mock.call_args_list}
        topic = "ebus/5/SPAN-TEST-001/upstream-lugs/active-power"

        # instant_grid_power_w = 950 → published as -950 (consumer will negate back)
        assert float(published[topic]) == pytest.approx(-950.0, abs=0.01)

    @pytest.mark.asyncio
    async def test_circuit_space_and_dipole(
        self,
        publisher: HomiePublisher,
        publish_mock: AsyncMock,
        sample_snapshot: SpanPanelSnapshot,
    ) -> None:
        await publisher.publish_init(sample_snapshot)

        published = {c.args[0]: c.args[1] for c in publish_mock.call_args_list}

        lr_uuid = _stable_circuit_uuid("living_room_lights")
        k_uuid = _stable_circuit_uuid("kitchen_outlets")

        assert published[f"ebus/5/SPAN-TEST-001/{lr_uuid}/space"] == "1"
        assert published[f"ebus/5/SPAN-TEST-001/{lr_uuid}/dipole"] == "false"
        assert published[f"ebus/5/SPAN-TEST-001/{k_uuid}/space"] == "3"
        assert published[f"ebus/5/SPAN-TEST-001/{k_uuid}/dipole"] == "true"


class TestPublishDiff:
    """publish_diff should only publish changed properties."""

    @pytest.mark.asyncio
    async def test_no_changes_publishes_nothing(
        self,
        publisher: HomiePublisher,
        publish_mock: AsyncMock,
        sample_snapshot: SpanPanelSnapshot,
    ) -> None:
        await publisher.publish_init(sample_snapshot)
        publish_mock.reset_mock()

        count = await publisher.publish_diff(sample_snapshot)
        assert count == 0
        publish_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_changed_power_publishes_diff(
        self,
        publisher: HomiePublisher,
        publish_mock: AsyncMock,
        sample_snapshot: SpanPanelSnapshot,
    ) -> None:
        await publisher.publish_init(sample_snapshot)
        publish_mock.reset_mock()

        # Change grid power
        modified = replace(sample_snapshot, instant_grid_power_w=1200.0)
        count = await publisher.publish_diff(modified)

        assert count > 0
        topics = [c.args[0] for c in publish_mock.call_args_list]
        assert "ebus/5/SPAN-TEST-001/upstream-lugs/active-power" in topics


class TestSetTopicResolution:
    """resolve_set_message should parse /set topics correctly."""

    @pytest.mark.asyncio
    async def test_core_set(
        self,
        publisher: HomiePublisher,
        publish_mock: AsyncMock,
        sample_snapshot: SpanPanelSnapshot,
    ) -> None:
        await publisher.publish_init(sample_snapshot)

        result = publisher.resolve_set_message(
            "ebus/5/SPAN-TEST-001/core/dominant-power-source/set"
        )
        assert result == ("core", "", "dominant-power-source")

    @pytest.mark.asyncio
    async def test_circuit_relay_set(
        self,
        publisher: HomiePublisher,
        publish_mock: AsyncMock,
        sample_snapshot: SpanPanelSnapshot,
    ) -> None:
        await publisher.publish_init(sample_snapshot)

        uuid = _stable_circuit_uuid("living_room_lights")
        result = publisher.resolve_set_message(
            f"ebus/5/SPAN-TEST-001/{uuid}/relay/set"
        )
        assert result is not None
        assert result[0] == "circuit"
        assert result[1] == "living_room_lights"
        assert result[2] == "relay"

    def test_unknown_topic_returns_none(self, publisher: HomiePublisher) -> None:
        result = publisher.resolve_set_message("some/random/topic")
        assert result is None
