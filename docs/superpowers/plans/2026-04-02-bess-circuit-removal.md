# BESS Circuit Removal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the phantom battery circuit so BESS exists only as GFE on the upstream lugs, with state living exclusively in the energy system layer.

**Architecture:** BESS configuration moves from a circuit template's `battery_behavior` dict to a top-level `bess` YAML section. The engine reads BESS config directly from there instead of scanning circuits. All circuit-proxy writeback logic, battery circuit detection helpers, and `feed_circuit_id` references are removed. The energy system's power resolution is unchanged.

**Tech Stack:** Python 3.14, YAML configs, pytest

---

### Task 1: Add `BESSConfigYAML` TypedDict and remove `BatteryBehavior`

**Files:**
- Modify: `src/span_panel_simulator/config_types.py:99-120`

- [ ] **Step 1: Replace `BatteryBehavior` with `BESSConfigYAML`**

Replace the `BatteryBehavior` TypedDict (lines 99-120) with a new `BESSConfigYAML` TypedDict for the top-level YAML section:

```python
class BESSConfigYAML(TypedDict, total=False):
    """Top-level BESS configuration in the simulator YAML."""

    enabled: bool
    nameplate_capacity_kwh: float
    max_charge_w: float
    max_discharge_w: float
    charge_efficiency: float
    discharge_efficiency: float
    backup_reserve_pct: float
    charge_mode: Literal["self-consumption", "custom", "backup-only"]
    charge_hours: list[int]
    discharge_hours: list[int]
```

- [ ] **Step 2: Update any imports of `BatteryBehavior`**

Search for imports of `BatteryBehavior` across the codebase and replace with `BESSConfigYAML` where needed, or remove the import if the consuming code is also being deleted (e.g., `_apply_battery_behavior` helpers).

Run: `grep -rn "BatteryBehavior" src/`

Remove or replace each occurrence. The helpers `_get_charge_power`, `_get_discharge_power`, `_get_idle_power`, `_get_solar_intensity_from_config` in engine.py use `BatteryBehavior` as a type hint — these methods are deleted in Task 4, so no replacement needed.

- [ ] **Step 3: Run type checker**

Run: `mypy src/span_panel_simulator/config_types.py`
Expected: PASS

- [ ] **Step 4: Commit**

```
git add src/span_panel_simulator/config_types.py
git commit -m "Replace BatteryBehavior TypedDict with BESSConfigYAML"
```

---

### Task 2: Remove `feed_circuit_id` from energy layer

**Files:**
- Modify: `src/span_panel_simulator/energy/types.py:94-112`
- Modify: `src/span_panel_simulator/energy/components.py:93-142`
- Modify: `src/span_panel_simulator/energy/system.py:73-90`
- Modify: `src/span_panel_simulator/models.py:71-85`

- [ ] **Step 1: Remove `feed_circuit_id` from `BESSConfig`**

In `src/span_panel_simulator/energy/types.py`, remove line 108:

```python
    feed_circuit_id: str = ""
```

- [ ] **Step 2: Remove `feed_circuit_id` from `BESSUnit.__init__`**

In `src/span_panel_simulator/energy/components.py`, remove the `feed_circuit_id` parameter (line 109) and the assignment `self.feed_circuit_id = feed_circuit_id` (line 132).

- [ ] **Step 3: Remove `feed_circuit_id` from `EnergySystem.from_config`**

In `src/span_panel_simulator/energy/system.py`, remove line 85:

```python
                feed_circuit_id=bc.feed_circuit_id,
```

- [ ] **Step 4: Remove `feed_circuit_id` from `SpanBatterySnapshot`**

In `src/span_panel_simulator/models.py`, remove line 85:

```python
    feed_circuit_id: str | None = None
```

- [ ] **Step 5: Run type checker**

Run: `mypy src/span_panel_simulator/energy/ src/span_panel_simulator/models.py`
Expected: May show errors in engine.py and publisher.py (fixed in later tasks). Energy layer itself should be clean.

- [ ] **Step 6: Commit**

```
git add src/span_panel_simulator/energy/types.py src/span_panel_simulator/energy/components.py src/span_panel_simulator/energy/system.py src/span_panel_simulator/models.py
git commit -m "Remove feed_circuit_id from energy layer and models"
```

---

### Task 3: Remove `feed` publishing from publisher

