"""Homie schema registry — parsed representation of homie_schema.json.

Provides typed access to node types, property definitions, datatypes, units,
and settable flags. Used by the publisher for validation, /set topic discovery,
and property mapping verification.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SchemaProperty:
    """A single Homie property definition from the schema."""

    key: str  # property key used in MQTT topics (e.g. "active-power")
    name: str  # human-readable name (e.g. "Measured active power")
    datatype: str  # "string", "float", "integer", "boolean", "enum"
    unit: str | None = None  # "W", "A", "kWh", "%", etc.
    format: str | None = None  # enum values: "UNKNOWN,OPEN,CLOSED" or range "1:32:1"
    settable: bool = False


@dataclass(frozen=True, slots=True)
class SchemaNodeType:
    """A Homie node type with its property definitions."""

    type_id: str  # e.g. "energy.ebus.device.circuit"
    properties: dict[str, SchemaProperty] = field(default_factory=dict)

    @property
    def settable_properties(self) -> list[str]:
        """Return property keys marked as settable."""
        return [k for k, p in self.properties.items() if p.settable]


@dataclass(frozen=True, slots=True)
class HomieSchemaRegistry:
    """Parsed Homie schema — typed access to the full schema structure."""

    node_types: dict[str, SchemaNodeType]
    firmware_version: str
    schema_hash: str
    raw_json: str  # original JSON text for HTTP serving

    def get_node_type(self, type_id: str) -> SchemaNodeType | None:
        """Look up a node type by its fully-qualified type ID."""
        return self.node_types.get(type_id)

    def get_property(self, type_id: str, prop_key: str) -> SchemaProperty | None:
        """Look up a specific property within a node type."""
        node_type = self.node_types.get(type_id)
        if node_type is None:
            return None
        return node_type.properties.get(prop_key)


def load_schema(path: Path) -> HomieSchemaRegistry:
    """Parse a homie_schema.json file into a typed registry.

    Args:
        path: Path to the JSON schema file.

    Returns:
        Parsed HomieSchemaRegistry with all node types and properties.

    Raises:
        FileNotFoundError: If the schema file does not exist.
        ValueError: If the schema structure is invalid.
    """
    return _build_registry(path.read_text(encoding="utf-8"))


def _build_registry(raw_json: str) -> HomieSchemaRegistry:
    """Parse Homie schema JSON text into a typed registry."""
    data = json.loads(raw_json)

    if not isinstance(data, dict):
        msg = "Schema root must be a JSON object"
        raise ValueError(msg)

    firmware_version: str = data.get("firmwareVersion", "")
    schema_hash: str = data.get("typesSchemaHash", "")
    types_data = data.get("types", {})

    if not isinstance(types_data, dict):
        msg = "Schema 'types' must be a JSON object"
        raise ValueError(msg)

    node_types: dict[str, SchemaNodeType] = {}

    for type_id, props_data in types_data.items():
        if not isinstance(props_data, dict):
            _LOGGER.warning("Skipping non-dict type entry: %s", type_id)
            continue

        properties: dict[str, SchemaProperty] = {}
        for prop_key, prop_data in props_data.items():
            if not isinstance(prop_data, dict):
                _LOGGER.warning("Skipping non-dict property: %s/%s", type_id, prop_key)
                continue

            prop_name = str(prop_data.get("name", prop_key))
            prop_datatype = str(prop_data.get("datatype", "string"))
            raw_unit = prop_data.get("unit")
            prop_unit: str | None = str(raw_unit) if raw_unit is not None else None
            raw_format = prop_data.get("format")
            prop_format: str | None = str(raw_format) if raw_format is not None else None
            prop_settable = bool(prop_data.get("settable", False))

            properties[prop_key] = SchemaProperty(
                key=prop_key,
                name=prop_name,
                datatype=prop_datatype,
                unit=prop_unit,
                format=prop_format,
                settable=prop_settable,
            )

        node_types[type_id] = SchemaNodeType(
            type_id=type_id,
            properties=properties,
        )

    _LOGGER.info(
        "Loaded schema: %d node types, firmware=%s, hash=%s",
        len(node_types),
        firmware_version,
        schema_hash,
    )

    return HomieSchemaRegistry(
        node_types=node_types,
        firmware_version=firmware_version,
        schema_hash=schema_hash,
        raw_json=raw_json,
    )


_CIRCUIT_TYPE_KEY = "energy.ebus.device.circuit"


def render_for_panel(
    registry: HomieSchemaRegistry,
    total_tabs: int,
) -> HomieSchemaRegistry:
    """Produce a size-specific copy of a schema registry.

    Patches the ``space`` property's ``format`` field to reflect the
    actual panel size and recomputes ``typesSchemaHash`` over the
    resulting ``types`` structure using the same algorithm span-panel-api
    uses for drift detection.

    The input ``registry`` is not mutated.

    Args:
        registry: Template registry (typically the bundled schema).
        total_tabs: Panel size — must be a positive even integer.

    Returns:
        A new ``HomieSchemaRegistry`` whose ``raw_json`` declares
        ``"format": "1:{total_tabs}:1"`` on the circuit ``space`` property
        and whose ``typesSchemaHash`` is derived from the rendered ``types``.

    Raises:
        ValueError: If ``total_tabs`` is not a positive even integer, or
            if the template is missing the expected ``space`` property.
    """
    if total_tabs <= 0 or total_tabs % 2 != 0:
        msg = f"total_tabs must be a positive even integer, got {total_tabs}"
        raise ValueError(msg)

    data = json.loads(registry.raw_json)
    types = data.get("types")
    if not isinstance(types, dict):
        msg = "Template schema missing 'types' object"
        raise ValueError(msg)

    circuit_type = types.get(_CIRCUIT_TYPE_KEY)
    if not isinstance(circuit_type, dict):
        msg = f"Template schema missing '{_CIRCUIT_TYPE_KEY}' node type"
        raise ValueError(msg)

    space_prop = circuit_type.get("space")
    if not isinstance(space_prop, dict):
        msg = f"Template schema missing '{_CIRCUIT_TYPE_KEY}/space' property"
        raise ValueError(msg)

    space_prop["format"] = f"1:{total_tabs}:1"

    types_json = json.dumps(types, sort_keys=True)
    new_hash = "sha256:" + hashlib.sha256(types_json.encode()).hexdigest()[:16]
    data["typesSchemaHash"] = new_hash

    raw_json = json.dumps(data, indent=4) + "\n"
    return _build_registry(raw_json)


def validate_value(prop: SchemaProperty, value: str) -> str | None:
    """Validate a published value against its schema property declaration.

    Returns an error message if validation fails, or ``None`` if the value
    is valid.  Only checks structural/type correctness — not ranges or
    semantic constraints.
    """
    if prop.datatype == "boolean":
        if value not in ("true", "false"):
            return f"expected 'true'/'false', got '{value}'"
    elif prop.datatype == "integer":
        if "." in value:
            return f"integer must not contain decimal, got '{value}'"
        try:
            int(value)
        except ValueError:
            return f"expected integer, got '{value}'"
    elif prop.datatype == "float":
        try:
            float(value)
        except ValueError:
            return f"expected float, got '{value}'"
    elif prop.datatype == "enum" and prop.format:
        allowed = [v.strip() for v in prop.format.split(",")]
        if value not in allowed:
            return f"expected one of [{prop.format}], got '{value}'"
    return None
