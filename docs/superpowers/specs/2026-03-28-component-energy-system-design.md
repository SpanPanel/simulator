# Component-Based Energy System Design

## Problem Statement

The simulator's energy balance logic is scattered across three inconsistent calculation
paths in an 1837-line god class (`DynamicSimulationEngine`). Each path uses different
sign conventions, different battery handling, and different grid power formulas. This
produces bugs that are symptomatic of poor system design:

- BESS discharge is not throttled to actual load demand (GFE constraint violated)
- Grid-offline mode force-discharges the battery, ignoring charge mode entirely
  (hybrid inverter + solar-excess cannot charge during islanding)
- Dashboard summary bypasses BSEE and SOE bounds (`battery = consumption - pv`)
- Non-hybrid vs hybrid PV behavior during islanding is not properly modeled
- The three-way interdependency between loads, PV production, and BESS
  charge/discharge is not modeled as a closed energy balance
- Adding or changing BESS configuration can produce stale or inconsistent state

These issues compound when the system needs to run modeling passes (before/after
comparison) or future optimization iterations, because each requires cloning engine
internals and reconstructing the energy balance from scattered state.

## Design Goals

1. **Physical fidelity**: Components represent physical devices. Power flows obey
   conservation of energy. The BESS as GFE never over-discharges. Hybrid vs
   non-hybrid inverter behavior is correct during islanding.

2. **Single source of truth**: One energy balance calculation, used by real-time
   snapshots, dashboard summaries, and modeling passes alike.

3. **Instantiable as a value**: An `EnergySystem` can be constructed from a
   configuration, ticked forward over timestamps, and discarded. No singletons,
   no shared mutable state, no coupling to the engine or dashboard. The modeling
   pass and optimizer construct their own independent instances.

4. **Testable in isolation**: The energy system is fully testable without the engine,
   dashboard, MQTT, or any transport layer.

5. **Uniform sign convention**: Non-negative magnitudes internally. Direction
   expressed by role (demand vs supply). eBus native convention applied once at
   the snapshot output boundary.

## Architecture

### Separation of Concerns

The behavior engine decides what each device *wants* to do (power intent).
The energy system decides what physically *happens* (power resolution).

```
BehaviorEngine ──> PowerInputs ──> EnergySystem.tick() ──> SystemState
                                                               │
                                       ┌───────────────────────┤
                                       v                       v
                                  Snapshot Assembly      Modeling Output
```

### Component Roles and Bus Resolution

Each physical device is a `Component` with a role that determines evaluation order.
Components attach to a `PanelBus`. Each tick, the bus resolves components in role
order and enforces conservation of energy as a postcondition.