**Files:**
- Modify: `src/span_panel_simulator/publisher.py:417-426`

- [ ] **Step 1: Remove `feed_circuit_id` publishing from `_map_bess`**

In `src/span_panel_simulator/publisher.py`, remove lines 423-424:

```python
        if bat.feed_circuit_id:
            p[self._prop_topic(n, "feed")] = self._ensure_circuit_uuid(bat.feed_circuit_id)
```

- [ ] **Step 2: Run type checker**

Run: `mypy src/span_panel_simulator/publisher.py`
Expected: PASS

- [ ] **Step 3: Commit**

```
git add src/span_panel_simulator/publisher.py
git commit -m "Remove feed property publishing from BESS MQTT node"
```

---

### Task 4: Remove battery circuit logic from behavior engine

**Files:**
- Modify: `src/span_panel_simulator/engine.py:100,147-150,156-168,271-279,461-512,514-544,546-560`
- Modify: `src/span_panel_simulator/behavior_mutable_state.py`

This task removes all battery-circuit-specific logic from `RealisticBehaviorEngine`. The energy system (not the behavior engine) drives BESS power.

- [ ] **Step 1: Remove `_last_battery_direction` from `__init__`**

In `engine.py` line 100, remove:

```python
        self._last_battery_direction: str = "idle"
```

- [ ] **Step 2: Remove `last_battery_direction` property**

Remove lines 147-150:

```python
    @property
    def last_battery_direction(self) -> str:
        """Most recent battery direction set by charge mode logic."""
        return self._last_battery_direction
```

- [ ] **Step 3: Remove `_last_battery_direction` from `capture_mutable_state` and `restore_mutable_state`**

In `capture_mutable_state` (line 158), remove the `last_battery_direction` kwarg.
In `restore_mutable_state` (line 167), remove the `self._last_battery_direction = ...` line.

Updated `capture_mutable_state`:

```python
    def capture_mutable_state(self) -> BehaviorEngineMutableState:
        """Return a deep snapshot of tick-local mutable fields."""
        return BehaviorEngineMutableState(
            circuit_cycle_states=copy.deepcopy(self._circuit_cycle_states),
            grid_offline=self._grid_offline,
        )
```

Updated `restore_mutable_state`:

```python
    def restore_mutable_state(self, state: BehaviorEngineMutableState) -> None:
        """Restore fields previously captured with :meth:`capture_mutable_state`."""
        self._circuit_cycle_states = copy.deepcopy(state.circuit_cycle_states)
        self._grid_offline = state.grid_offline
```

- [ ] **Step 4: Remove `last_battery_direction` from `BehaviorEngineMutableState`**

In `src/span_panel_simulator/behavior_mutable_state.py`, remove line 18:

```python
    last_battery_direction: str
```

- [ ] **Step 5: Remove `_apply_battery_behavior` call site**

In `engine.py` lines 271-279, remove the battery behavior block:

```python
        # Apply battery behavior
        battery_behavior = template.get("battery_behavior", {})
        if isinstance(battery_behavior, dict) and battery_behavior.get("enabled", False):
            base_power = self._apply_battery_behavior(
                base_power,
                template,
                current_time,
                stochastic_noise=stochastic_noise,
            )
```

- [ ] **Step 6: Remove `_apply_battery_behavior` method and its helpers**

Remove these methods entirely from `engine.py`:
- `_apply_battery_behavior` (lines 461-512)
- `_get_charge_power` (lines 514-518)
- `_get_discharge_power` (lines 520-524)
- `_get_idle_power` (lines 526-544)
- `_get_solar_intensity_from_config` (lines 546-560)

Also remove the `_get_demand_factor_from_config` method if it exists (check the lines following `_get_solar_intensity_from_config`).

- [ ] **Step 7: Run type checker**

Run: `mypy src/span_panel_simulator/engine.py src/span_panel_simulator/behavior_mutable_state.py`
Expected: May show errors from later-task removals. Battery behavior methods should be cleanly gone.

- [ ] **Step 8: Commit**

```
git add src/span_panel_simulator/engine.py src/span_panel_simulator/behavior_mutable_state.py
git commit -m "Remove battery behavior logic from behavior engine"
```

---

### Task 5: Refactor engine to read BESS config from top-level YAML

