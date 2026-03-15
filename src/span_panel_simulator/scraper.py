"""eBus scraper — connect to a real SPAN panel and collect retained topics.

Performs the authentication handshake via the panel's v2 REST API, connects
to its MQTTS broker, subscribes to ``ebus/5/{serial}/#``, and collects all
retained messages until the topic stream stabilises (no new topics for a
configurable window).

This module is intentionally self-contained: it does not import span-panel-api
or any HA integration code.  It only uses ``aiohttp`` (HTTP client) and
``aiomqtt`` (MQTT client), both already project dependencies.
"""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp
import aiomqtt

_LOGGER = logging.getLogger(__name__)

# Timeouts
_STABILITY_TIMEOUT_S = 5.0
_MAX_SCRAPE_TIMEOUT_S = 30.0
_HTTP_TIMEOUT_S = 15.0

# Status callback type: async (phase, detail) -> None
StatusCallback = Callable[[str, str], Awaitable[None]]


class ScrapeError(Exception):
    """Raised when the scrape pipeline encounters a recoverable error."""

    def __init__(self, phase: str, message: str) -> None:
        self.phase = phase
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class PanelCredentials:
    """MQTT credentials and identity returned by the panel's /register endpoint."""

    username: str
    password: str
    serial_number: str
    mqtts_port: int
    broker_host: str


@dataclass(frozen=True, slots=True)
class ScrapedPanel:
    """Result of a successful eBus scrape."""

    serial_number: str
    description: dict[str, dict[str, dict[str, str]]]
    properties: dict[str, str]
    mqtts_port: int
    ca_pem: bytes = field(repr=False)


async def register_with_panel(
    host: str,
    passphrase: str | None,
) -> tuple[PanelCredentials, bytes]:
    """Authenticate with a real SPAN panel and retrieve MQTT credentials.

    Args:
        host: IP or hostname of the panel.
        passphrase: Panel passphrase (None for door-bypass).

    Returns:
        A tuple of (PanelCredentials, ca_pem_bytes).

    Raises:
        ScrapeError: On network or authentication failure.
    """
    register_url = f"http://{host}/api/v2/auth/register"
    ca_url = f"http://{host}/api/v2/certificate/ca"
    client_name = f"sim-clone-{uuid.uuid4()}"

    body: dict[str, str] = {"name": client_name}
    if passphrase is not None:
        body["hopPassphrase"] = passphrase

    timeout = aiohttp.ClientTimeout(total=_HTTP_TIMEOUT_S)

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            # Step 1: Register for MQTT credentials
            async with session.post(register_url, json=body) as resp:
                if resp.status in (401, 403):
                    raise ScrapeError("registering", "Bad passphrase or access denied")
                resp.raise_for_status()
                data = await resp.json()

            creds = PanelCredentials(
                username=data["ebusBrokerUsername"],
                password=data["ebusBrokerPassword"],
                serial_number=data["serialNumber"],
                mqtts_port=int(data["ebusBrokerMqttsPort"]),
                broker_host=data.get("ebusBrokerHost", host),
            )

            # Step 2: Fetch CA certificate for TLS trust
            async with session.get(ca_url) as resp:
                resp.raise_for_status()
                ca_pem = await resp.read()

    except ScrapeError:
        raise
    except aiohttp.ClientError as exc:
        raise ScrapeError("registering", f"Panel unreachable: {exc}") from exc

    _LOGGER.info(
        "Registered with panel %s (serial=%s, mqtts_port=%d)",
        host,
        creds.serial_number,
        creds.mqtts_port,
    )
    return creds, ca_pem


