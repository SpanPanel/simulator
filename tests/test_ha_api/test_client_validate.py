"""Tests for HA API connection validation."""

from __future__ import annotations

from unittest.mock import AsyncMock

import aiohttp
import pytest

from span_panel_simulator.ha_api.client import HAClient, HAConnectionConfig


def _make_client() -> HAClient:
    return HAClient(
        HAConnectionConfig(
            base_url="http://ha.invalid:8123/api",
            token="synthetic-token",
            is_supervisor=False,
        )
    )


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError(),
        aiohttp.ClientConnectionError("refused"),
        PermissionError("401"),
        OSError("no route to host"),
    ],
    ids=["timeout", "connection-refused", "unauthorized", "os-error"],
)
async def test_validate_returns_false_when_ha_unreachable(error: Exception) -> None:
    """An unreachable or unauthorized HA degrades to False, never raises.

    A connect/total timeout surfaces as the builtin ``TimeoutError`` (an
    ``OSError``), not an ``aiohttp.ClientError`` — letting it escape kills
    simulator startup instead of continuing without HA.
    """
    client = _make_client()
    client._get = AsyncMock(side_effect=error)  # type: ignore[method-assign]

    assert await client.async_validate() is False


async def test_validate_returns_true_on_api_running() -> None:
    """The documented success response validates the connection."""
    client = _make_client()
    client._get = AsyncMock(return_value={"message": "API running."})  # type: ignore[method-assign]

    assert await client.async_validate() is True