**Files:**
- Modify: `src/span_panel_simulator/engine.py` — `_build_energy_system()` (lines 1741-1822)

- [ ] **Step 1: Replace circuit-scanning BESS config with top-level config read**

Replace the BESS config block in `_build_energy_system()` (lines 1776-1812) with a direct read from `self._config.get("bess", {})`:

```python
        bess_config: BESSConfig | None = None
        bess_yaml = self._config.get("bess", {})
        if isinstance(bess_yaml, dict) and bess_yaml.get("enabled", False):
            nameplate = float(bess_yaml.get("nameplate_capacity_kwh", 13.5))
            hybrid = pv_config is not None and pv_config.inverter_type == "hybrid"
            charge_hours_raw: list[int] = bess_yaml.get("charge_hours", [])
            discharge_hours_raw: list[int] = bess_yaml.get("discharge_hours", [])
            panel_tz = (
                str(self._behavior_engine.panel_timezone)
                if self._behavior_engine is not None
                else RealisticBehaviorEngine._DEFAULT_TZ
            )
            charge_mode = str(bess_yaml.get("charge_mode", "self-consumption"))
            bess_config = BESSConfig(
                nameplate_kwh=nameplate,
                max_charge_w=abs(float(bess_yaml.get("max_charge_w", 3500.0))),
                max_discharge_w=abs(float(bess_yaml.get("max_discharge_w", 3500.0))),
                charge_efficiency=float(bess_yaml.get("charge_efficiency", 0.95)),
                discharge_efficiency=float(bess_yaml.get("discharge_efficiency", 0.95)),
                backup_reserve_pct=float(bess_yaml.get("backup_reserve_pct", 20.0)),
                hybrid=hybrid,
                initial_soe_kwh=(
                    self._energy_system.bess.soe_kwh
                    if self._energy_system is not None and self._energy_system.bess is not None
                    else None
                ),
                panel_serial=self._config["panel_config"]["serial_number"],
                charge_hours=tuple(charge_hours_raw),
                discharge_hours=tuple(discharge_hours_raw),
                panel_timezone=panel_tz,
                charge_mode=charge_mode,
            )
```

Note: field names in the YAML now match `BESSConfig` directly (`max_charge_w` not `max_charge_power`).

- [ ] **Step 2: Run type checker**

Run: `mypy src/span_panel_simulator/engine.py`
Expected: May still show errors from battery circuit references not yet removed (Task 6).

- [ ] **Step 3: Commit**

```
git add src/span_panel_simulator/engine.py
git commit -m "Read BESS config from top-level YAML instead of circuit templates"
```

---

### Task 6: Remove battery circuit detection and writeback from engine

**Files:**
- Modify: `src/span_panel_simulator/engine.py`

- [ ] **Step 1: Remove `_find_battery_circuit` method**

Delete lines 1733-1739 (the method and its docstring). After Task 5's rewrite of `_build_energy_system`, this is the earlier line-number block — verify exact location.

- [ ] **Step 2: Remove `_is_battery_circuit` static method**

Delete lines 1391-1395.

- [ ] **Step 3: Remove battery circuit exclusion from `_collect_power_inputs`**

In `_collect_power_inputs` (lines 1705-1731), remove the `_is_battery_circuit` branch. The loop becomes:

```python
        for circuit in self._circuits.values():
            power = circuit.instant_power_w
            if circuit.energy_mode == "producer":
                pv_power += power
            else:
                load_power += power
```

Update the docstring to remove the BESS exclusion note.

- [ ] **Step 4: Remove battery circuit exclusion from `_powers_to_energy_inputs`**

In `_powers_to_energy_inputs` (lines 1397-1424), remove the `_is_battery_circuit` branch. Same simplification:

```python
        for cid, power in circuit_powers.items():
            circuit = self._circuits[cid]
            if circuit.energy_mode == "producer":
                pv_power += power
            else:
                load_power += power
```

Update the docstring to remove the BESS exclusion note.

- [ ] **Step 5: Remove battery circuit references from `get_snapshot`**

In `get_snapshot()`:

1. Remove line 1157: `battery_circuit = self._find_battery_circuit()`
2. Remove lines 1167-1169 (reflect battery power back to circuit):
   ```python
           if battery_circuit is not None and self._energy_system.bess is not None:
               battery_circuit._instant_power_w = self._energy_system.bess.effective_power_w
   ```
