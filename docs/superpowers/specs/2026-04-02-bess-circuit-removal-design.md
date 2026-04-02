# BESS Circuit Removal — Design Spec

**Date:** 2026-04-02
**Status:** Draft
**Scope:** Remove phantom battery circuit; BESS exists only as GFE on upstream lugs

## Problem

The simulator creates a `battery_storage` circuit for BESS, but real SPAN panels
with BESS have the battery on the upstream lugs acting as Grid Forming Equipment
(GFE). There is no breaker, no relay, and no circuit for the battery. The current
design forces a fake circuit into existence and uses it as a data proxy between
the energy system and the API layer, requiring workarounds like excluding the
battery circuit from load summation and writing resolved power back onto the
circuit each tick.

## Physical Topology

```
Utility <--[grid sensor]--> BESS <--> Panel (loads + PV)
```

- BESS sits between the grid sensor and the panel on the upstream lugs.
- The grid sensor measures net power at the utility meter point.
- `grid_sensor = load - pv - bess_discharge + bess_charge` (all positive magnitudes).
- BESS charges only from solar excess — never from the grid.
- Grid sensor: positive = importing, negative = exporting.

## Approach

Remove the battery circuit entirely (Approach B — full energy system decoupling).
BESS state exists only in `EnergySystem` / `BESSUnit` and `SpanBatterySnapshot`.
No circuit proxy, no writeback, no exclusion workarounds.

## Changes

### 1. Configuration Schema

**Remove from each config YAML that defines them:**
- `battery_storage` circuit entry from the `circuits` list
- `battery` template from `circuit_templates` (only consumer was the circuit entry above)
- `BatteryBehavior` TypedDict from `config_types.py`

**Add:**
- Top-level `bess` key in YAML config (peer to `panel_config`):

```yaml
bess:
  enabled: true
  nameplate_capacity_kwh: 13.5
  max_charge_w: 3500.0
  max_discharge_w: 3500.0
  charge_efficiency: 0.95
  discharge_efficiency: 0.95
  backup_reserve_pct: 20.0
  charge_mode: solar-gen
  charge_hours: [8, 9, 10, 11, 12, 13, 14, 15]
  discharge_hours: [16, 17, 18, 19, 20, 21, 22]
```

- `BESSConfigYAML` TypedDict in `config_types.py` for the new top-level section.

### 2. Engine Refactor

**Remove from `engine.py`:**
- `_find_battery_circuit()` — no battery circuit exists
- `_is_battery_circuit()` — no battery circuit to detect
- `_apply_battery_behavior()` — battery power resolved by EnergySystem, not circuit behavior
- Circuit-writeback logic in `get_snapshot()` — BESS power no longer proxied through a circuit
- Battery circuit exclusion in `_collect_power_inputs()` and `_powers_to_energy_inputs()`

**Modify in `engine.py`:**
- `_build_energy_system()` — read BESS config from `self._config["bess"]` instead of
  scanning circuits for `battery_behavior`. Direct dict-to-`BESSConfig` mapping.
- `get_snapshot()` — build `SpanBatterySnapshot` purely from `EnergySystem.bess` state.
  No circuit snapshot rebuild step.
- `compute_modeling_data()` — battery power from `SystemState` only, no circuit intermediate.

Grid sensor calculation stays in `EnergySystem.tick()` where it already lives. The bus
resolves load, PV, and BESS, then grid power is the remainder.

### 3. Model & Publisher Cleanup

**`energy/types.py` — `BESSConfig`:**
- Remove `feed_circuit_id` field.

**`energy/components.py` — `BESSUnit`:**
- Remove `feed_circuit_id` parameter and property.
- GFE constraint, SOE integration, hybrid PV control unchanged.

**`models.py` — `SpanBatterySnapshot`:**
- Remove `feed_circuit_id` field.

**`publisher.py`:**
- Remove `feed` property publishing from `_map_bess()`.
- `bess-0` MQTT node still publishes SOE, capacity, grid-state.

**`clone.py` — `_enrich_bess_template()`:**
- Refactor to write to top-level `bess` key in cloned config instead of enriching a
  circuit template. The `feed` property from scraped panel data is ignored.

### 4. Test Impact

**`tests/test_clone.py` — `test_bess_mode()`:**
- Rewrite to assert cloned config has top-level `bess` section with expected fields.
  The former battery circuit should not exist in cloned output.

**`tests/test_modeling.py`:**
- Move `battery_behavior` from circuit template fixtures to top-level `bess` key.

**`tests/test_energy/test_scenarios.py`:**
- Remove `feed_circuit_id` from explicit `BESSConfig` constructor calls.
  Energy layer behavior unchanged.

No new tests needed — structural refactor, not behavioral change.

## Out of Scope

- Enforcing "never charge from grid" constraint at the `BESSUnit._resolve_charge()` level
  (currently enforced by the scheduler in `EnergySystem.tick()`; works correctly today).
- AC-coupled BESS behind a breaker (different product configuration, design when needed).
- EVSE two-tab allocation (each EVSE needs two tabs; follow-on task).
