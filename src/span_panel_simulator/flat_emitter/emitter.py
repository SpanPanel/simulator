"""Public Emitter facade — wire-layer publisher with native-device runtime.

The producer hands the emitter a small per-tick driving signal via
``publish_tick(TickInputs)``: signed power per circuit/EVSE, current_time,
grid_online, panel envelope. The emitter resolves BESS dispatch, gates circuit
power through ``RelayResolver``, integrates energy via ``EnergyIntegrator``,
aggregates panel-level fields via ``PanelMeter``, builds the internal snapshot,
and publishes the Homie diff to MQTT.

The internal snapshot type (``EbusPanelSnapshot`` and friends) is used for the
diff cache and read-back via ``last_snapshot``; producers do not construct it."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from span_panel_simulator.flat_emitter.energy_integrator import EnergyIntegrator
from span_panel_simulator.flat_emitter.exceptions import EmitterStateError
from span_panel_simulator.flat_emitter.manifest import DeviceManifest
from span_panel_simulator.flat_emitter.manifest_physics import ManifestPhysicsView
from span_panel_simulator.flat_emitter.native_devices import (
    BESSConfig,
    BESSDevice,
    LoadSheddingConfig,
    LoadSheddingDevice,
    NativeTickContext,
)
from span_panel_simulator.flat_emitter.panel_meter import circuit_current_a
from span_panel_simulator.flat_emitter.panel_meter import resolve as resolve_panel
from span_panel_simulator.flat_emitter.relay_resolver import RelayResolver, RelayState
from span_panel_simulator.flat_emitter.snapshot import (
    EbusBatterySnapshot,
    EbusCircuitSnapshot,
    EbusEvseSnapshot,
    EbusLugsSnapshot,
    EbusPanelDoor,
    EbusPanelInfo,
    EbusPanelMeter,
    EbusPanelPcs,
    EbusPanelPowerFlows,
    EbusPanelSnapshot,
    EbusPanelStatus,
    EbusPvSnapshot,
)
from span_panel_simulator.flat_emitter.tick_inputs import TickInputs
from span_panel_simulator.flat_emitter.wire.bag_builder import BagBuilder
from span_panel_simulator.flat_emitter.wire.graph_builder import build_graph
from span_panel_simulator.flat_emitter.wire.lifecycle import LifecycleController
from span_panel_simulator.flat_emitter.wire.lifecycle import lwt_settings as _lwt
from span_panel_simulator.flat_emitter.wire.mapping_loader import load_mapping_table
from span_panel_simulator.flat_emitter.wire.profile_loader import load_profiles
from span_panel_simulator.flat_emitter.wire.publisher import Publisher
from span_panel_simulator.flat_emitter.wire.set_router import SetterRegistry, compute_subscriptions


@runtime_checkable
class _MqttClientLike(Protocol):
    def is_connected(self) -> bool: ...
    async def publish(
        self,
        topic: str,
        payload: bytes,
        qos: int = 0,
        retain: bool = False,
    ) -> None: ...
    async def subscribe(self, topic: str) -> None: ...


class Emitter:
    """One emitter per logical panel/clone."""

    def __init__(
        self,
        manifest: DeviceManifest,
        setter_registry: SetterRegistry,
        mqtt_client: _MqttClientLike,
        *,
        bess_configs: tuple[BESSConfig, ...] = (),
        load_shedding_config: LoadSheddingConfig | None = None,
        ebus_domain: str = "ebus",
        bus_version: str = "5",
    ) -> None:
        self._manifest = manifest

        self._profiles = load_profiles()
        self._mapping = load_mapping_table()
        self._mapping.validate_against(self._profiles)

        self._graph = build_graph(manifest, self._mapping, self._profiles)

        # ---- v0.3.0 internal state (must exist before internal /set handlers
        # bind, which must happen before compute_subscriptions validates handler
        # coverage). ----
        # BESS is pluralized: a panel can host multiple battery devices (e.g. a
        # Powerwall plus an Enphase IQ, or two Powerwalls). Keyed by
        # ``BESSConfig.instance_id``; duplicate IDs are a producer-side bug.
        self._bess: dict[str, BESSDevice] = {}
        for cfg in bess_configs:
            if cfg.instance_id in self._bess:
                raise EmitterStateError(f"duplicate bess_config instance_id={cfg.instance_id!r}")
            self._bess[cfg.instance_id] = BESSDevice(config=cfg)
        self._load_shedding: LoadSheddingDevice | None = (
            LoadSheddingDevice(config=load_shedding_config)
            if load_shedding_config is not None
            else None
        )
        # ManifestPhysicsView raises ManifestValidationError if the manifest
        # is missing required physics keys. publish_tick is the only publish
        # path now, so a malformed manifest is a hard error at construction.
        self._physics = ManifestPhysicsView(manifest)
        self._relays = RelayResolver()
        self._energy = EnergyIntegrator()
        self._priority_overrides: dict[str, str] = {}
        self._name_overrides: dict[str, str] = {}
        self._dominant_power_source_override: str | None = None

        for cid, cphys in self._physics.all_circuits().items():
            self._relays.register(cid, always_on=cphys.always_on)
            self._energy.register(cid)
            if cphys.initial_consumed_wh or cphys.initial_produced_wh:
                self._energy.seed(
                    cid,
                    consumed_wh=cphys.initial_consumed_wh,
                    produced_wh=cphys.initial_produced_wh,
                )
        for eid in self._physics.all_evse():
            self._energy.register(eid)
        # Lugs are metered points, so their energy registers integrate the power
        # THEIR OWN meter reports. They used to be handed the sum of the circuits
        # behind them instead, which is a different quantity: with 7 kW of PV and
        # 6 kW of load the lugs carry ~1 kW in one direction, but the gross sum
        # advanced `imported-energy` AND `exported-energy` in the same tick. A
        # capture of a live panel never does that -- the spec calls
        # `imported-energy` "the energy counterpart of positive `active-power`",
        # and a counterpart that integrates a different signal is not one.
        for lugs_id in self._physics.all_lugs():
            self._energy.register(lugs_id)
        # Seed any configured BESS whose manifest physics declares an initial SOE.
        for bess_id, bphys in self._physics.all_bess().items():
            if bphys.initial_soe_kwh is not None and bess_id in self._bess:
                self._bess[bess_id].set_soe(bphys.initial_soe_kwh)

        # Internal default /set handlers — registered BEFORE compute_subscriptions
        # so its missing-handler check passes. Producer-supplied handlers always
        # win (the helper checks .get() first).
        self._register_internal_setters(setter_registry)

        # ---- Wire layer subscriptions ----
        instances = [(i.entity_class, i.instance_id) for i in manifest.instances]
        settables_by_class = {
            ec: profile.settable_properties() for ec, profile in self._profiles.items()
        }

        root_id = next(
            i.instance_id
            for i in manifest.instances
            if any(
                m.entity_class == i.entity_class and m.placement.kind == "root-device"
                for m in self._mapping.values()
            )
        )

        def _device_id_for(ec: str, iid: str) -> str:
            placement = self._mapping[ec].placement
            if placement.kind == "node-on-parent":
                return root_id
            return iid

        def _node_id_for(ec: str, iid: str, cap: str) -> str:
            placement = self._mapping[ec].placement
            if placement.kind == "node-on-parent":
                template = placement.node_id_template or "{instance_id}"
                return template.format(
                    instance_id=iid,
                    instance_id_short=iid[:8],
                    display_name=self._manifest.get(ec, iid).display_name,
                )
            return cap

        def _datatype_for(ec: str, cap: str, key: str) -> str:
            return self._profiles[ec].capabilities[cap].properties[key].datatype

        self._subscriptions = compute_subscriptions(
            instances=instances,
            settables_by_class=settables_by_class,
            registry=setter_registry,
            domain=ebus_domain,
            bus_version=bus_version,
            device_id_for=_device_id_for,
            node_id_for=_node_id_for,
            datatype_for=_datatype_for,
        )

        self._lifecycle = LifecycleController(
            manifest,
            self._mapping,
            self._profiles,
            self._graph,
            mqtt_client,
            domain=ebus_domain,
            bus_version=bus_version,
            subscriptions=self._subscriptions,
        )

        self._publisher = Publisher(
            self._graph,
            mqtt_client,
            domain=ebus_domain,
            bus_version=bus_version,
        )
        self._bag_builder = BagBuilder(self._graph, self._mapping, self._profiles)
        self._last_snapshot: EbusPanelSnapshot | None = None
        self._started = False

    @staticmethod
    def lwt_settings(manifest: DeviceManifest) -> tuple[str, bytes, int, bool]:
        # Derive the root entity class from the mapping table rather than
        # hard-coding "panel"; future mapping tables may use a different root
        # (e.g. an MID device parenting a panel node).
        mapping = load_mapping_table()
        return _lwt(
            manifest,
            domain="ebus",
            bus_version="5",
            root_entity_class=mapping.root_entity_class(),
        )

    async def start(self) -> None:
        await self._lifecycle.start()
        self._started = True

    async def publish_tick(self, tick_inputs: TickInputs) -> EbusPanelSnapshot:
        """The v0.3.0 producer-facing publish path. Returns the constructed
        snapshot for the producer to read back."""
        if not self._started:
            raise EmitterStateError("Emitter.publish_tick() called before start()")
        snapshot = self._build_snapshot_from_tick(tick_inputs)
        await self._publish_diff(snapshot)
        return snapshot

    def seed_energy(
        self,
        instance_id: str,
        *,
        consumed_wh: float = 0.0,
        produced_wh: float = 0.0,
    ) -> None:
        """Overwrite an instance's energy accumulators. Typical use: producer
        reads last-known values from persistent storage and seeds at startup
        before the first ``publish_tick`` call. Raises ``KeyError`` for unknown
        instance IDs (caught typos before they cause silent data loss)."""
        self._energy.seed(instance_id, consumed_wh=consumed_wh, produced_wh=produced_wh)

    def seed_bess_soe(self, instance_id: str, soe_kwh: float) -> None:
        """Overwrite a BESS device's stored SOE. Raises ``EmitterStateError``
        if no BESS is configured or if ``instance_id`` is not among the
        configured BESS instances — both are producer-side programming
        errors."""
        if not self._bess:
            raise EmitterStateError(
                f"seed_bess_soe({instance_id!r}, ...): no BESS configured on this emitter"
            )
        if instance_id not in self._bess:
            known = sorted(self._bess.keys())
            raise EmitterStateError(
                f"seed_bess_soe: instance_id={instance_id!r} not among configured "
                f"BESS instances {known!r}"
            )
        self._bess[instance_id].set_soe(soe_kwh)

    async def stop(self, *, graceful: bool = True, clear_retained: bool = False) -> None:
        await self._lifecycle.stop(graceful=graceful, clear_retained=clear_retained)

    def update_bess_config(self, config: BESSConfig) -> None:
        """Replace (or add) a BESS device's configuration keyed by
        ``config.instance_id``. Takes effect on the next publish call.
        SOC/SOE state persists across in-place config swaps; freshly added
        BESS instances start from their config's ``initial_soc_pct``."""
        existing = self._bess.get(config.instance_id)
        if existing is None:
            self._bess[config.instance_id] = BESSDevice(config=config)
        else:
            existing.update_config(config)

    def update_load_shedding_config(self, config: LoadSheddingConfig) -> None:
        if self._load_shedding is None:
            self._load_shedding = LoadSheddingDevice(config=config)
        else:
            self._load_shedding.update_config(config)

    @property
    def last_snapshot(self) -> EbusPanelSnapshot | None:
        return self._last_snapshot

    @property
    def topology_version(self) -> int:
        return next(iter(self._mapping.values())).profile_version

    @property
    def relays(self) -> RelayResolver:
        """Read-write access to the per-circuit relay resolver. Used by /set
        handlers (registered by the emitter for ``circuit.switch/relay``) to
        update operator overrides."""
        return self._relays

    @property
    def dominant_power_source_override(self) -> str | None:
        """Operator-set dominant power source override, or None if not set.
        Set via /set ``panel.pcs/dominant-power-source`` topic."""
        return self._dominant_power_source_override

    # ---- internal --------------------------------------------------------

    def _register_internal_setters(self, registry: SetterRegistry) -> None:
        """Register default handlers for the four settable properties when the
        producer hasn't already supplied one. The handlers update emitter-
        internal state (RelayResolver, priority/name override maps, panel
        dominant-power-source override). The next ``publish_tick`` call reflects
        the change on the wire.

        Producers needing custom routing register their own handler before
        constructing the ``Emitter`` and the registry's existing entry wins."""

        async def on_circuit_relay(
            entity_class: str,
            instance_id: str,
            prop_path: str,
            value: object,
        ) -> None:
            del entity_class, prop_path
            # Homie boolean: True = relay closed (energized), False = open.
            closed = (
                bool(value)
                if isinstance(value, bool)
                else (str(value).strip().lower() in ("true", "1", "closed", "on"))
            )
            new_state = RelayState.CLOSED if closed else RelayState.OPEN
            if self._relays.known(instance_id):
                self._relays.set_user_override(instance_id, new_state)

        async def on_shed_priority(
            entity_class: str,
            instance_id: str,
            prop_path: str,
            value: object,
        ) -> None:
            del entity_class, prop_path
            self._priority_overrides[instance_id] = str(value).upper()

        async def on_circuit_name(
            entity_class: str,
            instance_id: str,
            prop_path: str,
            value: object,
        ) -> None:
            del entity_class, prop_path
            self._name_overrides[instance_id] = str(value)

        async def on_dom_power_source(
            entity_class: str,
            instance_id: str,
            prop_path: str,
            value: object,
        ) -> None:
            del entity_class, instance_id, prop_path
            self._dominant_power_source_override = str(value).upper()

        if registry.get("circuit", "circuit/relay") is None:
            registry.register("circuit", "circuit/relay", on_circuit_relay)
        if registry.get("circuit", "circuit/shed-priority") is None:
            registry.register("circuit", "circuit/shed-priority", on_shed_priority)
        if registry.get("circuit", "circuit/name") is None:
            registry.register("circuit", "circuit/name", on_circuit_name)
        if registry.get("panel", "core/dominant-power-source") is None:
            registry.register("panel", "core/dominant-power-source", on_dom_power_source)

    async def _publish_diff(self, snapshot: EbusPanelSnapshot) -> None:
        bag = self._bag_builder.build(snapshot)
        await self._publisher.publish(bag)
        self._last_snapshot = snapshot

    def _build_snapshot_from_tick(self, tick: TickInputs) -> EbusPanelSnapshot:
        panel_phys = self._physics.panel
        circuits_phys = self._physics.all_circuits()

        # Step 1: aggregate inputs for BESS dispatch (pre-shed: use raw producer
        # power, not gated, since shedding decisions DEPEND on BESS SOC).
        load_demand_w = sum(p for p in tick.circuits.values() if p > 0)
        pv_available_w = -sum(p for p in tick.circuits.values() if p < 0)

        # Step 2: BESS dispatch + battery snapshots — one per configured BESS.
        # ``battery_w`` is the SUM of signed dispatch across all batteries; it
        # feeds the panel meter aggregation as a single combined contribution.
        # The min-SOC across batteries is what drives load-shedding decisions
        # (the most-depleted BESS is the binding constraint).
        battery_snapshots: dict[str, EbusBatterySnapshot] = {}
        battery_w = 0.0
        for bess_id, bess_dev in self._bess.items():
            bphys = self._physics.bess(bess_id)
            snap = bess_dev.tick(
                NativeTickContext(
                    current_time=tick.current_time,
                    grid_online=tick.grid_online,
                    load_demand_w=load_demand_w,
                    pv_available_w=pv_available_w,
                )
            )
            snap.instance_id = bess_id
            snap.vendor_name = bphys.vendor_name
            snap.product_name = bphys.product_name
            snap.model = bphys.model
            snap.serial_number = bphys.serial_number
            snap.firmware_version = bphys.firmware_version
            snap.relative_position = bphys.relative_position
            snap.feed_circuit_id = bphys.feed
            snap.connected = snap.communication == "OK"
            snap.grid_state = "ON_GRID" if tick.grid_online else "OFF_GRID"
            battery_snapshots[bess_id] = snap
            battery_w += snap.active_power_w
        has_battery = bool(battery_snapshots)
        # ``min_soc`` is None when no BESS reports a SOE value (all uninitialised);
        # ``decide_shed`` then treats SOC as unknown.
        soc_values = [
            s.soe_percentage for s in battery_snapshots.values() if s.soe_percentage is not None
        ]
        min_soc: float | None = min(soc_values) if soc_values else None

        # Step 3: load-shedding decisions written into RelayResolver. Always
        # cleared first so a previous tick's shed state doesn't linger when the
        # grid comes back online or SOC recovers. Operator-set priority
        # overrides take precedence over manifest defaults.
        self._relays.clear_all_shed()
        if self._load_shedding is not None:
            effective_priorities = {
                cid: self._priority_overrides.get(cid, cphys.default_priority)
                for cid, cphys in circuits_phys.items()
            }
            shed_ids = self._load_shedding.decide_shed(
                grid_online=tick.grid_online,
                bess_soc_pct=min_soc,
                priorities=effective_priorities,
            )
            for cid in shed_ids:
                self._relays.set_shed(cid, open_relay=True)

        # Step 4: resolve final relay state per circuit (always-on > /set > shed
        # > default-CLOSED) and gate producer-reported power.
        gated_powers: dict[str, float] = {}
        for cid in circuits_phys:
            raw_power = tick.circuits.get(cid, 0.0)
            relay_state, _requester = self._relays.state(cid)
            gated_powers[cid] = 0.0 if relay_state == RelayState.OPEN else raw_power

        # Step 4: integrate energy per circuit using gated power (if relay open,
        # no energy flows).
        for cid, gated in gated_powers.items():
            self._energy.observe(cid, gated, tick.current_time)
        for eid, evse_power in tick.evse.items():
            if self._energy.known(eid):
                self._energy.observe(eid, evse_power, tick.current_time)

        # Step 5: panel-level aggregation.
        meter = resolve_panel(
            panel=panel_phys,
            circuits=circuits_phys,
            gated_powers=gated_powers,
            battery_w=battery_w,
            grid_online=tick.grid_online,
            has_battery=has_battery,
        )

        # Step 6: build per-circuit snapshots — applying any operator name and
        # priority overrides on top of manifest defaults.
        circuit_snaps: dict[str, EbusCircuitSnapshot] = {}
        for cid, cphys in circuits_phys.items():
            relay_state, requester = self._relays.state(cid)
            gated_p = gated_powers[cid]
            estate = self._energy.state(cid)
            effective_priority = self._priority_overrides.get(cid, cphys.default_priority)
            effective_name = self._name_overrides.get(
                cid,
                self._manifest.get("circuit", cid).display_name,
            )
            circuit_snaps[cid] = EbusCircuitSnapshot(
                circuit_id=cid,
                name=effective_name,
                relay_state=str(relay_state),
                instant_power_w=gated_p,
                produced_energy_wh=estate.produced_wh,
                consumed_energy_wh=estate.consumed_wh,
                tabs=list(cphys.tabs),
                priority=effective_priority,
                is_user_controllable=cphys.relay_behavior == "controllable",
                is_sheddable=effective_priority in ("OFF_GRID", "SOC_THRESHOLD"),
                is_never_backup=effective_priority == "NEVER",
                is_240v=cphys.dipole,
                current_a=circuit_current_a(
                    gated_p,
                    dipole=cphys.dipole,
                    line_voltage_v=panel_phys.line_voltage_v,
                ),
                breaker_rating_a=cphys.breaker_rating_a,
                always_on=cphys.always_on,
                pcs_managed=cphys.relay_behavior == "controllable",
                pcs_priority=cphys.pcs_priority,
                relay_requester=str(requester),
                energy_accum_update_time_s=int(tick.current_time),
                instant_power_update_time_s=int(tick.current_time),
            )

        # Step 7: PV snapshots — one entry per PV instance in the manifest.
        # Per-PV power telemetry comes from the producer's circuit feed; the
        # snapshot here carries the static identity from manifest physics.
        pv_snaps: dict[str, EbusPvSnapshot] = {}
        for pv_id, pv_phys in self._physics.all_pv().items():
            pv_snaps[pv_id] = EbusPvSnapshot(
                node_id=pv_id,
                feed_circuit_id=pv_phys.feed,
                vendor_name=pv_phys.vendor_name,
                product_name=pv_phys.product_name,
                serial_number=pv_phys.serial_number,
                nameplate_capacity_w=pv_phys.nameplate_capacity_w,
                firmware_version=pv_phys.firmware_version,
                relative_position=pv_phys.relative_position,
            )

        # Step 7b: Lugs snapshots — one per declared lugs instance. Per-leg
        # currents and power/energy come from the panel meter aggregation;
        # ``direction`` and ``feed`` come from manifest physics. Producers that
        # only model a single lugs (most US split-phase setups) get a single
        # entry here; OPNsense-fed multi-lugs panels get one per device.
        lugs_snaps: dict[str, EbusLugsSnapshot] = {}
        for lugs_id, lphys in self._physics.all_lugs().items():
            if lphys.direction == "upstream":
                l1 = meter.upstream_l1_current_a
                l2 = meter.upstream_l2_current_a
                # Upstream lugs are panel-side. With an upstream BESS, utility
                # grid flow is computed beyond the BESS and can differ.
                active_w = meter.upstream_active_power_w
            else:  # downstream
                l1 = meter.downstream_l1_current_a
                l2 = meter.downstream_l2_current_a
                active_w = meter.feedthrough_power_w
            # Integrate what this meter reads, in this meter's own frame: a lugs
            # meter takes the default reference direction, so positive is power
            # arriving through it and accrues `imported-energy`. One direction can
            # accrue per tick, which is the property the gross sum broke.
            self._energy.observe(lugs_id, active_w, tick.current_time)
            lugs_energy = self._energy.state(lugs_id)
            imported_wh = lugs_energy.consumed_wh
            exported_wh = lugs_energy.produced_wh
            lugs_snaps[lugs_id] = EbusLugsSnapshot(
                instance_id=lugs_id,
                direction=("upstream" if lphys.direction == "upstream" else "downstream"),
                feed=None,
                l1_current_a=l1,
                l2_current_a=l2,
                active_power_w=active_w,
                imported_energy_wh=imported_wh,
                exported_energy_wh=exported_wh,
            )

        # Step 8: EVSE snapshots derived from per-tick power.
        evse_snaps: dict[str, EbusEvseSnapshot] = {}
        for eid, ephys in self._physics.all_evse().items():
            power = tick.evse.get(eid, 0.0)
            charging = power > 100.0
            evse_snaps[eid] = EbusEvseSnapshot(
                node_id=eid,
                feed_circuit_id=ephys.feed,
                status="CHARGING" if charging else "AVAILABLE",
                lock_state="LOCKED" if charging else "UNLOCKED",
                advertised_current_a=ephys.max_current_a,
                vendor_name=ephys.vendor_name,
                product_name=ephys.product_name,
                part_number=ephys.part_number,
                serial_number=ephys.serial_number,
                firmware_version=ephys.firmware_version,
            )

        # Step 9: assemble the panel snapshot from capability sub-dataclasses.
        info = EbusPanelInfo(
            serial_number=panel_phys.serial_number,
            firmware_version=panel_phys.firmware_version,
            vendor_name=panel_phys.vendor_name,
            hardware_version=panel_phys.hardware_version,
            panel_size=panel_phys.panel_size,
            panel_model=panel_phys.panel_model,
            schema_topology=panel_phys.topology,
        )
        door = EbusPanelDoor(
            state=tick.envelope.door_state,
            proximity_proven=tick.envelope.proximity_proven,
        )
        consumed_total = sum(s.consumed_energy_wh for s in circuit_snaps.values())
        produced_total = sum(s.produced_energy_wh for s in circuit_snaps.values())
        feedthrough_consumed = sum(
            s.consumed_energy_wh
            for cid, s in circuit_snaps.items()
            if circuits_phys[cid].placement == "downstream-of-lugs"
        )
        feedthrough_produced = sum(
            s.produced_energy_wh
            for cid, s in circuit_snaps.items()
            if circuits_phys[cid].placement == "downstream-of-lugs"
        )
        meter_section = EbusPanelMeter(
            instant_grid_power_w=meter.instant_grid_power_w,
            main_meter_energy_consumed_wh=consumed_total,
            main_meter_energy_produced_wh=produced_total,
            feedthrough_power_w=meter.feedthrough_power_w,
            feedthrough_energy_consumed_wh=feedthrough_consumed,
            feedthrough_energy_produced_wh=feedthrough_produced,
            l1_voltage=meter.line_voltage_v,
            l2_voltage=meter.line_voltage_v,
            upstream_l1_current_a=meter.upstream_l1_current_a,
            upstream_l2_current_a=meter.upstream_l2_current_a,
            downstream_l1_current_a=meter.downstream_l1_current_a,
            downstream_l2_current_a=meter.downstream_l2_current_a,
        )
        status = EbusPanelStatus(
            main_relay_state=meter.main_relay_state,
            eth0_link=tick.envelope.eth0_link,
            wlan_link=tick.envelope.wlan_link,
            wwan_link=tick.envelope.wwan_link,
            wifi_ssid=tick.envelope.wifi_ssid,
            cloud_connection=tick.envelope.cloud_connection,
            postal_code=panel_phys.postal_code,
            time_zone=panel_phys.time_zone,
            uptime_s=tick.envelope.uptime_s,
        )
        pcs = EbusPanelPcs(
            main_breaker_rating_a=panel_phys.main_breaker_rating_a,
            grid_islandable=meter.grid_islandable,
            dominant_power_source=(
                self._dominant_power_source_override
                if self._dominant_power_source_override is not None
                else meter.dominant_power_source
            ),
            grid_state=meter.grid_state,
            dsm_state=meter.dsm_state,
            current_run_config=meter.current_run_config,
        )
        power_flows = EbusPanelPowerFlows(
            pv=meter.power_flow_pv,
            battery=meter.power_flow_battery,
            grid=meter.power_flow_grid,
            site=meter.power_flow_site,
        )

        return EbusPanelSnapshot(
            info=info,
            door=door,
            meter=meter_section,
            status=status,
            pcs=pcs,
            power_flows=power_flows,
            circuits=circuit_snaps,
            battery=battery_snapshots,
            pv=pv_snaps,
            evse=evse_snaps,
            lugs=lugs_snaps,
        )
