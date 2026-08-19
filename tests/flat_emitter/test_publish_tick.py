"""Integration tests for ``Emitter.publish_tick``.

Uses an in-memory FakeMqttClient (same pattern as test_emitter_public_surface)
to assert the wire output is well-formed without a real broker."""

from __future__ import annotations

import json

import pytest

from span_panel_simulator.flat_emitter import (
    BESSConfig,
    DeviceInstance,
    DeviceManifest,
    Emitter,
    EmitterStateError,
    LoadSheddingConfig,
    PanelEnvelopeTick,
    RelayState,
    SetterRegistry,
    TickInputs,
)


class FakeMqttClient:
    def __init__(self) -> None:
        self.published: list[tuple[str, bytes, int, bool]] = []
        self.subscribed: list[str] = []

    def is_connected(self) -> bool:
        return True

    async def publish(
        self,
        topic: str,
        payload: bytes,
        qos: int = 0,
        retain: bool = False,
    ) -> None:
        self.published.append((topic, payload, qos, retain))

    async def subscribe(self, topic: str) -> None:
        self.subscribed.append(topic)


def _panel_inst() -> DeviceInstance:
    return DeviceInstance(
        "panel",
        "abc-123",
        "Span Panel",
        metadata={
            "vendor-name": "Span",
            "serial-number": "abc-123",
            "firmware-version": "sim/v0.1.0",
            "hardware-version": "rev2",
            "panel-size": "40",
            "main-breaker-rating-a": "200",
            "panel-model": "MAIN_40",
            "postal-code": "94103",
            "time-zone": "America/Los_Angeles",
        },
    )


def _circuit_inst(
    cid: str = "kitchen",
    *,
    tabs: str = "1",
    priority: str = "NICE_TO_HAVE",
    relay_behavior: str = "controllable",
    placement: str = "downstream-of-lugs",
) -> DeviceInstance:
    return DeviceInstance(
        "circuit",
        cid,
        cid.title(),
        metadata={
            "tab-numbers": tabs,
            "breaker-rating-a": "20",
            "default-priority": priority,
            "relay-behavior": relay_behavior,
            "placement": placement,
        },
    )


def _bess_inst(instance_id: str = "abc-123-bess") -> DeviceInstance:
    return DeviceInstance(
        "bess",
        instance_id,
        "Battery",
        metadata={
            "vendor-name": "Span",
            "nameplate-capacity-kwh": "13.5",
        },
    )


def _registry() -> SetterRegistry:
    """Stub setter registry — handlers do nothing. Phase 1 doesn't yet route
    /set into RelayResolver internally; that's Phase 2."""
    setters = SetterRegistry()

    async def _noop(entity_class: str, instance_id: str, prop: str, value: object) -> None:
        del entity_class, instance_id, prop, value

    setters.register("circuit", "circuit/relay", _noop)
    setters.register("circuit", "circuit/shed-priority", _noop)
    setters.register("circuit", "circuit/name", _noop)
    setters.register("panel", "core/dominant-power-source", _noop)
    return setters


@pytest.fixture
def emitter_no_bess() -> Emitter:
    manifest = DeviceManifest(instances=(_panel_inst(), _circuit_inst()))
    return Emitter(manifest, _registry(), FakeMqttClient())


@pytest.fixture
def emitter_with_bess() -> Emitter:
    manifest = DeviceManifest(instances=(_panel_inst(), _circuit_inst(), _bess_inst()))
    bess_cfg = BESSConfig(
        instance_id="abc-123-bess",
        nameplate_capacity_kwh=13.5,
        max_charge_w=3500.0,
        max_discharge_w=3500.0,
    )
    return Emitter(manifest, _registry(), FakeMqttClient(), bess_configs=(bess_cfg,))


@pytest.mark.asyncio
async def test_publish_tick_before_start_raises(emitter_no_bess: Emitter) -> None:
    with pytest.raises(EmitterStateError, match="before start"):
        await emitter_no_bess.publish_tick(
            TickInputs(current_time=0.0, grid_online=True, circuits={"kitchen": 500.0}),
        )


