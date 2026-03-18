"""mDNS advertisement and discovery for SPAN panels.

Advertisement
    Each simulated panel is advertised as ``_ebus._tcp.local.`` so that
    the HA integration (and other eBus clients) discover it via
    zeroconf, just like real hardware.

Discovery
    ``PanelBrowser`` listens for ``_span._tcp.local.`` on the LAN and
    maintains a list of discovered real panels.  Used by the dashboard
    clone form when Home Assistant is not available (standalone mode).
"""

from __future__ import annotations

import logging
import socket
from dataclasses import dataclass
from threading import Lock

from zeroconf import IPVersion, ServiceBrowser, ServiceInfo, ServiceStateChange, Zeroconf
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


# ------------------------------------------------------------------
# Panel discovery (browser)
# ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DiscoveredPanel:
    """A SPAN panel found via mDNS."""

    serial: str
    host: str
    model: str
    firmware: str


class PanelBrowser:
    """Listens for ``_span._tcp.local.`` on the LAN.

    Maintains a thread-safe set of discovered panels that the dashboard
    can query at any time.  Simulated panels (serial starting with
    ``sim-``) are excluded so the clone form only shows real hardware.
    """

    def __init__(self) -> None:
        self._zeroconf: AsyncZeroconf | None = None
        self._browser: ServiceBrowser | None = None
        self._panels: dict[str, DiscoveredPanel] = {}
        self._lock = Lock()

    async def start(self) -> None:
        """Start the zeroconf browser."""
        self._zeroconf = AsyncZeroconf(ip_version=IPVersion.V4Only)
        self._browser = ServiceBrowser(
            self._zeroconf.zeroconf,
            SERVICE_TYPE_SPAN,
            handlers=[self._on_state_change],
        )
        _LOGGER.info("mDNS panel browser started (listening for %s)", SERVICE_TYPE_SPAN)

    async def stop(self) -> None:
        """Shut down the browser."""
        if self._browser is not None:
            self._browser.cancel()
            self._browser = None
        if self._zeroconf is not None:
            await self._zeroconf.async_close()
            self._zeroconf = None
        with self._lock:
            self._panels.clear()
        _LOGGER.info("mDNS panel browser stopped")

    @property
    def panels(self) -> list[DiscoveredPanel]:
        """Return a snapshot of currently discovered panels."""
        with self._lock:
            return list(self._panels.values())

    def _on_state_change(
        self,
        zeroconf: Zeroconf,
        service_type: str,
        name: str,
        state_change: ServiceStateChange,
    ) -> None:
        """Callback invoked by the ServiceBrowser on the zeroconf thread."""
        if state_change == ServiceStateChange.Removed:
            with self._lock:
                self._panels.pop(name, None)
            _LOGGER.debug("mDNS: panel removed %s", name)
            return

        info = zeroconf.get_service_info(service_type, name)
        if info is None:
            return

        props = {
            k.decode() if isinstance(k, bytes) else k: v.decode() if isinstance(v, bytes) else v
            for k, v in (info.properties or {}).items()
        }

        serial = props.get("serialNumber", "")
        if not serial or serial.lower().startswith("sim-"):
            return  # skip simulated panels

        addresses = info.parsed_scoped_addresses()
        host = addresses[0] if addresses else ""
        if not host:
            return

        panel = DiscoveredPanel(
            serial=serial,
            host=host,
            model=str(props.get("model", "")),
            firmware=str(props.get("firmwareVersion", "")),
        )

        with self._lock:
            self._panels[name] = panel
        _LOGGER.info("mDNS: discovered panel %s at %s", serial, host)
