# Schema-Driven Architecture Plan

## Status

- **Phase 1 (Schema Registry)**: Complete — `schema.py` with `HomieSchemaRegistry`
- **Phase 2 (Startup Validation)**: Complete — `_validate_against_schema()` in publisher
- **Phase 3 (Schema-Driven /set)**: Complete — `_get_set_topics_from_schema()`
- **Phase 4 (Declarative Mapping)**: Complete — extractor tuples per node type, `_apply_extractors`
- **Phase 5 (Type Validation)**: Complete — `validate_value()` in schema, debug-gated in publisher

## Problem

The Homie schema (`data/homie_schema.json`) and the publisher (`publisher.py`) are completely decoupled. The schema is served as a static file; the publisher hardcodes property names, settable flags, type strings, and unit semantics in imperative `_map_*` methods. When the schema changes, nothing alerts the developer that the publisher is out of sync.

## Goal

Make the schema the contract — not just documentation. The publisher's job shifts from "know what to publish" to "know how to compute each value the schema requires."

## Phases

### Phase 1: Schema Registry

Parse `homie_schema.json` into typed dataclasses at startup. This is the foundation — all subsequent phases reference the registry.

**New file:** `src/span_panel_simulator/schema.py`

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

**Changes:**
- `app.py`: Parse schema via `load_schema()`, pass registry to `BootstrapHttpServer` and `PanelInstance`
- `bootstrap.py`: Accept registry, serve `registry.to_json()` (or keep raw text alongside)
- `publisher.py`: Accept registry reference in constructor

### Phase 2: Startup Validation

After the first `publish_init`, cross-reference published properties against the schema. Log warnings for drift — no behavior change.

**In `publisher.py`:**

After `_snapshot_to_properties()` returns the full property map, extract the set of `(node_id, property_name)` pairs that were published. For each node in `$description`, look up its type in the registry and compare:

- **Missing**: schema declares property, publisher didn't emit it → `WARNING`
- **Extra**: publisher emitted property not in schema → `WARNING`

This runs once at startup (inside `publish_init`), not on every diff tick.

**Test:** A dedicated test loads the bundled schema, publishes a full snapshot, and asserts zero missing/extra warnings. This test breaks immediately when schema and publisher diverge.

### Phase 3: Schema-Driven `/set` Topics

Replace the hardcoded `get_set_topics()` with a schema-driven implementation.

**Current** (hardcoded):
```python
topics.append(self._set_topic(NODE_CORE, "dominant-power-source"))
for node_uuid in self._circuit_uuid_map.values():
    topics.append(self._set_topic(node_uuid, "relay"))
    topics.append(self._set_topic(node_uuid, "shed-priority"))
```

**New** (schema-driven):
```python
for node_id, node_type_id in self._description_nodes.items():
    node_type = self._schema.node_types.get(node_type_id)
    if node_type is None:
        continue
    for prop_name, prop in node_type.properties.items():
        if prop.settable:
            topics.append(self._set_topic(node_id, prop_name))
```

**Requires:** `_build_description` stores a `_description_nodes: dict[str, str]` mapping `node_id -> type_id` so `get_set_topics` can iterate it.

**Benefit:** When the schema adds a new `settable: true` property, the simulator subscribes automatically.

### Phase 4: Declarative Property Mapping

Replace imperative `_map_*` methods with a registry of `(property_name, extractor)` tuples per node type.

**New types in `publisher.py`:**
```python
PropertyExtractor = Callable[[SpanPanelSnapshot], str | None]
CircuitPropertyExtractor = Callable[[SpanCircuitSnapshot], str | None]
```

**Example — core properties:**
```python
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

**Generic map method:**
```python
def _map_from_extractors(
    self,
    node_id: str,
    extractors: Sequence[tuple[str, Callable[..., str | None]]],
    source: object,
    props: dict[str, str],
) -> None:
    for prop_name, extractor in extractors:
        value = extractor(source)
        if value is not None:
            props[self._prop_topic(node_id, prop_name)] = value
```

**Benefits:**
- Each property mapping is a single line — easy to audit against schema
- The extractor list can be validated against the schema registry at startup (Phase 2 validation becomes: "every schema property has an extractor, every extractor has a schema property")
- Adding a new property is adding one tuple, not editing a method body

**Migration:** Convert one `_map_*` method at a time. Each conversion is a self-contained refactor with no behavioral change (verified by existing tests).

### Phase 5: Type Validation (dev/test mode)

Add optional datatype validation that checks published values against schema declarations:

- `enum` with `format: "A,B,C"` → value must be one of A, B, C
- `float` → value must parse as float
- `integer` → value must parse as int and have no decimal
- `boolean` → value must be "true" or "false"

**Implementation:** A `validate_value(prop: SchemaProperty, value: str) -> str | None` function that returns an error message or None. Called during `publish_init` in debug mode (controlled by log level or a flag).

**Not called during `publish_diff`** — validation is a startup check only.

## File Impact Summary

| File | Phase | Change |
|------|-------|--------|
| `schema.py` (new) | 1 | Schema registry dataclasses + parser |
| `app.py` | 1 | Parse schema, pass registry through |
| `bootstrap.py` | 1 | Accept registry (keep serving raw JSON) |
| `publisher.py` | 1-4 | Accept registry, validate, declarative mapping |
| `homie_const.py` | 3 | TYPE_* constants still used but validated against registry |
| `tests/test_publisher.py` | 2 | Schema drift detection test |
| `tests/test_schema.py` (new) | 1,5 | Registry parsing + type validation tests |

## Ordering Constraint

Each phase builds on the previous. Phase 1 is prerequisite for all others. Phases 2 and 3 are independent of each other but both require Phase 1. Phase 4 benefits from Phase 2 (validation catches mistakes during refactor). Phase 5 requires Phase 1.

```
Phase 1 (Registry)
  ├── Phase 2 (Startup Validation)
  ├── Phase 3 (Schema-Driven /set)
  └── Phase 4 (Declarative Mapping) ← benefits from Phase 2
       └── Phase 5 (Type Validation)
```
