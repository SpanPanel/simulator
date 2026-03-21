"""SimulatorApp — multi-panel orchestrator.

Scans a configuration directory for YAML files, creates a PanelInstance
per file, and manages their lifecycle through a shared MQTT connection.
Each panel gets its own bootstrap HTTP server on a unique port, matching
real SPAN hardware where each panel is a separate network device.
Supports on-demand reload: re-scans configs, starts new panels, stops
removed panels, and restarts panels whose configs have changed.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiomqtt
import yaml
from aiohttp import web

from span_panel_simulator.bootstrap import BootstrapHttpServer
from span_panel_simulator.certs import generate_certificates
from span_panel_simulator.const import (
    DASHBOARD_PORT,
    DEFAULT_BASE_HTTP_PORT,
    DEFAULT_BROKER_PASSWORD,
    DEFAULT_BROKER_USERNAME,
    DEFAULT_FIRMWARE_VERSION,
    DEFAULT_TICK_INTERVAL_S,
    MQTTS_PORT,
)
from span_panel_simulator.dashboard import DashboardContext, create_dashboard_app
from span_panel_simulator.discovery import PanelAdvertiser, PanelBrowser
from span_panel_simulator.engine import _PANEL_SIZE_TO_MODEL
from span_panel_simulator.panel import PanelInstance
from span_panel_simulator.recorder import RecorderDataSource
from span_panel_simulator.schema import HomieSchemaRegistry, load_schema

if TYPE_CHECKING:
    from span_panel_simulator.certs import CertificateBundle
    from span_panel_simulator.engine import DynamicSimulationEngine
    from span_panel_simulator.ha_api.client import HAClient, HAConnectionConfig
    from span_panel_simulator.supervisor_discovery import SupervisorDiscovery

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


def _discover_configs(config_dir: Path, config_filter: str | None = None) -> dict[Path, str]:
    """Scan a directory for YAML config files and return path -> content hash.

    When *config_filter* is a filename, only that file is returned.
    When *config_filter* is ``None``, all YAML files are returned.
    When *config_filter* is empty string, nothing is returned (idle startup).
    """
    if config_filter is not None:
        if not config_filter:
            return {}  # Explicit "start nothing"
        path = config_dir / config_filter
        if path.exists():
            return {path: _file_hash(path)}
        return {}

    configs: dict[Path, str] = {}
    for pattern in ("*.yaml", "*.yml"):
        for path in sorted(config_dir.glob(pattern)):
            configs[path] = _file_hash(path)
    return configs


class SimulatorApp:
    """Orchestrates multiple simulated panels from a config directory.

    Each ``.yaml`` file in the config directory becomes an independent
    panel with its own serial number, MQTT topic namespace, and HTTP
    bootstrap server on a unique port.  All panels share a single MQTT
    broker connection.

    Call ``reload()`` at any time to re-scan the directory: new configs
    are started, removed configs are stopped, and changed configs are
    restarted.
    """

    def __init__(
        self,
        config_dir: Path,
        *,
        config_filter: str | None = None,
        tick_interval: float = DEFAULT_TICK_INTERVAL_S,
        firmware_version: str = DEFAULT_FIRMWARE_VERSION,
        broker_username: str = DEFAULT_BROKER_USERNAME,
        broker_password: str = DEFAULT_BROKER_PASSWORD,
        broker_host: str = "localhost",
        broker_port: int = MQTTS_PORT,
        base_http_port: int = DEFAULT_BASE_HTTP_PORT,
        cert_dir: Path | None = None,
        homie_schema_path: Path | None = None,
        dashboard_port: int = DASHBOARD_PORT,
        advertise_address: str | None = None,
        ha_config: HAConnectionConfig | None = None,
    ) -> None:
        self._config_dir = config_dir
        self._config_filter = config_filter
        self._tick_interval = tick_interval
        self._firmware = firmware_version
        self._broker_username = broker_username
        self._broker_password = broker_password
        self._broker_host = broker_host
        self._broker_port = broker_port
        self._base_http_port = base_http_port
        self._cert_dir = cert_dir or Path("/tmp/span-sim-certs")
        self._homie_schema_path = homie_schema_path
        self._dashboard_port = dashboard_port
        self._advertise_address = advertise_address

        # Tracked state
        self._panels: dict[Path, PanelInstance] = {}
        self._config_hashes: dict[Path, str] = {}
        self._serial_to_panel: dict[str, PanelInstance] = {}
        self._stopped_configs: set[str] = set()
        self._panel_servers: dict[str, BootstrapHttpServer] = {}
        self._panel_ports: dict[str, int] = {}
        self._used_ports: set[int] = set()
        self._dashboard_runner: web.AppRunner | None = None
        self._advertiser: PanelAdvertiser | None = None
        self._panel_browser: PanelBrowser | None = None
        self._certs: CertificateBundle | None = None
        self._schema: HomieSchemaRegistry | None = None
        self._running = False
        self._mqtt_client: aiomqtt.Client | None = None
        self._reload_event: asyncio.Event = asyncio.Event()
        self._ha_config = ha_config
        self._ha_client: HAClient | None = None
        self._supervisor_discovery: SupervisorDiscovery | None = None

    # ------------------------------------------------------------------
    # Port allocation
    # ------------------------------------------------------------------

    def _allocate_port(self) -> int:
        """Return the lowest available port from the base."""
        port = self._base_http_port
        while port in self._used_ports:
            port += 1
        self._used_ports.add(port)
        return port

    def _release_port(self, port: int) -> None:
        """Release a port back to the pool."""
        self._used_ports.discard(port)

    # ------------------------------------------------------------------
    # Dashboard helpers
    # ------------------------------------------------------------------

    def _get_panel_configs(self) -> dict[Path, str]:
        """Return a mapping of config path to serial number for running panels."""
        return {path: panel.serial_number for path, panel in self._panels.items()}

    def _get_panel_ports(self) -> dict[str, int]:
        """Return a mapping of serial number to HTTP port for running panels."""
        return dict(self._panel_ports)

    def _get_first_engine(self) -> DynamicSimulationEngine | None:
        """Return the engine of the first running panel, if any."""
        for panel in self._panels.values():
            if panel.engine is not None:
                return panel.engine
        return None

    def _get_power_summary(self) -> dict[str, object] | None:
        """Return current power flows from the first running panel."""
        engine = self._get_first_engine()
        if engine is None:
            return None
        return engine.get_power_summary()

    def _set_simulation_time(self, iso_str: str) -> None:
        """Set the simulation time on the first running panel."""
        engine = self._get_first_engine()
        if engine is not None:
            engine.override_simulation_start_time(iso_str)

    def _set_time_acceleration(self, accel: float) -> None:
        """Set the time acceleration on the first running panel."""
        engine = self._get_first_engine()
        if engine is not None:
            engine.set_time_acceleration(accel)

    def _set_grid_online(self, online: bool) -> None:
        """Set the grid online/offline state on the first running panel."""
        engine = self._get_first_engine()
        if engine is not None:
            engine.set_grid_online(online)

    def _set_grid_islandable(self, islandable: bool) -> None:
        """Set the grid islandable flag on the first running panel."""
        engine = self._get_first_engine()
        if engine is not None:
            engine.set_grid_islandable(islandable)

    def _set_circuit_priority(self, circuit_id: str, priority: str) -> None:
        """Push a circuit priority change to the running engine immediately."""
        engine = self._get_first_engine()
        if engine is not None:
            engine.set_dynamic_overrides(circuit_overrides={circuit_id: {"priority": priority}})

    def _set_circuit_relay(self, circuit_id: str, relay_state: str) -> None:
        """Push a circuit relay change to the running engine immediately."""
        engine = self._get_first_engine()
        if engine is not None:
            engine.set_dynamic_overrides(
                circuit_overrides={circuit_id: {"relay_state": relay_state}}
            )

    async def _get_modeling_data(self, horizon_hours: int) -> dict[str, Any] | None:
        """Compute modeling data from the first running engine."""
        engine = self._get_first_engine()
        if engine is None:
            return None
        return await engine.compute_modeling_data(horizon_hours)

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
        assert self._certs is not None
        assert self._schema is not None

        # Load recorder replay data if HA is connected and config has
        # recorder_entity mappings.  The RecorderDataSource is populated
        # here (outside the engine) so the engine stays backend-agnostic.
        recorder = await self._load_recorder_data(config_path)

        panel = PanelInstance(
            config_path=config_path,
            publish_fn=self._publish,
            tick_interval=self._tick_interval,
            schema=self._schema,
            recorder=recorder,
        )
        serial = await panel.start()

        self._panels[config_path] = panel
        self._serial_to_panel[serial] = panel

        # Derive model from panel tab count
        panel_model = "MAIN_32"
        if panel.engine is not None:
            panel_model = _PANEL_SIZE_TO_MODEL.get(panel.engine.total_tabs, "MAIN_32")

        # Create per-panel bootstrap HTTP server with port allocation
        port = self._allocate_port()
        server = BootstrapHttpServer(
            serial,
            self._firmware,
            self._certs,
            self._schema,
            broker_username=self._broker_username,
            broker_password=self._broker_password,
            broker_host=self._broker_host,
            port=port,
        )
        while True:
            try:
                await server.start()
                break
            except OSError:
                _LOGGER.warning("Port %d in use for panel %s, trying next port", port, serial)
                self._release_port(port)
                port = self._allocate_port()
                server = BootstrapHttpServer(
                    serial,
                    self._firmware,
                    self._certs,
                    self._schema,
                    broker_username=self._broker_username,
                    broker_password=self._broker_password,
                    broker_host=self._broker_host,
                    port=port,
                )

        self._panel_servers[serial] = server
        self._panel_ports[serial] = port

        # Register with mDNS advertiser
        if self._advertiser is not None:
            await self._advertiser.register_panel(
                serial, self._firmware, model=panel_model, port=port
            )

        # Register with Supervisor Discovery
        if self._supervisor_discovery is not None and self._supervisor_discovery.is_available:
            advertise_host = self._advertise_address or "127.0.0.1"
            await self._supervisor_discovery.register_panel(serial, advertise_host, port)

        _LOGGER.info("Registered panel %s from %s on port %d", serial, config_path.name, port)
        return panel

    async def _load_recorder_data(self, config_path: Path) -> RecorderDataSource | None:
        """Create and populate a RecorderDataSource from config + HA history.

        Returns ``None`` if HA is unavailable or the config has no
        ``recorder_entity`` mappings.  Failures are logged and swallowed
        so the panel still starts in synthetic mode.
        """
        if self._ha_client is None:
            return None

        try:
            raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        except Exception:
            return None

        if not isinstance(raw, dict):
            return None

        templates = raw.get("circuit_templates")
        if not isinstance(templates, dict):
            return None

        entity_ids: list[str] = []
        for tmpl in templates.values():
            if isinstance(tmpl, dict):
                entity_id = tmpl.get("recorder_entity")
                if isinstance(entity_id, str) and entity_id:
                    entity_ids.append(entity_id)

        if not entity_ids:
            return None

        _LOGGER.info(
            "Loading recorder data for %s (%d entities)",
            config_path.name,
            len(entity_ids),
        )
        recorder = RecorderDataSource()
        try:
            loaded = await recorder.load(self._ha_client, entity_ids)
        except Exception:
            _LOGGER.warning(
                "Recorder data loading failed for %s — using synthetic",
                config_path.name,
                exc_info=True,
            )
            return None

        if loaded == 0:
            _LOGGER.warning(
                "Recorder returned no data for %s — using synthetic",
                config_path.name,
            )
        return recorder if loaded > 0 else None

    async def _stop_panel(self, config_path: Path) -> None:
        """Stop and unregister a panel."""
        panel = self._panels.pop(config_path, None)
        if panel is None:
            return

        serial = panel.serial_number if panel.is_running else "unknown"
        await panel.stop()

        self._serial_to_panel.pop(serial, None)

        # Stop and remove per-panel HTTP server
        server = self._panel_servers.pop(serial, None)
        if server is not None:
            await server.stop()

        # Release allocated port
        port = self._panel_ports.pop(serial, None)
        if port is not None:
            self._release_port(port)

        # Unregister from mDNS
        if self._advertiser is not None:
            await self._advertiser.unregister_panel(serial)

        # Unregister from Supervisor Discovery
        if self._supervisor_discovery is not None:
            await self._supervisor_discovery.unregister_panel(serial)

        _LOGGER.info("Unregistered panel %s", serial)

    async def reload(self) -> dict[str, list[str]]:
        """Re-scan config directory and reconcile running panels.

        Returns a summary of what changed::

            {"started": [...], "stopped": [...], "reloaded": [...]}
        """
        current = _discover_configs(self._config_dir, self._config_filter)

        # Exclude configs the user explicitly stopped via the dashboard
        for path in list(current):
            if path.name in self._stopped_configs:
                del current[path]

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

    def set_config_filter(self, config_filter: str | None) -> None:
        """Switch the active config file.

        Stops all panels outside the new filter on the next reload.
        """
        self._config_filter = config_filter
        self._persist_last_config(config_filter)

    def _persist_last_config(self, config_name: str | None) -> None:
        """Save the active config name so it resumes on next startup."""
        state_file = self._config_dir / ".last_config"
        try:
            if config_name:
                state_file.write_text(config_name, encoding="utf-8")
            elif state_file.exists():
                state_file.unlink()
        except OSError:
            pass  # Best-effort; don't break the simulator

    # ------------------------------------------------------------------
    # Explicit per-panel lifecycle (called from dashboard)
    # ------------------------------------------------------------------

    def _transition_to_explicit_control(self) -> None:
        """Move from config-filter mode to per-panel start/stop control.

        Called once on the first explicit Start/Stop from the UI.
        Preserves current running state by marking all non-running
        configs as stopped.
        """
        if self._config_filter is None:
            return
        currently_running = {p.name for p in self._panels}
        all_on_disk: set[str] = set()
        for pattern in ("*.yaml", "*.yml"):
            all_on_disk.update(p.name for p in self._config_dir.glob(pattern))
        self._stopped_configs = all_on_disk - currently_running
        self._config_filter = None

    def request_start_panel(self, filename: str) -> None:
        """Start (or ensure running) the engine for a specific config."""
        self._transition_to_explicit_control()
        self._stopped_configs.discard(filename)
        self._persist_last_config(filename)
        self._reload_event.set()

    def request_stop_panel(self, filename: str) -> None:
        """Stop the engine for a specific config."""
        self._transition_to_explicit_control()
        self._stopped_configs.add(filename)
        self._reload_event.set()

    def request_restart_panel(self, filename: str) -> None:
        """Force-restart the engine for a specific config."""
        self._transition_to_explicit_control()
        self._stopped_configs.discard(filename)
        # Invalidate the stored hash so reload() sees a mismatch
        path = self._config_dir / filename
        if path in self._config_hashes:
            self._config_hashes[path] = ""
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
                if isinstance(message.payload, bytes | bytearray)
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
                    panel.serial_number,
                    target_type,
                    prop,
                    payload_str,
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
        certs = generate_certificates(self._cert_dir, advertise_address=self._advertise_address)
        self._certs = certs

        # 2. Resolve homie schema
        schema_path = self._homie_schema_path or _find_homie_schema()
        self._schema = load_schema(schema_path)

        # 3. Initialise HA API client and history provider (if configured).
        # Must happen before the dashboard starts listening so the first
        # request (e.g. via HA ingress) already sees ha_available=True.
        ha_client: HAClient | None = None
        if self._ha_config is not None:
            from span_panel_simulator.ha_api.client import HAClient

            ha_client = HAClient(self._ha_config)
            if await ha_client.async_validate():
                _LOGGER.info("HA API: connected and validated")
                self._ha_client = ha_client
            else:
                _LOGGER.warning("HA API: validation failed — continuing without HA")
                await ha_client.close()
                ha_client = None

        # 3b. Initialise Supervisor Discovery (add-on mode).
        from span_panel_simulator.supervisor_discovery import SupervisorDiscovery

        self._supervisor_discovery = SupervisorDiscovery()
        if self._supervisor_discovery.is_available:
            await self._supervisor_discovery.cleanup_stale()
            _LOGGER.info("Supervisor Discovery: available (add-on mode)")

        # 3c. Start mDNS advertiser and panel browser before the dashboard
        # is reachable so discovery results are available on first load.
        advertiser = PanelAdvertiser(
            advertise_address=self._advertise_address,
        )
        self._advertiser = advertiser
        await advertiser.start()

        browser = PanelBrowser()
        self._panel_browser = browser
        await browser.start()

        # 3d. Start dashboard on its own port — HA client and panel
        # browser are already initialised so the context is complete.
        dashboard_ctx = DashboardContext(
            config_dir=self._config_dir,
            config_filter=self._config_filter,
            get_panel_configs=self._get_panel_configs,
            get_panel_ports=self._get_panel_ports,
            request_reload=self.request_reload,
            set_config_filter=self.set_config_filter,
            start_panel=self.request_start_panel,
            stop_panel=self.request_stop_panel,
            restart_panel=self.request_restart_panel,
            get_power_summary=self._get_power_summary,
            set_simulation_time=self._set_simulation_time,
            set_time_acceleration=self._set_time_acceleration,
            set_grid_online=self._set_grid_online,
            set_grid_islandable=self._set_grid_islandable,
            set_circuit_priority=self._set_circuit_priority,
            set_circuit_relay=self._set_circuit_relay,
            get_modeling_data=self._get_modeling_data,
            ha_client=ha_client,
            history_provider=ha_client,
            panel_browser=browser,
        )
        dashboard_app = create_dashboard_app(dashboard_ctx)
        self._dashboard_runner = web.AppRunner(dashboard_app)
        await self._dashboard_runner.setup()
        dashboard_site = web.TCPSite(self._dashboard_runner, "0.0.0.0", self._dashboard_port)
        await dashboard_site.start()
        _LOGGER.info("Dashboard listening on http://0.0.0.0:%d", self._dashboard_port)

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
            # Stop all panels (this also stops per-panel HTTP servers)
            for path in list(self._panels):
                await self._stop_panel(path)
            # Cleanup Supervisor Discovery entries
            if self._supervisor_discovery is not None:
                await self._supervisor_discovery.cleanup_all()
            if self._panel_browser is not None:
                await self._panel_browser.stop()
            if self._advertiser is not None:
                await self._advertiser.stop()
            if self._dashboard_runner is not None:
                await self._dashboard_runner.cleanup()
            if self._ha_client is not None:
                await self._ha_client.close()
            _LOGGER.info("Simulator shut down")

    async def stop(self) -> None:
        """Signal the simulator to stop."""
        self._running = False
