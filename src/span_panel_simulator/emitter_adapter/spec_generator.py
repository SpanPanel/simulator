"""Build a ``DeviceManifest`` from a loaded clone-profile dict.

The manifest carries identity + physics keys per the v0.3.0 emitter contract.
The emitter parses physics fields via ``ManifestPhysicsView`` at construction
and uses them for relay-state ownership, energy integration, panel-meter
aggregation, and per-leg current calculation.

``build_manifest`` is a thin orchestrator: it asks each ``_xxx_instance(s)``
helper to build its slice of the manifest and concatenates the results.
Adding a new device class (e.g. MID, second BESS) is a single ``append``
on the orchestrator and a new helper — no need to touch the rest."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from span_panel_simulator.emitter_adapter.instance_ids import stable_circuit_uuid
from span_panel_simulator.flat_emitter import DeviceInstance, DeviceManifest
from span_panel_simulator.panel_models import PANEL_SIZE_TO_MODEL

if TYPE_CHECKING:
    from span_panel_simulator.config_types import (
        BESSConfigYAML,
        CircuitDefinitionExtended,
        CircuitTemplateExtended,
        SimulationConfig,
    )

# Allowed Homie-convention values for circuit relay-behavior and PV
# inverter-type — both are dash-form on the wire.  YAML clones written
# before the dash convention may still use underscore form, so we
# normalise once before validating.
_VALID_RELAY_BEHAVIORS = frozenset({"controllable", "non-controllable", "always-on"})

#: Homie 5, Topic IDs: "A topic level ID MAY ONLY contain lowercase letters from `a`
#: to `z`, numbers from `0` to `9` as well as the hyphen character (`-`)." Applied to
#: the EVSE serial because that serial *is* the drive's node id, so it is a topic
#: level and not merely a property value.
_HOMIE_ID = re.compile(r"[a-z0-9-]+")
_VALID_INVERTER_TYPES = frozenset({"hybrid", "ac-coupled"})


def _normalise_relay_behavior(raw: str) -> str:
    """Coerce ``controllable`` / ``non_controllable`` / ``always_on`` (any
    underscore-vs-dash form) to the dashed Homie convention; default to
    ``controllable`` when unrecognised."""
    candidate = raw.lower().replace("_", "-")
    return candidate if candidate in _VALID_RELAY_BEHAVIORS else "controllable"


def _normalise_inverter_type(raw: str) -> str:
    """Coerce ``hybrid`` / ``ac_coupled`` (any underscore-vs-dash form) to
    the dashed Homie convention; default to ``ac-coupled`` when unrecognised."""
    candidate = raw.lower().replace("_", "-")
    return candidate if candidate in _VALID_INVERTER_TYPES else "ac-coupled"


def build_manifest(profile: SimulationConfig) -> DeviceManifest:
    """Walk the loaded SimulationConfig dict; emit a DeviceManifest the emitter
    consumes. Identity + physics — no behaviour, no schedule, no modelling."""
    instances: list[DeviceInstance] = [
        _panel_instance(profile),
        *_lugs_instances(profile),
        *_circuit_instances(profile),
    ]
    bess = _bess_instance(profile)
    if bess is not None:
        instances.append(bess)
    pv = _pv_instance(profile)
    if pv is not None:
        instances.append(pv)
    instances.extend(_evse_instances(profile))
    return DeviceManifest(instances=tuple(instances))


def _panel_instance(profile: SimulationConfig) -> DeviceInstance:
    panel_cfg = profile["panel_config"]
    panel_id = panel_cfg["serial_number"]
    panel_size = int(panel_cfg.get("total_tabs", 40))
    panel_model = PANEL_SIZE_TO_MODEL.get(panel_size, f"MAIN_{panel_size}")
    return DeviceInstance(
        entity_class="panel",
        instance_id=panel_id,
        display_name=panel_cfg.get("display_name", "Span Panel"),
        metadata={
            "vendor-name": "Span",
            "serial-number": panel_id,
            "firmware-version": profile.get("firmware_version", "sim/v0.1.0"),
            "hardware-version": profile.get("hardware_version", "rev2"),
            "panel-size": str(panel_size),
            "main-breaker-rating-a": str(int(panel_cfg.get("main_size", 200))),
            "panel-model": panel_model,
            "postal-code": str(panel_cfg.get("postal_code", "94103")),
            "time-zone": str(panel_cfg.get("time_zone", "America/Los_Angeles")),
            "service-voltage-v": str(panel_cfg.get("service_voltage_v", 240.0)),
            "line-voltage-v": str(panel_cfg.get("line_voltage_v", 120.0)),
            "islandable": "true" if _islandable(profile) else "false",
        },
    )


def _lugs_instances(profile: SimulationConfig) -> list[DeviceInstance]:
    return [
        DeviceInstance(
            entity_class="lugs",
            instance_id="lugs-upstream",
            display_name="Upstream lugs",
            metadata={"direction": "upstream"},
        ),
        DeviceInstance(
            entity_class="lugs",
            instance_id="lugs-downstream",
            display_name="Downstream lugs",
            metadata={"direction": "downstream"},
        ),
    ]


def _circuit_instances(profile: SimulationConfig) -> list[DeviceInstance]:
    templates = profile.get("circuit_templates") or {}
    instances: list[DeviceInstance] = []
    for idx, c in enumerate(profile.get("circuits") or [], start=1):
        tabs = c.get("tabs") or [0]
        template_name = c.get("template", "")
        template: CircuitTemplateExtended | None = templates.get(template_name)
        if template is None:
            relay_behavior_raw = "controllable"
            priority = "NICE_TO_HAVE"
            breaker_rating = 20.0
        else:
            relay_behavior_raw = str(template.get("relay_behavior", "controllable"))
            priority = str(template.get("priority", "NICE_TO_HAVE")).upper()
            # ``breaker_rating_a`` is the producer-side legacy key; the typed
            # ``breaker_rating`` (no units suffix) is the canonical YAML field.
            # Read both so manifests built from older clones still work.
            breaker_rating = float(
                template.get("breaker_rating_a") or template.get("breaker_rating", 20),
            )
        relay_behavior = _normalise_relay_behavior(relay_behavior_raw)
        instances.append(
            DeviceInstance(
                entity_class="circuit",
                instance_id=stable_circuit_uuid(c["id"]),
                display_name=c.get("name", c["id"]),
                metadata={
                    "tab-numbers": ",".join(str(int(t)) for t in tabs if t),
                    "breaker-rating-a": str(breaker_rating),
                    "default-priority": priority,
                    "relay-behavior": relay_behavior,
                    "placement": str(c.get("placement", "downstream-of-lugs")),
                    "always-on": "true" if relay_behavior == "always-on" else "false",
                    # Per-circuit installer lock, read from the circuit and not
                    # the shared template, and never derived from `priority`:
                    # NEVER is a shed value ("never shed"), never-backup is a
                    # commissioning state ("permanently OFF_GRID, not settable").
                    "never-backup": "true" if c.get("never_backup", False) else "false",
                    "pcs-priority": str(c.get("pcs_priority", idx)),
                },
            ),
        )
    return instances


def _bess_instance(profile: SimulationConfig) -> DeviceInstance | None:
    bess_cfg = profile.get("bess") or {}
    if not bess_cfg.get("enabled"):
        return None
    bess_meta: dict[str, str] = {
        "vendor-name": str(bess_cfg.get("vendor", "Span")),
        "nameplate-capacity-kwh": str(bess_cfg.get("nameplate_capacity_kwh", 13.5)),
        "relative-position": str(bess_cfg.get("relative_position", "UPSTREAM")),
    }
    if "product_name" in bess_cfg:
        bess_meta["product-name"] = str(bess_cfg["product_name"])
    if "model" in bess_cfg:
        bess_meta["model"] = str(bess_cfg["model"])
    if "serial_number" in bess_cfg:
        bess_meta["serial-number"] = str(bess_cfg["serial_number"])
    if "firmware_version" in bess_cfg:
        bess_meta["firmware-version"] = str(bess_cfg["firmware_version"])
    if "feed" in bess_cfg:
        bess_meta["feed"] = str(bess_cfg["feed"])
    if "initial_soe_kwh" in bess_cfg:
        bess_meta["initial-soe-kwh"] = str(bess_cfg["initial_soe_kwh"])
    return DeviceInstance(
        entity_class="bess",
        instance_id=_bess_instance_id(bess_cfg),
        display_name="Battery",
        metadata=bess_meta,
    )


def _pv_instance(profile: SimulationConfig) -> DeviceInstance | None:
    pv_cfg = profile.get("pv") or {}
    pv_feed = _feed_circuit_id(profile, "pv")
    if not pv_cfg.get("enabled") and pv_feed is None:
        return None
    inverter_type = _normalise_inverter_type(str(pv_cfg.get("inverter_type", "ac-coupled")))
    relative_position = pv_cfg.get("relative_position")
    if relative_position is None:
        relative_position = "IN_PANEL" if pv_feed is not None else "UPSTREAM"
    metadata = {
        "vendor-name": str(pv_cfg.get("vendor", "Enphase")),
        "nameplate-capacity-w": str(
            pv_cfg.get("nameplate_capacity_w") or _device_nameplate_w(profile, "pv", 5000.0),
        ),
        "inverter-type": inverter_type,
        "relative-position": str(relative_position),
    }
    if "product_name" in pv_cfg:
        metadata["product-name"] = str(pv_cfg["product_name"])
    if "serial_number" in pv_cfg:
        metadata["serial-number"] = str(pv_cfg["serial_number"])
    if "firmware_version" in pv_cfg:
        metadata["firmware-version"] = str(pv_cfg["firmware_version"])
    if "feed" in pv_cfg:
        metadata["feed"] = str(pv_cfg["feed"])
    elif pv_feed is not None:
        metadata["feed"] = pv_feed
    return DeviceInstance(
        entity_class="pv",
        instance_id=str(pv_cfg.get("instance_id", "pv")),
        display_name="Solar",
        metadata=metadata,
    )


def _evse_instances(profile: SimulationConfig) -> list[DeviceInstance]:
    evse_cfg = profile.get("evse") or {}
    feed_circuits = _circuits_for_device_type(profile, "evse")
    explicit_feed = str(evse_cfg["feed"]) if "feed" in evse_cfg else None
    if explicit_feed:
        feeds = [explicit_feed]
    else:
        feeds = [stable_circuit_uuid(circuit["id"]) for circuit in feed_circuits]
    if not feeds and evse_cfg.get("enabled"):
        feeds = [""]
    if not feeds:
        return []

    panel_id = profile["panel_config"]["serial_number"]
    base_metadata = {
        "vendor-name": str(evse_cfg.get("vendor", "SPAN")),
        "product-name": str(evse_cfg.get("product", "SPAN Drive")),
        "part-number": str(evse_cfg.get("part_number", "SPN-DRV-001")),
        "firmware-version": str(evse_cfg.get("firmware_version", "sim/v0.1.0")),
        "max-current-a": str(evse_cfg.get("max_current_a", 32.0)),
    }
    instances: list[DeviceInstance] = []
    for idx, feed in enumerate(feeds, start=1):
        # The node id IS the drive serial, because that is what firmware publishes.
        #
        # This used to be a positional slot -- `evse`, `evse-2` -- which no panel
        # emits. `schema_0` keys its snapshot on the node id verbatim, and the
        # integration builds the HA device identifier from that key, so against
        # firmware the EVSE device is already serial-identified. v1.0 names the same
        # device `<panel>-<serial>` and its parser strips that back to the serial,
        # reproducing the flat key exactly -- which is what carries an EVSE's identity,
        # its history and its automations across a firmware upgrade unbroken.
        #
        # A positional slot broke that: upgrading re-keyed every EVSE and left the old
        # HA device stranded with its entities unavailable. The bug was only ever in
        # this fixture, but it is the fixture the upgrade path is proved against, so
        # it made a passing test out of a migration that does not survive.
        serial = _evse_serial_number(evse_cfg, panel_id, idx)
        instance_id = serial
        metadata = dict(base_metadata)
        metadata["serial-number"] = serial
        if feed:
            metadata["feed"] = feed
        instances.append(
            DeviceInstance(
                entity_class="evse",
                instance_id=instance_id,
                display_name=_evse_display_name(feed_circuits, idx),
                metadata=metadata,
            ),
        )
    return instances


def _islandable(profile: SimulationConfig) -> bool:
    """A panel can island when a hybrid PV inverter is configured (or any config
    explicitly sets ``islandable: true`` on panel_config)."""
    panel_cfg = profile["panel_config"]
    if "islandable" in panel_cfg:
        return bool(panel_cfg["islandable"])
    pv_cfg = profile.get("pv") or {}
    return bool(pv_cfg.get("enabled") and pv_cfg.get("inverter_type") == "hybrid")


def _bess_instance_id(bess: BESSConfigYAML) -> str:
    return str(bess.get("instance_id", "bess"))


def _feed_circuit_id(profile: SimulationConfig, device_type: str) -> str | None:
    circuit = _first_circuit_for_device_type(profile, device_type)
    if circuit is None:
        return None
    return stable_circuit_uuid(circuit["id"])


def _first_circuit_for_device_type(
    profile: SimulationConfig,
    device_type: str,
) -> CircuitDefinitionExtended | None:
    circuits = _circuits_for_device_type(profile, device_type)
    return circuits[0] if circuits else None


def _circuits_for_device_type(
    profile: SimulationConfig,
    device_type: str,
) -> list[CircuitDefinitionExtended]:
    templates = profile.get("circuit_templates") or {}
    circuits: list[CircuitDefinitionExtended] = []
    for circuit in profile.get("circuits") or []:
        template = templates.get(circuit.get("template", ""))
        if template is not None and template.get("device_type") == device_type:
            circuits.append(circuit)
    return circuits


def _evse_serial_number(evse_cfg: object, panel_id: str, idx: int) -> str:
    """The drive's serial, which is also its Homie node id.

    Lower-case because it is a topic level, not merely a value. Homie 5 allows a
    topic-level id to contain only ``a``-``z``, ``0``-``9`` and ``-``; the previous
    default was ``SIM-EVSE-…``, which was legal as a *property value* and would have
    been an illegal topic the moment it became the node id. Real drive serials are
    already topic-safe, which is why firmware can use one as a node id at all.

    A configured serial is validated rather than quietly rewritten: sanitising it
    would publish an id that no longer matches the ``info/serial-number`` beside it,
    and a consumer keying identity off the serial would silently stop matching across
    a firmware upgrade -- the exact failure this whole change exists to remove.
    """
    if isinstance(evse_cfg, dict) and "serial_number" in evse_cfg:
        raw = str(evse_cfg["serial_number"])
        serial = raw if idx == 1 else f"{raw}-{idx}"
        if not _HOMIE_ID.fullmatch(serial):
            msg = (
                f"evse.serial_number {serial!r} is not a usable Homie topic id "
                "(lower-case a-z, 0-9 and '-' only). It is published as the drive's "
                "node id, so an id outside that set would put an invalid topic on the "
                "wire."
            )
            raise ValueError(msg)
        return serial
    return f"sim-evse-{panel_id}" if idx == 1 else f"sim-evse-{panel_id}-{idx}"


def _evse_display_name(feed_circuits: list[CircuitDefinitionExtended], idx: int) -> str:
    if 0 <= idx - 1 < len(feed_circuits):
        return str(feed_circuits[idx - 1].get("name", "EV Charger"))
    return "EV Charger"


def _device_nameplate_w(profile: SimulationConfig, device_type: str, default: float) -> float:
    circuit = _first_circuit_for_device_type(profile, device_type)
    if circuit is None:
        return default
    template = (profile.get("circuit_templates") or {}).get(circuit.get("template", ""))
    if template is None:
        return default
    energy_profile = template.get("energy_profile", {})
    return float(energy_profile.get("nameplate_capacity_w", default))
