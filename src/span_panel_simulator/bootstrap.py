"""Bootstrap HTTP server — multi-panel aware.

Serves the eBus bootstrap endpoints with response formats matching
the real SPAN panel v2 API, plus admin endpoints for reload and
panel listing.

Endpoints:
  GET  /api/v2/status           → panel identity (serialNumber, firmwareVersion)
  POST /api/v2/auth/register    → JWT + MQTT credentials (camelCase fields)
  GET  /api/v2/certificate/ca   → self-signed CA PEM
  GET  /api/v2/homie/schema     → Homie property schema JSON
  POST /admin/reload            → trigger config reload
  GET  /admin/panels            → list running panels
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
    SIO_NAMESPACE,
    WS_PORT,
    WSS_PORT,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    import socketio

    from span_panel_simulator.certs import CertificateBundle
    from span_panel_simulator.schema import HomieSchemaRegistry

_LOGGER = logging.getLogger(__name__)


class BootstrapHttpServer:
    """HTTP server for eBus bootstrap and simulator admin."""

    def __init__(
        self,
        certs: CertificateBundle,
        schema: HomieSchemaRegistry,
        *,
        broker_username: str = DEFAULT_BROKER_USERNAME,
        broker_password: str = DEFAULT_BROKER_PASSWORD,
        broker_host: str = "localhost",
        host: str = "0.0.0.0",
        port: int = 443,
        reload_callback: Callable[[], None] | None = None,
        sio_server: socketio.AsyncServer | None = None,
    ) -> None:
        self._certs = certs
        self._broker_username = broker_username
        self._broker_password = broker_password
        self._broker_host = broker_host
        self._host = host
        self._port = port
        self._reload_callback = reload_callback
        self._has_sio = sio_server is not None

        self._homie_schema = schema.raw_json
        self._app = web.Application()
        self._runner: web.AppRunner | None = None

        # Attach Socket.IO server (adds /socket.io/ routes to the app)
        if sio_server is not None:
            sio_server.attach(self._app)

        # Panel registry: serial → firmware version
        self._panels: dict[str, str] = {}

        # Bootstrap endpoints
        self._app.router.add_get(PATH_STATUS, self._handle_status)
        self._app.router.add_post(PATH_REGISTER, self._handle_register)
        self._app.router.add_get(PATH_CA_CERT, self._handle_ca_cert)
        self._app.router.add_get(PATH_HOMIE_SCHEMA, self._handle_schema)

        # Admin endpoints
        self._app.router.add_post("/admin/reload", self._handle_reload)
        self._app.router.add_get("/admin/panels", self._handle_list_panels)

    # ------------------------------------------------------------------
    # Panel registry
    # ------------------------------------------------------------------

    def register_panel(self, serial: str, firmware: str) -> None:
        """Add a panel to the registry."""
        self._panels[serial] = firmware

    def unregister_panel(self, serial: str) -> None:
        """Remove a panel from the registry."""
        self._panels.pop(serial, None)

    # ------------------------------------------------------------------
    # Bootstrap handlers — field names match real SPAN v2 API
    # ------------------------------------------------------------------

    async def _handle_status(self, request: web.Request) -> web.Response:
        """GET /api/v2/status — return panel identity.

        Query params:
          ?serial=XXX  → return that specific panel
          (no param)   → return first panel (single-panel compatible)

        Response matches real panel: ``{"serialNumber": "...", "firmwareVersion": "..."}``
        """
        serial_filter = request.query.get("serial")

        if serial_filter:
            firmware = self._panels.get(serial_filter)
            if firmware is None:
                raise web.HTTPNotFound(text=f"Panel {serial_filter} not found")
            return web.json_response(
                {
                    "serialNumber": serial_filter,
                    "firmwareVersion": firmware,
                    "proximityProven": True,
                }
            )

        if not self._panels:
            raise web.HTTPServiceUnavailable(text="No panels running")

        # Return the first panel — mirrors a real SPAN panel which only
        # has one identity.  Use ?serial=XXX for a specific panel or
        # /admin/panels for the full list.
        serial, firmware = next(iter(self._panels.items()))
        return web.json_response(
            {
                "serialNumber": serial,
                "firmwareVersion": firmware,
                "proximityProven": True,
            }
        )

    async def _handle_register(self, request: web.Request) -> web.Response:
        """POST /api/v2/auth/register — return MQTT credentials.

        Accepts optional ``hopPassphrase`` in the request body (ignored
        by the simulator — any passphrase is accepted).

        Response matches real panel's camelCase field names exactly.
        """
        body: dict[str, str] = {}
        with contextlib.suppress(Exception):
            body = await request.json()

        # Determine which panel this is for (use first panel as default)
        serial_hint = body.get("serial", "")
        if serial_hint and serial_hint in self._panels:
            serial = serial_hint
        elif self._panels:
            serial = next(iter(self._panels))
        else:
            raise web.HTTPServiceUnavailable(text="No panels running")

        token = f"sim.{secrets.token_urlsafe(32)}.{secrets.token_urlsafe(16)}"
        passphrase = body.get("hopPassphrase", "sim-passphrase")

        # The broker host returned to the client must be the address the
        # client used to reach *us* — on a real panel the broker is co-located
        # with the HTTP server, so the client connects to the same IP for both.
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
            "hostname": f"span-sim-{serial}",
            "serialNumber": serial,
            "hopPassphrase": passphrase,
        }
        if self._has_sio:
            payload["sioNamespace"] = SIO_NAMESPACE

        return web.json_response(payload)

    async def _handle_ca_cert(self, _request: web.Request) -> web.Response:
        return web.Response(
            body=self._certs.ca_cert_pem,
            content_type="application/x-pem-file",
        )

    async def _handle_schema(self, _request: web.Request) -> web.Response:
        return web.Response(
            text=self._homie_schema,
            content_type="application/json",
        )

    # ------------------------------------------------------------------
    # Admin handlers
    # ------------------------------------------------------------------

    async def _handle_reload(self, _request: web.Request) -> web.Response:
        if self._reload_callback is not None:
            self._reload_callback()
            return web.json_response({"status": "reload_requested"})
        return web.json_response({"status": "no_reload_handler"}, status=503)

    async def _handle_list_panels(self, _request: web.Request) -> web.Response:
        return web.json_response(
            {
                "panels": [
                    {"serialNumber": serial, "firmwareVersion": fw}
                    for serial, fw in self._panels.items()
                ]
            }
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the HTTP server."""
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._host, self._port)
        await site.start()
        _LOGGER.info("Bootstrap HTTP server listening on %s:%d", self._host, self._port)

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