3. Remove `feed_circuit_id` from the `SpanBatterySnapshot` constructor (line 1195):
   ```python
                   feed_circuit_id=bess.feed_circuit_id,
   ```
4. Remove lines 1212-1226 (rebuild battery circuit snapshot block):
   ```python
               # Rebuild battery circuit snapshot — the original was captured
               # before the BSEE update and off-grid deficit calculation, so it
               # has stale power.  Sync the circuit object then re-snapshot.
               if battery_circuit is not None:
                   battery_circuit._instant_power_w = abs(power_flow_battery)
                   cid = battery_circuit.circuit_id
                   snap = battery_circuit.to_snapshot()
                   if cid in shed_ids:
                       snap = replace(
                           snap,
                           relay_state="OPEN",
                           relay_requester="BACKUP",
                           instant_power_w=0.0,
                       )
                   circuit_snapshots[cid] = snap
   ```

- [ ] **Step 6: Run type checker**

Run: `mypy src/span_panel_simulator/engine.py`
Expected: PASS (all battery circuit references removed)

- [ ] **Step 7: Commit**

```
git add src/span_panel_simulator/engine.py
git commit -m "Remove battery circuit detection and writeback from engine"
```

---

### Task 7: Migrate YAML configs to top-level `bess` section

**Files:**
- Modify: `configs/MAIN_40.yaml`
- Modify: `configs/default_MAIN_40.yaml`
- Modify: `configs/default_MAIN_32.yaml`
- Modify: `configs/default_MAIN_16.yaml`

For each config file, three changes: (a) add top-level `bess` section after `panel_config`, (b) remove the `battery`/`battery_storage` template from `circuit_templates`, (c) remove the `battery_storage` circuit entry from `circuits`.

- [ ] **Step 1: Migrate `configs/MAIN_40.yaml`**

Add after `panel_config` section (after line 6, before `circuit_templates`):

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

Remove the `battery` template block (lines 519-565).

Remove the `battery_storage` circuit entry (lines 821-823):
```yaml
- id: battery_storage
  name: Battery Storage
  template: battery
```

- [ ] **Step 2: Migrate `configs/default_MAIN_40.yaml`**

Same pattern. Add `bess` section after `panel_config`. Remove `battery` template (lines 504-549). Remove `battery_storage` circuit (lines 802-804).

- [ ] **Step 3: Migrate `configs/default_MAIN_32.yaml`**

Same pattern. Template is named `battery_storage` here (lines 443-462). Circuit entry is `battery_storage_1` (lines 668-670). Remove both, add top-level `bess`.

- [ ] **Step 4: Migrate `configs/default_MAIN_16.yaml`**

Same pattern. Remove `battery` template (lines 55-74). Remove `battery_storage` circuit (lines 107-109). Add top-level `bess`.

- [ ] **Step 5: Validate YAML**

Run: `python -c "import yaml; [yaml.safe_load(open(f)) for f in ['configs/MAIN_40.yaml', 'configs/default_MAIN_40.yaml', 'configs/default_MAIN_32.yaml', 'configs/default_MAIN_16.yaml']]"`
Expected: No errors

- [ ] **Step 6: Commit**

```
git add configs/
git commit -m "Migrate BESS config from circuit templates to top-level bess section"
```

---

### Task 8: Refactor clone pipeline for top-level BESS config

**Files:**
- Modify: `src/span_panel_simulator/clone.py:593-632`

- [ ] **Step 1: Refactor `_enrich_bess_template` to write top-level `bess` section**

Rename to `_build_bess_config` and change it to return a dict instead of mutating a template. It no longer needs `feed_map` or `templates` parameters since it's not enriching a circuit template.

```python
def _build_bess_config(
    properties: dict[str, str],
    prefix: str,
    bess_node_id: str,
) -> dict[str, object]:
    """Build top-level bess config from scraped BESS node properties."""
    nameplate = _float_prop(properties, prefix, bess_node_id, "nameplate-capacity")
    nameplate_kwh = nameplate if nameplate is not None else 13.5

    return {
        "enabled": True,
        "charge_mode": "custom",
        "nameplate_capacity_kwh": nameplate_kwh,
        "backup_reserve_pct": 20.0,
        "charge_efficiency": 0.95,
        "discharge_efficiency": 0.95,
        "max_charge_w": 3500.0,
        "max_discharge_w": 3500.0,
        "charge_hours": [0, 1, 2, 3, 4, 5],
        "discharge_hours": [16, 17, 18, 19, 20, 21],
    }
```

