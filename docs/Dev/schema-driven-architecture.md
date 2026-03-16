# Schema-Driven Architecture

## Problem

The Homie schema (`data/homie_schema.json`) and the publisher (`publisher.py`) are completely decoupled. The schema is served as a static file; the publisher hardcodes property names, settable flags, type strings, and unit semantics in imperative `_map_*` methods. When the schema changes, nothing alerts the developer that the publisher is out of sync.

## Goal

Make the schema the contract — not just documentation. The publisher's job shifts from "know what to publish" to "know how to compute each value the schema requires."

## Schema Registry

`src/span_panel_simulator/schema.py` parses `homie_schema.json` into typed dataclasses at startup. All downstream consumers reference the registry.

```
SchemaProperty(frozen dataclass)
  - name: str              # human-readable name
  - datatype: str          # "string" | "float" | "integer" | "boolean" | "enum"
  - unit: str | None       # "W", "A", "kWh", "%", etc.
  - format: str | None     # enum values: "UNKNOWN,OPEN,CLOSED"
  - settable: bool         # whether /set is accepted

SchemaNodeType(frozen dataclass)
  - type_id: str           # "energy.ebus.device.circuit"
  - properties: dict[str, SchemaProperty]

HomieSchemaRegistry(frozen dataclass)
  - node_types: dict[str, SchemaNodeType]
  - firmware_version: str
  - schema_hash: str

load_schema(path: Path) -> HomieSchemaRegistry
  Parse JSON, return typed registry.
```

- `app.py` parses the schema via `load_schema()` and passes the registry to `BootstrapHttpServer` and `PanelInstance`
- `bootstrap.py` accepts the registry and serves the raw JSON
- `publisher.py` accepts the registry reference in its constructor

## Startup Validation

After the first `publish_init`, the publisher cross-references published properties against the schema via `_validate_against_schema()`. For each node in `$description`, it looks up the type in the registry and compares:

- **Missing**: schema declares property, publisher didn't emit it → `WARNING`
- **Extra**: publisher emitted property not in schema → `WARNING`

This runs once at startup (inside `publish_init`), not on every diff tick. A dedicated test loads the bundled schema, publishes a full snapshot, and asserts zero missing/extra warnings — breaking immediately when schema and publisher diverge.

## Schema-Driven `/set` Topics

`_get_set_topics_from_schema()` replaces the former hardcoded `/set` subscription list:

```python
for node_id, node_type_id in self._description_nodes.items():
    node_type = self._schema.node_types.get(node_type_id)
    if node_type is None:
        continue
    for prop_name, prop in node_type.properties.items():
        if prop.settable:
            topics.append(self._set_topic(node_id, prop_name))
```

`_build_description` stores a `_description_nodes: dict[str, str]` mapping `node_id -> type_id` so `get_set_topics` can iterate it. When the schema adds a new `settable: true` property, the simulator subscribes automatically.

## Declarative Property Mapping

Imperative `_map_*` methods are replaced with a registry of `(property_name, extractor)` tuples per node type, applied via `_apply_extractors`.

```python
PropertyExtractor = Callable[[SpanPanelSnapshot], str | None]
CircuitPropertyExtractor = Callable[[SpanCircuitSnapshot], str | None]

_CORE_PROPERTIES: list[tuple[str, PropertyExtractor]] = [
    ("vendor-name", lambda s: "SPAN"),
    ("serial-number", lambda s: s.serial_number),
    ("software-version", lambda s: s.firmware_version),
    ("door", lambda s: s.door_state or "CLOSED"),
    ("relay", lambda s: s.main_relay_state or "CLOSED"),
    ("l1-voltage", lambda s: _fmt_float(s.l1_voltage, 1) if s.l1_voltage is not None else None),
    ...
]
```

Each property mapping is a single line — easy to audit against the schema. The extractor list is validated against the schema registry at startup (every schema property has an extractor, every extractor has a schema property). Adding a new property is adding one tuple, not editing a method body.

## Type Validation

`validate_value(prop: SchemaProperty, value: str) -> str | None` checks published values against schema declarations, gated by debug log level:

- `enum` with `format: "A,B,C"` → value must be one of A, B, C
- `float` → value must parse as float
- `integer` → value must parse as int and have no decimal
- `boolean` → value must be "true" or "false"

Called during `publish_init` only — not during `publish_diff`.

## File Summary

| File | Role |
|------|------|
| `schema.py` | Schema registry dataclasses + parser |
| `app.py` | Parses schema, passes registry through |
| `bootstrap.py` | Accepts registry, serves raw JSON |
| `publisher.py` | Accepts registry; startup validation; declarative mapping |
| `homie_const.py` | TYPE_* constants validated against registry |
| `tests/test_publisher.py` | Schema drift detection test |
| `tests/test_schema.py` | Registry parsing + type validation tests |

## Phase Dependencies

Each layer builds on the previous:

```
Schema Registry
  ├── Startup Validation
  ├── Schema-Driven /set
  └── Declarative Mapping ← benefits from Startup Validation
       └── Type Validation
```
