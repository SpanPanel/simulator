"""Tests for opower discovery and cost fetch via HA API."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from span_panel_simulator.ha_api.opower import (
    async_discover_opower,
    async_get_opower_cost,
)


def _make_client(
    config_entries: list[dict],
    devices: list[dict],
    entities: list[dict],
    statistics: dict | None = None,
) -> AsyncMock:
    """Create a mock HAClient with canned responses."""
    client = AsyncMock()
    client._ws_command_list = AsyncMock(
        side_effect=_ws_list_router(config_entries, devices, entities)
    )
    if statistics is not None:
        client.async_get_statistics = AsyncMock(return_value=statistics)
    return client


def _ws_list_router(
    config_entries: list[dict],
    devices: list[dict],
    entities: list[dict],
):
    """Return a side_effect function that routes WS list commands."""

    async def route(payload: dict) -> list[dict]:
        cmd_type = payload.get("type", "")
        if cmd_type == "config_entries/get":
            return config_entries
        if cmd_type == "config/device_registry/list":
            return devices
        if cmd_type == "config/entity_registry/list":
            return entities
        return []

    return route


OPOWER_CONFIG_ENTRY = {
    "entry_id": "opower_entry_1",
    "domain": "opower",
    "title": "Pacific Gas and Electric Company (PG&E)",
}

ELEC_DEVICE = {
    "id": "device_elec_1",
    "name": "ELEC account 3021618479",
    "config_entries": ["opower_entry_1"],
    "identifiers": [["opower", "pge_elec_3021618479"]],
}

GAS_DEVICE = {
    "id": "device_gas_1",
    "name": "GAS account 3021618302",
    "config_entries": ["opower_entry_1"],
    "identifiers": [["opower", "pge_gas_3021618302"]],
}

ELEC_ENTITIES = [
    {
        "entity_id": "sensor.opower_pge_elec_cost_to_date",
        "device_id": "device_elec_1",
        "original_device_class": "monetary",
        "entity_category": None,
    },
    {
        "entity_id": "sensor.opower_pge_elec_usage_to_date",
        "device_id": "device_elec_1",
        "original_device_class": "energy",
        "entity_category": None,
    },
    {
        "entity_id": "sensor.opower_pge_elec_forecasted_cost",
        "device_id": "device_elec_1",
        "original_device_class": "monetary",
        "entity_category": None,
    },
    {
        "entity_id": "sensor.opower_pge_elec_forecasted_usage",
        "device_id": "device_elec_1",
        "original_device_class": "energy",
        "entity_category": None,
    },
]

GAS_ENTITIES = [
    {
        "entity_id": "sensor.opower_pge_gas_cost_to_date",
        "device_id": "device_gas_1",
        "original_device_class": "monetary",
        "entity_category": None,
    },
    {
        "entity_id": "sensor.opower_pge_gas_usage_to_date",
        "device_id": "device_gas_1",
        "original_device_class": "energy",
        "entity_category": None,
    },
]


class TestAsyncDiscoverOpower:
    """Discover opower ELEC accounts from HA registries."""

    @pytest.mark.asyncio
    async def test_finds_elec_account(self) -> None:
        client = _make_client(
            config_entries=[OPOWER_CONFIG_ENTRY],
            devices=[ELEC_DEVICE, GAS_DEVICE],
            entities=[*ELEC_ENTITIES, *GAS_ENTITIES],
        )
        accounts = await async_discover_opower(client)
        assert len(accounts) == 1
        assert accounts[0].device_id == "device_elec_1"
        assert accounts[0].utility_name == "Pacific Gas and Electric Company (PG&E)"
        assert accounts[0].account_number == "3021618479"
        assert accounts[0].cost_entity_id == "sensor.opower_pge_elec_cost_to_date"
        assert accounts[0].usage_entity_id == "sensor.opower_pge_elec_usage_to_date"

    @pytest.mark.asyncio
    async def test_ignores_gas_accounts(self) -> None:
        client = _make_client(
            config_entries=[OPOWER_CONFIG_ENTRY],
            devices=[GAS_DEVICE],
            entities=[*GAS_ENTITIES],
        )
        accounts = await async_discover_opower(client)
        assert accounts == []

    @pytest.mark.asyncio
    async def test_no_opower_installed(self) -> None:
        client = _make_client(
            config_entries=[{"entry_id": "other", "domain": "met_eireann", "title": "Weather"}],
            devices=[],
            entities=[],
        )
        accounts = await async_discover_opower(client)
        assert accounts == []

    @pytest.mark.asyncio
    async def test_opower_installed_no_devices(self) -> None:
        client = _make_client(
            config_entries=[OPOWER_CONFIG_ENTRY],
            devices=[],
            entities=[],
        )
        accounts = await async_discover_opower(client)
        assert accounts == []

    @pytest.mark.asyncio
    async def test_multiple_elec_accounts(self) -> None:
        elec2 = {
            "id": "device_elec_2",
            "name": "ELEC account 9999999999",
            "config_entries": ["opower_entry_1"],
            "identifiers": [["opower", "pge_elec_9999999999"]],
        }
        elec2_entities = [
            {
                "entity_id": "sensor.opower_pge_elec_cost_to_date_2",
                "device_id": "device_elec_2",
                "original_device_class": "monetary",
                "entity_category": None,
            },
            {
                "entity_id": "sensor.opower_pge_elec_usage_to_date_2",
                "device_id": "device_elec_2",
                "original_device_class": "energy",
                "entity_category": None,
            },
        ]
        client = _make_client(
            config_entries=[OPOWER_CONFIG_ENTRY],
            devices=[ELEC_DEVICE, elec2],
            entities=[*ELEC_ENTITIES, *elec2_entities],
        )
        accounts = await async_discover_opower(client)
        assert len(accounts) == 2


class TestAsyncGetOpowerCost:
    """Fetch and sum daily cost statistics."""

    @pytest.mark.asyncio
    async def test_sums_daily_cost(self) -> None:
        stats = {
            "sensor.opower_pge_elec_cost_to_date": [
                {"start": "2026-01-01T00:00:00Z", "change": 3.50},
                {"start": "2026-01-02T00:00:00Z", "change": 4.20},
                {"start": "2026-01-03T00:00:00Z", "change": 2.80},
            ]
        }
        client = _make_client([], [], [], statistics=stats)
        result = await async_get_opower_cost(
            client,
            "sensor.opower_pge_elec_cost_to_date",
            "2026-01-01T00:00:00Z",
            "2026-01-04T00:00:00Z",
        )
        assert result is not None
        assert result.total_cost == pytest.approx(10.50)
        assert result.days_with_data == 3

    @pytest.mark.asyncio
    async def test_no_data_returns_none(self) -> None:
        stats: dict = {"sensor.opower_pge_elec_cost_to_date": []}
        client = _make_client([], [], [], statistics=stats)
        result = await async_get_opower_cost(
            client,
            "sensor.opower_pge_elec_cost_to_date",
            "2026-01-01T00:00:00Z",
            "2026-01-04T00:00:00Z",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_entity_not_in_response(self) -> None:
        stats: dict = {}
        client = _make_client([], [], [], statistics=stats)
        result = await async_get_opower_cost(
            client,
            "sensor.opower_pge_elec_cost_to_date",
            "2026-01-01T00:00:00Z",
            "2026-01-04T00:00:00Z",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_skips_entries_without_change(self) -> None:
        stats = {
            "sensor.opower_pge_elec_cost_to_date": [
                {"start": "2026-01-01T00:00:00Z", "change": 3.50},
                {"start": "2026-01-02T00:00:00Z"},
                {"start": "2026-01-03T00:00:00Z", "change": None},
                {"start": "2026-01-04T00:00:00Z", "change": 2.00},
            ]
        }
        client = _make_client([], [], [], statistics=stats)
        result = await async_get_opower_cost(
            client,
            "sensor.opower_pge_elec_cost_to_date",
            "2026-01-01T00:00:00Z",
            "2026-01-05T00:00:00Z",
        )
        assert result is not None
        assert result.total_cost == pytest.approx(5.50)
        assert result.days_with_data == 2
