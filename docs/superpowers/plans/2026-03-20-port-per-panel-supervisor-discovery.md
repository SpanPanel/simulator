# Port-per-Panel Bootstrap + Supervisor Discovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Each simulated panel gets its own HTTP port and Supervisor Discovery entry, enabling the span_panel integration to discover panels when the simulator runs as an HA add-on.

**Architecture:** Replace the single multiplexed `BootstrapHttpServer` with per-panel instances on sequential ports from a configurable base. Add a `SupervisorDiscovery` client that POSTs/DELETEs to the HA Supervisor API in add-on mode. The integration gains `async_step_hassio` to handle these discovery events.

**Tech Stack:** Python 3.12+, aiohttp, zeroconf, pytest, pytest-asyncio

**Spec:** `docs/superpowers/specs/2026-03-20-port-per-panel-supervisor-discovery-design.md`

---

## File Structure

### New files
- `src/span_panel_simulator/supervisor_discovery.py` — Supervisor Discovery API client (register/unregister panels)
- `tests/test_bootstrap_single.py` — Tests for simplified single-panel bootstrap server
- `tests/test_supervisor_discovery.py` — Tests for Supervisor Discovery client
- `tests/test_port_allocation.py` — Tests for port allocation and release logic

### Modified files
- `src/span_panel_simulator/const.py` — Rename `HTTPS_PORT` to `DEFAULT_BASE_HTTP_PORT`
- `src/span_panel_simulator/bootstrap.py` — Simplify to single-panel server
- `src/span_panel_simulator/discovery.py` — Per-panel port in `register_panel()`
- `src/span_panel_simulator/app.py` — Per-panel HTTP servers, port tracking, Supervisor Discovery lifecycle
- `src/span_panel_simulator/__main__.py` — `--base-http-port` with deprecated `--http-port` alias
- `span_panel_simulator/config.yaml` — Add `base_http_port` option
- `span_panel_simulator/run.sh` — Pass `--base-http-port`
- `scripts/run-local.sh` — Update `--http-port` to `--base-http-port`, remove `--advertise-http-port`, remove admin/reload reference

### Integration files (separate repo: `/Users/bflood/projects/HA/span`)
- `custom_components/span_panel/manifest.json` — Add `"hassio"`
- `custom_components/span_panel/config_flow.py` — Add `async_step_hassio()`

---

## Task 1: Rename constant and update CLI

**Files:**
- Modify: `src/span_panel_simulator/const.py`
- Modify: `src/span_panel_simulator/__main__.py`

- [ ] **Step 1: Rename `HTTPS_PORT` to `DEFAULT_BASE_HTTP_PORT` in const.py**

In `src/span_panel_simulator/const.py`, change line 11:
```python
# Before:
HTTPS_PORT = 8081
# After:
DEFAULT_BASE_HTTP_PORT = 8081
```

- [ ] **Step 2: Update `__main__.py` imports**

In `src/span_panel_simulator/__main__.py`, change the import (line 19):
```python
# Before:
    HTTPS_PORT,
# After:
    DEFAULT_BASE_HTTP_PORT,
```

- [ ] **Step 3: Replace `--http-port` with `--base-http-port` and deprecated alias**

In `src/span_panel_simulator/__main__.py`, replace the `--http-port` argument block (lines 63-68) with:
```python
    parser.add_argument(
        "--base-http-port",
        type=int,
        default=int(os.environ.get("HTTP_PORT", str(DEFAULT_BASE_HTTP_PORT))),
        help="Base port for per-panel bootstrap HTTP servers. "
        "First panel uses this port, second uses port+1, etc.",
    )
    parser.add_argument(
        "--http-port",
        type=int,
        default=None,
        help="Deprecated: use --base-http-port instead",
    )
```

- [ ] **Step 4: Remove `--advertise-http-port` argument**

Delete lines 94-98 of `src/span_panel_simulator/__main__.py`:
```python
    # DELETE THIS BLOCK:
    parser.add_argument(
        "--advertise-http-port",
        type=int,
        default=int(os.environ.get("ADVERTISE_HTTP_PORT", "0")) or None,
        help="Port to advertise via mDNS (when host port differs from container port)",
    )
```

- [ ] **Step 5: Update `main()` to resolve deprecated alias and remove `advertise_http_port`**

