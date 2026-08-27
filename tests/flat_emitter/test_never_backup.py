"""``never-backup`` is a commissioning lock, not a priority value.

The eBus schema migration guide states the flat mechanism precisely: the three
flat booleans ``never-backup``, ``always-on`` and ``sheddable`` are
"independent commissioning inputs stored as separate fields in each circuit's
commissioning state", and ``never-backup`` maps to the Homie ``$settable``
attribute on ``load-shed/priority`` -- "Published with ``$settable =
!never-backup``. Locked-priority circuits (commissioned permanently OFF_GRID)
appear as ``priority = OFF_GRID, $settable = false``; user-configurable
circuits appear as ``$settable = true``."

``NEVER`` is an ordinary priority *value* on a settable circuit -- "never shed
this circuit" -- and says nothing about whether an installer locked it. A
masked retained-topic capture of a production enclosure on r202633 (16
circuits) shows the two are independent: ``load-shed/priority`` reads OFF_GRID
x10, SOC_THRESHOLD x4 and NEVER x2, and ``$settable`` is present and true on
all 16, both NEVER circuits included.

These tests hold the two apart:

* a NEVER-priority circuit publishes ``never-backup = false`` and takes a
  priority ``/set``;
* a circuit commissioned ``never_backup`` publishes ``never-backup = true``,
  reads ``shed-priority = OFF_GRID`` whatever its template says, and drops a
  priority ``/set`` the way an always-on circuit drops a relay ``/set``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from span_panel_simulator.emitter_adapter.spec_generator import build_manifest
from span_panel_simulator.flat_emitter import (
    BESSConfig,
    DeviceInstance,
    DeviceManifest,
    Emitter,
    LoadSheddingConfig,
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
    never_backup: bool | None = None,
) -> DeviceInstance:
    metadata = {
        "tab-numbers": tabs,
        "breaker-rating-a": "20",
        "default-priority": priority,
        "relay-behavior": relay_behavior,
        "placement": "downstream-of-lugs",
    }
    if never_backup is not None:
        metadata["never-backup"] = "true" if never_backup else "false"
    return DeviceInstance("circuit", cid, cid.title(), metadata=metadata)


def _bess_inst() -> DeviceInstance:
    return DeviceInstance(
        "bess",
        "abc-123-bess",
        "Battery",
        metadata={"vendor-name": "Span", "nameplate-capacity-kwh": "13.5"},
    )


def _bess_cfg(soc_pct: float = 80.0) -> BESSConfig:
    return BESSConfig(
        instance_id="abc-123-bess",
        nameplate_capacity_kwh=13.5,
        max_charge_w=3500.0,
        max_discharge_w=3500.0,
        initial_soc_pct=soc_pct,
    )


def _retained(mqtt: FakeMqttClient) -> dict[str, str]:
    return {topic: payload.decode() for topic, payload, _qos, _retain in mqtt.published}


# ---- a NEVER priority is not a commissioning lock ---------------------------


@pytest.mark.asyncio
async def test_never_priority_circuit_is_not_never_backup() -> None:
    """ "Stays on in an outage" is a priority value, not an installer lock.

    Deriving the flag from the value made every NEVER circuit report itself
    locked, which is what the capture refutes: both of its NEVER circuits carry
    a settable priority."""
    mqtt = FakeMqttClient()
    em = Emitter(
        DeviceManifest(instances=(_panel_inst(), _circuit_inst("lights", priority="NEVER"))),
        SetterRegistry(),
        mqtt,
    )
    await em.start()
    snap = await em.publish_tick(
        TickInputs(current_time=0.0, grid_online=True, circuits={"lights": 80.0}),
    )
    assert snap.circuits["lights"].is_never_backup is False
    assert _retained(mqtt)["ebus/5/abc-123/lights/never-backup"] == "false"


@pytest.mark.asyncio
async def test_never_priority_circuit_accepts_a_priority_set() -> None:
    """A NEVER circuit is user-configurable, so its priority takes a ``/set``."""
    setters = SetterRegistry()
    mqtt = FakeMqttClient()
    em = Emitter(
        DeviceManifest(instances=(_panel_inst(), _circuit_inst("lights", priority="NEVER"))),
        setters,
        mqtt,
    )
    await em.start()

    handler = setters.get("circuit", "circuit/shed-priority")
    assert handler is not None
    await handler("circuit", "lights", "circuit/shed-priority", "OFF_GRID")

    snap = await em.publish_tick(
        TickInputs(current_time=0.0, grid_online=True, circuits={"lights": 80.0}),
    )
    assert snap.circuits["lights"].priority == "OFF_GRID"
    assert _retained(mqtt)["ebus/5/abc-123/lights/shed-priority"] == "OFF_GRID"


@pytest.mark.asyncio
async def test_never_priority_circuit_is_not_sheddable() -> None:
    """``sheddable`` is ``priority != NEVER && relay-controllable``; a NEVER
    circuit is never shed, so the flag is false."""
    em = Emitter(
        DeviceManifest(instances=(_panel_inst(), _circuit_inst("lights", priority="NEVER"))),
        SetterRegistry(),
        FakeMqttClient(),
    )
    await em.start()
    snap = await em.publish_tick(
        TickInputs(current_time=0.0, grid_online=True, circuits={"lights": 80.0}),
    )
    assert snap.circuits["lights"].is_sheddable is False


# ---- the commissioning lock -------------------------------------------------


@pytest.mark.asyncio
async def test_never_backup_circuit_publishes_the_flag() -> None:
    mqtt = FakeMqttClient()
    em = Emitter(
        DeviceManifest(
            instances=(
                _panel_inst(),
                _circuit_inst("hot_tub", priority="NICE_TO_HAVE", never_backup=True),
            )
        ),
        SetterRegistry(),
        mqtt,
    )
    await em.start()
    snap = await em.publish_tick(
        TickInputs(current_time=0.0, grid_online=True, circuits={"hot_tub": 3000.0}),
    )
    assert snap.circuits["hot_tub"].is_never_backup is True
    assert _retained(mqtt)["ebus/5/abc-123/hot_tub/never-backup"] == "true"


@pytest.mark.asyncio
async def test_never_backup_circuit_reads_off_grid() -> None:
    """ "Locked-priority circuits (commissioned permanently OFF_GRID) appear as
    ``priority = OFF_GRID, $settable = false``" -- the lock carries the value,
    so a locked circuit reads OFF_GRID whatever its template declared."""
    mqtt = FakeMqttClient()
    em = Emitter(
        DeviceManifest(
            instances=(
                _panel_inst(),
                _circuit_inst("hot_tub", priority="NICE_TO_HAVE", never_backup=True),
            )
        ),
        SetterRegistry(),
        mqtt,
    )
    await em.start()
    snap = await em.publish_tick(
        TickInputs(current_time=0.0, grid_online=True, circuits={"hot_tub": 3000.0}),
    )
    assert snap.circuits["hot_tub"].priority == "OFF_GRID"
    assert _retained(mqtt)["ebus/5/abc-123/hot_tub/shed-priority"] == "OFF_GRID"


@pytest.mark.asyncio
async def test_never_backup_circuit_refuses_a_priority_set() -> None:
    """The lock is what ``$settable = false`` means; the panel drops the write
    rather than acting on it, as an always-on relay drops a relay ``/set``."""
    setters = SetterRegistry()
    mqtt = FakeMqttClient()
    em = Emitter(
        DeviceManifest(
            instances=(
                _panel_inst(),
                _circuit_inst("hot_tub", priority="NICE_TO_HAVE", never_backup=True),
            )
        ),
        setters,
        mqtt,
    )
    await em.start()

    handler = setters.get("circuit", "circuit/shed-priority")
    assert handler is not None
    await handler("circuit", "hot_tub", "circuit/shed-priority", "NEVER")

    snap = await em.publish_tick(
        TickInputs(current_time=0.0, grid_online=True, circuits={"hot_tub": 3000.0}),
    )
    assert snap.circuits["hot_tub"].priority == "OFF_GRID"
    assert snap.circuits["hot_tub"].is_never_backup is True


@pytest.mark.asyncio
async def test_never_backup_circuit_sheds_off_grid_as_the_configuration() -> None:
    """A locked circuit is permanently OFF_GRID, so it sheds when the grid
    drops -- and the actor is the commissioning configuration, which the flat
    ``relay-requester`` enum spells ``NEVER_BACKUP`` (v1.0 ``CONFIGURATION``)."""
    mqtt = FakeMqttClient()
    em = Emitter(
        DeviceManifest(
            instances=(
                _panel_inst(),
                _circuit_inst("hot_tub", priority="NICE_TO_HAVE", never_backup=True),
                _bess_inst(),
            )
        ),
        SetterRegistry(),
        mqtt,
        bess_configs=(_bess_cfg(),),
        load_shedding_config=LoadSheddingConfig(soc_threshold_pct=20.0),
    )
    await em.start()
    snap = await em.publish_tick(
        TickInputs(current_time=0.0, grid_online=False, circuits={"hot_tub": 3000.0}),
    )
    assert snap.circuits["hot_tub"].relay_state == "OPEN"
    assert snap.circuits["hot_tub"].relay_requester == "NEVER_BACKUP"
    assert snap.circuits["hot_tub"].is_sheddable is True
    assert _retained(mqtt)["ebus/5/abc-123/hot_tub/relay-requester"] == "NEVER_BACKUP"


@pytest.mark.asyncio
async def test_always_on_circuit_is_not_sheddable() -> None:
    """``sheddable`` is the second conjunct too: a relay that cannot open
    cannot shed, however its priority reads."""
    em = Emitter(
        DeviceManifest(
            instances=(
                _panel_inst(),
                _circuit_inst("smoke_alarm", priority="OFF_GRID", relay_behavior="always-on"),
            )
        ),
        SetterRegistry(),
        FakeMqttClient(),
    )
    await em.start()
    snap = await em.publish_tick(
        TickInputs(current_time=0.0, grid_online=True, circuits={"smoke_alarm": 50.0}),
    )
    assert snap.circuits["smoke_alarm"].is_sheddable is False


# ---- the shipped configurations --------------------------------------------


@pytest.mark.asyncio
async def test_default_config_publishes_never_backup_false_on_every_circuit() -> None:
    """``configs/default_MAIN_40.yaml`` commissions no locked circuit, and
    several of its templates carry priority NEVER. Every circuit must therefore
    publish ``never-backup = false`` -- the regression that hid the priority
    select for two thirds of a panel."""
    profile = yaml.safe_load(Path("configs/default_MAIN_40.yaml").read_text())
    manifest = build_manifest(profile)
    mqtt = FakeMqttClient()
    em = Emitter(manifest, SetterRegistry(), mqtt)
    await em.start()
    snap = await em.publish_tick(TickInputs(current_time=0.0, grid_online=True, circuits={}))

    assert any(c.priority == "NEVER" for c in snap.circuits.values()), (
        "fixture no longer exercises NEVER-priority circuits"
    )
    locked = sorted(cid for cid, c in snap.circuits.items() if c.is_never_backup)
    assert locked == []
    retained = _retained(mqtt)
    published = {
        topic: value for topic, value in retained.items() if topic.endswith("/never-backup")
    }
    assert published, "no circuit published a never-backup topic"
    assert set(published.values()) == {"false"}