async def scrape_ebus(
    creds: PanelCredentials,
    ca_pem: bytes,
    *,
    status_callback: StatusCallback | None = None,
    stability_timeout: float = _STABILITY_TIMEOUT_S,
    max_timeout: float = _MAX_SCRAPE_TIMEOUT_S,
) -> ScrapedPanel:
    """Connect to a panel's MQTTS broker and collect all retained eBus topics.

    Args:
        creds: MQTT credentials from ``register_with_panel``.
        ca_pem: PEM-encoded CA certificate for the panel's broker.
        status_callback: Optional async callback for progress updates.
        stability_timeout: Seconds of silence before declaring scrape complete.
        max_timeout: Maximum total scrape duration.

    Returns:
        ScrapedPanel with the collected data.

    Raises:
        ScrapeError: On connection failure or missing required topics.
    """
    # Write CA PEM to a temp file for aiomqtt's TLS parameters
    with tempfile.NamedTemporaryFile(suffix=".pem", delete=False) as ca_file:
        ca_file.write(ca_pem)
    ca_path = Path(ca_file.name)
    try:
        collected, description = await _collect_retained_messages(
            creds,
            ca_path,
            stability_timeout=stability_timeout,
            max_timeout=max_timeout,
            status_callback=status_callback,
        )
    finally:
        ca_path.unlink(missing_ok=True)

    _validate_required_topics(collected, description, creds.serial_number)

    _LOGGER.info(
        "Scrape complete: %d topics collected for panel %s",
        len(collected),
        creds.serial_number,
    )

    return ScrapedPanel(
        serial_number=creds.serial_number,
        description=description,
        properties=collected,
        mqtts_port=creds.mqtts_port,
        ca_pem=ca_pem,
    )


async def _collect_retained_messages(
    creds: PanelCredentials,
    ca_cert_path: Path,
    *,
    stability_timeout: float,
    max_timeout: float,
    status_callback: StatusCallback | None,
) -> tuple[dict[str, str], dict[str, dict[str, dict[str, str]]]]:
    """Subscribe to eBus topics and collect until the stream stabilises."""
    subscribe_topic = f"ebus/5/{creds.serial_number}/#"
    collected: dict[str, str] = {}
    description: dict[str, dict[str, dict[str, str]]] = {}

    if status_callback:
        await status_callback("connecting", f"MQTTS to {creds.broker_host}:{creds.mqtts_port}")

    try:
        async with aiomqtt.Client(
            hostname=creds.broker_host,
            port=creds.mqtts_port,
            username=creds.username,
            password=creds.password,
            tls_params=aiomqtt.TLSParameters(ca_certs=str(ca_cert_path)),
        ) as client:
            await client.subscribe(subscribe_topic)

            if status_callback:
                await status_callback(
                    "scraping",
                    f"Subscribed to {subscribe_topic}, collecting retained messages",
                )

            loop = asyncio.get_running_loop()
            last_new_topic_time = loop.time()
            deadline = loop.time() + max_timeout
            generator = client.messages.__aiter__()

            while True:
                now = loop.time()
                remaining_stability = stability_timeout - (now - last_new_topic_time)
                remaining_deadline = deadline - now

                wait_time = min(remaining_stability, remaining_deadline)
                if wait_time <= 0:
                    break

                try:
                    message = await asyncio.wait_for(
                        generator.__anext__(),
                        timeout=wait_time,
                    )
                except (StopAsyncIteration, TimeoutError):
                    break

                topic_str = str(message.topic)
                payload = (
                    message.payload.decode("utf-8")
                    if isinstance(message.payload, bytes | bytearray)
                    else str(message.payload)
                )

                is_new = topic_str not in collected
                collected[topic_str] = payload

                if topic_str.endswith("/$description"):
                    description = json.loads(payload)

                if is_new:
                    last_new_topic_time = loop.time()

    except aiomqtt.MqttError as exc:
        raise ScrapeError("connecting", f"MQTTS connection failed: {exc}") from exc

    return collected, description


def _validate_required_topics(
    collected: dict[str, str],
    description: dict[str, dict[str, dict[str, str]]],
    serial: str,
) -> None:
    """Ensure the minimum required topics were received."""
    prefix = f"ebus/5/{serial}"

    if not description:
        raise ScrapeError("scraping", "No $description received from panel")

    state_topic = f"{prefix}/$state"
    if state_topic not in collected:
        raise ScrapeError("scraping", "No $state topic received from panel")

    serial_topic = f"{prefix}/core/serial-number"
    if serial_topic not in collected:
        raise ScrapeError("scraping", "No core/serial-number topic received from panel")

    # At least one circuit node should exist in the description
    nodes = description.get("nodes", {})
    circuit_count = sum(
        1
        for n in nodes.values()
        if isinstance(n, dict) and n.get("type") == "energy.ebus.device.circuit"
    )
    if circuit_count == 0:
        raise ScrapeError("scraping", "No circuit nodes found in $description")

    _LOGGER.debug(
        "Validation passed: %d topics, %d circuit nodes for %s",
        len(collected),
        circuit_count,
        serial,
    )