After `args = _parse_args(argv)`, add:
```python
    # Resolve deprecated --http-port alias
    base_http_port = args.base_http_port
    if args.http_port is not None:
        logging.warning("--http-port is deprecated, use --base-http-port instead")
        base_http_port = args.http_port
```

Update the `SimulatorApp(...)` call (lines 182-197):
- Change `http_port=args.http_port` to `base_http_port=base_http_port`
- Remove `advertise_http_port=args.advertise_http_port`

- [ ] **Step 6: Fix all remaining imports of `HTTPS_PORT`**

Search for `HTTPS_PORT` in `src/` and `tests/` and update any other references to `DEFAULT_BASE_HTTP_PORT`.

- [ ] **Step 7: Run tests to verify nothing broke**

Run: `pytest tests/ -v`
Expected: All existing tests pass (app.py still compiles because we haven't changed its constructor yet — that's Task 3).

- [ ] **Step 8: Commit**

```
feat: rename HTTPS_PORT to DEFAULT_BASE_HTTP_PORT, add --base-http-port CLI flag
```

---

## Task 2: Create SupervisorDiscovery client

This task is independent of the bootstrap/app changes — build it first so it's ready to wire in.

**Files:**
- Create: `src/span_panel_simulator/supervisor_discovery.py`
- Create: `tests/test_supervisor_discovery.py`

- [ ] **Step 1: Write tests for SupervisorDiscovery**

Create `tests/test_supervisor_discovery.py`:

```python
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
    with patch("aiohttp.ClientSession", return_value=mock_session):
        await discovery.register_panel("sim-001", "192.168.1.50", 8081)

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
    await discovery_no_token.register_panel("sim-001", "192.168.1.50", 8081)
    assert len(discovery_no_token._entries) == 0

    await discovery_no_token.unregister_panel("sim-001")  # Should not raise


async def test_register_failure_logged_not_raised(discovery: SupervisorDiscovery):
    """Supervisor API failures are swallowed — panel startup must not be blocked."""
    mock_session = _mock_session(401)
    with patch("aiohttp.ClientSession", return_value=mock_session):
        await discovery.register_panel("sim-001", "192.168.1.50", 8081)

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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_supervisor_discovery.py -v`
Expected: FAIL — module does not exist yet.

- [ ] **Step 3: Implement SupervisorDiscovery**

Create `src/span_panel_simulator/supervisor_discovery.py`:

```python
"""HA Supervisor Discovery API client.

Registers and unregisters simulated panels with the HA Supervisor so
the span_panel integration discovers them via ``async_step_hassio``
instead of mDNS.  All operations are no-ops when not running as an
add-on (no ``SUPERVISOR_TOKEN`` env var).
"""

from __future__ import annotations

import logging
import os

import aiohttp

_LOGGER = logging.getLogger(__name__)

_SUPERVISOR_DISCOVERY_URL = "http://supervisor/discovery"
_SERVICE_NAME = "span_panel"


class SupervisorDiscovery:
    """Manages Supervisor Discovery entries for simulated panels."""

    def __init__(self) -> None:
        self._token = os.environ.get("SUPERVISOR_TOKEN")
        self._entries: dict[str, str] = {}  # serial -> discovery UUID

    @property
    def is_available(self) -> bool:
        """Whether we are running in add-on mode with a Supervisor token."""
        return self._token is not None

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    async def cleanup_stale(self) -> None:
        """Remove discovery entries from prior runs.

        Queries GET /discovery, deletes any entries matching our service
        name.  Called once on startup before registering new panels.
        """
        if not self._token:
            return

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    _SUPERVISOR_DISCOVERY_URL, headers=self._headers()
                ) as resp:
                    if resp.status != 200:
                        return
                    data = await resp.json()

                entries = data.get("discovery", [])
                for entry in entries:
                    if entry.get("service") != _SERVICE_NAME:
                        continue
                    uuid = entry.get("uuid", "")
                    if not uuid:
                        continue
                    async with session.delete(
                        f"{_SUPERVISOR_DISCOVERY_URL}/{uuid}",
                        headers=self._headers(),
                    ) as del_resp:
                        if del_resp.status == 200:
                            _LOGGER.info(
                                "Supervisor discovery: cleaned up stale entry %s",
                                uuid,
                            )
        except (aiohttp.ClientError, OSError):
            _LOGGER.warning(
                "Supervisor discovery: stale cleanup failed",
                exc_info=True,
            )

    async def register_panel(self, serial: str, host: str, port: int) -> None:
        """Register a panel with the Supervisor Discovery API.

        No-ops if not in add-on mode.  Errors are logged and swallowed.
        """
        if not self._token:
            return

        payload = {
            "service": _SERVICE_NAME,
            "config": {
                "host": host,
                "port": port,
                "serial": serial,
            },
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    _SUPERVISOR_DISCOVERY_URL,
                    json=payload,
                    headers=self._headers(),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        uuid = data.get("uuid", "")
                        self._entries[serial] = uuid
                        _LOGGER.info(
                            "Supervisor discovery: registered %s (uuid=%s)",
                            serial,
                            uuid,
                        )
                    else:
                        text = await resp.text()
                        _LOGGER.warning(
                            "Supervisor discovery: register %s failed (%d: %s)",
                            serial,
                            resp.status,
                            text,
                        )
        except (aiohttp.ClientError, OSError):
            _LOGGER.warning(
                "Supervisor discovery: register %s failed (network error)",
                serial,
                exc_info=True,
            )

    async def unregister_panel(self, serial: str) -> None:
        """Remove a panel's discovery entry.

        No-ops if not in add-on mode or if the serial was never registered.
        """
        uuid = self._entries.pop(serial, None)
        if not uuid or not self._token:
            return

        try:
            async with aiohttp.ClientSession() as session:
                async with session.delete(
                    f"{_SUPERVISOR_DISCOVERY_URL}/{uuid}",
                    headers=self._headers(),
                ) as resp:
                    if resp.status == 200:
                        _LOGGER.info(
                            "Supervisor discovery: unregistered %s (uuid=%s)",
                            serial,
                            uuid,
                        )
                    else:
                        _LOGGER.warning(
                            "Supervisor discovery: unregister %s failed (%d)",
                            serial,
                            resp.status,
                        )
        except (aiohttp.ClientError, OSError):
            _LOGGER.warning(
                "Supervisor discovery: unregister %s failed (network error)",
                serial,
                exc_info=True,
            )

    async def cleanup_all(self) -> None:
        """Unregister all tracked panels. Called on shutdown."""
        for serial in list(self._entries):
            await self.unregister_panel(serial)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_supervisor_discovery.py -v`
Expected: All pass.

- [ ] **Step 5: Commit**

```
feat: add SupervisorDiscovery client for add-on panel registration
```

---

## Task 3: Rewrite BootstrapHttpServer + app.py + discovery.py together

These three files must change in lockstep — `bootstrap.py`'s constructor change breaks `app.py` if done separately. This task does all three atomically.

**Files:**
- Modify: `src/span_panel_simulator/bootstrap.py`
- Modify: `src/span_panel_simulator/discovery.py`
- Modify: `src/span_panel_simulator/app.py`
- Create: `tests/test_bootstrap_single.py`
- Create: `tests/test_port_allocation.py`

- [ ] **Step 1: Write tests for single-panel bootstrap server**

Create `tests/test_bootstrap_single.py`:

```python
"""Tests for single-panel BootstrapHttpServer."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from span_panel_simulator.bootstrap import BootstrapHttpServer


@pytest.fixture
def mock_certs() -> MagicMock:
    certs = MagicMock()
    certs.ca_cert_pem = b"-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----"
    return certs


@pytest.fixture
def mock_schema() -> MagicMock:
    schema = MagicMock()
    schema.raw_json = '{"properties": []}'
    return schema


@pytest.fixture
def server(mock_certs: MagicMock, mock_schema: MagicMock) -> BootstrapHttpServer:
    return BootstrapHttpServer(
        serial="sim-test-001",
        firmware="spanos2/sim/01",
        certs=mock_certs,
        schema=mock_schema,
    )


async def test_status_returns_single_panel(server: BootstrapHttpServer, aiohttp_client):
    """GET /api/v2/status always returns the one panel."""
    client = await aiohttp_client(server._app)
    resp = await client.get("/api/v2/status")
    assert resp.status == 200
    data = await resp.json()
    assert data["serialNumber"] == "sim-test-001"
    assert data["firmwareVersion"] == "spanos2/sim/01"


async def test_status_ignores_serial_query_param(server: BootstrapHttpServer, aiohttp_client):
    """?serial= param has no effect — always returns the one panel."""
    client = await aiohttp_client(server._app)
    resp = await client.get("/api/v2/status?serial=other")
    assert resp.status == 200
    data = await resp.json()
    assert data["serialNumber"] == "sim-test-001"


async def test_register_returns_broker_details(server: BootstrapHttpServer, aiohttp_client):
    """POST /api/v2/auth/register returns MQTT credentials."""
    client = await aiohttp_client(server._app)
    resp = await client.post("/api/v2/auth/register", json={})
    assert resp.status == 200
    data = await resp.json()
    assert data["serialNumber"] == "sim-test-001"
    assert "accessToken" in data
    assert "ebusBrokerHost" in data
    assert "ebusBrokerMqttsPort" in data


async def test_ca_cert_endpoint(server: BootstrapHttpServer, aiohttp_client):
    """GET /api/v2/certificate/ca returns PEM certificate."""
    client = await aiohttp_client(server._app)
    resp = await client.get("/api/v2/certificate/ca")
    assert resp.status == 200
    assert b"BEGIN CERTIFICATE" in await resp.read()


async def test_schema_endpoint(server: BootstrapHttpServer, aiohttp_client):
    """GET /api/v2/homie/schema returns JSON schema."""
    client = await aiohttp_client(server._app)
    resp = await client.get("/api/v2/homie/schema")
    assert resp.status == 200
    assert resp.content_type == "application/json"


async def test_no_admin_endpoints(server: BootstrapHttpServer, aiohttp_client):
    """Admin endpoints are removed — they live in the dashboard now."""
    client = await aiohttp_client(server._app)
    resp = await client.get("/admin/panels")
    assert resp.status == 404
    resp = await client.post("/admin/reload")
    assert resp.status == 404
```

- [ ] **Step 2: Write port allocation tests**

Create `tests/test_port_allocation.py`:

```python
"""Tests for port allocation logic."""

from __future__ import annotations

import pytest

from span_panel_simulator.app import SimulatorApp


@pytest.fixture
def app(tmp_path) -> SimulatorApp:
    """Minimal SimulatorApp for testing port allocation."""
    return SimulatorApp(config_dir=tmp_path, base_http_port=9000)


def test_allocate_sequential_ports(app: SimulatorApp):
    """Ports are allocated sequentially from the base."""
    p1 = app._allocate_port()
    p2 = app._allocate_port()
    p3 = app._allocate_port()
    assert p1 == 9000
    assert p2 == 9001
    assert p3 == 9002


def test_release_and_reuse(app: SimulatorApp):
    """Released ports are reused (lowest available)."""
    p1 = app._allocate_port()
    p2 = app._allocate_port()
    p3 = app._allocate_port()
    app._release_port(p2)  # Free 9001
    p4 = app._allocate_port()
    assert p4 == 9001  # Reuses the gap


def test_release_nonexistent_port(app: SimulatorApp):
    """Releasing a port not in use is a no-op."""
    app._release_port(12345)  # Should not raise


def test_rapid_allocate_release_cycle(app: SimulatorApp):
    """No port leaks after repeated allocate/release cycles."""
    for _ in range(10):
        p = app._allocate_port()
        app._release_port(p)
    # After all releases, next allocation should be the base port
    assert app._allocate_port() == 9000
```

- [ ] **Step 3: Rewrite `src/span_panel_simulator/bootstrap.py` as single-panel**

Replace the entire file with:

```python
"""Bootstrap HTTP server — single-panel.

Serves the eBus bootstrap endpoints with response formats matching
the real SPAN panel v2 API.

Endpoints:
  GET  /api/v2/status           → panel identity (serialNumber, firmwareVersion)
  POST /api/v2/auth/register    → JWT + MQTT credentials (camelCase fields)
  GET  /api/v2/certificate/ca   → self-signed CA PEM
  GET  /api/v2/homie/schema     → Homie property schema JSON
"""

from __future__ import annotations

import contextlib
import logging
import secrets
import time
from typing import TYPE_CHECKING

from aiohttp import web

from span_panel_simulator.const import (
    DEFAULT_BROKER_PASSWORD,
    DEFAULT_BROKER_USERNAME,
    MQTTS_PORT,
    PATH_CA_CERT,
    PATH_HOMIE_SCHEMA,
    PATH_REGISTER,
    PATH_STATUS,
    WS_PORT,
    WSS_PORT,
)

if TYPE_CHECKING:
    from span_panel_simulator.certs import CertificateBundle
    from span_panel_simulator.schema import HomieSchemaRegistry

_LOGGER = logging.getLogger(__name__)


class BootstrapHttpServer:
    """HTTP server for a single panel's eBus bootstrap endpoints."""

    def __init__(
        self,
        serial: str,
        firmware: str,
        certs: CertificateBundle,
        schema: HomieSchemaRegistry,
        *,
        broker_username: str = DEFAULT_BROKER_USERNAME,
        broker_password: str = DEFAULT_BROKER_PASSWORD,
        broker_host: str = "localhost",
        host: str = "0.0.0.0",
        port: int = 443,
    ) -> None:
        self._serial = serial
        self._firmware = firmware
        self._certs = certs
        self._broker_username = broker_username
        self._broker_password = broker_password
        self._broker_host = broker_host
        self._host = host
        self._port = port

        self._homie_schema = schema.raw_json
        self._app = web.Application()
        self._runner: web.AppRunner | None = None

        self._app.router.add_get(PATH_STATUS, self._handle_status)
        self._app.router.add_post(PATH_REGISTER, self._handle_register)
        self._app.router.add_get(PATH_CA_CERT, self._handle_ca_cert)
        self._app.router.add_get(PATH_HOMIE_SCHEMA, self._handle_schema)

    async def _handle_status(self, request: web.Request) -> web.Response:
        """GET /api/v2/status — return panel identity."""
        return web.json_response(
            {
                "serialNumber": self._serial,
                "firmwareVersion": self._firmware,
                "proximityProven": True,
            }
        )

    async def _handle_register(self, request: web.Request) -> web.Response:
        """POST /api/v2/auth/register — return MQTT credentials."""
        body: dict[str, str] = {}
        with contextlib.suppress(Exception):
            body = await request.json()

        token = f"sim.{secrets.token_urlsafe(32)}.{secrets.token_urlsafe(16)}"
        passphrase = body.get("hopPassphrase", "sim-passphrase")
        broker_host = request.host.split(":")[0] if request.host else self._broker_host

        payload: dict[str, object] = {
            "accessToken": token,
            "tokenType": "Bearer",
            "iatMs": int(time.time() * 1000),
            "ebusBrokerUsername": self._broker_username,
            "ebusBrokerPassword": self._broker_password,
            "ebusBrokerHost": broker_host,
            "ebusBrokerMqttsPort": MQTTS_PORT,
            "ebusBrokerWsPort": WS_PORT,
            "ebusBrokerWssPort": WSS_PORT,
            "hostname": f"span-sim-{self._serial}",
            "serialNumber": self._serial,
            "hopPassphrase": passphrase,
        }
        return web.json_response(payload)

    async def _handle_ca_cert(self, _request: web.Request) -> web.Response:
        return web.Response(body=self._certs.ca_cert_pem, content_type="application/x-pem-file")

    async def _handle_schema(self, _request: web.Request) -> web.Response:
        return web.Response(text=self._homie_schema, content_type="application/json")

    async def start(self) -> None:
        """Start the HTTP server."""
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._host, self._port)
        await site.start()
        _LOGGER.info("Bootstrap HTTP for %s on %s:%d", self._serial, self._host, self._port)

    async def stop(self) -> None:
        """Stop the HTTP server."""
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def __aenter__(self) -> BootstrapHttpServer:
        await self.start()
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.stop()
```

- [ ] **Step 4: Update `src/span_panel_simulator/discovery.py`**

In `PanelAdvertiser.__init__`, remove `http_port` parameter (keep only `advertise_address`):
```python
    def __init__(
        self,
        advertise_address: str | None = None,
    ) -> None:
        self._advertise_address = advertise_address
        self._zeroconf: AsyncZeroconf | None = None
        self._services: dict[str, list[ServiceInfo]] = {}
```

Change `register_panel` signature to accept `port`:
```python
    async def register_panel(
        self, serial: str, firmware: str, *, model: str = "MAIN_32", port: int = 80
    ) -> None:
```

