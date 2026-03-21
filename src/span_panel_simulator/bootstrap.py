"""Bootstrap HTTP server — single-panel per instance.

Each simulated panel gets its own ``BootstrapHttpServer`` bound to a
unique port, matching real SPAN hardware where each panel is a separate
device on a different IP.

Endpoints:
  GET  /api/v2/status           -> panel identity (serialNumber, firmwareVersion)
  POST /api/v2/auth/register    -> JWT + MQTT credentials (camelCase fields)
  GET  /api/v2/certificate/ca   -> self-signed CA PEM
  GET  /api/v2/homie/schema     -> Homie property schema JSON
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

        # Bootstrap endpoints
        self._app.router.add_get(PATH_STATUS, self._handle_status)
        self._app.router.add_post(PATH_REGISTER, self._handle_register)
        self._app.router.add_get(PATH_CA_CERT, self._handle_ca_cert)
        self._app.router.add_get(PATH_HOMIE_SCHEMA, self._handle_schema)

    # ------------------------------------------------------------------
    # Bootstrap handlers — field names match real SPAN v2 API
    # ------------------------------------------------------------------

    async def _handle_status(self, _request: web.Request) -> web.Response:
        """GET /api/v2/status — return this panel's identity.

        The ``?serial=`` query parameter is accepted but ignored — each
        server only knows about one panel.

        Response matches real panel: ``{"serialNumber": "...", "firmwareVersion": "..."}``
        """
        return web.json_response(
            {
                "serialNumber": self._serial,
                "firmwareVersion": self._firmware,
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
            "hostname": f"span-sim-{self._serial}",
            "serialNumber": self._serial,
            "hopPassphrase": passphrase,
        }

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
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the HTTP server."""
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._host, self._port)
        await site.start()
        _LOGGER.info(
            "Bootstrap HTTP server for %s listening on %s:%d",
            self._serial,
            self._host,
            self._port,
        )

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
