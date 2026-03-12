"""SimulatorApp — multi-panel orchestrator.

Scans a configuration directory for YAML files, creates a PanelInstance
per file, and manages their lifecycle through a shared MQTT connection.
Supports on-demand reload: re-scans configs, starts new panels, stops
removed panels, and restarts panels whose configs have changed.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING

import aiomqtt

from span_panel_simulator.bootstrap import BootstrapHttpServer
from span_panel_simulator.certs import generate_certificates
from span_panel_simulator.discovery import PanelAdvertiser
from span_panel_simulator.const import (
    DEFAULT_BROKER_PASSWORD,
    DEFAULT_BROKER_USERNAME,
    DEFAULT_FIRMWARE_VERSION,
    DEFAULT_TICK_INTERVAL_S,
    HTTPS_PORT,
    MQTTS_PORT,
)
from span_panel_simulator.panel import PanelInstance

if TYPE_CHECKING:
    from span_panel_simulator.certs import CertificateBundle

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


def _file_hash(path: Path) -> str:
    """Return a hex digest of a file's contents for change detection."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _discover_configs(config_dir: Path) -> dict[Path, str]:
    """Scan a directory for YAML config files and return path → content hash."""
    configs: dict[Path, str] = {}
    for pattern in ("*.yaml", "*.yml"):
        for path in sorted(config_dir.glob(pattern)):
            configs[path] = _file_hash(path)
    return configs


