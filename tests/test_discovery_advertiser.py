"""Tests for the mDNS TXT records a panel advertises.

The ports are the point. A consumer that finds this panel over mDNS has to
learn where to reach it from the record alone, and both ports move: they are
allocated per panel and reallocated across restarts, so anything the consumer
assumes will be wrong as soon as a second panel exists.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from span_panel_simulator.const import (
    DEFAULT_FIRMWARE_VERSION,
    HTTPS_PORT_OFFSET,
    https_port_for,
)
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
    props = await _ebus_properties(advertiser, port=8081, https_port=https_port_for(8081))

    assert props[b"httpPort"] == b"8081"
    assert props[b"httpsPort"] == b"9081"


async def test_standard_ports_are_left_unsaid(advertiser: PanelAdvertiser) -> None:
    """A panel on 80/443 publishes neither port.

    Silence is how a consumer is told the panel is where it would have looked
    anyway, and it is what real hardware does. Emitting the defaults would put
    values in the record that mean nothing.
    """
    props = await _ebus_properties(advertiser, port=80, https_port=443)

    assert b"httpPort" not in props
    assert b"httpsPort" not in props


def test_the_tls_port_offset_matches_panelbench() -> None:
    """The offset is a contract with panelbench, not a local preference.

    Stopping this simulator and starting panelbench rehearses a firmware upgrade
    on one panel, and a panel that moved its TLS port across an upgrade is not a
    panel that was upgraded. Both derive it as http + 1000; changing this number
    on one side alone silently breaks that rehearsal, so it is pinned here
    rather than left to whatever the constant happens to say.
    """
    assert HTTPS_PORT_OFFSET == 1000
    assert https_port_for(8081) == 9081
    assert https_port_for(80) == 1080
