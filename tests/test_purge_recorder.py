"""Tests for POST /purge-recorder dashboard route."""

from __future__ import annotations

from typing import Any

import pytest

from span_panel_simulator.dashboard import DashboardContext, create_dashboard_app

pytestmark = pytest.mark.asyncio


def _minimal_yaml() -> str:
    return "panel_config:\n  serial_number: sim-purge-test\n"


@pytest.fixture
def cfg_dir(tmp_path):
    d = tmp_path / "cfg"
    d.mkdir()
    (d / "stopped.yaml").write_text(_minimal_yaml(), encoding="utf-8")
    return d


async def test_purge_recorder_rejects_empty_filename(cfg_dir) -> None:
    ctx = DashboardContext(
        config_dir=cfg_dir,
        config_filter=None,
        get_panel_configs=lambda: {},
        get_panel_ports=lambda: {},
        request_reload=lambda: None,
    )
    app = create_dashboard_app(ctx)
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/purge-recorder", data={})
        assert resp.status == 200
        body = await resp.text()
        assert "No filename" in body


async def test_purge_recorder_rejects_path_traversal(cfg_dir) -> None:
    ctx = DashboardContext(
        config_dir=cfg_dir,
        config_filter=None,
        get_panel_configs=lambda: {},
        get_panel_ports=lambda: {},
        request_reload=lambda: None,
    )
    app = create_dashboard_app(ctx)
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/purge-recorder", data={"filename": "../stopped.yaml"})
        assert resp.status == 200
        body = await resp.text()
        assert "not found" in body.lower()


async def test_purge_recorder_rejects_missing_file(cfg_dir) -> None:
    ctx = DashboardContext(
        config_dir=cfg_dir,
        config_filter=None,
        get_panel_configs=lambda: {},
        get_panel_ports=lambda: {},
        request_reload=lambda: None,
    )
    app = create_dashboard_app(ctx)
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/purge-recorder", data={"filename": "nope.yaml"})
        assert resp.status == 200
        body = await resp.text()
        assert "not found" in body.lower()


async def test_purge_recorder_rejects_running_panel(cfg_dir) -> None:
    yaml_path = cfg_dir / "stopped.yaml"
    ctx = DashboardContext(
        config_dir=cfg_dir,
        config_filter=None,
        get_panel_configs=lambda: {yaml_path: "sim-purge-test"},
        get_panel_ports=lambda: {},
        request_reload=lambda: None,
    )
    app = create_dashboard_app(ctx)
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/purge-recorder", data={"filename": "stopped.yaml"})
        assert resp.status == 200
        body = await resp.text()
        assert "running" in body.lower()


async def test_purge_recorder_success_invokes_backend(
    cfg_dir, monkeypatch: pytest.MonkeyPatch
) -> None:
    called: list[Any] = []

    async def fake_purge(_ctx: Any, config_path: Any) -> None:
        called.append(config_path)

    monkeypatch.setattr(
        "span_panel_simulator.dashboard.routes._purge_recorder_for_config",
        fake_purge,
    )

    ctx = DashboardContext(
        config_dir=cfg_dir,
        config_filter=None,
        get_panel_configs=lambda: {},
        get_panel_ports=lambda: {},
        request_reload=lambda: None,
    )
    app = create_dashboard_app(ctx)
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/purge-recorder", data={"filename": "stopped.yaml"})
        assert resp.status == 200
        body = await resp.text()
        assert "success" in body.lower() or "purged" in body.lower()

    assert len(called) == 1
    assert called[0] == cfg_dir / "stopped.yaml"
