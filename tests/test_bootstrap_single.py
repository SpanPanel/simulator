"""Tests for the single-panel BootstrapHttpServer."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import MagicMock

from aiohttp.test_utils import TestClient, TestServer

from span_panel_simulator.bootstrap import BootstrapHttpServer
from span_panel_simulator.const import DEFAULT_FIRMWARE_VERSION
from span_panel_simulator.schema import load_schema, render_for_panel


def _make_server() -> BootstrapHttpServer:
    """Create a BootstrapHttpServer with mocked certs and schema."""
    certs = MagicMock()
    certs.ca_cert_pem = b"-----BEGIN CERTIFICATE-----\nFAKE\n-----END CERTIFICATE-----\n"

    schema = MagicMock()
    schema.raw_json = '{"test": true}'

    return BootstrapHttpServer(
        serial="sim-test-001",
        firmware=DEFAULT_FIRMWARE_VERSION,
        certs=certs,
        schema=schema,
        broker_username="span",
        broker_password="sim-password",
        broker_host="localhost",
    )


async def test_status_returns_single_panel() -> None:
    """GET /api/v2/status returns the one panel."""
    server = _make_server()
    async with TestClient(TestServer(server._app)) as client:
        resp = await client.get("/api/v2/status")
        assert resp.status == 200
        data = await resp.json()
        assert data["serialNumber"] == "sim-test-001"
        assert data["firmwareVersion"] == DEFAULT_FIRMWARE_VERSION
        assert data["proximityProven"] is True


async def test_status_ignores_serial_query_param() -> None:
    """?serial= has no effect — always returns the one panel."""
    server = _make_server()
    async with TestClient(TestServer(server._app)) as client:
        resp = await client.get("/api/v2/status?serial=other-panel")
        assert resp.status == 200
        data = await resp.json()
        assert data["serialNumber"] == "sim-test-001"


async def test_register_returns_broker_details() -> None:
    """POST /api/v2/auth/register returns MQTT creds."""
    server = _make_server()
    async with TestClient(TestServer(server._app)) as client:
        resp = await client.post("/api/v2/auth/register", json={})
        assert resp.status == 200
        data = await resp.json()
        assert "accessToken" in data
        assert data["ebusBrokerUsername"] == "span"
        assert data["ebusBrokerPassword"] == "sim-password"
        assert data["serialNumber"] == "sim-test-001"
        assert data["hostname"] == "span-sim-sim-test-001"


async def test_ca_cert_endpoint() -> None:
    """GET /api/v2/certificate/ca returns PEM."""
    server = _make_server()
    async with TestClient(TestServer(server._app)) as client:
        resp = await client.get("/api/v2/certificate/ca")
        assert resp.status == 200
        assert resp.content_type == "application/x-pem-file"
        body = await resp.read()
        assert b"BEGIN CERTIFICATE" in body


async def test_schema_endpoint() -> None:
    """GET /api/v2/homie/schema returns JSON."""
    server = _make_server()
    async with TestClient(TestServer(server._app)) as client:
        resp = await client.get("/api/v2/homie/schema")
        assert resp.status == 200
        assert resp.content_type == "application/json"
        data = await resp.json()
        assert data == {"test": True}


async def test_no_admin_endpoints() -> None:
    """/admin/panels and /admin/reload return 404."""
    server = _make_server()
    async with TestClient(TestServer(server._app)) as client:
        resp = await client.get("/admin/panels")
        assert resp.status == 404

        resp = await client.post("/admin/reload")
        assert resp.status == 404


async def test_schema_endpoint_serves_40_tab_format() -> None:
    """Bootstrap HTTP endpoint serves a rendered schema whose space.format matches panel size."""
    template = load_schema(
        Path(__file__).parent.parent
        / "src"
        / "span_panel_simulator"
        / "data"
        / "homie_schema.json"
    )
    rendered = render_for_panel(template, 40)

    certs = MagicMock()
    certs.ca_cert_pem = b"-----BEGIN CERTIFICATE-----\nFAKE\n-----END CERTIFICATE-----\n"

    server = BootstrapHttpServer(
        serial="sim-40t-001",
        firmware=DEFAULT_FIRMWARE_VERSION,
        certs=certs,
        schema=rendered,
        broker_username="span",
        broker_password="sim-password",
        broker_host="localhost",
    )

    async with TestClient(TestServer(server._app)) as client:
        resp = await client.get("/api/v2/homie/schema")
        assert resp.status == 200
        data = await resp.json()
        assert data["types"]["energy.ebus.device.circuit"]["space"]["format"] == "1:40:1"
        # Hash is content-derived, not the stamped-in-template value
        expected_hash = (
            "sha256:"
            + hashlib.sha256(json.dumps(data["types"], sort_keys=True).encode()).hexdigest()[:16]
        )
        assert data["typesSchemaHash"] == expected_hash