class SimulatorApp:
    """Orchestrates multiple simulated panels from a config directory.

    Each ``.yaml`` file in the config directory becomes an independent
    panel with its own serial number and MQTT topic namespace.  All
    panels share a single MQTT broker connection and bootstrap HTTP
    server.

    Call ``reload()`` at any time to re-scan the directory: new configs
    are started, removed configs are stopped, and changed configs are
    restarted.
    """

    def __init__(
        self,
        config_dir: Path,
        *,
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
        self._config_dir = config_dir
        self._tick_interval = tick_interval
        self._firmware = firmware_version
        self._broker_username = broker_username
        self._broker_password = broker_password
        self._broker_host = broker_host
        self._broker_port = broker_port
        self._http_port = http_port
        self._cert_dir = cert_dir or Path("/tmp/span-sim-certs")
        self._homie_schema_path = homie_schema_path

        # Tracked state
        self._panels: dict[Path, PanelInstance] = {}
        self._config_hashes: dict[Path, str] = {}
        self._serial_to_panel: dict[str, PanelInstance] = {}
        self._http_server: BootstrapHttpServer | None = None
        self._advertiser: PanelAdvertiser | None = None
        self._certs: CertificateBundle | None = None
        self._running = False
        self._mqtt_client: aiomqtt.Client | None = None
        self._reload_event: asyncio.Event = asyncio.Event()

    # ------------------------------------------------------------------
    # MQTT publish callback (shared across all panels)
    # ------------------------------------------------------------------

    async def _publish(self, topic: str, payload: str, retain: bool) -> None:
        assert self._mqtt_client is not None
        await self._mqtt_client.publish(topic, payload, retain=retain)

    # ------------------------------------------------------------------
    # Panel lifecycle
    # ------------------------------------------------------------------

    async def _start_panel(self, config_path: Path) -> PanelInstance:
        """Create, initialise, and register a panel from a config file."""
        panel = PanelInstance(
            config_path=config_path,
            publish_fn=self._publish,
            tick_interval=self._tick_interval,
        )
        serial = await panel.start()

        self._panels[config_path] = panel
        self._serial_to_panel[serial] = panel

        # Update the HTTP server and mDNS registries
        if self._http_server is not None:
            self._http_server.register_panel(serial, self._firmware)
        if self._advertiser is not None:
            await self._advertiser.register_panel(serial, self._firmware)

        _LOGGER.info("Registered panel %s from %s", serial, config_path.name)
        return panel

    async def _stop_panel(self, config_path: Path) -> None:
        """Stop and unregister a panel."""
        panel = self._panels.pop(config_path, None)
        if panel is None:
            return

        serial = panel.serial_number if panel.is_running else "unknown"
        await panel.stop()

        self._serial_to_panel.pop(serial, None)
        if self._http_server is not None:
            self._http_server.unregister_panel(serial)
        if self._advertiser is not None:
            await self._advertiser.unregister_panel(serial)

        _LOGGER.info("Unregistered panel %s", serial)

    async def reload(self) -> dict[str, list[str]]:
        """Re-scan config directory and reconcile running panels.

        Returns a summary of what changed::

            {"started": [...], "stopped": [...], "reloaded": [...]}
        """
        current = _discover_configs(self._config_dir)
        prev = self._config_hashes

        to_start = set(current) - set(prev)
        to_stop = set(prev) - set(current)
        to_check = set(current) & set(prev)
        to_reload = {p for p in to_check if current[p] != prev[p]}

        result: dict[str, list[str]] = {"started": [], "stopped": [], "reloaded": []}

        # Stop removed panels
        for path in to_stop:
            panel = self._panels.get(path)
            serial = panel.serial_number if panel and panel.is_running else path.stem
            await self._stop_panel(path)
            result["stopped"].append(serial)

        # Reload changed panels
        for path in to_reload:
            panel = self._panels.get(path)
            if panel is not None:
                await self._stop_panel(path)
                new_panel = await self._start_panel(path)
                result["reloaded"].append(new_panel.serial_number)
            else:
                new_panel = await self._start_panel(path)
                result["started"].append(new_panel.serial_number)

        # Start new panels
        for path in to_start:
            panel = await self._start_panel(path)
            result["started"].append(panel.serial_number)

        self._config_hashes = current

        _LOGGER.info(
            "Reload complete: started=%d, stopped=%d, reloaded=%d",
            len(result["started"]),
            len(result["stopped"]),
            len(result["reloaded"]),
        )
        return result

    def request_reload(self) -> None:
        """Signal the main loop to perform a reload on the next iteration."""
        self._reload_event.set()

    # ------------------------------------------------------------------
    # /set message routing
    # ------------------------------------------------------------------

    async def _handle_set_messages(self) -> None:
        """Subscribe to /set topics for all panels and route to the correct engine."""
        assert self._mqtt_client is not None

        # Subscribe to wildcard for all panel serials
        for panel in self._panels.values():
            if panel.publisher is not None:
                for topic in panel.publisher.get_set_topics():
                    await self._mqtt_client.subscribe(topic)

        async for message in self._mqtt_client.messages:
            topic_str = str(message.topic)
            payload_str = (
                message.payload.decode("utf-8")
                if isinstance(message.payload, (bytes, bytearray))
                else str(message.payload)
            )

            # Route to the correct panel by trying each publisher
            for panel in self._panels.values():
                if panel.publisher is None or panel.engine is None:
                    continue
                parsed = panel.publisher.resolve_set_message(topic_str)
                if parsed is None:
                    continue

                target_type, circuit_id, prop = parsed
                _LOGGER.info(
                    "Set command [%s]: %s/%s = %s",
                    panel.serial_number, target_type, prop, payload_str,
                )

                if target_type == "circuit" and prop == "relay":
                    panel.engine.set_dynamic_overrides(
                        circuit_overrides={circuit_id: {"relay_state": payload_str}}
                    )
                elif target_type == "circuit" and prop == "shed-priority":
                    panel.engine.set_dynamic_overrides(
                        circuit_overrides={circuit_id: {"priority": payload_str}}
                    )
                break  # Only one panel should match

    # ------------------------------------------------------------------
    # Reload watcher
    # ------------------------------------------------------------------

    async def _reload_watcher(self) -> None:
        """Wait for reload signals and execute them."""
        while self._running:
            await self._reload_event.wait()
            self._reload_event.clear()
            try:
                await self.reload()
                # Re-subscribe for any new panels' /set topics
                if self._mqtt_client is not None:
                    for panel in self._panels.values():
                        if panel.publisher is not None:
                            for topic in panel.publisher.get_set_topics():
                                await self._mqtt_client.subscribe(topic)
            except Exception:
                _LOGGER.exception("Reload failed")

    # ------------------------------------------------------------------
    # Main lifecycle
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Run the full simulator lifecycle."""
        # 1. Generate TLS certificates
        certs = generate_certificates(self._cert_dir)
        self._certs = certs

        # 2. Resolve homie schema
        schema_path = self._homie_schema_path or _find_homie_schema()

        # 3. Start bootstrap HTTP server (multi-panel aware)
        http_server = BootstrapHttpServer(
            certs=certs,
            homie_schema_path=schema_path,
            broker_username=self._broker_username,
            broker_password=self._broker_password,
            broker_host=self._broker_host,
            port=self._http_port,
            reload_callback=self.request_reload,
        )
        self._http_server = http_server
        await http_server.start()

        # 4. Start mDNS advertiser
        advertiser = PanelAdvertiser(http_port=self._http_port)
        self._advertiser = advertiser
        await advertiser.start()

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

                # 6. Initial config scan — start all panels
                await self.reload()

                # 7. Run /set handler and reload watcher concurrently
                async with asyncio.TaskGroup() as tg:
                    tg.create_task(self._handle_set_messages())
                    tg.create_task(self._reload_watcher())

        finally:
            self._running = False
            # Stop all panels
            for path in list(self._panels):
                await self._stop_panel(path)
            if self._advertiser is not None:
                await self._advertiser.stop()
            if self._http_server is not None:
                await self._http_server.stop()
            _LOGGER.info("Simulator shut down")

    async def stop(self) -> None:
        """Signal the simulator to stop."""
        self._running = False