@pytest.mark.asyncio
async def test_publish_tick_emits_circuit_power(emitter_no_bess: Emitter) -> None:
    await emitter_no_bess.start()
    snap = await emitter_no_bess.publish_tick(
        TickInputs(current_time=0.0, grid_online=True, circuits={"kitchen": 500.0}),
    )
    assert "kitchen" in snap.circuits
    assert snap.circuits["kitchen"].instant_power_w == 500.0
    assert snap.circuits["kitchen"].relay_state == "CLOSED"
    assert snap.circuits["kitchen"].current_a == pytest.approx(500.0 / 120.0)
    assert snap.meter.instant_grid_power_w == 500.0
    # power-flows is the node frame: importing means power enters the panel.
    assert snap.power_flows.grid == -500.0
    assert snap.pcs.grid_state == "ON_GRID"


@pytest.mark.asyncio
async def test_publish_tick_uses_live_panel_flat_topic_shape(emitter_no_bess: Emitter) -> None:
    await emitter_no_bess.start()
    fake = emitter_no_bess._publisher._mqtt
    assert isinstance(fake, FakeMqttClient)

    description_payload = next(
        payload
        for topic, payload, _qos, _retain in fake.published
        if topic == "ebus/5/abc-123/$description"
    )
    description = json.loads(description_payload)
    assert description["nodes"]["core"]["type"] == (
        "energy.ebus.device.distribution-enclosure.core"
    )
    assert description["nodes"]["kitchen"]["type"] == "energy.ebus.device.circuit"

    await emitter_no_bess.publish_tick(
        TickInputs(current_time=0.0, grid_online=True, circuits={"kitchen": 500.0}),
    )
    retained = {topic: payload.decode() for topic, payload, _qos, _retain in fake.published}
    assert retained["ebus/5/abc-123/core/software-version"] == "sim/v0.1.0"
    assert retained["ebus/5/abc-123/core/grid-islandable"] == "false"
    assert retained["ebus/5/abc-123/kitchen/active-power"] == "-500.0"
    assert retained["ebus/5/abc-123/kitchen/space"] == "1"
    assert retained["ebus/5/abc-123/kitchen/relay-requester"] == "NONE"


@pytest.mark.asyncio
async def test_publish_tick_integrates_energy_across_ticks(emitter_no_bess: Emitter) -> None:
    await emitter_no_bess.start()
    await emitter_no_bess.publish_tick(
        TickInputs(current_time=0.0, grid_online=True, circuits={"kitchen": 1000.0}),
    )
    await emitter_no_bess.publish_tick(
        TickInputs(current_time=3600.0, grid_online=True, circuits={"kitchen": 1000.0}),
    )
    snap = emitter_no_bess.last_snapshot
    assert snap is not None
    # 1000 W for 1 hour = 1000 Wh
    assert snap.circuits["kitchen"].consumed_energy_wh == pytest.approx(1000.0)
    assert snap.meter.main_meter_energy_consumed_wh == pytest.approx(1000.0)


@pytest.mark.asyncio
async def test_publish_tick_relay_open_zeros_power(emitter_no_bess: Emitter) -> None:
    await emitter_no_bess.start()
    emitter_no_bess.relays.set_user_override("kitchen", RelayState.OPEN)
    snap = await emitter_no_bess.publish_tick(
        TickInputs(current_time=0.0, grid_online=True, circuits={"kitchen": 1000.0}),
    )
    assert snap.circuits["kitchen"].instant_power_w == 0.0
    assert snap.circuits["kitchen"].relay_state == "OPEN"
    assert snap.circuits["kitchen"].relay_requester == "USER"
    # Grid power follows the gated value, not the producer's reported value.
    assert snap.meter.instant_grid_power_w == 0.0


@pytest.mark.asyncio
async def test_publish_tick_off_grid_zeros_grid_power(emitter_no_bess: Emitter) -> None:
    await emitter_no_bess.start()
    snap = await emitter_no_bess.publish_tick(
        TickInputs(current_time=0.0, grid_online=False, circuits={"kitchen": 1000.0}),
    )
    assert snap.meter.instant_grid_power_w == 0.0
    assert snap.pcs.grid_state == "OFF_GRID"
    assert snap.meter.l1_voltage == 0.0
    assert snap.meter.l2_voltage == 0.0
    assert snap.status.main_relay_state == "OPEN"