- [ ] **Step 2: Update the caller of `_enrich_bess_template`**

Find where `_enrich_bess_template` is called in `clone.py` and update it to:
1. Call the renamed `_build_bess_config` function
2. Assign the returned dict to `config["bess"]` instead of mutating a template
3. Do NOT create a battery circuit entry in the circuits list

- [ ] **Step 3: Verify that the cloned config no longer creates a battery circuit**

The circuit that was formerly the battery circuit's feed target should remain as a normal circuit if it has other uses, or be removed if it only existed for battery purposes. In practice, the clone pipeline scrapes real circuits — the battery circuit was synthetic. The `feed` cross-reference is simply not used.

- [ ] **Step 4: Run type checker**

Run: `mypy src/span_panel_simulator/clone.py`
Expected: PASS

- [ ] **Step 5: Commit**

```
git add src/span_panel_simulator/clone.py
git commit -m "Refactor clone pipeline to write top-level bess config"
```

---

### Task 9: Update tests

**Files:**
- Modify: `tests/test_clone.py:200-214`
- Modify: `tests/test_modeling.py:140-170`
- Modify: `tests/test_energy/test_scenarios.py` (if `feed_circuit_id` is explicitly passed)

- [ ] **Step 1: Update `test_bess_mode` in `test_clone.py`**

The test currently asserts that a circuit template gets `battery_behavior`. Rewrite to assert the cloned config has a top-level `bess` section:

```python
    def test_bess_mode(self) -> None:
        """Cloned panel with BESS node gets top-level bess config."""
        config = translate_scraped_panel(_make_scraped())
        bess = config.get("bess")
        assert isinstance(bess, dict)
        assert bess["enabled"] is True
        assert bess["nameplate_capacity_kwh"] == 13.5
```

- [ ] **Step 2: Update `test_modeling.py` fixture**

Move the battery config from the circuit template to top-level. Replace lines 140-154 (the `battery` template and its `battery_behavior`) with a simple removal of the battery template and circuit. Add a top-level `bess` section to the YAML fixture:

```yaml
bess:
  enabled: true
  charge_mode: "custom"
  charge_hours: [10, 11, 12, 13, 14]
  discharge_hours: [17, 18, 19, 20, 21]
  nameplate_capacity_kwh: 13.5
  backup_reserve_pct: 20
  max_charge_w: 3500.0
  max_discharge_w: 3500.0
```

Remove the `battery` template block and the `batt` circuit entry from the fixture's `circuits` list.

- [ ] **Step 3: Check `test_scenarios.py` for `feed_circuit_id`**

The `_bess()` helper in `tests/test_energy/test_scenarios.py` does NOT pass `feed_circuit_id` (it uses the default). No change needed — but verify after Task 2's removal that the `BESSConfig` constructor call still works without the field.

- [ ] **Step 4: Run full test suite**

Run: `pytest tests/ -v`
Expected: All tests pass

- [ ] **Step 5: Commit**

```
git add tests/
git commit -m "Update tests for top-level BESS config"
```

---

### Task 10: Final verification and cleanup

- [ ] **Step 1: Run full type check**

Run: `mypy src/`
Expected: PASS with no battery-related errors

- [ ] **Step 2: Run full test suite**

Run: `pytest tests/ -v`
Expected: All tests pass

- [ ] **Step 3: Search for orphaned references**

Run: `grep -rn "battery_behavior\|feed_circuit_id\|_find_battery_circuit\|_is_battery_circuit\|_apply_battery_behavior\|BatteryBehavior" src/ tests/ configs/`

Expected: No matches (or only in documentation/comments that should be cleaned up).

- [ ] **Step 4: Verify no battery circuit in running simulation**

Start the simulator with MAIN_40.yaml and confirm:
- No `battery_storage` circuit appears in the circuits list
- BESS snapshot still shows SOE, nameplate, vendor info
- Grid sensor reflects correct power flows

- [ ] **Step 5: Commit any cleanup**

```
git add -A
git commit -m "Final cleanup: remove orphaned battery circuit references"
```
