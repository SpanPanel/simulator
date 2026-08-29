"""HA Supervisor Discovery API client.

Registers and unregisters simulated panels with the HA Supervisor so
the span_panel integration discovers them via ``async_step_hassio``
instead of mDNS.  All operations are no-ops when not running as an
add-on (no ``SUPERVISOR_TOKEN`` env var).
"""

from __future__ import annotations

import logging
import os
import socket

import aiohttp

_LOGGER = logging.getLogger(__name__)


def _container_hostname() -> str:
    """Return the Docker container hostname for Supervisor discovery.

    In add-on mode the Supervisor assigns a hostname that HA Core can
    resolve via Docker DNS (e.g. ``f8c38f2b-span-panel-simulator``).
    """
    return socket.gethostname()


_SUPERVISOR_DISCOVERY_URL = "http://supervisor/discovery"
_SERVICE_NAME = "span_panel"


def _payload(body: object) -> dict[str, object]:
    """Unwrap a Supervisor API response envelope.

    Every Supervisor endpoint answers ``{"result": ..., "data": {...}}``;
    the fields we want live under ``data``, never at the top level.
    """
    if not isinstance(body, dict):
        return {}
    data = body.get("data")
    return data if isinstance(data, dict) else {}


class SupervisorDiscovery:
    """Manages Supervisor Discovery entries for simulated panels."""

    def __init__(self, advertise_address: str | None = None) -> None:
        self._token = os.environ.get("SUPERVISOR_TOKEN")
        self._advertise_address = advertise_address
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
                body = await resp.json()

            entries = _payload(body).get("discovery", [])
            if not isinstance(entries, list):
                return
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

    async def register_panel(self, serial: str, port: int, https_port: int = 443) -> None:
        """Register a panel with the Supervisor Discovery API.

        The host registered is the advertised address when one is known. That is
        the address the panel's certificate names, and the integration's
        Supervisor path rewrites an existing entry's host to whatever is
        registered here -- deliberately and without the guard its other
        discovery routes apply, because an add-on legitimately reallocates its
        own ports. So registering anything the leaf does not name silently moves
        a working entry to an address that cannot pass verification.

        The container hostname is the fallback rather than the rule. Under
        ``host_network: true`` it is only a per-add-on alias for the host, so it
        is absent from the other emulator's certificate and differs between the
        two -- which breaks the simulator-to-panelbench upgrade rehearsal, where
        the panel is supposed to keep its identity across the swap. The
        advertised address is what both emulators share.

        No-ops if not in add-on mode.

        ``https_port`` is published alongside the HTTP one because a consumer
        that pins the authority this panel serves then has to reach the leaf
        that authority signed, and this process is the only party that knows
        where: the port is allocated per panel and reallocated across restarts.
        Omitting it leaves the consumer to assume 443 and find nothing there.
        """
        if not self._token:
            return

        host = self._advertise_address or _container_hostname()
        payload = {
            "service": _SERVICE_NAME,
            "config": {
                "host": host,
                "port": port,
                "https_port": https_port,
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
                    body = await resp.json()
                    uuid = _payload(body).get("uuid")
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
                            body,
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
