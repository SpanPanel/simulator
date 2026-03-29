"""eBus-to-YAML translation — converts scraped panel data to simulator config.

Pure data transformation: takes a ``ScrapedPanel`` (description + retained
property values) and produces a complete YAML config dict matching the
``SimulationConfig`` TypedDict shape.

Design principles:
  - Each circuit gets its own template (``clone_{space}``) for per-circuit
    fidelity.  Users can consolidate via the dashboard later.
  - Energy profile mode is inferred from device node ``feed`` cross-references.
  - No unit conversion is needed — all eBus power values are in watts.
  - The clone serial is ``{original_serial}-clone``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import yaml

from span_panel_simulator.homie_const import TYPE_BESS, TYPE_CIRCUIT, TYPE_EVSE, TYPE_PV
from span_panel_simulator.validation import validate_yaml_config

if TYPE_CHECKING:
    from pathlib import Path

    from span_panel_simulator.scraper import ScrapedPanel

_LOGGER = logging.getLogger(__name__)


def make_clone_serial(original_serial: str) -> str:
    """Derive the clone serial from an original panel serial.

    Ensures the ``sim-`` prefix and appends ``-clone``.
    """
    base = original_serial
    if not base.lower().startswith("sim-"):
        base = f"sim-{base}"
    return f"{base}-clone"


_NIGHT_CHARGING_HOURS: dict[int, float] = {
    0: 1.0,
    1: 1.0,
    2: 1.0,
    3: 1.0,
    4: 1.0,
    5: 1.0,
    6: 0.0,
    7: 0.0,
    8: 0.0,
    9: 0.0,
    10: 0.0,
    11: 0.0,
    12: 0.0,
    13: 0.0,
    14: 0.0,
    15: 0.0,
    16: 0.0,
    17: 0.0,
    18: 0.0,
    19: 0.0,
    20: 0.0,
    21: 0.0,
    22: 0.0,
    23: 0.0,
}


# ------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------


def translate_scraped_panel(
    scraped: ScrapedPanel,
    *,
    host: str | None = None,
    passphrase: str | None = None,
) -> dict[str, object]:
    """Translate a scraped panel into a simulator config dict.

    Args:
        scraped: The scraped panel data.
        host: Source panel IP/hostname (stored in panel_source for refresh).
        passphrase: Source panel passphrase (stored in panel_source for refresh).

    Returns a dict matching the ``SimulationConfig`` TypedDict shape,
    ready for YAML serialisation and ``validate_yaml_config()``.
    """
    nodes = scraped.description.get("nodes", {})
    prefix = f"ebus/5/{scraped.serial_number}"

    # Classify nodes by type
    circuit_nodes = _nodes_of_type(nodes, TYPE_CIRCUIT)
    bess_nodes = _nodes_of_type(nodes, TYPE_BESS)
    pv_nodes = _nodes_of_type(nodes, TYPE_PV)
    evse_nodes = _nodes_of_type(nodes, TYPE_EVSE)

    # Build feed cross-reference: circuit_uuid → device_type
    feed_map = _build_feed_map(scraped.properties, prefix, bess_nodes, pv_nodes, evse_nodes)

    # Extract panel-level values
    main_breaker = _int_prop(scraped.properties, prefix, "core", "breaker-rating") or 200

    # Derive panel size from maximum space value across all circuits
    total_tabs = _derive_total_tabs(scraped.properties, prefix, circuit_nodes)

    clone_serial = make_clone_serial(scraped.serial_number)

    panel_config: dict[str, object] = {
        "serial_number": clone_serial,
        "total_tabs": total_tabs,
        "main_size": main_breaker,
        "latitude": 37.7,
        "longitude": -122.4,
    }

    # Build per-circuit templates and definitions
    templates: dict[str, dict[str, object]] = {}
    circuits: list[dict[str, object]] = []
    used_tabs: set[int] = set()

    for node_uuid in sorted(circuit_nodes):
        result = _translate_circuit(
            scraped.properties,
            prefix,
            node_uuid,
            feed_map,
        )
        if result is None:
            continue

        template_name, template, circuit_def, tabs = result
        templates[template_name] = template
        circuits.append(circuit_def)
        used_tabs.update(tabs)

    # Enrich BESS circuit template
    for bess_id in bess_nodes:
        _enrich_bess_template(scraped.properties, prefix, bess_id, feed_map, templates)

    # Enrich PV circuit template
    for pv_id in pv_nodes:
        _enrich_pv_template(scraped.properties, prefix, pv_id, feed_map, templates)

    # Enrich EVSE circuit templates
    for evse_id in evse_nodes:
        _enrich_evse_template(scraped.properties, prefix, evse_id, feed_map, templates)

    # Battery entities sit between panel lugs and grid — strip their tabs.
    for circ in circuits:
        tpl_name = circ.get("template")
        tpl = templates.get(str(tpl_name), {}) if tpl_name else {}
        bb = tpl.get("battery_behavior")
        if isinstance(bb, dict) and bb.get("enabled"):
            freed = circ.pop("tabs", [])
            if isinstance(freed, list):
                used_tabs -= set(freed)

    # Unmapped tabs
    all_tabs = set(range(1, total_tabs + 1))
    unmapped = sorted(all_tabs - used_tabs)

    config: dict[str, object] = {
        "panel_config": panel_config,
        "circuit_templates": templates,
        "circuits": circuits,
        "unmapped_tabs": unmapped,
        "simulation_params": {
            "update_interval": 5,
            "time_acceleration": 1.0,
            "noise_factor": 0.02,
            "enable_realistic_behaviors": True,
        },
    }

    if host is not None:
        config["panel_source"] = {
            "origin_serial": scraped.serial_number,
            "host": host,
            "passphrase": passphrase,
            "last_synced": datetime.now(UTC).isoformat(),
        }

    _LOGGER.info(
        "Translated panel %s: %d circuits, %d templates, bess=%s, pv=%s, evse=%s",
        clone_serial,
        len(circuits),
        len(templates),
        bool(bess_nodes),
        bool(pv_nodes),
        bool(evse_nodes),
    )

    return config


def update_config_from_scrape(
    config: dict[str, object],
    scraped: ScrapedPanel,
) -> bool:
    """Update an existing config dict with fresh values from a scrape.

    Patches energy seeds and ``panel_source.last_synced`` in-place.
    Used by the startup refresh path.

    Note: typical_power is intentionally *not* updated here.  The eBus
    ``active-power`` property is an instantaneous snapshot, not a
    representative average.  The HA integration derives a more meaningful
    typical_power from historical observation and pushes it via
    ``apply_usage_profiles``.

    Returns True if any values were changed.
    """
    prefix = f"ebus/5/{scraped.serial_number}"
    templates = config.get("circuit_templates")
    if not isinstance(templates, dict):
        return False

    nodes = scraped.description.get("nodes", {})
    circuit_nodes = _nodes_of_type(nodes, TYPE_CIRCUIT)

    changed = False

    for node_uuid in circuit_nodes:
        space = _int_prop(scraped.properties, prefix, node_uuid, "space")
        if space is None:
            continue

        template_name = f"clone_{space}"
        template = templates.get(template_name)
        if not isinstance(template, dict):
            continue

        ep = template.get("energy_profile")
        if not isinstance(ep, dict):
            continue

        # Update energy seeds
        imported = _float_prop(scraped.properties, prefix, node_uuid, "imported-energy")
        if (
            imported is not None
            and imported > 0
            and ep.get("initial_consumed_energy_wh") != imported
        ):
            ep["initial_consumed_energy_wh"] = imported
            changed = True

        exported = _float_prop(scraped.properties, prefix, node_uuid, "exported-energy")
        if (
            exported is not None
            and exported > 0
            and ep.get("initial_produced_energy_wh") != exported
        ):
            ep["initial_produced_energy_wh"] = exported
            changed = True

    # Update last_synced timestamp
    panel_source = config.get("panel_source")
    if isinstance(panel_source, dict):
        panel_source["last_synced"] = datetime.now(UTC).isoformat()
        changed = True

    return changed


def clone_config_path(config_dir: Path, original_serial: str) -> Path:
    """Return the default clone config path for a given serial."""
    return config_dir / f"{original_serial}-clone.yaml"


def write_clone_config(
    config: dict[str, object],
    config_dir: Path,
    original_serial: str,
    *,
    filename: str | None = None,
) -> Path:
    """Validate and write a cloned config to the config directory.

    Args:
        config: The config dict to write.
        config_dir: Directory to write into.
        original_serial: Original panel serial (used for default filename).
        filename: Optional custom filename override (must end with .yaml).

    Returns the path to the written file.

    Raises:
        ValueError: If the config fails validation.
    """
    validate_yaml_config(config)

    if filename is None:
        filename = f"{original_serial}-clone.yaml"
    output_path = config_dir / filename
    output_path.write_text(
        yaml.dump(config, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )

    _LOGGER.info("Wrote clone config to %s", output_path)
    return output_path


def update_config_location(config_path: Path, latitude: float, longitude: float) -> str:
    """Update latitude, longitude, and timezone in a YAML config file.

    Reads the existing config, sets the new coordinates and derived
    IANA timezone, then writes the file back.

    Args:
        config_path: Path to the YAML config file.
        latitude: Degrees north.
        longitude: Degrees east.

    Returns:
        The resolved IANA timezone string.

    Raises:
        ValueError: If the file does not contain a valid config dict.
    """
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        msg = f"Invalid config format in {config_path}"
        raise ValueError(msg)

    panel_cfg = raw.get("panel_config", {})
    panel_cfg["latitude"] = latitude
    panel_cfg["longitude"] = longitude

    from timezonefinder import TimezoneFinder

    tz_result = TimezoneFinder().timezone_at(lat=latitude, lng=longitude)
    tz_name: str = str(tz_result) if tz_result is not None else "America/Los_Angeles"
    panel_cfg["time_zone"] = tz_name

    raw["panel_config"] = panel_cfg

    config_path.write_text(
        yaml.dump(raw, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )

    _LOGGER.info(
        "Updated location in %s: %.4f, %.4f → %s",
        config_path.name,
        latitude,
        longitude,
        tz_name,
    )
    return tz_name


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------


def _get_prop(
    properties: dict[str, str],
    prefix: str,
    node_id: str,
    prop: str,
) -> str | None:
    """Extract a single property value from the scraped topic map."""
    topic = f"{prefix}/{node_id}/{prop}"
    return properties.get(topic)


def _float_prop(
    properties: dict[str, str],
    prefix: str,
    node_id: str,
    prop: str,
) -> float | None:
    """Extract a float property, returning None if absent or unparseable."""
    raw = _get_prop(properties, prefix, node_id, prop)
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _int_prop(
    properties: dict[str, str],
    prefix: str,
    node_id: str,
    prop: str,
) -> int | None:
    """Extract an integer property."""
    raw = _get_prop(properties, prefix, node_id, prop)
    if raw is None:
        return None
    try:
        return int(float(raw))
    except ValueError:
        return None


def _bool_prop(
    properties: dict[str, str],
    prefix: str,
    node_id: str,
    prop: str,
) -> bool | None:
    """Extract a boolean property (Homie convention: ``"true"``/``"false"``)."""
    raw = _get_prop(properties, prefix, node_id, prop)
    if raw is None:
        return None
    return raw.lower() == "true"


def _nodes_of_type(
    nodes: dict[str, dict[str, str]],
    node_type: str,
) -> list[str]:
    """Return node IDs matching the given Homie type string."""
    return [
        node_id
        for node_id, node_def in nodes.items()
        if isinstance(node_def, dict) and node_def.get("type") == node_type
    ]


def _build_feed_map(
    properties: dict[str, str],
    prefix: str,
    bess_nodes: list[str],
    pv_nodes: list[str],
    evse_nodes: list[str],
) -> dict[str, str]:
    """Build a mapping from circuit UUID to device type based on feed properties.

    Device nodes (BESS, PV, EVSE) have a ``feed`` property whose value is the
    UUID of the circuit they're associated with.
    """
    feed_map: dict[str, str] = {}

    for node_id in bess_nodes:
        circuit_uuid = _get_prop(properties, prefix, node_id, "feed")
        if circuit_uuid:
            feed_map[circuit_uuid] = "bess"

    for node_id in pv_nodes:
        circuit_uuid = _get_prop(properties, prefix, node_id, "feed")
        if circuit_uuid:
            feed_map[circuit_uuid] = "pv"

    for node_id in evse_nodes:
        circuit_uuid = _get_prop(properties, prefix, node_id, "feed")
        if circuit_uuid:
            feed_map[circuit_uuid] = "evse"

    return feed_map


def _derive_total_tabs(
    properties: dict[str, str],
    prefix: str,
    circuit_nodes: list[str],
) -> int:
    """Derive panel size from the maximum space value across circuits."""
    max_space = 0
    for node_id in circuit_nodes:
        space = _int_prop(properties, prefix, node_id, "space")
        if space is not None and space > max_space:
            max_space = space

        # If dipole, the companion tab is space + 2
        is_dipole = _bool_prop(properties, prefix, node_id, "dipole")
        if is_dipole and space is not None:
            companion = space + 2
            if companion > max_space:
                max_space = companion

    # Round up to standard panel sizes
    for standard_size in (16, 24, 32, 40, 48):
        if max_space <= standard_size:
            return standard_size

    return max_space


def _translate_circuit(
    properties: dict[str, str],
    prefix: str,
    node_uuid: str,
    feed_map: dict[str, str],
) -> tuple[str, dict[str, object], dict[str, object], list[int]] | None:
    """Translate a single circuit node into a template and definition.

    Returns (template_name, template_dict, circuit_def, tabs) or None
    if the circuit cannot be translated (missing space).
    """
    space = _int_prop(properties, prefix, node_uuid, "space")
    if space is None:
        _LOGGER.warning("Circuit %s has no space property, skipping", node_uuid)
        return None

    name = _get_prop(properties, prefix, node_uuid, "name") or f"Circuit {space}"
    is_dipole = _bool_prop(properties, prefix, node_uuid, "dipole") or False
    breaker_rating = _int_prop(properties, prefix, node_uuid, "breaker-rating") or 20
    active_power = _float_prop(properties, prefix, node_uuid, "active-power")
    priority = _get_prop(properties, prefix, node_uuid, "shed-priority") or "NEVER"
    always_on = _bool_prop(properties, prefix, node_uuid, "always-on") or False

    # Tabs
    tabs = [space, space + 2] if is_dipole else [space]
    voltage = 240.0 if is_dipole else 120.0

    # Energy profile mode from feed cross-reference
    device_role = feed_map.get(node_uuid)
    mode = _device_role_to_mode(device_role)

    # Relay behavior
    relay_behavior = "non_controllable" if always_on else "controllable"

    # Power range and typical power
    max_power = breaker_rating * voltage
    typical = abs(active_power) if active_power is not None else max_power * 0.3
    # Clamp typical to max
    typical = min(typical, max_power)

    if mode == "producer":
        power_range = [-max_power, 0.0]
        typical_power = -typical if typical > 0 else -max_power * 0.6
    elif mode == "bidirectional":
        power_range = [-max_power, max_power]
        typical_power = typical
    else:
        power_range = [0.0, max_power]
        typical_power = typical

    # Seed energy accumulators from scraped values
    imported_energy = _float_prop(properties, prefix, node_uuid, "imported-energy")
    exported_energy = _float_prop(properties, prefix, node_uuid, "exported-energy")

    # Build template
    energy_profile: dict[str, object] = {
        "mode": mode,
        "power_range": power_range,
        "typical_power": typical_power,
        "power_variation": 0.1,
    }

    if imported_energy is not None and imported_energy > 0:
        energy_profile["initial_consumed_energy_wh"] = imported_energy
    if exported_energy is not None and exported_energy > 0:
        energy_profile["initial_produced_energy_wh"] = exported_energy

    template: dict[str, object] = {
        "energy_profile": energy_profile,
        "relay_behavior": relay_behavior,
        "priority": priority,
        "breaker_rating": breaker_rating,
    }

    if device_role == "evse":
        template["device_type"] = "evse"
    elif device_role == "pv":
        template["device_type"] = "pv"

    # Circuit definition
    circuit_id = f"circuit_{space}"
    template_name = f"clone_{space}"

    circuit_def: dict[str, object] = {
        "id": circuit_id,
        "name": name,
        "template": template_name,
        "tabs": tabs,
    }

    return template_name, template, circuit_def, tabs


def _device_role_to_mode(device_role: str | None) -> str:
    """Map a device role from the feed map to an energy profile mode."""
    if device_role == "pv":
        return "producer"
    if device_role in ("bess", "evse"):
        return "bidirectional"
    return "consumer"


def _enrich_bess_template(
    properties: dict[str, str],
    prefix: str,
    bess_node_id: str,
    feed_map: dict[str, str],
    templates: dict[str, dict[str, object]],
) -> None:
    """Add battery_behavior to the circuit template fed by this BESS node."""
    circuit_uuid = _get_prop(properties, prefix, bess_node_id, "feed")
    template = _find_template_for_feed(circuit_uuid, feed_map, templates, properties, prefix)
    if template is None:
        return

    nameplate = _float_prop(properties, prefix, bess_node_id, "nameplate-capacity")
    nameplate_kwh = nameplate if nameplate is not None else 13.5

    # Derive max charge/discharge from breaker rating
    breaker = template.get("breaker_rating", 40)
    breaker_val = float(breaker) if isinstance(breaker, int | float) else 40.0
    ep = template.get("energy_profile")
    is_240v = False
    if isinstance(ep, dict):
        pr = ep.get("power_range")
        if isinstance(pr, list) and len(pr) == 2:
            is_240v = abs(pr[0]) > 120 * breaker_val
    voltage = 240.0 if is_240v else 120.0
    max_power = breaker_val * voltage * 0.8

    template["battery_behavior"] = {
        "enabled": True,
        "charge_mode": "custom",
        "nameplate_capacity_kwh": nameplate_kwh,
        "backup_reserve_pct": 20.0,
        "charge_efficiency": 0.95,
        "discharge_efficiency": 0.95,
        "max_charge_power": max_power,
        "max_discharge_power": max_power,
        "charge_hours": [0, 1, 2, 3, 4, 5],
        "discharge_hours": [16, 17, 18, 19, 20, 21],
        "idle_hours": [6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 22, 23],
    }


def _enrich_pv_template(
    properties: dict[str, str],
    prefix: str,
    pv_node_id: str,
    feed_map: dict[str, str],
    templates: dict[str, dict[str, object]],
) -> None:
    """Enrich the PV circuit template with nameplate capacity and solar profile."""
    circuit_uuid = _get_prop(properties, prefix, pv_node_id, "feed")
    template = _find_template_for_feed(circuit_uuid, feed_map, templates, properties, prefix)
    if template is None:
        return

    nameplate = _float_prop(properties, prefix, pv_node_id, "nameplate-capacity")
    if nameplate is not None and nameplate > 0:
        ep = template.get("energy_profile")
        if isinstance(ep, dict):
            ep["nameplate_capacity_w"] = nameplate
            ep["power_range"] = [-nameplate, 0.0]
            ep["typical_power"] = -nameplate * 0.6


def _enrich_evse_template(
    properties: dict[str, str],
    prefix: str,
    evse_node_id: str,
    feed_map: dict[str, str],
    templates: dict[str, dict[str, object]],
) -> None:
    """Enrich the EVSE circuit template with time-of-day charging profile."""
    circuit_uuid = _get_prop(properties, prefix, evse_node_id, "feed")
    template = _find_template_for_feed(circuit_uuid, feed_map, templates, properties, prefix)
    if template is None:
        return

    template["time_of_day_profile"] = {
        "enabled": True,
        "hour_factors": dict(_NIGHT_CHARGING_HOURS),
    }


def _find_template_for_feed(
    circuit_uuid: str | None,
    feed_map: dict[str, str],
    templates: dict[str, dict[str, object]],
    properties: dict[str, str],
    prefix: str,
) -> dict[str, object] | None:
    """Find the template associated with a circuit UUID via the feed map.

    The feed map maps circuit_uuid → device_role.  We need to find which
    template_name (``clone_{space}``) corresponds to that circuit UUID by
    looking up the circuit's ``space`` property.
    """
    if circuit_uuid is None:
        return None

    space = _int_prop(properties, prefix, circuit_uuid, "space")
    if space is None:
        return None

    template_name = f"clone_{space}"
    return templates.get(template_name)