Inside `register_panel`, replace all `self._http_port` references with `port`:
- Line 114: `if self._http_port != 80:` → `if port != 80:`
- Line 115: `ebus_properties["httpPort"] = str(self._http_port)` → `ebus_properties["httpPort"] = str(port)`
- Line 130: `(SERVICE_TYPE_SPAN, span_properties, self._http_port)` → `(SERVICE_TYPE_SPAN, span_properties, port)`
- Line 153-158 log message: replace `self._http_port` with `port`

- [ ] **Step 5: Update `src/span_panel_simulator/app.py`**

This is the largest change. Key modifications:

**Imports** — Add to `TYPE_CHECKING` block:
```python
    from span_panel_simulator.supervisor_discovery import SupervisorDiscovery
```

**Constructor** — Replace `http_port` and `advertise_http_port` params with `base_http_port`:
```python
    base_http_port: int = DEFAULT_BASE_HTTP_PORT,
```

Replace `HTTPS_PORT` import with `DEFAULT_BASE_HTTP_PORT`.

Add new tracked state:
```python
    self._base_http_port = base_http_port
    self._panel_servers: dict[str, BootstrapHttpServer] = {}
    self._panel_ports: dict[str, int] = {}
    self._used_ports: set[int] = set()
    self._supervisor_discovery: SupervisorDiscovery | None = None
```

Remove: `self._http_port`, `self._advertise_http_port`, `self._http_server`.

**Add port allocation methods:**
```python
    def _allocate_port(self) -> int:
        """Return the lowest available port from the base."""
        port = self._base_http_port
        while port in self._used_ports:
            port += 1
        self._used_ports.add(port)
        return port

    def _release_port(self, port: int) -> None:
        """Release a port back to the pool."""
        self._used_ports.discard(port)
```

**`_start_panel`** — Replace the `self._http_server.register_panel(serial, self._firmware)` and `self._advertiser.register_panel(serial, ...)` blocks with per-panel server creation:
```python
    # Allocate port and create per-panel HTTP server
    port = self._allocate_port()
    while True:
        server = BootstrapHttpServer(
            serial=serial,
            firmware=self._firmware,
            certs=self._certs,
            schema=self._schema,
            broker_username=self._broker_username,
            broker_password=self._broker_password,
            broker_host=self._broker_host,
            port=port,
        )
        try:
            await server.start()
            break
        except OSError:
            _LOGGER.warning("Port %d in use, trying next", port)
            self._release_port(port)
            port = self._allocate_port()

    self._panel_servers[serial] = server
    self._panel_ports[serial] = port

    if self._advertiser is not None:
        await self._advertiser.register_panel(
            serial, self._firmware, model=panel_model, port=port
        )

    if self._supervisor_discovery is not None:
        advertise_host = self._advertise_address or "127.0.0.1"
        await self._supervisor_discovery.register_panel(serial, advertise_host, port)
```

**`_stop_panel`** — Replace `self._http_server.unregister_panel(serial)` with:
```python
    server = self._panel_servers.pop(serial, None)
    if server is not None:
        await server.stop()
    port = self._panel_ports.pop(serial, None)
    if port is not None:
        self._release_port(port)

    if self._supervisor_discovery is not None:
        await self._supervisor_discovery.unregister_panel(serial)
```

**`run()`** — Remove the single-server creation block (old lines 540-550).

Update `PanelAdvertiser` construction — remove `http_port` arg:
```python
    advertiser = PanelAdvertiser(advertise_address=self._advertise_address)
```

Add Supervisor Discovery init after HA API client setup:
```python
    from span_panel_simulator.supervisor_discovery import SupervisorDiscovery
    self._supervisor_discovery = SupervisorDiscovery()
    if self._supervisor_discovery.is_available:
        await self._supervisor_discovery.cleanup_stale()
        _LOGGER.info("Supervisor Discovery: available (add-on mode)")
```

Update `finally` block — remove `self._http_server.stop()`, add:
```python
    if self._supervisor_discovery is not None:
        await self._supervisor_discovery.cleanup_all()
    for srv in self._panel_servers.values():
        await srv.stop()
    self._panel_servers.clear()
```

- [ ] **Step 6: Run full test suite**

Run: `pytest tests/ -v`
Expected: All tests pass.

- [ ] **Step 7: Run type checker and linter**

Run: `mypy src/span_panel_simulator/ && ruff check src/ tests/`
Expected: No errors.

- [ ] **Step 8: Commit**

```
feat: per-panel HTTP servers, port allocation, and Supervisor Discovery wiring
```

---

