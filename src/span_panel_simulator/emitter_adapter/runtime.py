"""Per-clone wiring between simulator engine and emitter wire layer.

``start_clone`` builds the manifest from the engine's config, instantiates an
``Emitter`` (with optional BESS + load-shedding native-device configs), opens the
per-clone MQTT client, runs the cold-start lifecycle, and returns a ``CloneRuntime``
the simulator's panel instance holds across ticks.

``publish_tick`` collects the engine's per-circuit signed power into a
``TickInputs`` and hands it to ``Emitter.publish_tick``. The emitter does the
rest: BESS dispatch, load shedding, energy integration, panel meter aggregation,
diff publication. /set commands are handled by the emitter's internal default
handlers (no producer-side setter wiring needed)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import aiomqtt

from span_panel_simulator.emitter_adapter.instance_ids import stable_circuit_uuid
from span_panel_simulator.emitter_adapter.spec_generator import build_manifest
from span_panel_simulator.flat_emitter import (
    BESSConfig,
    ChargeMode,
    DeviceManifest,
    EbusPanelSnapshot,
    Emitter,
    LoadSheddingConfig,
    PanelEnvelopeTick,
    SetterRegistry,
    TickInputs,
)

if TYPE_CHECKING:
    from span_panel_simulator.config_types import (
        BESSConfigYAML,
        BrokerConfigYAML,
        SimulationConfig,
    )
    from span_panel_simulator.engine import DynamicSimulationEngine


@dataclass(frozen=True, slots=True)
class BrokerConnection:
    """Broker connection parameters threaded through clone start-up.

    A frozen dataclass so the same connection profile can be safely
    reused across panels (and across future second-emitter scenarios
    such as a separate BESS or PV emitter on the same broker)."""

    host: str
    port: int
    username: str | None = None
    password: str | None = None
    ca_cert_path: str | None = None


@runtime_checkable
class MqttPublisher(Protocol):
    """Structural subset of the MQTT client interface the emitter consumes.

    Mirrors the emitter's internal ``_MqttClientLike`` protocol; we redeclare
    it here rather than import an underscore-private symbol across packages."""

    def is_connected(self) -> bool: ...
    async def publish(
        self,
        topic: str,
        payload: bytes,
        qos: int = 0,
        retain: bool = False,
    ) -> None: ...
    async def subscribe(self, topic: str) -> None: ...
    async def disconnect(self) -> None: ...


@dataclass(slots=True)
class CloneRuntime:
    engine: DynamicSimulationEngine
    manifest: DeviceManifest
    setters: SetterRegistry
    mqtt: MqttPublisher
    emitter: Emitter
    uuid_to_circuit_id: dict[str, str]


class _AiomqttPublisher:
    """Adapter wrapping ``aiomqtt.Client`` to satisfy the emitter's duck-typed
    MQTT interface (``is_connected``, ``publish``, ``subscribe``)."""

    def __init__(
        self,
        host: str,
        port: int,
        client_id: str,
        username: str | None = None,
        password: str | None = None,
        will: aiomqtt.Will | None = None,
        ca_cert_path: str | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._client_id = client_id
        self._username = username
        self._password = password
        self._will = will
        self._ca_cert_path = ca_cert_path
        self._client: aiomqtt.Client | None = None

    async def connect(self) -> None:
        tls_params = (
            aiomqtt.TLSParameters(ca_certs=self._ca_cert_path) if self._ca_cert_path else None
        )
        self._client = aiomqtt.Client(
            hostname=self._host,
            port=self._port,
            identifier=self._client_id,
            username=self._username,
            password=self._password,
            will=self._will,
            tls_params=tls_params,
        )
        await self._client.__aenter__()

    async def disconnect(self) -> None:
        if self._client is not None:
            await self._client.__aexit__(None, None, None)
            self._client = None

    def is_connected(self) -> bool:
        """Return True once :meth:`connect` has constructed the underlying client.

        Note: this is a coarse "ready to attempt publishing" gate, not a live
        link check — aiomqtt does not surface broker reachability synchronously.
        A True return indicates the emitter may issue publishes; transport
        failures will surface as exceptions from :meth:`publish` / :meth:`subscribe`."""
        return self._client is not None

    async def publish(
        self,
        topic: str,
        payload: bytes,
        qos: int = 0,
        retain: bool = False,
    ) -> None:
        assert self._client is not None
        await self._client.publish(topic, payload=payload, qos=qos, retain=retain)

    async def subscribe(self, topic: str) -> None:
        assert self._client is not None
        await self._client.subscribe(topic)


def bess_config_from_engine(engine: DynamicSimulationEngine) -> BESSConfig | None:
    """Build the emitter-side BESSConfig from the engine's loaded clone profile.
    Returns None when the profile has no BESS enabled."""
    bess = engine.config.get("bess") or {}
    return _build_bess_config(engine.serial_number, bess)


def _build_bess_config(serial_number: str, bess: BESSConfigYAML) -> BESSConfig | None:
    """Build a BESSConfig dataclass from a typed YAML section."""
    if not bess.get("enabled"):
        return None
    raw_mode = bess.get("charge_mode", "self-consumption")
    mode: ChargeMode = "backup-only" if raw_mode == "backup-only" else "self-consumption"
    del serial_number
    return BESSConfig(
        instance_id=str(bess.get("instance_id", "bess")),
        nameplate_capacity_kwh=float(bess.get("nameplate_capacity_kwh", 13.5)),
        max_charge_w=float(bess.get("max_charge_w", 3500.0)),
        max_discharge_w=float(bess.get("max_discharge_w", 3500.0)),
        charge_efficiency=float(bess.get("charge_efficiency", 0.95)),
        discharge_efficiency=float(bess.get("discharge_efficiency", 0.95)),
        backup_reserve_pct=float(bess.get("backup_reserve_pct", 20.0)),
        charge_mode=mode,
        charge_hours=tuple(bess.get("charge_hours", [10, 11, 12, 13, 14, 15])),
        discharge_hours=tuple(bess.get("discharge_hours", [17, 18, 19, 20, 21])),
    )


def update_bess_config_live(runtime: CloneRuntime, bess_yaml: BESSConfigYAML) -> None:
    """Apply a fresh BESS config to a running emitter without restart.

    Mutates the engine's loaded config so subsequent reads see the new
    values, rebuilds the emitter-side ``BESSConfig`` from the YAML, and
    swaps it on the emitter (SOC/SOE state is preserved across the swap).
    """
    runtime.engine.config["bess"] = bess_yaml
    new_cfg = bess_config_from_engine(runtime.engine)
    if new_cfg is None:
        return
    runtime.emitter.update_bess_config(new_cfg)


def _load_shedding_config_from_engine(
    engine: DynamicSimulationEngine,
) -> LoadSheddingConfig:
    panel_cfg = engine.config["panel_config"]
    return LoadSheddingConfig(
        soc_threshold_pct=float(panel_cfg.get("soc_shed_threshold", 20.0)),
    )


def _resolve_broker(
    config_broker: BrokerConfigYAML,
    fallback: BrokerConnection | None,
) -> BrokerConnection:
    """Combine YAML ``broker:`` overrides with the app-supplied fallback.

    The YAML section takes precedence (a clone can pin its own broker);
    fields it omits fall through to *fallback*; missing fallback values
    use sensible defaults (127.0.0.1:1883 anonymous, no TLS).
    """
    fb_host = fallback.host if fallback is not None else "127.0.0.1"
    fb_port = fallback.port if fallback is not None else None
    fb_user = fallback.username if fallback is not None else None
    fb_pass = fallback.password if fallback is not None else None
    fb_ca = fallback.ca_cert_path if fallback is not None else None

    host = config_broker.get("host") or fb_host
    raw_port = config_broker.get("port") if config_broker.get("port") is not None else fb_port
    port = int(raw_port) if raw_port is not None else 1883
    username = config_broker.get("username") or fb_user
    password = config_broker.get("password") or fb_pass
    ca_cert_path = config_broker.get("ca_cert_path") or fb_ca
    return BrokerConnection(
        host=host,
        port=port,
        username=username,
        password=password,
        ca_cert_path=ca_cert_path,
    )


async def start_clone(
    engine: DynamicSimulationEngine,
    *,
    broker: BrokerConnection | None = None,
) -> CloneRuntime:
    """Assemble the emitter for ``engine``: build manifest, open MQTT, run
    lifecycle. Returns a runtime the panel holds across ticks.

    Broker connection precedence (highest first):
        1. ``broker:`` section in the YAML config (config explicitness wins).
        2. The supplied ``broker`` fallback (typically constructed once by
           ``SimulatorApp`` from CLI/env).
        3. Default 127.0.0.1:1883 anonymous (no TLS)."""
    manifest = build_manifest(engine.config)

    # ``engine.serial_number`` is the loaded config's ``panel_config.serial_number``
    # — the same key ``build_manifest`` scopes circuit uuids with, read after the
    # ``sim-`` prefix has been applied. Reading it any other way here would map the
    # published uuids back to nothing.
    panel_id = engine.serial_number
    uuid_to_circuit_id = {
        stable_circuit_uuid(panel_id, c["id"]): c["id"] for c in engine.config["circuits"]
    }

    # The emitter registers internal default /set handlers from the empty
    # SetterRegistry — no producer-side wiring required.
    setters = SetterRegistry()

    lwt_topic, lwt_payload, lwt_qos, lwt_retain = Emitter.lwt_settings(manifest)
    will = aiomqtt.Will(
        topic=lwt_topic,
        payload=lwt_payload,
        qos=lwt_qos,
        retain=lwt_retain,
    )

    broker_cfg: BrokerConfigYAML = engine.config.get("broker") or {}
    resolved = _resolve_broker(broker_cfg, broker)
    mqtt = _AiomqttPublisher(
        host=resolved.host,
        port=resolved.port,
        client_id=f"span-sim-{engine.serial_number}",
        username=resolved.username,
        password=resolved.password,
        will=will,
        ca_cert_path=resolved.ca_cert_path,
    )
    await mqtt.connect()

    # The emitter Phase 2 reshape pluralised the BESS-config parameter:
    # ``bess_configs`` is a tuple keyed internally by ``instance_id``. The
    # simulator models a single BESS per panel today, so we wrap the
    # optional config in a one-element tuple (or empty tuple when absent).
    bess_cfg = bess_config_from_engine(engine)
    bess_configs: tuple[BESSConfig, ...] = (bess_cfg,) if bess_cfg is not None else ()
    emitter = Emitter(
        manifest,
        setters,
        mqtt,
        bess_configs=bess_configs,
        load_shedding_config=_load_shedding_config_from_engine(engine),
    )

    runtime = CloneRuntime(
        engine=engine,
        manifest=manifest,
        setters=setters,
        mqtt=mqtt,
        emitter=emitter,
        uuid_to_circuit_id=uuid_to_circuit_id,
    )

    await emitter.start()
    return runtime


async def publish_tick(runtime: CloneRuntime) -> EbusPanelSnapshot:
    """Collect the engine's current per-tick driving signal into ``TickInputs``
    and hand it to the emitter for publication."""
    raw = await runtime.engine.get_tick_inputs()
    tick = TickInputs(
        current_time=raw["current_time"],
        grid_online=raw["grid_online"],
        circuits=raw["circuits"],
        evse=_evse_tick_inputs(
            runtime.engine.config,
            runtime.engine.serial_number,
            raw["circuits"],
        ),
        envelope=PanelEnvelopeTick(),
    )
    return await runtime.emitter.publish_tick(tick)


async def stop_clone(runtime: CloneRuntime, *, graceful: bool = True) -> None:
    try:
        await runtime.emitter.stop(graceful=graceful, clear_retained=True)
    finally:
        await runtime.mqtt.disconnect()


def _evse_tick_inputs(
    config: SimulationConfig,
    panel_id: str,
    circuit_powers: dict[str, float],
) -> dict[str, float]:
    """Mirror EVSE feed-circuit power into TickInputs.evse.

    The emitter uses circuit powers for panel aggregation and EVSE powers for
    EVSE device status/energy. Real panels link the EVSE node to its feed
    circuit via the ``feed`` property, so the simulator derives the same link
    from every circuit template tagged ``device_type: evse`` unless explicitly
    configured to one feed.
    """
    evse_cfg = config.get("evse")
    base_evse_id = "evse"
    explicit_feed: str | None = None
    if isinstance(evse_cfg, dict):
        base_evse_id = str(evse_cfg.get("instance_id", base_evse_id))
        raw_feed = evse_cfg.get("feed")
        explicit_feed = str(raw_feed) if raw_feed else None

    feeds = [explicit_feed] if explicit_feed else _feeds_for_device_type(config, panel_id, "evse")
    return {
        (base_evse_id if idx == 1 else f"{base_evse_id}-{idx}"): circuit_powers.get(feed, 0.0)
        for idx, feed in enumerate(feeds, start=1)
        if feed is not None
    }


def _first_feed_for_device_type(
    config: SimulationConfig,
    panel_id: str,
    device_type: str,
) -> str | None:
    feeds = _feeds_for_device_type(config, panel_id, device_type)
    return feeds[0] if feeds else None


def _feeds_for_device_type(
    config: SimulationConfig,
    panel_id: str,
    device_type: str,
) -> list[str]:
    templates = config.get("circuit_templates")
    circuits = config.get("circuits")
    if not isinstance(templates, dict) or not isinstance(circuits, list):
        return []
    feeds: list[str] = []
    for item in circuits:
        if not isinstance(item, dict):
            continue
        template_name = item.get("template")
        if template_name is None or not isinstance(template_name, str):
            continue
        template = templates.get(template_name)
        if isinstance(template, dict) and template.get("device_type") == device_type:
            circuit_id = item.get("id")
            if circuit_id is not None:
                feeds.append(stable_circuit_uuid(panel_id, str(circuit_id)))
    return feeds
