"""Tests for the profile builder with manifest entries."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from span_panel_simulator.ha_api.manifest import CircuitManifestEntry
from span_panel_simulator.ha_api.profile_builder import build_profiles


def _make_entry(entity_id: str, template: str) -> CircuitManifestEntry:
    return CircuitManifestEntry(
        entity_id=entity_id,
        template=template,
        device_type="consumer",
        tabs=[1],
    )


def _hourly_stats(
    entity_id: str, mean: float, count: int = 48
) -> dict[str, list[dict[str, object]]]:
    """Generate fake hourly stats for a single entity spanning *count* hours."""
    now = datetime.now(UTC)
    records: list[dict[str, object]] = []
    for i in range(count):
        ts = now - timedelta(hours=count - i)
        records.append(
            {
                "start": ts.timestamp() * 1000,
                "mean": mean + (i % 5),
                "min": mean * 0.5,
                "max": mean * 2.0,
            }
        )
    return {entity_id: records}


def _monthly_stats(entity_id: str, mean: float) -> dict[str, list[dict[str, object]]]:
    """Generate fake monthly stats for the last 6 months."""
    now = datetime.now(UTC)
    records: list[dict[str, object]] = []
    for m in range(6):
        ts = now - timedelta(days=30 * (6 - m))
        records.append(
            {
                "start": ts.timestamp() * 1000,
                "mean": mean + m * 10,
                "min": mean * 0.5,
                "max": mean * 2.0,
            }
        )
    return {entity_id: records}


class TestBuildProfiles:
    """Tests for build_profiles with manifest entries."""

    @pytest.mark.asyncio
    async def test_builds_profile_keyed_by_template(self) -> None:
        entity_id = "sensor.span_panel_kitchen_power"
        template = "clone_1"
        entry = _make_entry(entity_id, template)
        entity_map = {entity_id: template}

        client = AsyncMock()
        client.async_get_statistics = AsyncMock(
            side_effect=[
                _hourly_stats(entity_id, 500.0),
                _monthly_stats(entity_id, 500.0),
            ]
        )

        profiles = await build_profiles(client, [entry], entity_map)

        assert template in profiles
        p = profiles[template]
        assert "typical_power" in p
        assert "hour_factors" in p
        assert "duty_cycle" in p
        assert "monthly_factors" in p
        assert "power_variation" in p

    @pytest.mark.asyncio
    async def test_empty_entries_returns_empty(self) -> None:
        client = AsyncMock()
        profiles = await build_profiles(client, [], {})
        assert profiles == {}
        client.async_get_statistics.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_stats_returns_empty(self) -> None:
        entity_id = "sensor.span_panel_noop_power"
        entry = _make_entry(entity_id, "clone_99")
        entity_map = {entity_id: "clone_99"}

        client = AsyncMock()
        client.async_get_statistics = AsyncMock(return_value={})

        profiles = await build_profiles(client, [entry], entity_map)
        assert profiles == {}

    @pytest.mark.asyncio
    async def test_multiple_entries(self) -> None:
        e1 = "sensor.span_panel_kitchen_power"
        e2 = "sensor.span_panel_bedroom_power"
        entries = [_make_entry(e1, "clone_1"), _make_entry(e2, "clone_2")]
        entity_map = {e1: "clone_1", e2: "clone_2"}

        hourly = {**_hourly_stats(e1, 300.0), **_hourly_stats(e2, 150.0)}
        monthly = {**_monthly_stats(e1, 300.0), **_monthly_stats(e2, 150.0)}

        client = AsyncMock()
        client.async_get_statistics = AsyncMock(side_effect=[hourly, monthly])

        profiles = await build_profiles(client, entries, entity_map)
        assert "clone_1" in profiles
        assert "clone_2" in profiles

    @pytest.mark.asyncio
    async def test_unmapped_entity_skipped(self) -> None:
        """Entry whose entity_id is not in entity_to_template is skipped."""
        entity_id = "sensor.span_panel_orphan_power"
        entry = _make_entry(entity_id, "clone_orphan")
        entity_map: dict[str, str] = {}  # deliberately empty

        client = AsyncMock()
        client.async_get_statistics = AsyncMock(
            side_effect=[
                _hourly_stats(entity_id, 100.0),
                _monthly_stats(entity_id, 100.0),
            ]
        )

        profiles = await build_profiles(client, [entry], entity_map)
        assert profiles == {}