@pytest.mark.asyncio
async def test_publish_tick_with_bess_reports_battery(emitter_with_bess: Emitter) -> None:
    await emitter_with_bess.start()
    snap = await emitter_with_bess.publish_tick(
        TickInputs(current_time=0.0, grid_online=True, circuits={"kitchen": 1000.0}),
    )
    bess = snap.battery["abc-123-bess"]
    assert bess.communication == "OK"
    assert bess.nameplate_capacity_kwh == 13.5
    # First tick establishes baseline; SOE reflects initial 50%.
    assert bess.soe_percentage == pytest.approx(50.0)


@pytest.mark.asyncio
async def test_publish_tick_diff_only_publishes_changes(emitter_no_bess: Emitter) -> None:
    await emitter_no_bess.start()
    fake = emitter_no_bess._publisher._mqtt
    assert isinstance(fake, FakeMqttClient)

    await emitter_no_bess.publish_tick(
        TickInputs(current_time=0.0, grid_online=True, circuits={"kitchen": 500.0}),
    )
    after_first = len(fake.published)

    # Identical second tick → no new publishes (only retained values changed by
    # update_time_s timestamps still flow if mapped).
    await emitter_no_bess.publish_tick(
        TickInputs(current_time=1.0, grid_online=True, circuits={"kitchen": 500.0}),
    )
    after_second = len(fake.published)
    # Energy accumulators advance, so consumed-energy will publish; but most
    # property values are unchanged. Assert second publish set is much smaller.
    assert after_second - after_first < after_first


@pytest.mark.asyncio
async def test_publish_tick_pv_export_drives_grid_negative(emitter_no_bess: Emitter) -> None:
    # Add a PV-feed circuit by replacing the manifest. Easier: build a fresh
    # emitter with both circuits.
    manifest = DeviceManifest(
        instances=(
            _panel_inst(),
            _circuit_inst("kitchen", tabs="1"),
            _circuit_inst("solar", tabs="3"),
        )
    )
    em = Emitter(manifest, _registry(), FakeMqttClient())
    await em.start()
    snap = await em.publish_tick(
        TickInputs(
            current_time=0.0, grid_online=True, circuits={"kitchen": 500.0, "solar": -2000.0}
        ),
    )
    # load - pv = 500 - 2000 = -1500 (exporting)
    assert snap.meter.instant_grid_power_w == -1500.0
    # ... and the node frame reports the same instant with the opposite sign on
    # both terms: the array feeds the panel, the surplus leaves through the grid.
    assert snap.power_flows.pv == -2000.0
    assert snap.power_flows.grid == 1500.0


@pytest.mark.asyncio
async def test_circuit_active_power_wire_sign_is_inverse_of_internal_model() -> None:
    manifest = DeviceManifest(
        instances=(
            _panel_inst(),
            _circuit_inst("kitchen", tabs="1"),
            _circuit_inst("solar", tabs="3"),
        )
    )
    fake = FakeMqttClient()
    em = Emitter(manifest, _registry(), fake)
    await em.start()
    snap = await em.publish_tick(
        TickInputs(
            current_time=0.0,
            grid_online=True,
            circuits={"kitchen": 500.0, "solar": -2000.0},
        ),
    )

    retained = {topic: payload.decode() for topic, payload, _qos, _retain in fake.published}
    assert snap.circuits["kitchen"].instant_power_w == 500.0
    assert snap.circuits["solar"].instant_power_w == -2000.0
    assert retained["ebus/5/abc-123/kitchen/active-power"] == "-500.0"
    assert retained["ebus/5/abc-123/solar/active-power"] == "2000.0"


@pytest.mark.asyncio
async def test_seed_energy_carries_into_first_tick(emitter_no_bess: Emitter) -> None:
    emitter_no_bess.seed_energy("kitchen", consumed_wh=5000.0, produced_wh=100.0)
    await emitter_no_bess.start()
    await emitter_no_bess.publish_tick(
        TickInputs(current_time=0.0, grid_online=True, circuits={"kitchen": 1000.0}),
    )
    await emitter_no_bess.publish_tick(
        TickInputs(current_time=3600.0, grid_online=True, circuits={"kitchen": 1000.0}),
    )
    snap = emitter_no_bess.last_snapshot
    assert snap is not None
    assert snap.circuits["kitchen"].consumed_energy_wh == pytest.approx(6000.0)
    assert snap.circuits["kitchen"].produced_energy_wh == pytest.approx(100.0)