| Role        | Evaluation Order | Description                                    | Examples       |
|-------------|-----------------|------------------------------------------------|----------------|
| **LOAD**    | First           | Declares demand                                | Consumer circuits, EVSE |
| **SOURCE**  | Second          | Declares supply up to available capacity       | PV inverter    |
| **STORAGE** | Third           | Charges from excess or discharges to meet deficit | BESS         |
| **SLACK**   | Last            | Absorbs residual (whatever the bus can't balance) | Grid meter (or BESS when islanded) |

Resolution proceeds:

1. **LOAD**: Accumulates total demand on the bus.
2. **SOURCE**: PV declares available supply. Bus now knows demand and supply.
3. **STORAGE**: BESS sees the deficit (demand - supply) or excess (supply - demand)
   and acts accordingly, constrained by GFE rules, SOE bounds, and charge mode.
4. **SLACK**: Grid absorbs whatever remains. If disconnected, contributes nothing
   (BESS as GFE already covered the deficit in step 3).

Conservation assertion: after all roles resolve, `total_demand = total_supply + grid`.
BESS discharge is already included in `total_supply`; BESS charge in `total_demand`.
If the residual exceeds floating-point tolerance, it is a bug.

## Core Types

### PowerContribution

What a component returns from `resolve()`. Single sign convention: all values
are non-negative magnitudes. Direction is expressed by which field is populated.

```python
@dataclass
class PowerContribution:
    demand_w: float = 0.0    # power consumed (always >= 0)
    supply_w: float = 0.0    # power produced (always >= 0)
```

### BusState

Accumulates the energy balance as components resolve. Each downstream component
sees the cumulative state from all upstream roles.

Note: while components return non-negative `PowerContribution` values, the bus
accumulates them into `total_demand_w` and `total_supply_w` directly. BESS discharge
adds to supply; BESS charge adds to demand. `storage_contribution_w` is a signed
convenience field for downstream components to see the net storage effect without
inspecting both demand and supply deltas.

```python
@dataclass
class BusState:
    total_demand_w: float = 0.0        # includes BESS charging
    total_supply_w: float = 0.0        # includes BESS discharging
    storage_contribution_w: float = 0.0  # net: positive = discharge, negative = charge
                                         # (derived from supply/demand deltas, not
                                         # a separate sign convention)
    grid_power_w: float = 0.0

    @property
    def net_deficit_w(self) -> float:
        """Remaining demand after all supply and storage contributions."""
        return self.total_demand_w - self.total_supply_w

    def is_balanced(self) -> bool:
        """Conservation check: grid absorbs exactly what remains."""
        residual = self.total_demand_w - self.total_supply_w - self.grid_power_w
        return abs(residual) < 0.01
```

### PowerInputs

External inputs that drive each tick. Provided by the behavior engine (or recorder
replay, or synthetic generation). The energy system does not generate power values;
it resolves how they flow.

```python
@dataclass
class PowerInputs:
    pv_available_w: float = 0.0
    bess_requested_w: float = 0.0
    bess_scheduled_state: str = "idle"  # "charging" | "discharging" | "idle"
    load_demand_w: float = 0.0
    grid_connected: bool = True
```

### SystemState

The resolved output of a tick. Single source of truth consumed by snapshot
assembly, dashboard, and modeling output.

```python
@dataclass
class SystemState:
    grid_power_w: float
    pv_power_w: float
    bess_power_w: float
    bess_state: str
    load_power_w: float
    soe_kwh: float
    soe_percentage: float
    balanced: bool
```

### Configuration Dataclasses

```python
@dataclass(frozen=True)
class GridConfig:
    connected: bool = True

@dataclass(frozen=True)
class PVConfig:
    nameplate_w: float = 0.0
    inverter_type: str = "ac_coupled"  # "ac_coupled" | "hybrid"

@dataclass(frozen=True)
class BESSConfig:
    nameplate_kwh: float = 13.5
    max_charge_w: float = 3500.0
    max_discharge_w: float = 3500.0
    charge_efficiency: float = 0.95
    discharge_efficiency: float = 0.95
    backup_reserve_pct: float = 20.0
    hard_min_pct: float = 5.0
    hybrid: bool = False
    initial_soe_kwh: float | None = None  # defaults to 50% of nameplate

@dataclass(frozen=True)
class LoadConfig:
    demand_w: float = 0.0

@dataclass(frozen=True)
class EnergySystemConfig:
    grid: GridConfig
    pv: PVConfig | None = None
    bess: BESSConfig | None = None
    loads: list[LoadConfig] = field(default_factory=list)
```

## Concrete Components

### GridMeter (Role: SLACK)

Absorbs whatever the bus cannot balance. When disconnected, contributes nothing.

```python
class GridMeter(Component):
    role = SLACK
    connected: bool

    def resolve(self, bus_state: BusState) -> PowerContribution:
        if not self.connected:
            return PowerContribution()
        deficit = bus_state.net_deficit_w
        if deficit > 0:
            return PowerContribution(supply_w=deficit)   # importing
        elif deficit < 0:
            return PowerContribution(demand_w=-deficit)   # absorbing excess
        return PowerContribution()
```

### PVSource (Role: SOURCE)

Declares available production. Does not make decisions. The `online` flag is
controlled by the system topology: if AC-coupled and grid disconnected, PV goes
offline unless the co-located BESS is hybrid.

```python
class PVSource(Component):
    role = SOURCE
    available_power_w: float
    online: bool

    def resolve(self, bus_state: BusState) -> PowerContribution:
        if not self.online:
            return PowerContribution()
        return PowerContribution(supply_w=self.available_power_w)
```

### BESSUnit (Role: STORAGE)

The most complex component. Encapsulates SOE tracking, GFE behavior, hybrid
inverter control, and charge/discharge constraints.

**GFE constraint**: When discharging, the BESS only sources what the bus actually
demands. If PV already covers all loads, discharge power is zero. The BESS as GFE
never pushes excess power back through the grid meter.

**Hybrid inverter**: Not a separate component. It is a configuration property of
`BESSUnit`. When `hybrid=True`, the BESS keeps its co-located PV online during
islanding. When `hybrid=False`, PV goes offline when grid disconnects.

**Islanding behavior**: When grid is disconnected, the BESS becomes the de facto
slack. If hybrid, PV continues producing and the BESS only covers the gap. If
solar-excess mode and PV exceeds loads, the BESS charges from the excess. If
non-hybrid, PV is offline and the BESS covers all load demand.

```python
class BESSUnit(Component):
    role = STORAGE

    # Configuration
    nameplate_capacity_kwh: float
    max_charge_w: float
    max_discharge_w: float
    charge_efficiency: float
    discharge_efficiency: float
    backup_reserve_pct: float
    hard_min_pct: float
    hybrid: bool
    pv_source: PVSource | None  # co-located PV reference (if hybrid)

    # State
    soe_kwh: float
    scheduled_state: str
    requested_power_w: float

    # Output (set after resolve)
    effective_power_w: float
    effective_state: str

    def resolve(self, bus_state: BusState) -> PowerContribution:
        if self.scheduled_state == "idle":
            return PowerContribution()
        if self.scheduled_state == "discharging":
            return self._resolve_discharge(bus_state)
        if self.scheduled_state == "charging":
            return self._resolve_charge(bus_state)

    def _resolve_discharge(self, bus_state: BusState) -> PowerContribution:
        deficit = bus_state.net_deficit_w
        if deficit <= 0:
            self.effective_power_w = 0.0
            self.effective_state = "idle"
            return PowerContribution()
        power = min(self.requested_power_w, deficit, self.max_discharge_w,
                    self._max_discharge_for_soe())
        self.effective_power_w = power
        self.effective_state = "discharging"
        return PowerContribution(supply_w=power)

    def _resolve_charge(self, bus_state: BusState) -> PowerContribution:
        power = min(self.requested_power_w, self.max_charge_w,
                    self._max_charge_for_soe())
        self.effective_power_w = power
        self.effective_state = "charging"
        return PowerContribution(demand_w=power)

    def _max_discharge_for_soe(self) -> float:
        """Max instantaneous discharge (W) before hitting SOE floor.

        Computed from available energy above the reserve threshold and
        the discharge efficiency. Prevents over-drain within a single tick.
        """
        ...

    def _max_charge_for_soe(self) -> float:
        """Max instantaneous charge (W) before hitting SOE ceiling.

        Computed from remaining capacity below 100% and the charge
        efficiency. Prevents overcharge within a single tick.
        """
        ...

    def integrate_energy(self, delta_s: float) -> None:
        """Integrate effective power over elapsed time to update SOE.

        Applies charge/discharge efficiency:
        - Charging:    soe += (power / 1000) * hours * charge_efficiency
        - Discharging: soe -= (power / 1000) * hours / discharge_efficiency

        Clamps to [hard_min, nameplate] bounds after integration.
        """
        ...

    def update_pv_online_status(self, grid_connected: bool) -> None:
        """If hybrid, keep co-located PV online even when grid disconnected."""
        if self.pv_source is not None:
            if self.hybrid:
                self.pv_source.online = True
            elif not grid_connected:
                self.pv_source.online = False
```

### LoadGroup (Role: LOAD)

Declares demand. Could be individual circuits or aggregate.

```python
class LoadGroup(Component):
    role = LOAD
    demand_w: float

    def resolve(self, bus_state: BusState) -> PowerContribution:
        return PowerContribution(demand_w=self.demand_w)
```

### EVSE

Modeled as a `LoadGroup` — pure consumer. Positive demand, no sign flip. EVSE
special properties (status, advertised current, lock state) live on the circuit
and device layer, not in the energy system. If V2H is added in the future, EVSE
promotes to STORAGE role, but it would present as a BESS to the panel.

## EnergySystem

### Construction

```python
class EnergySystem:
    bus: PanelBus
    grid: GridMeter
    pv: PVSource | None
    bess: BESSUnit | None

    @staticmethod
    def from_config(config: EnergySystemConfig) -> EnergySystem:
        """Pure factory. No side effects, no external dependencies."""
        ...
```

### Tick

```python
def tick(self, ts: float, inputs: PowerInputs) -> SystemState:
    # 1. Apply topology: grid connected, PV online status
    self.grid.connected = inputs.grid_connected
    if self.bess is not None:
        self.bess.update_pv_online_status(inputs.grid_connected)
    elif self.pv is not None and not inputs.grid_connected:
        self.pv.online = False  # no BESS to keep PV alive

    # 2. Set component inputs from PowerInputs
    if self.pv is not None:
        self.pv.available_power_w = inputs.pv_available_w
    if self.bess is not None:
        self.bess.scheduled_state = inputs.bess_scheduled_state
        self.bess.requested_power_w = inputs.bess_requested_w

    # 3. Resolve bus (components already configured above)
    bus_state = self.bus.resolve()

    # 4. Integrate BESS energy over time delta
    if self.bess is not None:
        self.bess.integrate_energy(delta_s)

    # 5. Return resolved state
    return SystemState(
        grid_power_w=bus_state.grid_power_w,
        pv_power_w=self.pv.available_power_w if self.pv and self.pv.online else 0.0,
        bess_power_w=self.bess.effective_power_w if self.bess else 0.0,
        bess_state=self.bess.effective_state if self.bess else "idle",
        load_power_w=bus_state.total_demand_w,
        soe_kwh=self.bess.soe_kwh if self.bess else 0.0,
        soe_percentage=self.bess.soe_percentage if self.bess else 0.0,
        balanced=bus_state.is_balanced(),
    )
```

### Usage Patterns

```python
# Live simulation — one instance, ticked by engine clock
energy_system = EnergySystem.from_config(current_config)
state = energy_system.tick(current_time, inputs)

# Modeling — two independent instances
system_before = EnergySystem.from_config(config_before)
system_after = EnergySystem.from_config(config_after)
for ts in timestamps:
    state_b = system_before.tick(ts, inputs_b)
    state_a = system_after.tick(ts, inputs_a)

# Optimization — N independent instances
for config in candidates:
    system = EnergySystem.from_config(config)
    cost = simulate_horizon(system, behavior, timestamps)
```

## Sign Convention

### Internal Convention

All power values inside the energy system are **non-negative magnitudes**. Direction
is expressed by which field they populate (`demand_w` vs `supply_w`). There is no
sign flipping inside the energy system.

### Output Boundary: eBus Native Convention

The snapshot assembly layer translates `SystemState` to SPAN eBus native convention:

| SystemState field | eBus field | eBus convention |
|-------------------|-----------|-----------------|
| `grid_power_w` | `instant_grid_power_w` | positive = importing |
| `grid_power_w` | `power_flow_grid` | positive = importing |
| `pv_power_w` | `power_flow_pv` | positive = producing |
| `bess_power_w` + `bess_state` | `power_flow_battery` | positive = charging, negative = discharging |
| `load_power_w` | `power_flow_site` | positive = consuming |

### HA Sensor Layer (unchanged)

The HA integration's existing `value_fn` lambdas apply user-facing sign flips:

| Sensor | eBus Native | User-Facing | Flip |
|--------|-------------|-------------|------|
| Battery Power | + = charging | negated: + = discharging | Yes |
| PV Power | + = producing | negated | Yes |
| Grid Power Flow | + = importing | negated: + = exporting | Yes |
| Site Power | + = consuming | as-is | No |
| Circuit Power (PV) | + = producing | negated | Yes |
| Circuit Power (EVSE/other) | + = consuming | as-is | No |

The energy system does not concern itself with user-facing conventions. It outputs
eBus native; the HA integration handles the rest.

## Engine Integration

### What the Engine Retains

- Clock management (`SimulationClock`)
- Circuit management and ticking (behavior engine generates power values)
- Behavior engine (`RealisticBehaviorEngine`) — generates power intent
- Dynamic overrides and tab synchronization
- Snapshot assembly (converts `SystemState` to `SpanPanelSnapshot`)
- Load shedding decisions (which circuits to shed based on priority)

### What the Engine Sheds

- All three grid power calculations (replaced by `SystemState.grid_power_w`)
- Battery power clamping / GFE logic (replaced by `BESSUnit._resolve_discharge`)
- Solar-excess two-pass aggregation (bus role ordering handles this naturally)
- Sign convention juggling in snapshot assembly
- `get_power_summary()` inline aggregation (reads from `SystemState`)
- BSEE class (`bsee.py` deleted, absorbed into `BESSUnit`)

### Engine After Integration

```python
class DynamicSimulationEngine:
    _clock: SimulationClock
    _behavior_engine: RealisticBehaviorEngine
    _circuits: dict[str, SimulatedCircuit]
    _energy_system: EnergySystem

    async def get_snapshot(self) -> SpanPanelSnapshot:
        # 1. Tick circuits (behavior engine generates power values)
        self._tick_circuits(current_time)

        # 2. Collect power inputs from circuit state
        inputs = self._collect_power_inputs()

        # 3. Resolve energy balance
        system_state = self._energy_system.tick(current_time, inputs)

        # 4. Reflect effective battery power back to circuit
        self._apply_system_state_to_circuits(system_state)

        # 5. Assemble snapshot
        return self._build_snapshot(system_state, circuit_snapshots)

    async def compute_modeling_data(self, horizon_hours: int) -> dict:
        config_before = self._build_energy_config(baseline=True)
        config_after = self._build_energy_config(baseline=False)

        system_before = EnergySystem.from_config(config_before)
        system_after = EnergySystem.from_config(config_after)

        for ts in timestamps:
            inputs_b = self._modeling_inputs_at(ts, baseline=True)
            inputs_a = self._modeling_inputs_at(ts, baseline=False)
            state_b = system_before.tick(ts, inputs_b)
            state_a = system_after.tick(ts, inputs_a)
            results.append(state_b, state_a)

        return self._format_modeling_response(results)
```

## Testing Strategy

### Layer 1: Component Unit Tests (`test_components.py`)

Each component resolves correctly given a `BusState`. Tests are pure —
instantiate component, call `resolve()`, assert result.

- GridMeter absorbs exact deficit
- GridMeter returns zero when disconnected
- PVSource returns zero when offline
- PVSource returns available power when online
- BESSUnit discharge throttled to deficit (GFE constraint)
- BESSUnit idles when solar exceeds demand
- BESSUnit stops discharge at backup reserve
- BESSUnit stops charge at max SOE
- BESSUnit charge limited by max charge rate
- LoadGroup returns configured demand

### Layer 2: Bus Integration Tests (`test_bus.py`)

Full resolution cycle. Conservation enforced as assertion.

- Conservation: load + PV + BESS + grid, power in = power out
- Conservation holds when BESS is throttled
- Conservation holds when grid is disconnected
- Conservation holds with no BESS
- Conservation holds with no PV
- Charging increases grid import
- Discharging decreases grid import
- Grid never goes negative from BESS discharge alone
- Grid exactly zero when BESS covers full deficit

### Layer 3: Topology / Scenario Tests (`test_scenarios.py`)

Physical behavior under configuration and state changes. Every issue identified
in the original conversation is covered.

**GFE and discharge throttling (Issues 1, 2):**
- BESS discharge clamped to actual load deficit
- Grid never negative from battery discharge
- Grid exactly zero when BESS matches deficit

**Hybrid vs non-hybrid islanding (Issues 3, 5, 8):**
- Non-hybrid island: PV offline, BESS covers all loads
- Hybrid island: PV online, BESS covers only the gap
- Hybrid island + solar-excess: excess PV charges BESS
- Non-hybrid island ignores solar-excess (PV offline, no excess)

**Single calculation path (Issues 4, 6, 7):**
- All consumers (snapshot, dashboard, modeling) read from same SystemState
- Architectural: single `tick()` method, not three aggregation functions

**Modeling reflects BESS changes (Issue 9):**
- Adding BESS to "after" config reduces grid trace
- Changing nameplate affects discharge duration across horizon

**No stale state (Issue 10):**
- Two EnergySystem instances share no state
- Configuration change produces correct results immediately

**EVSE:**
- EVSE behaves as pure consumer load, no sign flip

**SOE integration:**
- SOE tracks correctly over multi-tick discharge
- SOE tracks correctly over multi-tick charge
- Efficiency losses applied correctly (charge and discharge)

## File Structure

```
src/span_panel_simulator/
├── energy/
│   ├── __init__.py          # Public API re-exports
│   ├── types.py             # ComponentRole, PowerContribution, BusState,
│   │                        # SystemState, PowerInputs, config dataclasses
│   ├── components.py        # Component base, GridMeter, PVSource, BESSUnit, LoadGroup
│   ├── bus.py               # PanelBus — role-ordered resolution, conservation check
│   └── system.py            # EnergySystem — from_config factory, tick entry point
│
├── engine.py                # Slimmed: delegates energy math to EnergySystem
├── circuit.py               # Circuit energy counters remain; battery direction from SystemState
├── bsee.py                  # DELETED — absorbed into BESSUnit
└── dashboard/
    └── routes.py            # Reads from SystemState, no separate aggregation

tests/
├── test_energy/
│   ├── test_components.py   # Layer 1: component unit tests
│   ├── test_bus.py          # Layer 2: bus integration / conservation
│   └── test_scenarios.py    # Layer 3: topology / scenario tests
└── ...                      # Existing tests unchanged
```

Approximate sizes:
- `energy/types.py`: ~120 lines
- `energy/components.py`: ~250 lines
- `energy/bus.py`: ~60 lines
- `energy/system.py`: ~100 lines
- Total new code: ~540 lines (replaces ~400 lines of scattered logic)

## Migration Path

### Phase 1: Build energy system in isolation

- Create `energy/` package with all components, bus, system, types
- Write all three test layers
- Tests pass against new code only. Engine untouched. Nothing breaks.

### Phase 2: Wire into engine (dual path)

- Engine constructs `EnergySystem` alongside existing logic
- `get_snapshot()` uses `SystemState` for grid/battery/pv values
- Old calculation kept behind a flag for comparison during development
- Existing 193 tests still pass

### Phase 3: Eliminate old paths

- Remove three separate grid power calculations from engine.py
- Remove `get_power_summary()` inline aggregation
- Remove modeling pass inline energy balance
- Delete `bsee.py`
- Update `circuit.py` battery direction to read from `SystemState`

### Phase 4: Clean up

- Remove GFE throttling from old BSEE (absorbed into BESSUnit)
- Remove grid-offline force-discharge override from behavior engine
- Remove dashboard `battery = consumption - pv` shortcut
- Remove sign convention juggling from snapshot assembly
