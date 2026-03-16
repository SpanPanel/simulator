"""WebSocket handler for the panel clone pipeline.

Accepts a WSS connection on ``/ws/clone``, validates the incoming
``clone_panel`` request, and orchestrates the scrape-translate-write
pipeline.  Status updates are streamed back to the caller as each
phase progresses.

This module is the glue between the WebSocket transport (aiohttp),
the eBus scraper (``scraper.py``), and the translation layer
(``clone.py``).  It contains no business logic of its own.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from aiohttp import WSMsgType, web

from span_panel_simulator.clone import translate_scraped_panel, write_clone_config
from span_panel_simulator.homie_const import TYPE_BESS, TYPE_EVSE, TYPE_PV
from span_panel_simulator.scraper import ScrapeError, register_with_panel, scrape_ebus

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_LOGGER = logging.getLogger(__name__)


class CloneHandler:
    """Handles WebSocket clone requests and drives the scrape pipeline."""

    def __init__(
        self,
        config_dir: Path,
        reload_callback: Callable[[], None],
    ) -> None:
        self._config_dir = config_dir
        self._reload_callback = reload_callback

    async def handle_websocket(self, request: web.Request) -> web.WebSocketResponse:
        """aiohttp WebSocket handler for ``/ws/clone``."""
        ws = web.WebSocketResponse()
        await ws.prepare(request)

        try:
            await self._process_clone(ws)
        except Exception:
            _LOGGER.exception("Unhandled error in clone handler")
            await _send_error(ws, "unknown", "Internal server error")
        finally:
            await ws.close()

        return ws

    async def _process_clone(self, ws: web.WebSocketResponse) -> None:
        """Wait for a clone_panel message and run the pipeline."""
        # Wait for the request message
        msg = await ws.receive()
        if msg.type != WSMsgType.TEXT:
            await _send_error(ws, "unknown", "Expected a JSON text message")
            return

        request = _parse_request(msg.data)
        if request is None:
            await _send_error(ws, "unknown", "Invalid request: expected clone_panel message")
            return

        host, passphrase = request

        # Phase 1: Register with the real panel
        await _send_status(ws, "registering", f"Authenticating with panel at {host}")
        try:
            creds, ca_pem = await register_with_panel(host, passphrase)
        except ScrapeError as exc:
            await _send_error(ws, exc.phase, str(exc))
            return

        # Phase 2: Scrape eBus topics
        async def status_cb(phase: str, detail: str) -> None:
            await _send_status(ws, phase, detail)

        try:
            scraped = await scrape_ebus(creds, ca_pem, status_callback=status_cb)
        except ScrapeError as exc:
            await _send_error(ws, exc.phase, str(exc))
            return

        # Phase 3: Translate to YAML config
        await _send_status(ws, "translating", "Mapping eBus properties to simulator config")
        try:
            config = translate_scraped_panel(scraped, host=host, passphrase=passphrase)
        except Exception as exc:
            await _send_error(ws, "translating", f"Translation failed: {exc}")
            return

        # Phase 4: Write config and reload
        await _send_status(ws, "writing", "Writing config and triggering reload")
        try:
            output_path = write_clone_config(
                config,
                self._config_dir,
                scraped.serial_number,
            )
        except ValueError as exc:
            await _send_error(ws, "writing", f"Config validation failed: {exc}")
            return

        self._reload_callback()

        # Build result summary
        nodes = scraped.description.get("nodes", {})
        circuit_count = sum(
            1
            for n in nodes.values()
            if isinstance(n, dict) and n.get("type") == "energy.ebus.device.circuit"
        )
        has_bess = any(isinstance(n, dict) and n.get("type") == TYPE_BESS for n in nodes.values())
        has_pv = any(isinstance(n, dict) and n.get("type") == TYPE_PV for n in nodes.values())
        has_evse = any(isinstance(n, dict) and n.get("type") == TYPE_EVSE for n in nodes.values())

        base = scraped.serial_number
        if not base.lower().startswith("sim-"):
            base = f"sim-{base}"
        clone_serial = f"{base}-clone"
        await ws.send_json(
            {
                "type": "result",
                "status": "ok",
                "serial": scraped.serial_number,
                "clone_serial": clone_serial,
                "filename": output_path.name,
                "circuits": circuit_count,
                "has_bess": has_bess,
                "has_pv": has_pv,
                "has_evse": has_evse,
            }
        )

        _LOGGER.info(
            "Clone complete: %s -> %s (%d circuits)",
            scraped.serial_number,
            clone_serial,
            circuit_count,
        )


def _parse_request(data: str) -> tuple[str, str | None] | None:
    """Parse and validate a clone_panel request message.

    Returns (host, passphrase) or None if invalid.
    """
    try:
        msg = json.loads(data)
    except json.JSONDecodeError:
        return None

    if not isinstance(msg, dict):
        return None
    if msg.get("type") != "clone_panel":
        return None

    host = msg.get("host")
    if not isinstance(host, str) or not host:
        return None

    passphrase = msg.get("passphrase")
    if passphrase is not None and not isinstance(passphrase, str):
        return None

    return host, passphrase


async def _send_status(ws: web.WebSocketResponse, phase: str, detail: str) -> None:
    """Send a status update to the WebSocket client."""
    await ws.send_json(
        {
            "type": "status",
            "phase": phase,
            "detail": detail,
        }
    )


async def _send_error(ws: web.WebSocketResponse, phase: str, message: str) -> None:
    """Send an error result to the WebSocket client."""
    await ws.send_json(
        {
            "type": "result",
            "status": "error",
            "phase": phase,
            "message": message,
        }
    )