@pytest.mark.asyncio
async def test_seed_energy_unknown_id_raises(emitter_no_bess: Emitter) -> None:
    with pytest.raises(KeyError):
        emitter_no_bess.seed_energy("ghost", consumed_wh=1.0)


@pytest.mark.asyncio
async def test_seed_bess_soe_overwrites(emitter_with_bess: Emitter) -> None:
    emitter_with_bess.seed_bess_soe("abc-123-bess", soe_kwh=10.0)
    await emitter_with_bess.start()
    snap = await emitter_with_bess.publish_tick(
        TickInputs(current_time=0.0, grid_online=True, circuits={"kitchen": 1000.0}),
    )
    # SOE / nameplate * 100 = 10/13.5 * 100 = ~74.07
    bess = snap.battery["abc-123-bess"]
    assert bess.soe_kwh == pytest.approx(10.0)
    assert bess.soe_percentage == pytest.approx(10.0 / 13.5 * 100.0)


@pytest.mark.asyncio
async def test_seed_bess_soe_no_bess_raises(emitter_no_bess: Emitter) -> None:
    with pytest.raises(EmitterStateError, match="no BESS"):
        emitter_no_bess.seed_bess_soe("anything", soe_kwh=1.0)


@pytest.mark.asyncio
async def test_seed_bess_soe_wrong_id_raises(emitter_with_bess: Emitter) -> None:
    with pytest.raises(EmitterStateError, match="not among configured"):
        emitter_with_bess.seed_bess_soe("wrong-id", soe_kwh=1.0)


@pytest.mark.asyncio
async def test_envelope_overrides_propagate(emitter_no_bess: Emitter) -> None:
    await emitter_no_bess.start()
    env = PanelEnvelopeTick(
        door_state="OPEN",
        proximity_proven=False,
        wifi_ssid="MyHouse",
        eth0_link=False,
        cloud_connection="DISCONNECTED",
    )
    snap = await emitter_no_bess.publish_tick(
        TickInputs(current_time=0.0, grid_online=True, circuits={"kitchen": 500.0}, envelope=env),
    )
    assert snap.door.state == "OPEN"
    assert snap.door.proximity_proven is False
    assert snap.status.wifi_ssid == "MyHouse"
    assert snap.status.eth0_link is False
    assert snap.status.cloud_connection == "DISCONNECTED"


@pytest.mark.asyncio
async def test_load_shed_off_grid_opens_off_grid_priority_circuit() -> None:
    manifest = DeviceManifest(
        instances=(
            _panel_inst(),
            _circuit_inst("hot_tub", priority="OFF_GRID"),
            _circuit_inst("fridge", tabs="3", priority="MUST_HAVE"),
            _bess_inst(),
        )
    )
    bess = BESSConfig(
        instance_id="abc-123-bess",
        nameplate_capacity_kwh=13.5,
        max_charge_w=3500.0,
        max_discharge_w=3500.0,
        initial_soc_pct=80.0,
    )
    em = Emitter(
        manifest,
        _registry(),
        FakeMqttClient(),
        bess_configs=(bess,),
        load_shedding_config=LoadSheddingConfig(soc_threshold_pct=20.0),
    )
    await em.start()
    snap = await em.publish_tick(
        TickInputs(
            current_time=0.0, grid_online=False, circuits={"hot_tub": 3000.0, "fridge": 200.0}
        ),
    )
    # OFF_GRID priority shed regardless of SOC.
    assert snap.circuits["hot_tub"].relay_state == "OPEN"
    assert snap.circuits["hot_tub"].relay_requester == "BACKUP"
    assert snap.circuits["hot_tub"].instant_power_w == 0.0
    # MUST_HAVE not shed.
    assert snap.circuits["fridge"].relay_state == "CLOSED"
    assert snap.circuits["fridge"].instant_power_w == 200.0


