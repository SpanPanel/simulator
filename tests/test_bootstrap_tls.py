"""Tests for the bootstrap server's TLS listener.

These bind real sockets with real certificates rather than driving the app
through ``TestServer``, because what is under test is precisely the part
``TestServer`` bypasses: that the panel actually serves TLS, and that the leaf
it serves is signed by the authority it publishes over plain HTTP.

That pairing is the whole contract a pinning consumer depends on. It fetches
the authority unpinned, checks it validates what the panel serves, and only
then sends the passphrase -- so a panel that publishes an authority it cannot
back with a live leaf fails that consumer at the last step before
registration, which is exactly where the failure is most expensive.
"""

from __future__ import annotations

import socket
import ssl
from pathlib import Path
from unittest.mock import MagicMock

import aiohttp
import pytest

from span_panel_simulator.bootstrap import BootstrapHttpServer
from span_panel_simulator.certs import generate_certificates
from span_panel_simulator.const import DEFAULT_FIRMWARE_VERSION

_SERIAL = "sim-tls-001"


def _free_port() -> int:
    """Return a port that was free a moment ago."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
async def running_server(tmp_path: Path):
    """A started panel server on real ports with real certificates."""
    certs = generate_certificates(tmp_path, advertise_address="127.0.0.1")

    schema = MagicMock()
    schema.raw_json = '{"test": true}'

    server = BootstrapHttpServer(
        serial=_SERIAL,
        firmware=DEFAULT_FIRMWARE_VERSION,
        certs=certs,
        schema=schema,
        host="127.0.0.1",
        port=_free_port(),
        https_port=_free_port(),
    )
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


async def _published_ca(server: BootstrapHttpServer) -> str:
    """Fetch the authority the way an unpinned consumer does — over plain HTTP."""
    url = f"http://127.0.0.1:{server._port}/api/v2/certificate/ca"
    async with aiohttp.ClientSession() as session, session.get(url) as resp:
        assert resp.status == 200
        return (await resp.read()).decode()


def _consumer_context(ca_pem: str) -> ssl.SSLContext:
    """Build the context a pinning consumer actually uses.

    Mirrors ``span_panel_api.build_panel_ssl_context`` rather than calling it —
    the library is not a dependency of the simulator and should not become one
    to run a test. What must be copied exactly is the cleared
    ``VERIFY_X509_STRICT``: real SPAN panels ship a minimal authority with no
    Authority Key Identifier, Python 3.13 turned that flag on by default, and
    the library clears it so those panels keep working.

    Verifying here with the flag left on would test a client that does not
    exist, and would push this simulator towards a strictly RFC-correct
    authority that real hardware does not have — retiring the coverage that
    keeps the library's workaround honest. Everything that does matter stays
    on: the panel's own CA is the only anchor, and the hostname is checked.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = True
    context.verify_flags &= ~ssl.VERIFY_X509_STRICT
    context.load_verify_locations(cadata=ca_pem)
    return context


async def test_tls_leaf_is_signed_by_the_published_authority(
    running_server: BootstrapHttpServer,
) -> None:
    """The HTTPS listener serves a leaf that the published CA validates.

    Verification is left fully on — hostname included — because a consumer
    that relaxed it would not be testing the pin it relies on.
    """
    context = _consumer_context(await _published_ca(running_server))

    url = f"https://127.0.0.1:{running_server._https_port}/api/v2/status"
    async with aiohttp.ClientSession() as session, session.get(url, ssl=context) as resp:
        assert resp.status == 200
        assert (await resp.json())["serialNumber"] == _SERIAL


async def test_tls_listener_is_rejected_without_the_published_authority(
    running_server: BootstrapHttpServer,
) -> None:
    """A client that does not hold the panel's CA cannot verify the panel.

    Guards the inverse of the test above: it would still pass if the listener
    served something publicly trusted, or if verification were quietly off.
    """
    url = f"https://127.0.0.1:{running_server._https_port}/api/v2/status"
    with pytest.raises(aiohttp.ClientConnectorCertificateError):
        async with aiohttp.ClientSession() as session, session.get(url):
            pass


async def test_http_listener_still_serves_the_pre_anchor_probes(
    running_server: BootstrapHttpServer,
) -> None:
    """Status and the CA stay reachable over plain HTTP.

    Both necessarily happen before a consumer holds an anchor — it probes
    status to decide this is a SPAN panel at all, and fetches the CA to get
    the anchor — so moving either behind TLS would strand it.
    """
    base = f"http://127.0.0.1:{running_server._port}"
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{base}/api/v2/status") as resp:
            assert resp.status == 200
            assert (await resp.json())["serialNumber"] == _SERIAL
        async with session.get(f"{base}/api/v2/certificate/ca") as resp:
            assert resp.status == 200
            assert "BEGIN CERTIFICATE" in (await resp.text())


async def test_register_is_available_over_tls(running_server: BootstrapHttpServer) -> None:
    """Registration — the exchange carrying the passphrase — is served over TLS."""
    context = _consumer_context(await _published_ca(running_server))

    url = f"https://127.0.0.1:{running_server._https_port}/api/v2/auth/register"
    async with (
        aiohttp.ClientSession() as session,
        session.post(url, json={"hopPassphrase": "sim-passphrase"}, ssl=context) as resp,
    ):
        assert resp.status == 200
        body = await resp.json()
        assert body["serialNumber"] == _SERIAL
        assert body["ebusBrokerPassword"] == "sim-password"
