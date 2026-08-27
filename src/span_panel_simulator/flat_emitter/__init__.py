"""Flat-schema Homie wire publisher with native-device runtime.

Vendored from ``ebus-emitter`` (https://github.com/electrification-bus/simulator) at
commit ``5b84de8``, version 0.2.1 — the release that corrected the circuit energy
reference frame. MIT licensed, same copyright holders as this repository.

**This copy no longer tracks upstream, by design.** The two diverged permanently when
upstream moved to the parent/child (v1.0) Homie data model while this simulator continues
to publish the flat schema that SPAN firmware r202603-r202627 speaks. Upstream changes are
not ported; fix bugs here.

Why vendored rather than depended on:

- ``ebus-emitter`` is not published to PyPI, so the HA add-on image had no way to install
  it — ``pip install .`` in the Dockerfile silently produced an image that failed at
  startup with ``ModuleNotFoundError: No module named 'ebus_emitter'``.
- The previous arrangement (editable install from ``EBUS_EMITTER_PATH``) worked only for
  developers who had run ``scripts/dev-setup.sh``, and left a cross-repo version skew that
  nothing enforced.
- Because the fork is permanent, an external dependency delivered no upstream changes while
  costing path configuration, stale editable metadata, and a distribution problem for a
  package that will never be published.

It also collapses a real correctness hazard: ``clone.py`` seeds energy accumulators against
what this code publishes. While they lived in separate repos each side could look locally
correct while jointly inverting circuit energy — which is exactly what happened, and what
no single test suite could see. Both ends now sit in one repo under one test run.

Architecture (v0.3.0):

- **Wire layer** (``wire/``): vendored Homie 5 device profiles + mapping descriptors,
  graph builder, lifecycle controller, /set router, property bag diff cache, SDK seam.
- **Native devices** (``native_devices/``): emitter-resident, configured-and-self-driving
  device runtimes (BESS dispatch, load shedding).
- **Manifest physics** (``manifest_physics.py``): typed accessor over
  ``DeviceInstance.metadata`` for physics-relevant fields (voltage, breaker rating,
  tabs/legs, placement, default priority, relay behaviour, commissioning flags).
- **Tick pipeline** (``relay_resolver.py`` + ``priority_resolver.py`` +
  ``energy_integrator.py`` + ``panel_meter.py`` + ``conventions/tab_legs.py``): per-tick
  state machinery the emitter uses to resolve circuit relay state and shed priority,
  integrate energy, derive per-leg currents, and aggregate panel-level fields.

Producer contract (v0.3.0): build a ``DeviceManifest`` once at startup, then call
``Emitter.publish_tick(TickInputs)`` each tick with signed circuit/EVSE powers,
``current_time``, and ``grid_online``. The emitter does the rest."""

from span_panel_simulator.flat_emitter.conventions.tab_legs import Leg, legs_for_tabs
from span_panel_simulator.flat_emitter.emitter import Emitter
from span_panel_simulator.flat_emitter.exceptions import (
    EmitterError,
    EmitterStateError,
    ManifestValidationError,
    MissingSetterError,
    ProfileValidationError,
    RuntimeSpecValidationError,
)
from span_panel_simulator.flat_emitter.manifest import DeviceInstance, DeviceManifest
from span_panel_simulator.flat_emitter.manifest_physics import (
    BessPhysics,
    CircuitPhysics,
    EvsePhysics,
    LugsPhysics,
    ManifestPhysicsView,
    PanelPhysics,
    PvPhysics,
)
from span_panel_simulator.flat_emitter.native_devices import (
    BESSConfig,
    BESSDevice,
    ChargeMode,
    LoadSheddingConfig,
    LoadSheddingDevice,
    NativeDevice,
    NativeTickContext,
)
from span_panel_simulator.flat_emitter.priority_resolver import (
    LOCKED_PRIORITY,
    PriorityResolver,
)
from span_panel_simulator.flat_emitter.relay_resolver import (
    RelayRequester,
    RelayResolver,
    RelayState,
)
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
from span_panel_simulator.flat_emitter.tick_inputs import PanelEnvelopeTick, TickInputs
from span_panel_simulator.flat_emitter.wire.set_router import SetterHandler, SetterRegistry

__all__ = [
    "LOCKED_PRIORITY",
    "BESSConfig",
    "BESSDevice",
    "BessPhysics",
    "ChargeMode",
    "CircuitPhysics",
    "DeviceInstance",
    "DeviceManifest",
    "EbusBatterySnapshot",
    "EbusCircuitSnapshot",
    "EbusEvseSnapshot",
    "EbusLugsSnapshot",
    "EbusPanelDoor",
    "EbusPanelInfo",
    "EbusPanelMeter",
    "EbusPanelPcs",
    "EbusPanelPowerFlows",
    "EbusPanelSnapshot",
    "EbusPanelStatus",
    "EbusPvSnapshot",
    "Emitter",
    "EmitterError",
    "EmitterStateError",
    "EvsePhysics",
    "Leg",
    "LoadSheddingConfig",
    "LoadSheddingDevice",
    "LugsPhysics",
    "ManifestPhysicsView",
    "ManifestValidationError",
    "MissingSetterError",
    "NativeDevice",
    "NativeTickContext",
    "PanelEnvelopeTick",
    "PanelPhysics",
    "PriorityResolver",
    "ProfileValidationError",
    "PvPhysics",
    "RelayRequester",
    "RelayResolver",
    "RelayState",
    "RuntimeSpecValidationError",
    "SetterHandler",
    "SetterRegistry",
    "TickInputs",
    "legs_for_tabs",
]
