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
from aiohttp import web

from span_panel_simulator.bootstrap import BootstrapHttpServer
from span_panel_simulator.certs import generate_certificates
from span_panel_simulator.clone import update_config_location
from span_panel_simulator.const import (
    DASHBOARD_PORT,
    DEFAULT_BROKER_PASSWORD,
    DEFAULT_BROKER_USERNAME,
    DEFAULT_FIRMWARE_VERSION,
    DEFAULT_TICK_INTERVAL_S,
    HTTPS_PORT,
    MQTTS_PORT,
)
from span_panel_simulator.dashboard import DashboardContext, create_dashboard_app
from span_panel_simulator.discovery import PanelAdvertiser
from span_panel_simulator.engine import _PANEL_SIZE_TO_MODEL
from span_panel_simulator.panel import PanelInstance
from span_panel_simulator.profile_applicator import apply_usage_profiles
from span_panel_simulator.schema import HomieSchemaRegistry, load_schema
from span_panel_simulator.sio_handler import SioContext, create_sio_server

if TYPE_CHECKING:
    from span_panel_simulator.certs import CertificateBundle
    from span_panel_simulator.engine import DynamicSimulationEngine

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

    When *config_filter* is set, only the named file is returned.
    """
    if config_filter:
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
        config_filter: str | None = None,
        tick_interval: float = DEFAULT_TICK_INTERVAL_S,
        firmware_version: str = DEFAULT_FIRMWARE_VERSION,
        broker_username: str = DEFAULT_BROKER_USERNAME,
        broker_password: str = DEFAULT_BROKER_PASSWORD,
        broker_host: str = "localhost",
        broker_port: int = MQTTS_PORT,
        http_port: int = HTTPS_PORT,
        cert_dir: Path | None = None,
        homie_schema_path: Path | None = None,
        dashboard_port: int = DASHBOARD_PORT,
        advertise_address: str | None = None,
        advertise_http_port: int | None = None,
    ) -> None:
        self._config_dir = config_dir
        self._config_filter = config_filter
        self._tick_interval = tick_interval
        self._firmware = firmware_version
        self._broker_username = broker_username
        self._broker_password = broker_password
        self._broker_host = broker_host
        self._broker_port = broker_port
        self._http_port = http_port
        self._cert_dir = cert_dir or Path("/tmp/span-sim-certs")
        self._homie_schema_path = homie_schema_path
        self._dashboard_port = dashboard_port
        self._advertise_address = advertise_address
        self._advertise_http_port = advertise_http_port

        # Tracked state
        self._panels: dict[Path, PanelInstance] = {}
        self._config_hashes: dict[Path, str] = {}
        self._serial_to_panel: dict[str, PanelInstance] = {}
        self._http_server: BootstrapHttpServer | None = None
        self._dashboard_runner: web.AppRunner | None = None
        self._advertiser: PanelAdvertiser | None = None
        self._certs: CertificateBundle | None = None
        self._schema: HomieSchemaRegistry | None = None
        self._running = False
        self._mqtt_client: aiomqtt.Client | None = None
        self._reload_event: asyncio.Event = asyncio.Event()

    # ------------------------------------------------------------------
    # Dashboard helpers
    # ------------------------------------------------------------------

    def _get_panel_configs(self) -> dict[Path, str]:
        """Return a mapping of config path to serial number for running panels."""
        return {path: panel.serial_number for path, panel in self._panels.items()}

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
        sim_time = engine.get_current_simulation_time()
        # Access last snapshot data from circuits
        grid = 0.0
        pv = 0.0
        battery = 0.0
        total_consumption = 0.0
        for circuit in engine._circuits.values():
            power = circuit.instant_power_w
            if circuit.energy_mode == "producer":
                pv += power
            elif circuit.energy_mode == "bidirectional":
                battery += power
            else:
                total_consumption += power
        if engine.grid_online:
            grid = total_consumption - pv
        else:
            grid = 0.0
            # Off-grid: battery covers the load deficit (consumption minus PV)
            if engine.has_battery:
                battery = total_consumption - pv

        # Shedding info
        shed_ids: list[str] = []
        soc_pct: float | None = None
        soc_threshold = 20.0
        if engine._bsee is not None:
            soc_pct = engine._bsee.soe_percentage
        if engine._config is not None:
            soc_threshold = engine._config["panel_config"].get("soc_shed_threshold", 20.0)
        if not engine.grid_online and engine.has_battery:
            for circuit in engine._circuits.values():
                if circuit.energy_mode in ("producer", "bidirectional"):
                    continue
                if circuit._priority == "OFF_GRID" or (
                    circuit._priority == "SOC_THRESHOLD"
                    and soc_pct is not None
                    and soc_pct < soc_threshold
                ):
                    shed_ids.append(circuit.circuit_id)

        # Circuits manually opened by user (via relay override)
        user_open_ids: list[str] = []
        for cid, overrides in engine._dynamic_overrides.items():
            if overrides.get("relay_state") == "OPEN" and cid not in shed_ids:
                user_open_ids.append(cid)

        # All circuits off when offline without battery
        all_off = not engine.grid_online and not engine.has_battery

        # Resolve panel timezone string
        time_zone = "America/Los_Angeles"
        if engine._behavior_engine is not None:
            time_zone = str(engine._behavior_engine.panel_timezone)

        return {
            "grid_w": round(grid, 1),
            "pv_w": round(pv, 1),
            "battery_w": round(battery, 1),
            "consumption_w": round(total_consumption, 1),
            "simulation_time": sim_time,
            "grid_online": engine.grid_online,
            "has_battery": engine.has_battery,
            "is_islandable": engine.is_grid_islandable,
            "soc_pct": round(soc_pct, 1) if soc_pct is not None else None,
            "soc_threshold": soc_threshold,
            "shed_ids": shed_ids,
            "user_open_ids": user_open_ids,
            "all_off": all_off,
            "time_zone": time_zone,
        }

    def _set_simulation_time(self, iso_str: str) -> None:
        """Set the simulation time on the first running panel."""
        engine = self._get_first_engine()
        if engine is not None:
            engine.override_simulation_start_time(iso_str)

    def _set_time_acceleration(self, accel: float) -> None:
        """Set the time acceleration on the first running panel."""
        engine = self._get_first_engine()
        if engine is not None:
            engine._clock.time_acceleration = accel

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

    # ------------------------------------------------------------------
    # Socket.IO callbacks
    # ------------------------------------------------------------------

    async def _update_panel_location(
        self, serial: str, latitude: float, longitude: float
    ) -> dict[str, str]:
        """Update a panel's config file with new coordinates and trigger reload.

        Called by the Socket.IO ``set_location`` event handler.
        """
        config_path: Path | None = None
        for path, panel in self._panels.items():
            if panel.is_running and panel.serial_number == serial:
                config_path = path
                break

        if config_path is None:
            return {"status": "error", "message": f"Panel {serial} not found"}

        try:
            tz_name = update_config_location(config_path, latitude, longitude)
        except (ValueError, OSError) as exc:
            return {"status": "error", "message": str(exc)}

        self.request_reload()
        return {"status": "ok", "time_zone": tz_name}

    async def _clone_panel(
        self,
        host: str,
        passphrase: str | None,
        latitude: float,
        longitude: float,
    ) -> dict[str, object]:
        """Run the clone pipeline and apply location.

        Called by the Socket.IO ``clone_panel`` event handler.
        """
        from span_panel_simulator.clone import translate_scraped_panel, write_clone_config
        from span_panel_simulator.homie_const import TYPE_BESS, TYPE_EVSE, TYPE_PV
        from span_panel_simulator.scraper import ScrapeError, register_with_panel, scrape_ebus

        # Phase 1: Register
        try:
            creds, ca_pem = await register_with_panel(host, passphrase)
        except ScrapeError as exc:
            return {"status": "error", "phase": exc.phase, "message": str(exc)}

        # Phase 2: Scrape
        try:
            scraped = await scrape_ebus(creds, ca_pem)
        except ScrapeError as exc:
            return {"status": "error", "phase": exc.phase, "message": str(exc)}

        # Phase 3: Translate
        try:
            config = translate_scraped_panel(scraped, host=host, passphrase=passphrase)
        except Exception as exc:
            return {"status": "error", "phase": "translating", "message": str(exc)}

        # Phase 4: Write
        try:
            output_path = write_clone_config(config, self._config_dir, scraped.serial_number)
        except ValueError as exc:
            return {"status": "error", "phase": "writing", "message": str(exc)}

        # Phase 5: Apply location
        tz_name = update_config_location(output_path, latitude, longitude)

        self.request_reload()

        # Build result summary
        nodes = scraped.description.get("nodes", {})
        circuit_count = sum(
            1
            for n in nodes.values()
            if isinstance(n, dict) and n.get("type") == "energy.ebus.device.circuit"
        )
        base = scraped.serial_number
        if not base.lower().startswith("sim-"):
            base = f"sim-{base}"
        clone_serial = f"{base}-clone"

        return {
            "status": "ok",
            "serial": scraped.serial_number,
            "clone_serial": clone_serial,
            "filename": output_path.name,
            "circuits": circuit_count,
            "has_bess": any(
                isinstance(n, dict) and n.get("type") == TYPE_BESS for n in nodes.values()
            ),
            "has_pv": any(
                isinstance(n, dict) and n.get("type") == TYPE_PV for n in nodes.values()
            ),
            "has_evse": any(
                isinstance(n, dict) and n.get("type") == TYPE_EVSE for n in nodes.values()
            ),
            "time_zone": tz_name,
        }

    async def _apply_usage_profiles(
        self,
        clone_serial: str,
        profiles: dict[str, dict[str, object]],
    ) -> dict[str, object]:
        """Merge HA-derived usage profiles into a running clone's config.

        Called by the Socket.IO ``apply_usage_profiles`` event handler.
        """
        panel = self._serial_to_panel.get(clone_serial)
        if panel is None:
            return {
                "status": "error",
                "message": f"Panel {clone_serial} not found",
            }

        updated = apply_usage_profiles(panel.config_path, profiles)
        self.request_reload()

        return {"status": "ok", "templates_updated": updated}

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
            schema=self._schema,
        )
        serial = await panel.start()

        self._panels[config_path] = panel
        self._serial_to_panel[serial] = panel

        # Derive model from panel tab count
        panel_model = "MAIN_32"
        if panel.engine is not None and panel.engine._config is not None:
            total_tabs = panel.engine._config["panel_config"].get("total_tabs", 32)
            panel_model = _PANEL_SIZE_TO_MODEL.get(total_tabs, "MAIN_32")

        # Update the HTTP server and mDNS registries
        if self._http_server is not None:
            self._http_server.register_panel(serial, self._firmware)
        if self._advertiser is not None:
            await self._advertiser.register_panel(serial, self._firmware, model=panel_model)

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
        current = _discover_configs(self._config_dir, self._config_filter)
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

        # 3. Start bootstrap HTTP server (multi-panel aware) with Socket.IO
        sio_ctx = SioContext(
            update_panel_location=self._update_panel_location,
            clone_panel=self._clone_panel,
            apply_usage_profiles=self._apply_usage_profiles,
        )
        sio = create_sio_server(sio_ctx)

        http_server = BootstrapHttpServer(
            certs=certs,
            schema=self._schema,
            broker_username=self._broker_username,
            broker_password=self._broker_password,
            broker_host=self._broker_host,
            port=self._http_port,
            reload_callback=self.request_reload,
            sio_server=sio,
        )
        self._http_server = http_server
        await http_server.start()

        # 3b. Start dashboard on its own port
        dashboard_ctx = DashboardContext(
            config_dir=self._config_dir,
            config_filter=self._config_filter,
            get_panel_configs=self._get_panel_configs,
            request_reload=self.request_reload,
            get_power_summary=self._get_power_summary,
            set_simulation_time=self._set_simulation_time,
            set_time_acceleration=self._set_time_acceleration,
            set_grid_online=self._set_grid_online,
            set_grid_islandable=self._set_grid_islandable,
            set_circuit_priority=self._set_circuit_priority,
            set_circuit_relay=self._set_circuit_relay,
        )
        dashboard_app = create_dashboard_app(dashboard_ctx)
        self._dashboard_runner = web.AppRunner(dashboard_app)
        await self._dashboard_runner.setup()
        dashboard_site = web.TCPSite(self._dashboard_runner, "0.0.0.0", self._dashboard_port)
        await dashboard_site.start()
        _LOGGER.info("Dashboard listening on http://0.0.0.0:%d", self._dashboard_port)

        # 4. Start mDNS advertiser
        advertiser = PanelAdvertiser(
            http_port=self._advertise_http_port or self._http_port,
            advertise_address=self._advertise_address,
        )
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
            if self._dashboard_runner is not None:
                await self._dashboard_runner.cleanup()
            if self._http_server is not None:
                await self._http_server.stop()
            _LOGGER.info("Simulator shut down")

    async def stop(self) -> None:
        """Signal the simulator to stop."""
        self._running = False
