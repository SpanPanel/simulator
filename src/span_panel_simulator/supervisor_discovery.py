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

        session = aiohttp.ClientSession()
        try:
            async with session.get(_SUPERVISOR_DISCOVERY_URL, headers=self._headers()) as resp:
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
        finally:
            await session.close()

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

        session = aiohttp.ClientSession()
        try:
            async with session.post(
                _SUPERVISOR_DISCOVERY_URL,
                json=payload,
                headers=self._headers(),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    uuid = data.get("uuid")
                    if isinstance(uuid, str) and uuid:
                        self._entries[serial] = uuid
                        _LOGGER.info(
                            "Supervisor discovery: registered %s (uuid=%s)",
                            serial,
                            uuid,
                        )
                    else:
                        _LOGGER.warning(
                            "Supervisor discovery: register %s returned invalid uuid: %s",
                            serial,
                            data,
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
        finally:
            await session.close()

    async def unregister_panel(self, serial: str) -> None:
        """Remove a panel's discovery entry.

        No-ops if not in add-on mode or if the serial was never registered.
        """
        if not self._token:
            return
        uuid = self._entries.get(serial)
        if not uuid:
            return

        session = aiohttp.ClientSession()
        try:
            async with session.delete(
                f"{_SUPERVISOR_DISCOVERY_URL}/{uuid}",
                headers=self._headers(),
            ) as resp:
                if resp.status == 200:
                    self._entries.pop(serial, None)
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
        finally:
            await session.close()

    async def cleanup_all(self) -> None:
        """Unregister all tracked panels. Called on shutdown."""
        for serial in list(self._entries):
            await self.unregister_panel(serial)