@pytest.mark.asyncio
async def test_load_shed_soc_threshold_only_when_soc_low() -> None:
    manifest = DeviceManifest(
        instances=(
            _panel_inst(),
            _circuit_inst("ev", priority="SOC_THRESHOLD"),
            _bess_inst(),
        )
    )
    bess = BESSConfig(
        instance_id="abc-123-bess",
        nameplate_capacity_kwh=13.5,
        max_charge_w=3500.0,
        max_discharge_w=3500.0,
        initial_soc_pct=50.0,
    )
    em = Emitter(
        manifest,
        _registry(),
        FakeMqttClient(),
        bess_configs=(bess,),
        load_shedding_config=LoadSheddingConfig(soc_threshold_pct=20.0),
    )
    await em.start()
    # SOC=50%, threshold=20% → NOT shed.
    snap_high = await em.publish_tick(
        TickInputs(current_time=0.0, grid_online=False, circuits={"ev": 7000.0}),
    )
    assert snap_high.circuits["ev"].relay_state == "CLOSED"

    # Drop SOC well below threshold by seeding.
    em.seed_bess_soe("abc-123-bess", soe_kwh=1.0)  # ~7.4%
    snap_low = await em.publish_tick(
        TickInputs(current_time=1.0, grid_online=False, circuits={"ev": 7000.0}),
    )
    assert snap_low.circuits["ev"].relay_state == "OPEN"
    assert snap_low.circuits["ev"].relay_requester == "BACKUP"


@pytest.mark.asyncio
async def test_user_override_beats_load_shed() -> None:
    manifest = DeviceManifest(
        instances=(
            _panel_inst(),
            _circuit_inst("hot_tub", priority="OFF_GRID"),
            _bess_inst(),
        )
    )
    bess = BESSConfig(
        instance_id="abc-123-bess",
        nameplate_capacity_kwh=13.5,
        max_charge_w=3500.0,
        max_discharge_w=3500.0,
        initial_soc_pct=10.0,
    )
    em = Emitter(
        manifest,
        _registry(),
        FakeMqttClient(),
        bess_configs=(bess,),
        load_shedding_config=LoadSheddingConfig(soc_threshold_pct=20.0),
    )
    await em.start()
    em.relays.set_user_override("hot_tub", RelayState.CLOSED)
    snap = await em.publish_tick(
        TickInputs(current_time=0.0, grid_online=False, circuits={"hot_tub": 3000.0}),
    )
    # Operator commanded CLOSED; load-shed wants OPEN; operator wins.
    assert snap.circuits["hot_tub"].relay_state == "CLOSED"
    assert snap.circuits["hot_tub"].relay_requester == "USER"
    assert snap.circuits["hot_tub"].instant_power_w == 3000.0


@pytest.mark.asyncio
async def test_always_on_beats_load_shed() -> None:
    # Mark the circuit as always-on via relay-behavior.
    manifest = DeviceManifest(
        instances=(
            _panel_inst(),
            _circuit_inst("smoke_alarm", priority="OFF_GRID", relay_behavior="always-on"),
            _bess_inst(),
        )
    )
    bess = BESSConfig(
        instance_id="abc-123-bess",
        nameplate_capacity_kwh=13.5,
        max_charge_w=3500.0,
        max_discharge_w=3500.0,
        initial_soc_pct=5.0,
    )
    em = Emitter(
        manifest,
        _registry(),
        FakeMqttClient(),
        bess_configs=(bess,),
        load_shedding_config=LoadSheddingConfig(soc_threshold_pct=20.0),
    )
    await em.start()
    snap = await em.publish_tick(
        TickInputs(current_time=0.0, grid_online=False, circuits={"smoke_alarm": 50.0}),
    )
    # Always-on cannot open regardless.
    assert snap.circuits["smoke_alarm"].relay_state == "CLOSED"
    assert snap.circuits["smoke_alarm"].relay_requester == "NEVER"
    assert snap.circuits["smoke_alarm"].instant_power_w == 50.0


@pytest.mark.asyncio
async def test_shed_clears_when_grid_recovers() -> None:
    manifest = DeviceManifest(
        instances=(
            _panel_inst(),
            _circuit_inst("hot_tub", priority="OFF_GRID"),
            _bess_inst(),
        )
    )
    bess = BESSConfig(
        instance_id="abc-123-bess",
        nameplate_capacity_kwh=13.5,
        max_charge_w=3500.0,
        max_discharge_w=3500.0,
    )
    em = Emitter(
        manifest,
        _registry(),
        FakeMqttClient(),
        bess_configs=(bess,),
        load_shedding_config=LoadSheddingConfig(),
    )
    await em.start()
    snap_off = await em.publish_tick(
        TickInputs(current_time=0.0, grid_online=False, circuits={"hot_tub": 3000.0}),
    )
    assert snap_off.circuits["hot_tub"].relay_state == "OPEN"

    snap_back = await em.publish_tick(
        TickInputs(current_time=1.0, grid_online=True, circuits={"hot_tub": 3000.0}),
    )
    # Grid restored, shed cleared, circuit back online.
    assert snap_back.circuits["hot_tub"].relay_state == "CLOSED"
    assert snap_back.circuits["hot_tub"].instant_power_w == 3000.0