## Task 4: Update add-on config and run scripts

**Files:**
- Modify: `span_panel_simulator/config.yaml`
- Modify: `span_panel_simulator/run.sh`
- Modify: `scripts/run-local.sh`

- [ ] **Step 1: Add `base_http_port` to config.yaml**

Add to `options:` section after `dashboard_enabled`:
```yaml
  base_http_port: 8081
```

Add to `schema:` section after `dashboard_enabled`:
```yaml
  base_http_port: "int(1024,65535)?"
```

- [ ] **Step 2: Update run.sh**

Add after line 21 (`DASHBOARD_ENABLED=...`):
```bash
BASE_HTTP_PORT=$(jq -r '.base_http_port // 8081' "${OPTIONS_FILE}")
```

Replace line 96 (`--http-port 8081`) with:
```bash
    --base-http-port "${BASE_HTTP_PORT}"
```

- [ ] **Step 3: Update scripts/run-local.sh**

Replace `--http-port` with `--base-http-port` (line ~180).
Remove `--advertise-http-port` usage (line ~189).
Replace the admin/reload echo (line 199) with:
```bash
    echo "    Dashboard: http://${advertise_addr:-localhost}:${DASHBOARD_PORT}/"
```

- [ ] **Step 4: Commit**

```
feat: add base_http_port to add-on config, update run scripts
```

---

## Task 5: Integration — add `async_step_hassio`

**Files (in `/Users/bflood/projects/HA/span` repo):**
- Modify: `custom_components/span_panel/manifest.json`
- Modify: `custom_components/span_panel/config_flow.py`

- [ ] **Step 1: Add `"hassio"` to manifest.json**

Check the current manifest format in `/Users/bflood/projects/HA/span/custom_components/span_panel/manifest.json` and add a top-level `"hassio"` key. The exact format depends on the HA version — typically just `"hassio": "span_panel"` or a list mapping.

- [ ] **Step 2: Add `async_step_hassio` to config_flow.py**

Add the import at the top of the file:
```python
from homeassistant.helpers.service_info.hassio import HassioServiceInfo
```

Add this method to the `SpanPanelConfigFlow` class, near `async_step_zeroconf`:

```python
    async def async_step_hassio(self, discovery_info: HassioServiceInfo) -> ConfigFlowResult:
        """Handle discovery from HA Supervisor (simulator add-on)."""
        config = discovery_info.config
        host = str(config.get("host", ""))
        port = int(config.get("port", 80))

        if not host:
            return self.async_abort(reason="no_host")

        # Abort if this host is already configured
        self._async_abort_entries_match({CONF_HOST: host})

        # Validate panel is reachable and v2
        self._http_port = port
        detection = await detect_api_version(host, port=port)
        if detection.api_version != "v2" or detection.status_info is None:
            return self.async_abort(reason="v2_not_detected")

        # Set up flow — same path as zeroconf discovery
        self.use_ssl = False
        await self.setup_flow(TriggerFlowType.CREATE_ENTRY, host, False)
        await self.ensure_not_already_configured()

        return await self.async_step_confirm_discovery()
```

- [ ] **Step 3: Run integration tests if available**

Run: `pytest tests/ -v` (in the span repo)
Expected: Existing tests pass.

- [ ] **Step 4: Commit (in span repo)**

```
feat: add async_step_hassio for Supervisor Discovery from simulator add-on
```

---

## Task 6: End-to-end verification

- [ ] **Step 1: Run full simulator test suite**

Run: `pytest tests/ -v`
Expected: All tests pass.

- [ ] **Step 2: Run type checker**

Run: `mypy src/span_panel_simulator/`
Expected: No errors.

- [ ] **Step 3: Run linter**

Run: `ruff check src/ tests/`
Expected: No errors.

- [ ] **Step 4: Standalone smoke test**

Start the simulator with two configs:
```bash
python -m span_panel_simulator --config-dir configs/ --base-http-port 9000
```

Verify:
- Two bootstrap HTTP servers start on ports 9000 and 9001
- `curl http://localhost:9000/api/v2/status` returns panel A's serial
- `curl http://localhost:9001/api/v2/status` returns panel B's serial
- mDNS entries show different ports in `httpPort` TXT property
- Dashboard on port 18080 still works

- [ ] **Step 5: Commit any fixes**

```
fix: address issues found in end-to-end verification
```
