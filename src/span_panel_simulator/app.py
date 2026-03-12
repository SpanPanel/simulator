"""SimulatorApp — top-level orchestrator.

Loads YAML config, initialises the simulation engine, connects the
HomiePublisher to an MQTT broker, starts the bootstrap HTTP server,
and runs the simulation tick loop.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

import aiomqtt

from span_panel_simulator.bootstrap import BootstrapHttpServer
from span_panel_simulator.certs import CertificateBundle, generate_certificates
from span_panel_simulator.const import (
    DEFAULT_BROKER_PASSWORD,
    DEFAULT_BROKER_USERNAME,
    DEFAULT_FIRMWARE_VERSION,
    DEFAULT_TICK_INTERVAL_S,
    HTTPS_PORT,
    MQTTS_PORT,
)
from span_panel_simulator.engine import DynamicSimulationEngine
from span_panel_simulator.publisher import HomiePublisher

if TYPE_CHECKING:
    from span_panel_simulator.models import SpanPanelSnapshot

_LOGGER = logging.getLogger(__name__)


def _find_homie_schema() -> Path:
    """Locate the bundled homie_schema.json."""
    import importlib.resources

    ref = importlib.resources.files("span_panel_simulator") / "data" / "homie_schema.json"
    path = Path(str(ref))
    if not path.exists():
        msg = f"Bundled homie_schema.json not found at {path}"
        raise FileNotFoundError(msg)
    return path


class SimulatorApp:
    """Orchestrates the full eBus simulator lifecycle."""

    def __init__(
        self,
        config_path: Path,
        *,
        serial_override: str | None = None,
        tick_interval: float = DEFAULT_TICK_INTERVAL_S,
        firmware_version: str = DEFAULT_FIRMWARE_VERSION,
        broker_username: str = DEFAULT_BROKER_USERNAME,
        broker_password: str = DEFAULT_BROKER_PASSWORD,
        broker_host: str = "localhost",
        broker_port: int = MQTTS_PORT,
        http_port: int = HTTPS_PORT,
        cert_dir: Path | None = None,
        homie_schema_path: Path | None = None,
    ) -> None:
        self._config_path = config_path
        self._serial_override = serial_override
        self._tick_interval = tick_interval
        self._firmware = firmware_version
        self._broker_username = broker_username
        self._broker_password = broker_password
        self._broker_host = broker_host
        self._broker_port = broker_port
        self._http_port = http_port
        self._cert_dir = cert_dir or Path("/tmp/span-sim-certs")
        self._homie_schema_path = homie_schema_path

        self._engine: DynamicSimulationEngine | None = None
        self._publisher: HomiePublisher | None = None
        self._http_server: BootstrapHttpServer | None = None
        self._certs: CertificateBundle | None = None
        self._running = False
        self._mqtt_client: aiomqtt.Client | None = None

    async def _init_engine(self) -> DynamicSimulationEngine:
        """Create and initialise the simulation engine."""
        engine = DynamicSimulationEngine(
            serial_number=self._serial_override,
            config_path=self._config_path,
        )
        await engine.initialize_async()
        _LOGGER.info("Simulation engine initialised (serial=%s)", engine.serial_number)
        return engine

    async def _publish(self, topic: str, payload: str, retain: bool) -> None:
        """Publish callback passed to HomiePublisher."""
        assert self._mqtt_client is not None
        await self._mqtt_client.publish(topic, payload, retain=retain)

    async def _handle_set_messages(self) -> None:
        """Subscribe to /set topics and feed changes back to the engine."""
        assert self._mqtt_client is not None
        assert self._publisher is not None
        assert self._engine is not None

        for topic in self._publisher.get_set_topics():
            await self._mqtt_client.subscribe(topic)
            _LOGGER.debug("Subscribed to %s", topic)

        async for message in self._mqtt_client.messages:
            topic_str = str(message.topic)
            payload_str = (
                message.payload.decode("utf-8")
                if isinstance(message.payload, (bytes, bytearray))
                else str(message.payload)
            )

            parsed = self._publisher.resolve_set_message(topic_str)
            if parsed is None:
                continue

            target_type, circuit_id, prop = parsed
            _LOGGER.info("Set command: %s/%s = %s", target_type, prop, payload_str)

            if target_type == "circuit" and prop == "relay":
                self._engine.set_dynamic_overrides(
                    circuit_overrides={circuit_id: {"relay_state": payload_str}}
                )
            elif target_type == "circuit" and prop == "shed-priority":
                self._engine.set_dynamic_overrides(
                    circuit_overrides={circuit_id: {"priority": payload_str}}
                )

    async def _tick_loop(self) -> None:
        """Run the simulation tick loop."""
        assert self._engine is not None
        assert self._publisher is not None

        # Initial full publish
        snapshot: SpanPanelSnapshot = await self._engine.get_snapshot()
        await self._publisher.publish_init(snapshot)

        _LOGGER.info("Entering tick loop (interval=%.1fs)", self._tick_interval)
        while self._running:
            await asyncio.sleep(self._tick_interval)
            snapshot = await self._engine.get_snapshot()
            await self._publisher.publish_diff(snapshot)

    async def run(self) -> None:
        """Run the full simulator lifecycle."""
        # 1. Generate TLS certificates
        certs = generate_certificates(self._cert_dir)
        self._certs = certs

        # 2. Initialise simulation engine
        engine = await self._init_engine()
        self._engine = engine
        serial = engine.serial_number

        # 3. Resolve homie schema
        schema_path = self._homie_schema_path or _find_homie_schema()

        # 4. Start bootstrap HTTP server
        http_server = BootstrapHttpServer(
            serial_number=serial,
            certs=certs,
            homie_schema_path=schema_path,
            firmware_version=self._firmware,
            broker_username=self._broker_username,
            broker_password=self._broker_password,
            port=self._http_port,
        )
        self._http_server = http_server
        await http_server.start()

        # 5. Connect to MQTT broker and run
        self._running = True
        try:
            async with aiomqtt.Client(
                hostname=self._broker_host,
                port=self._broker_port,
                username=self._broker_username,
                password=self._broker_password,
                tls_params=aiomqtt.TLSParameters(
                    ca_certs=str(certs.ca_cert_path),
                ),
            ) as client:
                self._mqtt_client = client
                self._publisher = HomiePublisher(
                    serial_number=serial,
                    publish_fn=self._publish,
                )

                # Run tick loop and /set handler concurrently
                async with asyncio.TaskGroup() as tg:
                    tg.create_task(self._tick_loop())
                    tg.create_task(self._handle_set_messages())

        finally:
            self._running = False
            if self._http_server is not None:
                await self._http_server.stop()
            _LOGGER.info("Simulator shut down")

    async def stop(self) -> None:
        """Signal the simulator to stop."""
        self._running = False
