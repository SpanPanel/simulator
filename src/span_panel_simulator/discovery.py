"""mDNS advertisement for simulated SPAN panels.

Each panel is advertised as ``_ebus._tcp.local.`` so that the HA
integration (and other eBus clients) discover it via zeroconf, just
like real hardware.
"""

from __future__ import annotations

import logging
import socket

from zeroconf import IPVersion, ServiceInfo
from zeroconf.asyncio import AsyncZeroconf

_LOGGER = logging.getLogger(__name__)

# mDNS service types the integration listens for (manifest.json)
SERVICE_TYPE_EBUS = "_ebus._tcp.local."
SERVICE_TYPE_SPAN = "_span._tcp.local."


def _get_host_addresses(advertise_address: str | None = None) -> list[str]:
    """Return IPv4 addresses to advertise via mDNS.

    Args:
        advertise_address: Explicit address to advertise.  When running
            inside a VM (e.g. Colima) the auto-detected addresses may be
            internal to the VM; set this to the VM's routable IP so that
            clients on the host network can reach the simulator.
    """
    if advertise_address:
        return [advertise_address]

    addrs: list[str] = []
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            addr = str(info[4][0])
            if not addr.startswith("127."):
                addrs.append(addr)
    except OSError:
        pass
    if not addrs:
        addrs.append("127.0.0.1")
    return addrs


class PanelAdvertiser:
    """Manages mDNS advertisements for simulated panels."""

    def __init__(
        self,
        http_port: int = 443,
        advertise_address: str | None = None,
    ) -> None:
        self._http_port = http_port
        self._advertise_address = advertise_address
        self._zeroconf: AsyncZeroconf | None = None
        self._services: dict[str, list[ServiceInfo]] = {}  # serial → [ServiceInfo]

    async def start(self) -> None:
        """Start the zeroconf responder."""
        self._zeroconf = AsyncZeroconf(ip_version=IPVersion.V4Only)
        _LOGGER.info("mDNS advertiser started")

    async def stop(self) -> None:
        """Unregister all services and shut down."""
        if self._zeroconf is None:
            return

        for serial in list(self._services):
            await self.unregister_panel(serial)

        await self._zeroconf.async_close()
        self._zeroconf = None
        _LOGGER.info("mDNS advertiser stopped")

    async def register_panel(self, serial: str, firmware: str, *, model: str = "MAIN_32") -> None:
        """Advertise a panel on the local network.

        Registers two service types per panel:
          - ``_ebus._tcp.local.``  — v2 eBus discovery (Homie TXT format)
          - ``_span._tcp.local.``  — generic SPAN discovery
        """
        if self._zeroconf is None:
            return

        addresses = _get_host_addresses(self._advertise_address)
        parsed_addrs = [socket.inet_aton(a) for a in addresses]

        # Derive a hostname matching the real panel naming convention
        hostname = f"span-sim-{serial}"

        # eBus TXT properties match real panel format (homie_domain, etc.)
        ebus_properties: dict[str, str] = {
            "homie_domain": "ebus",
            "homie_version": "5",
            "homie_roles": "device",
            "mqtt_broker": hostname,
            "txtvers": "1",
        }

        # Include httpPort when serving on a non-standard port so that
        # the HA integration discovers the correct HTTP bootstrap address
        if self._http_port != 80:
            ebus_properties["httpPort"] = str(self._http_port)

        # _span._tcp properties
        span_properties: dict[str, str] = {
            "serialNumber": serial,
            "firmwareVersion": firmware,
            "model": model,
        }

        services: list[ServiceInfo] = []
        for svc_type, props, srv_port in (
            # Real panels advertise SRV port 0 on _ebus._tcp (the port
            # isn't used for service connection — MQTT broker details come
            # from the /api/v2/auth/register HTTP response instead)
            (SERVICE_TYPE_EBUS, ebus_properties, 0),
            (SERVICE_TYPE_SPAN, span_properties, self._http_port),
        ):
            name = f"SPAN-{serial}.{svc_type}"
            info = ServiceInfo(
                type_=svc_type,
                name=name,
                addresses=parsed_addrs,
                port=srv_port,
                properties=props,
                server=f"{hostname}.local.",
            )
            try:
                await self._zeroconf.async_register_service(info)
            except Exception:
                _LOGGER.warning(
                    "mDNS registration failed for %s (name conflict?) — "
                    "panel will still work via direct IP",
                    name,
                )
                continue
            services.append(info)

        self._services[serial] = services
        _LOGGER.info(
            "Advertised panel %s on %s (ebus SRV port 0, HTTP port %d)",
            serial,
            ", ".join(addresses),
            self._http_port,
        )

    async def unregister_panel(self, serial: str) -> None:
        """Remove mDNS advertisements for a panel."""
        if self._zeroconf is None:
            return

        services = self._services.pop(serial, [])
        for info in services:
            await self._zeroconf.async_unregister_service(info)

        if services:
            _LOGGER.info("Removed mDNS advertisement for panel %s", serial)
