"""Tests for the mDNS TXT records a panel advertises.

The ports are the point. A consumer that finds this panel over mDNS has to
learn where to reach it from the record alone, and both ports move: they are
allocated per panel and reallocated across restarts, so anything the consumer
assumes will be wrong as soon as a second panel exists.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from span_panel_simulator.const import DEFAULT_FIRMWARE_VERSION
from span_panel_simulator.discovery import SERVICE_TYPE_EBUS, PanelAdvertiser


@pytest.fixture
def advertiser() -> PanelAdvertiser:
    """An advertiser with a stubbed zeroconf, so nothing touches the network."""
    adv = PanelAdvertiser(advertise_address="127.0.0.1")
    adv._zeroconf = AsyncMock()
    return adv


async def _ebus_properties(advertiser: PanelAdvertiser, **kwargs: int) -> dict[bytes, bytes]:
    """Register a panel and return the TXT properties of its _ebus._tcp record."""
    with patch(
        "span_panel_simulator.discovery._get_host_addresses",
        return_value=["127.0.0.1"],
    ):
        await advertiser.register_panel("sim-001", DEFAULT_FIRMWARE_VERSION, **kwargs)

    for info in advertiser._services["sim-001"]:
        if info.type == SERVICE_TYPE_EBUS:
            return info.properties
    raise AssertionError("no _ebus._tcp record was registered")


async def test_non_standard_ports_are_both_published(advertiser: PanelAdvertiser) -> None:
    """A panel on offset ports advertises both of them."""
    props = await _ebus_properties(advertiser, port=8081, https_port=8443)

    assert props[b"httpPort"] == b"8081"
    assert props[b"httpsPort"] == b"8443"


async def test_standard_ports_are_left_unsaid(advertiser: PanelAdvertiser) -> None:
    """A panel on 80/443 publishes neither port.

    Silence is how a consumer is told the panel is where it would have looked
    anyway, and it is what real hardware does. Emitting the defaults would put
    values in the record that mean nothing.
    """
    props = await _ebus_properties(advertiser, port=80, https_port=443)

    assert b"httpPort" not in props
    assert b"httpsPort" not in props