@pytest.mark.asyncio
async def test_internal_setters_registered_when_no_producer_handler() -> None:
    """Producer can pass an empty SetterRegistry — Emitter fills in defaults
    for the four settable properties from its own internal state."""
    manifest = DeviceManifest(instances=(_panel_inst(), _circuit_inst()))
    setters = SetterRegistry()
    em = Emitter(manifest, setters, FakeMqttClient())
    # All four required handlers should now be present.
    assert setters.get("circuit", "circuit/relay") is not None
    assert setters.get("circuit", "circuit/shed-priority") is not None
    assert setters.get("circuit", "circuit/name") is not None
    assert setters.get("panel", "core/dominant-power-source") is not None
    del em  # silence unused


@pytest.mark.asyncio
async def test_internal_relay_setter_routes_to_relay_resolver() -> None:
    manifest = DeviceManifest(instances=(_panel_inst(), _circuit_inst()))
    setters = SetterRegistry()
    em = Emitter(manifest, setters, FakeMqttClient())
    await em.start()

    # Simulate /set circuit/relay = false (open).
    handler = setters.get("circuit", "circuit/relay")
    assert handler is not None
    await handler("circuit", "kitchen", "circuit/relay", False)

    snap = await em.publish_tick(
        TickInputs(current_time=0.0, grid_online=True, circuits={"kitchen": 1000.0}),
    )
    assert snap.circuits["kitchen"].relay_state == "OPEN"
    assert snap.circuits["kitchen"].instant_power_w == 0.0


@pytest.mark.asyncio
async def test_internal_name_setter_overrides_display_name() -> None:
    manifest = DeviceManifest(instances=(_panel_inst(), _circuit_inst()))
    setters = SetterRegistry()
    em = Emitter(manifest, setters, FakeMqttClient())
    await em.start()

    handler = setters.get("circuit", "circuit/name")
    assert handler is not None
    await handler("circuit", "kitchen", "circuit/name", "Kitchen Lights")

    snap = await em.publish_tick(
        TickInputs(current_time=0.0, grid_online=True, circuits={"kitchen": 1000.0}),
    )
    assert snap.circuits["kitchen"].name == "Kitchen Lights"


@pytest.mark.asyncio
async def test_internal_priority_setter_changes_shed_decision() -> None:
    manifest = DeviceManifest(
        instances=(
            _panel_inst(),
            _circuit_inst("ev", priority="MUST_HAVE"),
            _bess_inst(),
        )
    )
    setters = SetterRegistry()
    em = Emitter(
        manifest,
        setters,
        FakeMqttClient(),
        bess_configs=(
            BESSConfig(
                instance_id="abc-123-bess",
                nameplate_capacity_kwh=13.5,
                max_charge_w=3500.0,
                max_discharge_w=3500.0,
            ),
        ),
        load_shedding_config=LoadSheddingConfig(),
    )
    await em.start()

    # Initially MUST_HAVE: not shed off-grid.
    snap = await em.publish_tick(
        TickInputs(current_time=0.0, grid_online=False, circuits={"ev": 7000.0}),
    )
    assert snap.circuits["ev"].relay_state == "CLOSED"

    # Operator changes priority to OFF_GRID.
    handler = setters.get("circuit", "circuit/shed-priority")
    assert handler is not None
    await handler("circuit", "ev", "circuit/shed-priority", "OFF_GRID")

    snap2 = await em.publish_tick(
        TickInputs(current_time=1.0, grid_online=False, circuits={"ev": 7000.0}),
    )
    assert snap2.circuits["ev"].relay_state == "OPEN"
    assert snap2.circuits["ev"].priority == "OFF_GRID"


