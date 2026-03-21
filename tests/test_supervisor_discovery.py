"""Tests for Supervisor Discovery API client."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import ClientSession

from span_panel_simulator.supervisor_discovery import SupervisorDiscovery


@pytest.fixture
def discovery() -> SupervisorDiscovery:
    """Discovery client with a fake token (simulates add-on mode)."""
    d = SupervisorDiscovery()
    d._token = "test-token"
    return d


@pytest.fixture
def discovery_no_token() -> SupervisorDiscovery:
    """Discovery client without token (standalone mode)."""
    d = SupervisorDiscovery()
    d._token = None
    return d


def _mock_session(response_status: int, response_json: dict | None = None) -> MagicMock:
    """Create a mock aiohttp.ClientSession with context manager support."""
    mock_resp = AsyncMock()
    mock_resp.status = response_status
    if response_json is not None:
        mock_resp.json = AsyncMock(return_value=response_json)
    mock_resp.text = AsyncMock(return_value="error")

    mock_session = AsyncMock(spec=ClientSession)
    mock_session.post.return_value.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_session.post.return_value.__aexit__ = AsyncMock(return_value=False)
    mock_session.delete.return_value.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_session.delete.return_value.__aexit__ = AsyncMock(return_value=False)

    return mock_session


async def test_register_panel_posts_to_supervisor(discovery: SupervisorDiscovery):
    """register_panel POSTs to /discovery and tracks the UUID."""
    mock_session = _mock_session(200, {"uuid": "disc-uuid-123"})
    with (
        patch("aiohttp.ClientSession", return_value=mock_session),
        patch(
            "span_panel_simulator.supervisor_discovery._container_hostname",
            return_value="f8c38f2b-span-panel-simulator",
        ),
    ):
        await discovery.register_panel("sim-001", 8081)

    assert discovery._entries.get("sim-001") == "disc-uuid-123"
    mock_session.post.assert_called_once()


async def test_unregister_panel_deletes_from_supervisor(discovery: SupervisorDiscovery):
    """unregister_panel DELETEs /discovery/{uuid}."""
    discovery._entries["sim-001"] = "disc-uuid-123"

    mock_session = _mock_session(200)
    with patch("aiohttp.ClientSession", return_value=mock_session):
        await discovery.unregister_panel("sim-001")

    assert "sim-001" not in discovery._entries
    mock_session.delete.assert_called_once()


async def test_no_op_without_token(discovery_no_token: SupervisorDiscovery):
    """All operations are no-ops when SUPERVISOR_TOKEN is not set."""
    await discovery_no_token.register_panel("sim-001", 8081)
    assert len(discovery_no_token._entries) == 0
    await discovery_no_token.unregister_panel("sim-001")  # Should not raise


async def test_register_failure_logged_not_raised(discovery: SupervisorDiscovery):
    """Supervisor API failures are swallowed — panel startup must not be blocked."""
    mock_session = _mock_session(401)
    with (
        patch("aiohttp.ClientSession", return_value=mock_session),
        patch(
            "span_panel_simulator.supervisor_discovery._container_hostname",
            return_value="f8c38f2b-span-panel-simulator",
        ),
    ):
        await discovery.register_panel("sim-001", 8081)
    assert "sim-001" not in discovery._entries


async def test_cleanup_stale_on_startup(discovery: SupervisorDiscovery):
    """cleanup_stale removes entries matching our service from prior runs."""
    existing_entries = [
        {"uuid": "old-uuid-1", "service": "span_panel"},
        {"uuid": "old-uuid-2", "service": "other_service"},
    ]
    get_resp = AsyncMock()
    get_resp.status = 200
    get_resp.json = AsyncMock(return_value={"discovery": existing_entries})

    del_resp = AsyncMock()
    del_resp.status = 200

    mock_session = AsyncMock(spec=ClientSession)
    mock_session.get.return_value.__aenter__ = AsyncMock(return_value=get_resp)
    mock_session.get.return_value.__aexit__ = AsyncMock(return_value=False)
    mock_session.delete.return_value.__aenter__ = AsyncMock(return_value=del_resp)
    mock_session.delete.return_value.__aexit__ = AsyncMock(return_value=False)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        await discovery.cleanup_stale()

    # Should only delete span_panel entries, not other_service
    mock_session.delete.assert_called_once()
