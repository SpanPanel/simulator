"""Bootstrap HTTP server — the 4 endpoints needed before MQTT handoff.

Serves:
  GET  /api/v2/status           → panel identity
  POST /api/v2/auth/register    → JWT + MQTT credentials
  GET  /api/v2/certificate/ca   → self-signed CA PEM
  GET  /api/v2/homie/schema     → Homie property schema JSON
"""

from __future__ import annotations

import logging
import secrets
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web

from span_panel_simulator.const import (
    DEFAULT_BROKER_PASSWORD,
    DEFAULT_BROKER_USERNAME,
    DEFAULT_FIRMWARE_VERSION,
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

_LOGGER = logging.getLogger(__name__)


class BootstrapHttpServer:
    """Minimal HTTP server for the eBus bootstrap handshake."""

    def __init__(
        self,
        serial_number: str,
        certs: CertificateBundle,
        homie_schema_path: Path,
        *,
        firmware_version: str = DEFAULT_FIRMWARE_VERSION,
        broker_username: str = DEFAULT_BROKER_USERNAME,
        broker_password: str = DEFAULT_BROKER_PASSWORD,
        host: str = "0.0.0.0",
        port: int = 443,
    ) -> None:
        self._serial = serial_number
        self._certs = certs
        self._firmware = firmware_version
        self._broker_username = broker_username
        self._broker_password = broker_password
        self._host = host
        self._port = port

        self._homie_schema = homie_schema_path.read_text(encoding="utf-8")
        self._app = web.Application()
        self._runner: web.AppRunner | None = None

        self._app.router.add_get(PATH_STATUS, self._handle_status)
        self._app.router.add_post(PATH_REGISTER, self._handle_register)
        self._app.router.add_get(PATH_CA_CERT, self._handle_ca_cert)
        self._app.router.add_get(PATH_HOMIE_SCHEMA, self._handle_schema)

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    async def _handle_status(self, _request: web.Request) -> web.Response:
        return web.json_response({
            "serial": self._serial,
            "firmware": self._firmware,
        })

    async def _handle_register(self, _request: web.Request) -> web.Response:
        # Generate a simulated JWT (not cryptographically meaningful)
        token = f"sim.{secrets.token_urlsafe(32)}.{secrets.token_urlsafe(16)}"
        return web.json_response({
            "accessToken": token,
            "mqtt": {
                "username": self._broker_username,
                "password": self._broker_password,
                "host": "localhost",
                "ports": {
                    "mqtts": MQTTS_PORT,
                    "ws": WS_PORT,
                    "wss": WSS_PORT,
                },
            },
        })

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