@pytest.mark.asyncio
async def test_internal_dom_power_source_setter_overrides_meter() -> None:
    manifest = DeviceManifest(instances=(_panel_inst(), _circuit_inst()))
    setters = SetterRegistry()
    em = Emitter(manifest, setters, FakeMqttClient())
    await em.start()

    # Default on-grid → GRID
    snap1 = await em.publish_tick(
        TickInputs(current_time=0.0, grid_online=True, circuits={"kitchen": 100.0}),
    )
    assert snap1.pcs.dominant_power_source == "GRID"

    handler = setters.get("panel", "core/dominant-power-source")
    assert handler is not None
    await handler("panel", "abc-123", "core/dominant-power-source", "BATTERY")

    snap2 = await em.publish_tick(
        TickInputs(current_time=1.0, grid_online=True, circuits={"kitchen": 100.0}),
    )
    assert snap2.pcs.dominant_power_source == "BATTERY"


@pytest.mark.asyncio
async def test_producer_handler_takes_precedence_over_internal() -> None:
    """If the producer registered its own handler, the emitter does NOT clobber it."""
    captured: list[str] = []

    async def producer_handler(
        entity_class: str,
        instance_id: str,
        prop_path: str,
        value: object,
    ) -> None:
        del entity_class, instance_id, prop_path
        captured.append(str(value))

    manifest = DeviceManifest(instances=(_panel_inst(), _circuit_inst()))
    setters = SetterRegistry()
    setters.register("circuit", "circuit/relay", producer_handler)
    em = Emitter(manifest, setters, FakeMqttClient())
    await em.start()

    handler = setters.get("circuit", "circuit/relay")
    assert handler is producer_handler
    await handler("circuit", "kitchen", "circuit/relay", True)
    assert captured == ["True"]
    # Internal RelayResolver was NOT updated since producer handler ran instead.
    relay_state, _req = em.relays.state("kitchen")
    assert relay_state == RelayState.CLOSED  # default


@pytest.mark.asyncio
async def test_dipole_circuit_per_leg_currents() -> None:
    manifest = DeviceManifest(
        instances=(
            _panel_inst(),
            _circuit_inst("hvac", tabs="1,2"),
        )
    )
    em = Emitter(manifest, _registry(), FakeMqttClient())
    await em.start()
    snap = await em.publish_tick(
        TickInputs(current_time=0.0, grid_online=True, circuits={"hvac": 4800.0}),
    )
    # Dipole 4800W / 240V = 20A on each leg
    assert snap.meter.upstream_l1_current_a == pytest.approx(20.0)
    assert snap.meter.upstream_l2_current_a == pytest.approx(20.0)
    # Per-circuit current uses line-to-line voltage for dipole.
    assert snap.circuits["hvac"].current_a == pytest.approx(20.0)
    assert snap.circuits["hvac"].is_240v is True


@pytest.mark.asyncio
async def test_lugs_energy_integrates_its_own_meter_not_the_circuits_behind_it() -> None:
    """A lugs meter's registers are the counterpart of its own ``active-power``.

    With PV and load running at once the lugs carry only the net, in one
    direction. Summing the circuits behind them advanced ``imported-energy`` AND
    ``exported-energy`` in the same tick -- a state a live panel never produces,
    and one that makes ``imported - exported`` describe something other than what
    actually flowed through the lugs.
    """
    manifest = DeviceManifest(
        instances=(
            _panel_inst(),
            DeviceInstance("lugs", "lugs-upstream", "Upstream lugs", {"direction": "upstream"}),
            _circuit_inst("kitchen", tabs="1"),
            _circuit_inst("solar", tabs="3"),
        )
    )
    powers = {"kitchen": 2000.0, "solar": -6000.0}
    em = Emitter(manifest, _registry(), FakeMqttClient())
    await em.start()
    # The first observation only establishes the integrator's clock.
    await em.publish_tick(TickInputs(current_time=0.0, grid_online=True, circuits=powers))
    snap = await em.publish_tick(
        TickInputs(current_time=3600.0, grid_online=True, circuits=powers)
    )

    lugs = snap.lugs["lugs-upstream"]
    # 2000 W of load against 6000 W of PV: 4000 W leaves through the lugs, for an
    # hour. Exactly one register may move, and by the integral of that power.
    assert lugs.active_power_w == pytest.approx(-4000.0)
    assert lugs.exported_energy_wh == pytest.approx(4000.0)
    assert lugs.imported_energy_wh == 0.0
    # The circuits behind it are busy in both directions at once -- which is what
    # the registers used to be handed.
    assert snap.circuits["kitchen"].consumed_energy_wh > 0.0
    assert snap.circuits["solar"].produced_energy_wh > 0.0
