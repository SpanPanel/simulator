"""Socket.IO handler -- versioned integration channel.

Provides a Socket.IO namespace that allows Home Assistant integrations
to push configuration data and trigger operations on the simulator in
real time.  The namespace is versioned so future protocol changes can
be introduced without breaking existing clients.

Protocol v1.0 -- namespace ``/v1/panel``::

    connect              -> server emits "protocol" {"version": "1.0"}
    set_location         -> client sends {"serial", "latitude", "longitude"}
                            server acks  {"status": "ok", "time_zone": str}
    clone_panel          -> client sends {"host", "passphrase", "latitude", "longitude"}
                            server acks  {"status": "ok", "clone_serial", "circuits", ...}
    apply_usage_profiles -> client sends {"clone_serial", "profiles": {template: {…}}}
                            server acks  {"status": "ok", "templates_updated": int}
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

import socketio

from span_panel_simulator.const import SIO_NAMESPACE, SIO_PROTOCOL_VERSION

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from typing import Any

_LOGGER = logging.getLogger(__name__)


@dataclass
class SioContext:
    """Callback interface injected into the Socket.IO namespace."""

    update_panel_location: Callable[[str, float, float], Coroutine[Any, Any, dict[str, str]]]
    clone_panel: Callable[
        [str, str | None, float, float],
        Coroutine[Any, Any, dict[str, object]],
    ]
    apply_usage_profiles: Callable[
        [str, dict[str, dict[str, object]]],
        Coroutine[Any, Any, dict[str, object]],
    ]


def create_sio_server(ctx: SioContext) -> socketio.AsyncServer:
    """Create an async Socket.IO server with the panel namespace registered."""
    sio: socketio.AsyncServer = socketio.AsyncServer(async_mode="aiohttp")
    sio.register_namespace(_PanelNamespace(SIO_NAMESPACE, ctx))
    return sio


class _PanelNamespace(socketio.AsyncNamespace):  # type: ignore[misc]
    """``/v1/panel`` — panel configuration events."""

    def __init__(self, namespace: str, ctx: SioContext) -> None:
        super().__init__(namespace)
        self._ctx = ctx

    async def on_connect(
        self,
        sid: str,
        environ: dict[str, object],
        auth: dict[str, object] | None = None,
    ) -> None:
        _LOGGER.info("Socket.IO client connected: %s", sid)
        await self.emit("protocol", {"version": SIO_PROTOCOL_VERSION}, to=sid)

    async def on_disconnect(self, sid: str) -> None:
        _LOGGER.info("Socket.IO client disconnected: %s", sid)

    async def on_set_location(self, sid: str, data: dict[str, object]) -> dict[str, str]:
        """Handle a location push from the integration.

        Expected payload::

            {"serial": "<panel-serial>",
             "latitude": <float>,
             "longitude": <float>}

        Returns an ack dict with ``status`` and ``time_zone``.
        """
        serial = data.get("serial")
        latitude = data.get("latitude")
        longitude = data.get("longitude")

        if not isinstance(serial, str) or not serial:
            return {"status": "error", "message": "Missing or invalid 'serial'"}
        if not isinstance(latitude, int | float):
            return {"status": "error", "message": "Missing or invalid 'latitude'"}
        if not isinstance(longitude, int | float):
            return {"status": "error", "message": "Missing or invalid 'longitude'"}

        result = await self._ctx.update_panel_location(serial, float(latitude), float(longitude))
        _LOGGER.info(
            "set_location from %s: serial=%s lat=%.4f lon=%.4f -> %s",
            sid,
            serial,
            latitude,
            longitude,
            result.get("time_zone", "?"),
        )
        return result

    async def on_clone_panel(self, sid: str, data: dict[str, object]) -> dict[str, object]:
        """Clone a real panel and apply HA's location to the clone.

        Expected payload::

            {"host": "<panel-ip>",
             "passphrase": "<passphrase-or-null>",
             "latitude": <float>,
             "longitude": <float>}

        Returns a result dict with clone details and resolved timezone.
        """
        host = data.get("host")
        if not isinstance(host, str) or not host:
            return {"status": "error", "phase": "validation", "message": "Missing 'host'"}

        passphrase = data.get("passphrase")
        if passphrase is not None and not isinstance(passphrase, str):
            return {"status": "error", "phase": "validation", "message": "Invalid 'passphrase'"}

        latitude = data.get("latitude")
        longitude = data.get("longitude")
        if not isinstance(latitude, int | float):
            return {"status": "error", "phase": "validation", "message": "Missing 'latitude'"}
        if not isinstance(longitude, int | float):
            return {"status": "error", "phase": "validation", "message": "Missing 'longitude'"}

        result = await self._ctx.clone_panel(
            str(host),
            str(passphrase) if passphrase else None,
            float(latitude),
            float(longitude),
        )
        _LOGGER.info(
            "clone_panel from %s: host=%s -> %s",
            sid,
            host,
            result.get("status"),
        )
        return result

    async def on_apply_usage_profiles(
        self, sid: str, data: dict[str, object]
    ) -> dict[str, object]:
        """Merge HA-derived usage profiles into a clone config.

        Expected payload::

            {"clone_serial": "<sim-...-clone>",
             "profiles": {"clone_1": {"typical_power": …, …}, …}}

        Returns an ack dict with ``status`` and ``templates_updated``.
        """
        clone_serial = data.get("clone_serial")
        if not isinstance(clone_serial, str) or not clone_serial:
            return {"status": "error", "message": "Missing or invalid 'clone_serial'"}

        profiles = data.get("profiles")
        if not isinstance(profiles, dict) or not profiles:
            return {"status": "error", "message": "Missing or invalid 'profiles'"}

        result = await self._ctx.apply_usage_profiles(clone_serial, profiles)
        _LOGGER.info(
            "apply_usage_profiles from %s: serial=%s -> %s",
            sid,
            clone_serial,
            result.get("status"),
        )
        return result
