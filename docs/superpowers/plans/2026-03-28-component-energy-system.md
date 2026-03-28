# Component-Based Energy System Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the scattered energy balance logic in the simulator engine with a modular, component-based energy system that enforces conservation of energy and correctly models GFE, hybrid inverter, and islanding behavior.

**Architecture:** Physical components (GridMeter, PVSource, BESSUnit, LoadGroup) resolve on a PanelBus in role order (LOAD → SOURCE → STORAGE → SLACK). The EnergySystem is a pure value object — constructed from config, ticked with PowerInputs, returns SystemState. The engine delegates all energy math to it.

**Tech Stack:** Python 3.14, dataclasses, pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-03-28-component-energy-system-design.md`

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `src/span_panel_simulator/energy/__init__.py` | Create | Public API re-exports |
| `src/span_panel_simulator/energy/types.py` | Create | ComponentRole enum, PowerContribution, BusState, SystemState, PowerInputs, config dataclasses |
| `src/span_panel_simulator/energy/components.py` | Create | Component base class, GridMeter, PVSource, BESSUnit, LoadGroup |
| `src/span_panel_simulator/energy/bus.py` | Create | PanelBus — role-ordered resolution, conservation check |
| `src/span_panel_simulator/energy/system.py` | Create | EnergySystem — from_config factory, tick entry point |
| `tests/test_energy/__init__.py` | Create | Test package init |
| `tests/test_energy/test_components.py` | Create | Layer 1: component unit tests |
| `tests/test_energy/test_bus.py` | Create | Layer 2: bus integration / conservation tests |
| `tests/test_energy/test_scenarios.py` | Create | Layer 3: topology / scenario tests |
| `src/span_panel_simulator/engine.py` | Modify | Wire EnergySystem into snapshot, modeling, and dashboard paths; remove old energy balance code |
| `src/span_panel_simulator/circuit.py` | Modify | Battery direction reads from SystemState instead of behavior engine |
| `src/span_panel_simulator/bsee.py` | Delete | Absorbed into BESSUnit |

---

## Phase 1: Build Energy System in Isolation

### Task 1: Types Module

**Files:**
- Create: `src/span_panel_simulator/energy/__init__.py`
- Create: `src/span_panel_simulator/energy/types.py`
- Test: `tests/test_energy/__init__.py`
- Test: `tests/test_energy/test_components.py`

- [ ] **Step 1: Create package structure**

```bash
mkdir -p src/span_panel_simulator/energy tests/test_energy
```

- [ ] **Step 2: Write types module**

Create `src/span_panel_simulator/energy/__init__.py`:

```python
"""Component-based energy system for the SPAN panel simulator."""
```

Create `tests/test_energy/__init__.py`:

```python
"""Energy system tests."""
```

Create `src/span_panel_simulator/energy/types.py` with all core types from the spec:
- `ComponentRole` enum: `LOAD`, `SOURCE`, `STORAGE`, `SLACK`
- `PowerContribution` dataclass: `demand_w`, `supply_w` (both non-negative)
- `BusState` dataclass: `total_demand_w`, `total_supply_w`, `storage_contribution_w`, `grid_power_w`, `net_deficit_w` property, `is_balanced()` method
- `PowerInputs` dataclass: `pv_available_w`, `bess_requested_w`, `bess_scheduled_state`, `load_demand_w`, `grid_connected`
- `SystemState` dataclass: `grid_power_w`, `pv_power_w`, `bess_power_w`, `bess_state`, `load_power_w`, `soe_kwh`, `soe_percentage`, `balanced`
- Config dataclasses (all frozen): `GridConfig`, `PVConfig`, `BESSConfig`, `LoadConfig`, `EnergySystemConfig`

Implementation details from spec:
- `BusState.net_deficit_w` = `total_demand_w - total_supply_w`
- `BusState.is_balanced()` checks `abs(total_demand_w - total_supply_w - grid_power_w) < 0.01`
- `BESSConfig.initial_soe_kwh` defaults to `None` (factory computes 50% of nameplate)
- `EnergySystemConfig.loads` uses `field(default_factory=list)`

- [ ] **Step 3: Write type smoke tests**

Create `tests/test_energy/test_components.py` with initial type tests:

```python
"""Layer 1: Component unit tests for the energy system."""

from __future__ import annotations

from span_panel_simulator.energy.types import (
    BESSConfig,
    BusState,
    ComponentRole,
    EnergySystemConfig,
    GridConfig,
    LoadConfig,
    PowerContribution,
    PowerInputs,
    PVConfig,
    SystemState,
)


class TestPowerContribution:
    def test_default_zero(self) -> None:
        pc = PowerContribution()
        assert pc.demand_w == 0.0
        assert pc.supply_w == 0.0

    def test_demand_only(self) -> None:
        pc = PowerContribution(demand_w=5000.0)
        assert pc.demand_w == 5000.0
        assert pc.supply_w == 0.0


class TestBusState:
    def test_net_deficit_positive(self) -> None:
        bs = BusState(total_demand_w=5000.0, total_supply_w=3000.0)
        assert bs.net_deficit_w == 2000.0

    def test_net_deficit_negative_means_excess(self) -> None:
        bs = BusState(total_demand_w=2000.0, total_supply_w=5000.0)
        assert bs.net_deficit_w == -3000.0

    def test_balanced_when_grid_absorbs_residual(self) -> None:
        bs = BusState(
            total_demand_w=5000.0,
            total_supply_w=3000.0,
            grid_power_w=2000.0,
        )
        assert bs.is_balanced()

    def test_not_balanced_when_residual_exists(self) -> None:
        bs = BusState(
            total_demand_w=5000.0,
            total_supply_w=3000.0,
            grid_power_w=1000.0,
        )
        assert not bs.is_balanced()


class TestComponentRole:
    def test_role_ordering(self) -> None:
        roles = [ComponentRole.SLACK, ComponentRole.LOAD, ComponentRole.STORAGE, ComponentRole.SOURCE]
        sorted_roles = sorted(roles, key=lambda r: r.value)
        assert sorted_roles == [
            ComponentRole.LOAD,
            ComponentRole.SOURCE,
            ComponentRole.STORAGE,
            ComponentRole.SLACK,
        ]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_energy/test_components.py -v`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add src/span_panel_simulator/energy/ tests/test_energy/
git commit -m "Add energy system types module with core dataclasses"
```

---

### Task 2: GridMeter and LoadGroup Components

**Files:**
- Create: `src/span_panel_simulator/energy/components.py`
- Test: `tests/test_energy/test_components.py`

- [ ] **Step 1: Write failing tests for GridMeter and LoadGroup**

Append to `tests/test_energy/test_components.py`:

```python
from span_panel_simulator.energy.components import GridMeter, LoadGroup


class TestLoadGroup:
    def test_returns_demand(self) -> None:
        load = LoadGroup(demand_w=5000.0)
        contribution = load.resolve(BusState())
        assert contribution.demand_w == 5000.0
        assert contribution.supply_w == 0.0

    def test_zero_demand(self) -> None:
        load = LoadGroup(demand_w=0.0)
        contribution = load.resolve(BusState())
        assert contribution.demand_w == 0.0


class TestGridMeter:
    def test_absorbs_deficit(self) -> None:
        grid = GridMeter(connected=True)
        bus = BusState(total_demand_w=5000.0, total_supply_w=3000.0)
        contribution = grid.resolve(bus)
        assert contribution.supply_w == 2000.0
        assert contribution.demand_w == 0.0

    def test_absorbs_excess(self) -> None:
        grid = GridMeter(connected=True)
        bus = BusState(total_demand_w=2000.0, total_supply_w=5000.0)
        contribution = grid.resolve(bus)
        assert contribution.demand_w == 3000.0
        assert contribution.supply_w == 0.0

    def test_zero_when_balanced(self) -> None:
        grid = GridMeter(connected=True)
        bus = BusState(total_demand_w=3000.0, total_supply_w=3000.0)
        contribution = grid.resolve(bus)
        assert contribution.demand_w == 0.0
        assert contribution.supply_w == 0.0

    def test_zero_when_disconnected(self) -> None:
        grid = GridMeter(connected=False)
        bus = BusState(total_demand_w=5000.0, total_supply_w=0.0)
        contribution = grid.resolve(bus)
        assert contribution.demand_w == 0.0
        assert contribution.supply_w == 0.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_energy/test_components.py::TestGridMeter -v`
Expected: FAIL with `ImportError`

- [ ] **Step 3: Implement GridMeter and LoadGroup**

Create `src/span_panel_simulator/energy/components.py`:

```python
"""Physical energy components for the panel bus.

Each component has a role (LOAD, SOURCE, STORAGE, SLACK) and implements
``resolve()`` which returns a ``PowerContribution`` given the current
``BusState``.  All power values are non-negative magnitudes; direction
is expressed by which field (``demand_w`` vs ``supply_w``) is populated.
"""

from __future__ import annotations

from span_panel_simulator.energy.types import (
    BusState,
    ComponentRole,
    PowerContribution,
)


class Component:
    """Base class for all bus components."""

    role: ComponentRole

    def resolve(self, bus_state: BusState) -> PowerContribution:
        raise NotImplementedError


class LoadGroup(Component):
    """Consumer load — declares demand on the bus."""

    role = ComponentRole.LOAD

    def __init__(self, demand_w: float = 0.0) -> None:
        self.demand_w = demand_w

    def resolve(self, bus_state: BusState) -> PowerContribution:
        return PowerContribution(demand_w=self.demand_w)


class GridMeter(Component):
    """Utility grid connection — slack bus that absorbs residual."""

    role = ComponentRole.SLACK

    def __init__(self, connected: bool = True) -> None:
        self.connected = connected

    def resolve(self, bus_state: BusState) -> PowerContribution:
        if not self.connected:
            return PowerContribution()
        deficit = bus_state.net_deficit_w
        if deficit > 0:
            return PowerContribution(supply_w=deficit)
        elif deficit < 0:
            return PowerContribution(demand_w=-deficit)
        return PowerContribution()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_energy/test_components.py -v`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add src/span_panel_simulator/energy/components.py tests/test_energy/test_components.py
git commit -m "Add GridMeter and LoadGroup energy components"
```

---

### Task 3: PVSource Component

**Files:**
- Modify: `src/span_panel_simulator/energy/components.py`
- Test: `tests/test_energy/test_components.py`

- [ ] **Step 1: Write failing tests for PVSource**

Append to `tests/test_energy/test_components.py`:

```python
from span_panel_simulator.energy.components import GridMeter, LoadGroup, PVSource


class TestPVSource:
    def test_returns_available_power_when_online(self) -> None:
        pv = PVSource(available_power_w=4000.0, online=True)
        contribution = pv.resolve(BusState())
        assert contribution.supply_w == 4000.0
        assert contribution.demand_w == 0.0

    def test_returns_zero_when_offline(self) -> None:
        pv = PVSource(available_power_w=4000.0, online=False)
        contribution = pv.resolve(BusState())
        assert contribution.supply_w == 0.0
        assert contribution.demand_w == 0.0

    def test_zero_production(self) -> None:
        pv = PVSource(available_power_w=0.0, online=True)
        contribution = pv.resolve(BusState())
        assert contribution.supply_w == 0.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_energy/test_components.py::TestPVSource -v`
Expected: FAIL with `ImportError`

- [ ] **Step 3: Implement PVSource**

Add to `src/span_panel_simulator/energy/components.py`:

```python
class PVSource(Component):
    """Solar PV inverter — declares available production."""

    role = ComponentRole.SOURCE

    def __init__(self, available_power_w: float = 0.0, online: bool = True) -> None:
        self.available_power_w = available_power_w
        self.online = online

    def resolve(self, bus_state: BusState) -> PowerContribution:
        if not self.online:
            return PowerContribution()
        return PowerContribution(supply_w=self.available_power_w)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_energy/test_components.py -v`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add src/span_panel_simulator/energy/components.py tests/test_energy/test_components.py
git commit -m "Add PVSource energy component"
```

---

### Task 4: BESSUnit Component — Discharge and GFE

**Files:**
- Modify: `src/span_panel_simulator/energy/components.py`
- Test: `tests/test_energy/test_components.py`

- [ ] **Step 1: Write failing tests for BESSUnit discharge behavior**

Append to `tests/test_energy/test_components.py`:

```python
from span_panel_simulator.energy.components import BESSUnit, GridMeter, LoadGroup, PVSource


class TestBESSUnitDischarge:
    def _make_bess(
        self,
        *,
        nameplate_kwh: float = 13.5,
        max_discharge_w: float = 5000.0,
        max_charge_w: float = 3500.0,
        soe_kwh: float = 10.0,
        backup_reserve_pct: float = 20.0,
        hard_min_pct: float = 5.0,
        scheduled_state: str = "discharging",
        requested_power_w: float = 5000.0,
    ) -> BESSUnit:
        return BESSUnit(
            nameplate_capacity_kwh=nameplate_kwh,
            max_charge_w=max_charge_w,
            max_discharge_w=max_discharge_w,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            backup_reserve_pct=backup_reserve_pct,
            hard_min_pct=hard_min_pct,
            hybrid=False,
            pv_source=None,
            soe_kwh=soe_kwh,
            scheduled_state=scheduled_state,
            requested_power_w=requested_power_w,
        )

    def test_discharge_throttled_to_deficit(self) -> None:
        """GFE: only source what loads demand."""
        bess = self._make_bess(requested_power_w=5000.0)
        bus = BusState(total_demand_w=2000.0, total_supply_w=0.0)
        contribution = bess.resolve(bus)
        assert contribution.supply_w == 2000.0
        assert bess.effective_state == "discharging"

    def test_discharge_idle_when_no_deficit(self) -> None:
        """Solar covers all loads — no discharge needed."""
        bess = self._make_bess(requested_power_w=5000.0)
        bus = BusState(total_demand_w=3000.0, total_supply_w=4000.0)
        contribution = bess.resolve(bus)
        assert contribution.supply_w == 0.0
        assert bess.effective_state == "idle"
        assert bess.effective_power_w == 0.0

    def test_discharge_limited_by_max_rate(self) -> None:
        bess = self._make_bess(max_discharge_w=2000.0, requested_power_w=5000.0)
        bus = BusState(total_demand_w=8000.0, total_supply_w=0.0)
        contribution = bess.resolve(bus)
        assert contribution.supply_w == 2000.0

    def test_discharge_limited_by_requested(self) -> None:
        bess = self._make_bess(requested_power_w=1500.0)
        bus = BusState(total_demand_w=5000.0, total_supply_w=0.0)
        contribution = bess.resolve(bus)
        assert contribution.supply_w == 1500.0

    def test_discharge_stops_at_backup_reserve(self) -> None:
        """SOE at backup reserve — cannot discharge."""
        bess = self._make_bess(
            nameplate_kwh=10.0,
            soe_kwh=2.0,  # exactly 20% of 10 kWh
            backup_reserve_pct=20.0,
        )
        bus = BusState(total_demand_w=5000.0, total_supply_w=0.0)
        contribution = bess.resolve(bus)
        assert contribution.supply_w == 0.0
        assert bess.effective_state == "idle"

    def test_idle_state_returns_zero(self) -> None:
        bess = self._make_bess(scheduled_state="idle")
        bus = BusState(total_demand_w=5000.0, total_supply_w=0.0)
        contribution = bess.resolve(bus)
        assert contribution.supply_w == 0.0
        assert contribution.demand_w == 0.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_energy/test_components.py::TestBESSUnitDischarge -v`
Expected: FAIL with `ImportError`

- [ ] **Step 3: Implement BESSUnit**

Add to `src/span_panel_simulator/energy/components.py`:

```python
_SOE_MAX_PCT = 100.0
_MAX_INTEGRATION_DELTA_S = 300.0


class BESSUnit(Component):
    """Battery Energy Storage System — GFE-aware storage component.

    When discharging, the BESS only sources what the bus actually demands
    (GFE constraint).  Enforces SOE bounds, max charge/discharge rates,
    and efficiency losses.  Controls co-located PV online status when
    configured as a hybrid inverter.
    """

    role = ComponentRole.STORAGE

    def __init__(
        self,
        *,
        nameplate_capacity_kwh: float,
        max_charge_w: float,
        max_discharge_w: float,
        charge_efficiency: float,
        discharge_efficiency: float,
        backup_reserve_pct: float,
        hard_min_pct: float,
        hybrid: bool,
        pv_source: PVSource | None,
        soe_kwh: float,
        scheduled_state: str = "idle",
        requested_power_w: float = 0.0,
    ) -> None:
        self.nameplate_capacity_kwh = nameplate_capacity_kwh
        self.max_charge_w = max_charge_w
        self.max_discharge_w = max_discharge_w
        self.charge_efficiency = charge_efficiency
        self.discharge_efficiency = discharge_efficiency
        self.backup_reserve_pct = backup_reserve_pct
        self.hard_min_pct = hard_min_pct
        self.hybrid = hybrid
        self.pv_source = pv_source
        self.soe_kwh = soe_kwh
        self.scheduled_state = scheduled_state
        self.requested_power_w = requested_power_w

        # Output — set by resolve()
        self.effective_power_w: float = 0.0
        self.effective_state: str = "idle"

        # Timestamp tracking for energy integration
        self._last_ts: float | None = None

    @property
    def soe_percentage(self) -> float:
        if self.nameplate_capacity_kwh <= 0:
            return 0.0
        return self.soe_kwh / self.nameplate_capacity_kwh * 100.0

    def resolve(self, bus_state: BusState) -> PowerContribution:
        if self.scheduled_state == "idle":
            self.effective_power_w = 0.0
            self.effective_state = "idle"
            return PowerContribution()
        if self.scheduled_state == "discharging":
            return self._resolve_discharge(bus_state)
        if self.scheduled_state == "charging":
            return self._resolve_charge(bus_state)
        self.effective_power_w = 0.0
        self.effective_state = "idle"
        return PowerContribution()

    def _resolve_discharge(self, bus_state: BusState) -> PowerContribution:
        deficit = bus_state.net_deficit_w
        if deficit <= 0:
            self.effective_power_w = 0.0
            self.effective_state = "idle"
            return PowerContribution()
        max_for_soe = self._max_discharge_for_soe()
        if max_for_soe <= 0:
            self.effective_power_w = 0.0
            self.effective_state = "idle"
            return PowerContribution()
        power = min(self.requested_power_w, deficit, self.max_discharge_w, max_for_soe)
        self.effective_power_w = power
        self.effective_state = "discharging"
        return PowerContribution(supply_w=power)

    def _resolve_charge(self, bus_state: BusState) -> PowerContribution:
        max_for_soe = self._max_charge_for_soe()
        if max_for_soe <= 0:
            self.effective_power_w = 0.0
            self.effective_state = "idle"
            return PowerContribution()
        power = min(self.requested_power_w, self.max_charge_w, max_for_soe)
        self.effective_power_w = power
        self.effective_state = "charging"
        return PowerContribution(demand_w=power)

    def _max_discharge_for_soe(self) -> float:
        """Max discharge power before hitting SOE floor (backup reserve)."""
        min_kwh = self.nameplate_capacity_kwh * self.backup_reserve_pct / 100.0
        available_kwh = self.soe_kwh - min_kwh
        if available_kwh <= 0:
            return 0.0
        # Convert to instantaneous watts (assuming 1-second resolution is
        # conservative; actual integration uses real delta).
        return available_kwh * 1000.0 * 3600.0  # effectively unlimited for a single tick

    def _max_charge_for_soe(self) -> float:
        """Max charge power before hitting SOE ceiling (100%)."""
        max_kwh = self.nameplate_capacity_kwh * _SOE_MAX_PCT / 100.0
        headroom_kwh = max_kwh - self.soe_kwh
        if headroom_kwh <= 0:
            return 0.0
        return headroom_kwh * 1000.0 * 3600.0  # effectively unlimited for a single tick

    def integrate_energy(self, ts: float) -> None:
        """Integrate effective power over elapsed time to update SOE."""
        if self._last_ts is None:
            self._last_ts = ts
            return

        delta_s = ts - self._last_ts
        self._last_ts = ts
        if delta_s <= 0:
            return
        delta_s = min(delta_s, _MAX_INTEGRATION_DELTA_S)
        delta_hours = delta_s / 3600.0

        mag = abs(self.effective_power_w)
        if self.effective_state == "charging" and mag > 0:
            energy_kwh = (mag / 1000.0) * delta_hours * self.charge_efficiency
            self.soe_kwh += energy_kwh
        elif self.effective_state == "discharging" and mag > 0:
            energy_kwh = (mag / 1000.0) * delta_hours / self.discharge_efficiency
            self.soe_kwh -= energy_kwh

        max_kwh = self.nameplate_capacity_kwh * _SOE_MAX_PCT / 100.0
        min_kwh = self.nameplate_capacity_kwh * self.hard_min_pct / 100.0
        self.soe_kwh = max(min_kwh, min(max_kwh, self.soe_kwh))

    def update_pv_online_status(self, grid_connected: bool) -> None:
        """Control co-located PV based on hybrid inverter capability."""
        if self.pv_source is None:
            return
        if self.hybrid:
            self.pv_source.online = True
        elif not grid_connected:
            self.pv_source.online = False
        else:
            self.pv_source.online = True
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_energy/test_components.py -v`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add src/span_panel_simulator/energy/components.py tests/test_energy/test_components.py
git commit -m "Add BESSUnit component with GFE discharge throttling"
```

---

### Task 5: BESSUnit — Charging, SOE Integration, and Hybrid PV Control

**Files:**
- Test: `tests/test_energy/test_components.py`

- [ ] **Step 1: Write failing tests for charging, SOE, and hybrid PV**

Append to `tests/test_energy/test_components.py`:

```python
class TestBESSUnitCharge:
    def _make_bess(self, **kwargs: object) -> BESSUnit:
        defaults: dict[str, object] = dict(
            nameplate_capacity_kwh=13.5,
            max_charge_w=3500.0,
            max_discharge_w=5000.0,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            backup_reserve_pct=20.0,
            hard_min_pct=5.0,
            hybrid=False,
            pv_source=None,
            soe_kwh=6.75,
            scheduled_state="charging",
            requested_power_w=3000.0,
        )
        defaults.update(kwargs)
        return BESSUnit(**defaults)

    def test_charge_at_requested_rate(self) -> None:
        bess = self._make_bess(requested_power_w=2000.0)
        bus = BusState(total_demand_w=1000.0, total_supply_w=5000.0)
        contribution = bess.resolve(bus)
        assert contribution.demand_w == 2000.0
        assert bess.effective_state == "charging"

    def test_charge_limited_by_max_rate(self) -> None:
        bess = self._make_bess(max_charge_w=1500.0, requested_power_w=3000.0)
        bus = BusState()
        contribution = bess.resolve(bus)
        assert contribution.demand_w == 1500.0

    def test_charge_stops_at_full_soe(self) -> None:
        bess = self._make_bess(nameplate_capacity_kwh=10.0, soe_kwh=10.0)
        bus = BusState()
        contribution = bess.resolve(bus)
        assert contribution.demand_w == 0.0
        assert bess.effective_state == "idle"


class TestBESSUnitSOEIntegration:
    def _make_bess(self, **kwargs: object) -> BESSUnit:
        defaults: dict[str, object] = dict(
            nameplate_capacity_kwh=10.0,
            max_charge_w=3500.0,
            max_discharge_w=5000.0,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            backup_reserve_pct=20.0,
            hard_min_pct=5.0,
            hybrid=False,
            pv_source=None,
            soe_kwh=5.0,
            scheduled_state="idle",
            requested_power_w=0.0,
        )
        defaults.update(kwargs)
        return BESSUnit(**defaults)

    def test_discharge_decreases_soe(self) -> None:
        bess = self._make_bess(
            soe_kwh=5.0,
            scheduled_state="discharging",
            requested_power_w=2000.0,
        )
        bus = BusState(total_demand_w=5000.0, total_supply_w=0.0)
        bess.resolve(bus)
        # First tick establishes timestamp
        bess.integrate_energy(1000.0)
        # Second tick integrates over 1 hour
        bess.integrate_energy(4600.0)  # 3600 seconds later
        # 2000W for 1 hour / 0.95 efficiency = ~2.105 kWh consumed
        assert bess.soe_kwh < 5.0
        expected = 5.0 - (2.0 / 0.95)
        assert abs(bess.soe_kwh - expected) < 0.01

    def test_charge_increases_soe(self) -> None:
        bess = self._make_bess(
            soe_kwh=3.0,
            scheduled_state="charging",
            requested_power_w=2000.0,
        )
        bus = BusState()
        bess.resolve(bus)
        bess.integrate_energy(1000.0)
        bess.integrate_energy(4600.0)
        # 2000W for 1 hour * 0.95 efficiency = 1.9 kWh stored
        expected = 3.0 + (2.0 * 0.95)
        assert abs(bess.soe_kwh - expected) < 0.01

    def test_soe_clamped_to_bounds(self) -> None:
        bess = self._make_bess(nameplate_capacity_kwh=10.0, soe_kwh=9.9)
        bess.effective_state = "charging"
        bess.effective_power_w = 50000.0  # absurdly high
        bess.integrate_energy(0.0)
        bess.integrate_energy(3600.0)
        assert bess.soe_kwh <= 10.0


class TestBESSUnitHybridPV:
    def test_hybrid_keeps_pv_online_when_grid_disconnected(self) -> None:
        pv = PVSource(available_power_w=4000.0, online=True)
        bess = BESSUnit(
            nameplate_capacity_kwh=13.5,
            max_charge_w=3500.0,
            max_discharge_w=5000.0,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            backup_reserve_pct=20.0,
            hard_min_pct=5.0,
            hybrid=True,
            pv_source=pv,
            soe_kwh=6.75,
        )
        bess.update_pv_online_status(grid_connected=False)
        assert pv.online is True

    def test_non_hybrid_sheds_pv_when_grid_disconnected(self) -> None:
        pv = PVSource(available_power_w=4000.0, online=True)
        bess = BESSUnit(
            nameplate_capacity_kwh=13.5,
            max_charge_w=3500.0,
            max_discharge_w=5000.0,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            backup_reserve_pct=20.0,
            hard_min_pct=5.0,
            hybrid=False,
            pv_source=pv,
            soe_kwh=6.75,
        )
        bess.update_pv_online_status(grid_connected=False)
        assert pv.online is False

    def test_non_hybrid_pv_online_when_grid_connected(self) -> None:
        pv = PVSource(available_power_w=4000.0, online=False)
        bess = BESSUnit(
            nameplate_capacity_kwh=13.5,
            max_charge_w=3500.0,
            max_discharge_w=5000.0,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            backup_reserve_pct=20.0,
            hard_min_pct=5.0,
            hybrid=False,
            pv_source=pv,
            soe_kwh=6.75,
        )
        bess.update_pv_online_status(grid_connected=True)
        assert pv.online is True
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_energy/test_components.py -v`
Expected: All pass (implementation already done in Task 4)

- [ ] **Step 3: Commit**

```bash
git add tests/test_energy/test_components.py
git commit -m "Add BESSUnit charge, SOE integration, and hybrid PV tests"
```

---

### Task 6: PanelBus

**Files:**
- Create: `src/span_panel_simulator/energy/bus.py`
- Test: `tests/test_energy/test_bus.py`

- [ ] **Step 1: Write failing bus integration tests**

Create `tests/test_energy/test_bus.py`:

```python
"""Layer 2: Bus integration tests — conservation enforcement."""

from __future__ import annotations

from span_panel_simulator.energy.bus import PanelBus
from span_panel_simulator.energy.components import BESSUnit, GridMeter, LoadGroup, PVSource


def _make_bess(**kwargs: object) -> BESSUnit:
    defaults: dict[str, object] = dict(
        nameplate_capacity_kwh=13.5,
        max_charge_w=3500.0,
        max_discharge_w=5000.0,
        charge_efficiency=0.95,
        discharge_efficiency=0.95,
        backup_reserve_pct=20.0,
        hard_min_pct=5.0,
        hybrid=False,
        pv_source=None,
        soe_kwh=10.0,
        scheduled_state="idle",
        requested_power_w=0.0,
    )
    defaults.update(kwargs)
    return BESSUnit(**defaults)


class TestBusConservation:
    def test_load_only_grid_covers(self) -> None:
        bus = PanelBus(
            components=[LoadGroup(demand_w=5000.0), GridMeter(connected=True)]
        )
        state = bus.resolve()
        assert state.is_balanced()
        assert state.grid_power_w == 5000.0

    def test_load_and_pv_grid_covers_deficit(self) -> None:
        bus = PanelBus(
            components=[
                LoadGroup(demand_w=5000.0),
                PVSource(available_power_w=3000.0, online=True),
                GridMeter(connected=True),
            ]
        )
        state = bus.resolve()
        assert state.is_balanced()
        assert abs(state.grid_power_w - 2000.0) < 0.01

    def test_pv_exceeds_load_grid_absorbs(self) -> None:
        bus = PanelBus(
            components=[
                LoadGroup(demand_w=2000.0),
                PVSource(available_power_w=5000.0, online=True),
                GridMeter(connected=True),
            ]
        )
        state = bus.resolve()
        assert state.is_balanced()
        # Grid absorbs 3kW excess (negative = exporting/absorbing)
        assert abs(state.grid_power_w - (-3000.0)) < 0.01
        # Demand/supply only reflect non-SLACK components
        assert abs(state.total_demand_w - 2000.0) < 0.01
        assert abs(state.total_supply_w - 5000.0) < 0.01

    def test_bess_discharge_reduces_grid(self) -> None:
        bess = _make_bess(scheduled_state="discharging", requested_power_w=3000.0)
        bus = PanelBus(
            components=[
                LoadGroup(demand_w=5000.0),
                bess,
                GridMeter(connected=True),
            ]
        )
        state = bus.resolve()
        assert state.is_balanced()
        assert abs(state.grid_power_w - 2000.0) < 0.01

    def test_bess_charge_increases_grid(self) -> None:
        bess = _make_bess(scheduled_state="charging", requested_power_w=3000.0, soe_kwh=5.0)
        bus = PanelBus(
            components=[
                LoadGroup(demand_w=2000.0),
                bess,
                GridMeter(connected=True),
            ]
        )
        state = bus.resolve()
        assert state.is_balanced()
        assert abs(state.grid_power_w - 5000.0) < 0.01

    def test_grid_never_negative_from_bess_discharge(self) -> None:
        bess = _make_bess(scheduled_state="discharging", requested_power_w=5000.0)
        bus = PanelBus(
            components=[
                LoadGroup(demand_w=2000.0),
                PVSource(available_power_w=1000.0, online=True),
                bess,
                GridMeter(connected=True),
            ]
        )
        state = bus.resolve()
        assert state.is_balanced()
        assert state.grid_power_w >= -0.01  # never negative

    def test_conservation_grid_disconnected(self) -> None:
        bess = _make_bess(scheduled_state="discharging", requested_power_w=5000.0)
        bus = PanelBus(
            components=[
                LoadGroup(demand_w=3000.0),
                bess,
                GridMeter(connected=False),
            ]
        )
        state = bus.resolve()
        assert state.is_balanced()
        assert abs(state.grid_power_w) < 0.01

    def test_conservation_no_bess(self) -> None:
        bus = PanelBus(
            components=[
                LoadGroup(demand_w=5000.0),
                PVSource(available_power_w=2000.0, online=True),
                GridMeter(connected=True),
            ]
        )
        state = bus.resolve()
        assert state.is_balanced()

    def test_conservation_no_pv(self) -> None:
        bus = PanelBus(
            components=[
                LoadGroup(demand_w=5000.0),
                GridMeter(connected=True),
            ]
        )
        state = bus.resolve()
        assert state.is_balanced()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_energy/test_bus.py -v`
Expected: FAIL with `ImportError`

- [ ] **Step 3: Implement PanelBus**

Create `src/span_panel_simulator/energy/bus.py`:

```python
"""PanelBus — role-ordered power flow resolution with conservation enforcement."""

from __future__ import annotations

from span_panel_simulator.energy.components import Component
from span_panel_simulator.energy.types import BusState, ComponentRole


class PanelBus:
    """Resolves components in role order and enforces conservation of energy.

    Components are evaluated in order: LOAD -> SOURCE -> STORAGE -> SLACK.
    SLACK (grid) contributions are tracked in ``grid_power_w`` only — they
    are NOT folded into ``total_demand_w``/``total_supply_w`` so that the
    conservation check ``total_demand = total_supply + grid_power`` holds
    without double-counting.
    """

    def __init__(self, components: list[Component]) -> None:
        self._components = components

    def resolve(self) -> BusState:
        state = BusState()
        for role in (ComponentRole.LOAD, ComponentRole.SOURCE, ComponentRole.STORAGE, ComponentRole.SLACK):
            for component in self._components:
                if component.role != role:
                    continue
                contribution = component.resolve(state)
                if role == ComponentRole.SLACK:
                    # Grid tracked separately — not in demand/supply totals
                    state.grid_power_w += contribution.supply_w - contribution.demand_w
                else:
                    state.total_demand_w += contribution.demand_w
                    state.total_supply_w += contribution.supply_w
                    if role == ComponentRole.STORAGE:
                        state.storage_contribution_w += contribution.supply_w - contribution.demand_w
        return state
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_energy/test_bus.py -v`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add src/span_panel_simulator/energy/bus.py tests/test_energy/test_bus.py
git commit -m "Add PanelBus with role-ordered resolution and conservation checks"
```

---

### Task 7: EnergySystem

**Files:**
- Create: `src/span_panel_simulator/energy/system.py`
- Modify: `src/span_panel_simulator/energy/__init__.py`
- Test: `tests/test_energy/test_scenarios.py`

- [ ] **Step 1: Write failing scenario tests**

Create `tests/test_energy/test_scenarios.py`:

```python
"""Layer 3: Topology and scenario tests covering all identified issues."""

from __future__ import annotations

from span_panel_simulator.energy.system import EnergySystem
from span_panel_simulator.energy.types import (
    BESSConfig,
    EnergySystemConfig,
    GridConfig,
    LoadConfig,
    PowerInputs,
    PVConfig,
)


def _grid_online() -> GridConfig:
    return GridConfig(connected=True)


def _grid_offline() -> GridConfig:
    return GridConfig(connected=False)


def _pv(nameplate_w: float = 6000.0, inverter_type: str = "ac_coupled") -> PVConfig:
    return PVConfig(nameplate_w=nameplate_w, inverter_type=inverter_type)


def _bess(
    *,
    nameplate_kwh: float = 13.5,
    max_discharge_w: float = 5000.0,
    max_charge_w: float = 3500.0,
    hybrid: bool = False,
    backup_reserve_pct: float = 20.0,
    initial_soe_kwh: float | None = None,
) -> BESSConfig:
    return BESSConfig(
        nameplate_kwh=nameplate_kwh,
        max_discharge_w=max_discharge_w,
        max_charge_w=max_charge_w,
        hybrid=hybrid,
        backup_reserve_pct=backup_reserve_pct,
        initial_soe_kwh=initial_soe_kwh,
    )


# === GFE and Discharge Throttling (Issues 1, 2) ===


class TestGFEThrottling:
    def test_grid_never_negative_from_bess_discharge(self) -> None:
        system = EnergySystem.from_config(EnergySystemConfig(
            grid=_grid_online(),
            pv=_pv(),
            bess=_bess(),
            loads=[LoadConfig(demand_w=3000.0)],
        ))
        state = system.tick(1000.0, PowerInputs(
            pv_available_w=2000.0,
            bess_scheduled_state="discharging",
            bess_requested_w=5000.0,
            load_demand_w=3000.0,
            grid_connected=True,
        ))
        assert state.balanced
        assert state.grid_power_w >= -0.01
        assert state.bess_power_w == 1000.0

    def test_bess_covers_exact_deficit(self) -> None:
        system = EnergySystem.from_config(EnergySystemConfig(
            grid=_grid_online(),
            bess=_bess(),
            loads=[LoadConfig(demand_w=3000.0)],
        ))
        state = system.tick(1000.0, PowerInputs(
            bess_scheduled_state="discharging",
            bess_requested_w=5000.0,
            load_demand_w=3000.0,
            grid_connected=True,
        ))
        assert state.balanced
        assert abs(state.grid_power_w) < 0.01
        assert abs(state.bess_power_w - 3000.0) < 0.01

    def test_bess_idle_when_pv_exceeds_load(self) -> None:
        system = EnergySystem.from_config(EnergySystemConfig(
            grid=_grid_online(),
            pv=_pv(),
            bess=_bess(),
            loads=[LoadConfig(demand_w=2000.0)],
        ))
        state = system.tick(1000.0, PowerInputs(
            pv_available_w=5000.0,
            bess_scheduled_state="discharging",
            bess_requested_w=5000.0,
            load_demand_w=2000.0,
            grid_connected=True,
        ))
        assert state.balanced
        assert state.bess_power_w == 0.0
        assert state.bess_state == "idle"


# === Hybrid vs Non-Hybrid Islanding (Issues 3, 5, 8) ===


class TestIslanding:
    def test_non_hybrid_pv_offline_bess_covers_all(self) -> None:
        system = EnergySystem.from_config(EnergySystemConfig(
            grid=_grid_offline(),
            pv=_pv(inverter_type="ac_coupled"),
            bess=_bess(hybrid=False),
            loads=[LoadConfig(demand_w=3000.0)],
        ))
        state = system.tick(1000.0, PowerInputs(
            pv_available_w=4000.0,
            bess_scheduled_state="discharging",
            bess_requested_w=5000.0,
            load_demand_w=3000.0,
            grid_connected=False,
        ))
        assert state.balanced
        assert state.pv_power_w == 0.0
        assert state.bess_power_w == 3000.0
        assert state.grid_power_w == 0.0

    def test_hybrid_pv_online_bess_covers_gap(self) -> None:
        system = EnergySystem.from_config(EnergySystemConfig(
            grid=_grid_offline(),
            pv=_pv(inverter_type="hybrid"),
            bess=_bess(hybrid=True),
            loads=[LoadConfig(demand_w=5000.0)],
        ))
        state = system.tick(1000.0, PowerInputs(
            pv_available_w=3000.0,
            bess_scheduled_state="discharging",
            bess_requested_w=5000.0,
            load_demand_w=5000.0,
            grid_connected=False,
        ))
        assert state.balanced
        assert state.pv_power_w == 3000.0
        assert state.bess_power_w == 2000.0
        assert state.grid_power_w == 0.0

    def test_hybrid_island_solar_excess_charges_bess(self) -> None:
        system = EnergySystem.from_config(EnergySystemConfig(
            grid=_grid_offline(),
            pv=_pv(inverter_type="hybrid"),
            bess=_bess(hybrid=True, max_charge_w=3000.0, initial_soe_kwh=5.0),
            loads=[LoadConfig(demand_w=2000.0)],
        ))
        state = system.tick(1000.0, PowerInputs(
            pv_available_w=5000.0,
            bess_scheduled_state="charging",
            bess_requested_w=3000.0,
            load_demand_w=2000.0,
            grid_connected=False,
        ))
        assert state.balanced
        assert state.pv_power_w == 5000.0
        assert state.bess_state == "charging"
        assert state.bess_power_w == 3000.0
        assert state.grid_power_w == 0.0

    def test_non_hybrid_island_ignores_solar_excess(self) -> None:
        system = EnergySystem.from_config(EnergySystemConfig(
            grid=_grid_offline(),
            pv=_pv(inverter_type="ac_coupled"),
            bess=_bess(hybrid=False),
            loads=[LoadConfig(demand_w=3000.0)],
        ))
        state = system.tick(1000.0, PowerInputs(
            pv_available_w=5000.0,
            bess_scheduled_state="charging",
            bess_requested_w=3000.0,
            load_demand_w=3000.0,
            grid_connected=False,
        ))
        assert state.balanced
        assert state.pv_power_w == 0.0
        # Must discharge to cover load since PV is offline
        assert state.bess_power_w == 3000.0
        assert state.bess_state == "discharging"


# === Charging/Discharging Grid Impact (Issues 6, 7, 9) ===


class TestGridImpact:
    def test_charging_increases_grid_import(self) -> None:
        system = EnergySystem.from_config(EnergySystemConfig(
            grid=_grid_online(),
            bess=_bess(max_charge_w=3000.0),
            loads=[LoadConfig(demand_w=2000.0)],
        ))
        state = system.tick(1000.0, PowerInputs(
            bess_scheduled_state="charging",
            bess_requested_w=3000.0,
            load_demand_w=2000.0,
            grid_connected=True,
        ))
        assert state.balanced
        assert abs(state.grid_power_w - 5000.0) < 0.01

    def test_discharging_decreases_grid_import(self) -> None:
        system = EnergySystem.from_config(EnergySystemConfig(
            grid=_grid_online(),
            bess=_bess(max_discharge_w=3000.0),
            loads=[LoadConfig(demand_w=5000.0)],
        ))
        state = system.tick(1000.0, PowerInputs(
            bess_scheduled_state="discharging",
            bess_requested_w=3000.0,
            load_demand_w=5000.0,
            grid_connected=True,
        ))
        assert state.balanced
        assert abs(state.grid_power_w - 2000.0) < 0.01

    def test_add_bess_reduces_grid_in_modeling(self) -> None:
        config_no_bess = EnergySystemConfig(
            grid=_grid_online(),
            loads=[LoadConfig(demand_w=5000.0)],
        )
        config_with_bess = EnergySystemConfig(
            grid=_grid_online(),
            bess=_bess(max_discharge_w=3000.0),
            loads=[LoadConfig(demand_w=5000.0)],
        )
        sys_b = EnergySystem.from_config(config_no_bess)
        sys_a = EnergySystem.from_config(config_with_bess)

        state_b = sys_b.tick(1000.0, PowerInputs(load_demand_w=5000.0))
        state_a = sys_a.tick(1000.0, PowerInputs(
            load_demand_w=5000.0,
            bess_scheduled_state="discharging",
            bess_requested_w=3000.0,
        ))
        assert state_b.balanced and state_a.balanced
        assert abs(state_b.grid_power_w - 5000.0) < 0.01
        assert abs(state_a.grid_power_w - 2000.0) < 0.01


# === No Stale State (Issue 10) ===


class TestIndependentInstances:
    def test_two_instances_share_no_state(self) -> None:
        config = EnergySystemConfig(
            grid=_grid_online(),
            bess=_bess(initial_soe_kwh=10.0),
            loads=[LoadConfig(demand_w=3000.0)],
        )
        sys1 = EnergySystem.from_config(config)
        sys2 = EnergySystem.from_config(config)

        # Discharge sys1 for a tick
        sys1.tick(1000.0, PowerInputs(
            bess_scheduled_state="discharging",
            bess_requested_w=3000.0,
            load_demand_w=3000.0,
        ))
        sys1.tick(4600.0, PowerInputs(
            bess_scheduled_state="discharging",
            bess_requested_w=3000.0,
            load_demand_w=3000.0,
        ))

        # sys2 should still have initial SOE
        state2 = sys2.tick(1000.0, PowerInputs(
            bess_scheduled_state="discharging",
            bess_requested_w=3000.0,
            load_demand_w=3000.0,
        ))
        assert state2.soe_kwh == 10.0


# === EVSE as Consumer ===


class TestEVSE:
    def test_evse_is_pure_load(self) -> None:
        system = EnergySystem.from_config(EnergySystemConfig(
            grid=_grid_online(),
            loads=[
                LoadConfig(demand_w=3000.0),
                LoadConfig(demand_w=7200.0),  # EVSE at 30A
            ],
        ))
        state = system.tick(1000.0, PowerInputs(
            load_demand_w=10200.0,
            grid_connected=True,
        ))
        assert state.balanced
        assert abs(state.grid_power_w - 10200.0) < 0.01
        assert abs(state.load_power_w - 10200.0) < 0.01


# === SOE Duration (Nameplate) ===


class TestNameplateDuration:
    def test_larger_nameplate_sustains_longer(self) -> None:
        small_config = EnergySystemConfig(
            grid=_grid_online(),
            bess=_bess(nameplate_kwh=5.0, max_discharge_w=2500.0, initial_soe_kwh=2.5),
            loads=[LoadConfig(demand_w=2500.0)],
        )
        large_config = EnergySystemConfig(
            grid=_grid_online(),
            bess=_bess(nameplate_kwh=20.0, max_discharge_w=2500.0, initial_soe_kwh=10.0),
            loads=[LoadConfig(demand_w=2500.0)],
        )
        small = EnergySystem.from_config(small_config)
        large = EnergySystem.from_config(large_config)

        inputs = PowerInputs(
            bess_scheduled_state="discharging",
            bess_requested_w=2500.0,
            load_demand_w=2500.0,
            grid_connected=True,
        )

        # Simulate 2 hours in 60-second ticks
        ts = 0.0
        for _ in range(120):
            ts += 60.0
            s_small = small.tick(ts, inputs)
            s_large = large.tick(ts, inputs)

        # Small BESS (5kWh, started at 50%, reserve=20%=1kWh, usable=1.5kWh)
        # should be depleted; large should still be going
        assert s_small.bess_state == "idle"
        assert s_large.bess_state == "discharging"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_energy/test_scenarios.py -v`
Expected: FAIL with `ImportError`

- [ ] **Step 3: Implement EnergySystem**

Create `src/span_panel_simulator/energy/system.py`:

```python
"""EnergySystem — the top-level energy balance resolver.

Constructed from an ``EnergySystemConfig``, ticked with ``PowerInputs``,
and returns a ``SystemState``.  Pure value object — no external dependencies,
no shared mutable state.
"""

from __future__ import annotations

from span_panel_simulator.energy.bus import PanelBus
from span_panel_simulator.energy.components import BESSUnit, GridMeter, LoadGroup, PVSource
from span_panel_simulator.energy.types import (
    BESSConfig,
    EnergySystemConfig,
    PowerInputs,
    SystemState,
)


class EnergySystem:
    """Component-based energy balance resolver.

    Instantiate via ``from_config()``.  Call ``tick()`` each simulation
    step to resolve power flows across the bus.
    """

    def __init__(
        self,
        bus: PanelBus,
        grid: GridMeter,
        pv: PVSource | None,
        bess: BESSUnit | None,
        load: LoadGroup,
    ) -> None:
        self.bus = bus
        self.grid = grid
        self.pv = pv
        self.bess = bess
        self.load = load

    @staticmethod
    def from_config(config: EnergySystemConfig) -> EnergySystem:
        grid = GridMeter(connected=config.grid.connected)

        pv: PVSource | None = None
        if config.pv is not None:
            pv = PVSource(available_power_w=0.0, online=True)

        bess: BESSUnit | None = None
        if config.bess is not None:
            bc: BESSConfig = config.bess
            initial_soe = bc.initial_soe_kwh
            if initial_soe is None:
                initial_soe = bc.nameplate_kwh * 0.5
            bess = BESSUnit(
                nameplate_capacity_kwh=bc.nameplate_kwh,
                max_charge_w=bc.max_charge_w,
                max_discharge_w=bc.max_discharge_w,
                charge_efficiency=bc.charge_efficiency,
                discharge_efficiency=bc.discharge_efficiency,
                backup_reserve_pct=bc.backup_reserve_pct,
                hard_min_pct=bc.hard_min_pct,
                hybrid=bc.hybrid,
                pv_source=pv,
                soe_kwh=initial_soe,
            )

        total_demand = sum(lc.demand_w for lc in config.loads)
        load = LoadGroup(demand_w=total_demand)

        components: list = [load]
        if pv is not None:
            components.append(pv)
        if bess is not None:
            components.append(bess)
        components.append(grid)

        bus = PanelBus(components=components)
        return EnergySystem(bus=bus, grid=grid, pv=pv, bess=bess, load=load)

    def tick(self, ts: float, inputs: PowerInputs) -> SystemState:
        # 1. Apply topology
        self.grid.connected = inputs.grid_connected
        if self.bess is not None:
            self.bess.update_pv_online_status(inputs.grid_connected)
        elif self.pv is not None and not inputs.grid_connected:
            self.pv.online = False

        # 2. Set component inputs
        self.load.demand_w = inputs.load_demand_w
        if self.pv is not None:
            self.pv.available_power_w = inputs.pv_available_w
        if self.bess is not None:
            self.bess.scheduled_state = inputs.bess_scheduled_state
            self.bess.requested_power_w = inputs.bess_requested_w

            # Non-hybrid islanding override: if grid disconnected and PV
            # is offline, BESS must discharge regardless of schedule
            if not inputs.grid_connected and not self.bess.hybrid:
                self.bess.scheduled_state = "discharging"
                self.bess.requested_power_w = self.bess.max_discharge_w

        # 3. Resolve bus
        bus_state = self.bus.resolve()

        # 4. Integrate BESS energy
        if self.bess is not None:
            self.bess.integrate_energy(ts)

        # 5. Return resolved state
        pv_power = 0.0
        if self.pv is not None and self.pv.online:
            pv_power = self.pv.available_power_w

        bess_power = 0.0
        bess_state = "idle"
        soe_kwh = 0.0
        soe_pct = 0.0
        if self.bess is not None:
            bess_power = self.bess.effective_power_w
            bess_state = self.bess.effective_state
            soe_kwh = self.bess.soe_kwh
            soe_pct = self.bess.soe_percentage

        return SystemState(
            grid_power_w=bus_state.grid_power_w,
            pv_power_w=pv_power,
            bess_power_w=bess_power,
            bess_state=bess_state,
            load_power_w=inputs.load_demand_w,
            soe_kwh=soe_kwh,
            soe_percentage=soe_pct,
            balanced=bus_state.is_balanced(),
        )
```

- [ ] **Step 4: Update `__init__.py` with public API exports**

Update `src/span_panel_simulator/energy/__init__.py`:

```python
"""Component-based energy system for the SPAN panel simulator.

Public API:
    EnergySystem    — top-level resolver (construct via from_config)
    SystemState     — resolved output of a tick
    PowerInputs     — external inputs that drive each tick
    EnergySystemConfig, GridConfig, PVConfig, BESSConfig, LoadConfig — configuration
"""

from span_panel_simulator.energy.system import EnergySystem
from span_panel_simulator.energy.types import (
    BESSConfig,
    EnergySystemConfig,
    GridConfig,
    LoadConfig,
    PowerInputs,
    PVConfig,
    SystemState,
)

__all__ = [
    "BESSConfig",
    "EnergySystem",
    "EnergySystemConfig",
    "GridConfig",
    "LoadConfig",
    "PowerInputs",
    "PVConfig",
    "SystemState",
]
```

- [ ] **Step 5: Run all energy tests**

Run: `.venv/bin/python -m pytest tests/test_energy/ -v`
Expected: All pass

- [ ] **Step 6: Run full test suite to confirm nothing is broken**

Run: `.venv/bin/python -m pytest tests/ -v`
Expected: All 193+ tests pass

- [ ] **Step 7: Commit**

```bash
git add src/span_panel_simulator/energy/ tests/test_energy/
git commit -m "Add EnergySystem with from_config factory and full scenario tests"
```

---

## Phase 2: Wire Into Engine

### Task 8: Add EnergySystem to Engine Initialization

**Files:**
- Modify: `src/span_panel_simulator/engine.py`

The engine needs to construct an `EnergySystem` from its configuration. This task adds the construction without yet using it for power flow — that comes in Task 9.

- [ ] **Step 1: Add import and instance variable**

In `src/span_panel_simulator/engine.py`, add import after existing imports (after line 30):

```python
from span_panel_simulator.energy import (
    BESSConfig,
    EnergySystem,
    EnergySystemConfig,
    GridConfig,
    LoadConfig,
    PowerInputs,
    PVConfig,
    SystemState,
)
```

In `DynamicSimulationEngine.__init__()` (after line 843 `self._bsee` assignment), add:

```python
self._energy_system: EnergySystem | None = None
```

- [ ] **Step 2: Add `_build_energy_system()` helper**

Add method to `DynamicSimulationEngine` (after `_create_bsee()`, after line 1839):

```python
def _build_energy_system(self) -> EnergySystem | None:
    """Construct an EnergySystem from current circuit configuration."""
    if not self._config:
        return None

    # Grid config
    grid_config = GridConfig(connected=not self._forced_grid_offline)

    # PV config — find producer circuits
    pv_config: PVConfig | None = None
    for circuit in self._circuits.values():
        if circuit.energy_mode == "producer":
            nameplate = float(circuit.template.get("typical_power", 0))
            inverter_type = circuit.template.get("inverter_type", "ac_coupled")
            pv_config = PVConfig(nameplate_w=abs(nameplate), inverter_type=inverter_type)
            break

    # BESS config — from battery circuit template
    bess_config: BESSConfig | None = None
    battery_circuit = self._find_battery_circuit()
    if battery_circuit is not None:
        battery_cfg = battery_circuit.template.get("battery_behavior", {})
        if isinstance(battery_cfg, dict) and battery_cfg.get("enabled", False):
            nameplate = float(battery_cfg.get("nameplate_capacity_kwh", 13.5))
            hybrid = battery_cfg.get("inverter_type") == "hybrid"
            bess_config = BESSConfig(
                nameplate_kwh=nameplate,
                max_charge_w=abs(float(battery_cfg.get("max_charge_power", 3500.0))),
                max_discharge_w=abs(float(battery_cfg.get("max_discharge_power", 3500.0))),
                charge_efficiency=float(battery_cfg.get("charge_efficiency", 0.95)),
                discharge_efficiency=float(battery_cfg.get("discharge_efficiency", 0.95)),
                backup_reserve_pct=float(battery_cfg.get("backup_reserve_pct", 20.0)),
                hybrid=hybrid,
                initial_soe_kwh=self._bsee.soe_kwh if self._bsee is not None else None,
            )

    # Load configs — all consumer circuits
    loads = [LoadConfig() for c in self._circuits.values() if c.energy_mode == "consumer"]

    config = EnergySystemConfig(
        grid=grid_config,
        pv=pv_config,
        bess=bess_config,
        loads=loads,
    )
    return EnergySystem.from_config(config)
```

- [ ] **Step 3: Construct energy system during initialization**

In `initialize_async()` (after `_create_bsee()` call, after line 906), add:

```python
self._energy_system = self._build_energy_system()
```

- [ ] **Step 4: Run full test suite**

Run: `.venv/bin/python -m pytest tests/ -v`
Expected: All tests pass (energy system constructed but not yet used for output)

- [ ] **Step 5: Commit**

```bash
git add src/span_panel_simulator/energy/ src/span_panel_simulator/engine.py
git commit -m "Wire EnergySystem construction into engine initialization"
```

---

### Task 9: Use EnergySystem in `get_snapshot()`

**Files:**
- Modify: `src/span_panel_simulator/engine.py`

Replace the inline energy aggregation (lines ~1250-1296) with `EnergySystem.tick()`. The engine still ticks circuits via the behavior engine, but reads grid/battery/pv power from `SystemState`.

- [ ] **Step 1: Add `_collect_power_inputs()` helper**

Add method to `DynamicSimulationEngine`:

```python
def _collect_power_inputs(self) -> PowerInputs:
    """Collect current circuit state into PowerInputs for the energy system."""
    pv_power = 0.0
    load_power = 0.0
    bess_power = 0.0
    bess_state = "idle"

    for circuit in self._circuits.values():
        power = circuit.instant_power_w
        if circuit.energy_mode == "producer":
            pv_power += power
        elif circuit.energy_mode == "bidirectional":
            bess_power = power
            if self._bsee is not None:
                bess_state = self._bsee.battery_state
            elif self._behavior_engine is not None:
                bess_state = self._behavior_engine.last_battery_direction
        else:
            load_power += power

    return PowerInputs(
        pv_available_w=pv_power,
        bess_requested_w=bess_power,
        bess_scheduled_state=bess_state,
        load_demand_w=load_power,
        grid_connected=not self._forced_grid_offline,
    )
```

- [ ] **Step 2: Replace energy aggregation in `get_snapshot()`**

In `get_snapshot()`, after the circuit ticking and global overrides (after line ~1232, before the old aggregation), replace the aggregation block (lines ~1249-1295) with:

```python
# 5. Resolve energy balance via component system
system_state: SystemState | None = None
if self._energy_system is not None:
    inputs = self._collect_power_inputs()
    system_state = self._energy_system.tick(current_time, inputs)

    # Reflect effective battery power back to circuit
    battery_circuit = self._find_battery_circuit()
    if battery_circuit is not None and self._energy_system.bess is not None:
        battery_circuit._instant_power_w = self._energy_system.bess.effective_power_w

    grid_power = system_state.grid_power_w
    site_power = system_state.load_power_w - system_state.pv_power_w
    battery_power_w = system_state.bess_power_w
```

Keep the old code path as a fallback for when `_energy_system` is None (during initialization before config is loaded).

- [ ] **Step 3: Update BSEE interaction**

After the energy system resolves, the old BSEE update call (line ~1286) should be skipped when the energy system is active (BESSUnit handles SOE tracking). Guard it:

```python
if self._bsee is not None and self._energy_system is None:
    self._bsee.update(current_time, battery_power_w, site_power_w=site_power)
    # ... existing BSEE reflection code
```

- [ ] **Step 4: Run full test suite**

Run: `.venv/bin/python -m pytest tests/ -v`
Expected: All tests pass

- [ ] **Step 5: Commit**

```bash
git add src/span_panel_simulator/engine.py
git commit -m "Use EnergySystem for power flow resolution in get_snapshot"
```

---

### Task 10: Use EnergySystem in `get_power_summary()` and `compute_modeling_data()`

**Files:**
- Modify: `src/span_panel_simulator/engine.py`

- [ ] **Step 1: Update `get_power_summary()` (lines ~1067-1154)**

Replace the inline aggregation with a read from the energy system's last state. Add a `_last_system_state` field to cache the most recent tick result:

In `__init__()` add:

```python
self._last_system_state: SystemState | None = None
```

In `get_snapshot()` after `system_state = self._energy_system.tick(...)`:

```python
self._last_system_state = system_state
```

Then simplify `get_power_summary()` to read from `_last_system_state` when available, falling back to the old logic only when the energy system isn't initialized.

- [ ] **Step 2: Update `compute_modeling_data()` (lines ~1507-1682)**

Replace the inline grid/battery calculation (lines ~1627-1650) with independent `EnergySystem` instances:

In `compute_modeling_data()`, before the timestamp loop, construct two energy systems:

```python
config_before = self._build_energy_config(baseline=True)
config_after = self._build_energy_config(baseline=False)
system_before = EnergySystem.from_config(config_before) if config_before else None
system_after = EnergySystem.from_config(config_after) if config_after else None
```

Add `_build_energy_config()` helper that creates `EnergySystemConfig` from the current or baseline configuration.

Inside the timestamp loop, replace the manual grid calculation with:

```python
if system_after is not None:
    inputs_a = PowerInputs(
        pv_available_w=prod_a,
        bess_requested_w=raw_batt_a,
        bess_scheduled_state=cloned_bsee_state,
        load_demand_w=site_a + prod_a,  # consumption component
        grid_connected=True,
    )
    state_a = system_after.tick(ts, inputs_a)
    grid_after = state_a.grid_power_w
    signed_battery_after = -state_a.bess_power_w if state_a.bess_state == "discharging" else state_a.bess_power_w
```

- [ ] **Step 3: Run full test suite**

Run: `.venv/bin/python -m pytest tests/ -v`
Expected: All tests pass

- [ ] **Step 4: Commit**

```bash
git add src/span_panel_simulator/engine.py
git commit -m "Use EnergySystem in power summary and modeling data"
```

---

## Phase 3: Eliminate Old Paths

### Task 11: Remove Old Energy Balance Code and BSEE

**Files:**
- Modify: `src/span_panel_simulator/engine.py`
- Delete: `src/span_panel_simulator/bsee.py`
- Modify: `src/span_panel_simulator/circuit.py`

- [ ] **Step 1: Remove old inline energy aggregation from `get_snapshot()`**

Remove the fallback path that was guarded by `if self._energy_system is None`. At this point the energy system is always present. Remove the old aggregation code (the `total_consumption`, `total_production`, `battery_circuit_power` accumulation loop and the `site_power = total_consumption - total_production` / `grid_power = site_power - battery_circuit_power` formulas).

- [ ] **Step 2: Remove old `get_power_summary()` inline aggregation**

Remove the `battery_state` sign-flipping block (lines ~1093-1110) and the `max(0.0, total_consumption - total_production)` formula. Replace with reads from `_last_system_state`.

- [ ] **Step 3: Remove old BSEE interaction from engine**

Remove:
- `from span_panel_simulator.bsee import BatteryStorageEquipment` import (line 26)
- `self._bsee` field from `__init__()` (line 843)
- `_create_bsee()` method (lines 1809-1839)
- `_bsee` assignment in `initialize_async()` (line 906)
- The `self._bsee.update()` call and reflection block in `get_snapshot()` (lines ~1285-1295)
- `self._bsee.set_forced_offline()` in `set_grid_online()` (line 982)
- All reads of `self._bsee` properties (soe_percentage, battery_state, etc.) — replace with reads from `SystemState` or `self._energy_system.bess`
- The cloned BSEE construction in `compute_modeling_data()` (lines ~1579-1591)

- [ ] **Step 4: Remove grid-offline force-discharge override from behavior engine**

In `_apply_battery_behavior()` (line ~478), remove:

```python
if self._grid_offline:
    self._last_battery_direction = "discharging"
    return self._get_discharge_power(battery_config, current_hour)
```

The `EnergySystem.tick()` now handles islanding behavior in its non-hybrid override logic.

- [ ] **Step 5: Remove dashboard `battery = consumption - pv` shortcut**

In `get_power_summary()` (line ~1110), remove:

```python
if self.has_battery:
    battery = total_consumption - pv
```

This is now handled by `SystemState`.

- [ ] **Step 6: Update circuit.py battery direction**

In `circuit.py` `_resolve_battery_direction()` (lines 297-320): this method should read from the energy system's `bess.effective_state` rather than querying the behavior engine's `last_battery_direction`. The engine should pass this through after each tick.

- [ ] **Step 7: Delete `bsee.py`**

```bash
git rm src/span_panel_simulator/bsee.py
```

- [ ] **Step 8: Run full test suite**

Run: `.venv/bin/python -m pytest tests/ -v`
Expected: All tests pass

- [ ] **Step 9: Run type checker**

Run: `.venv/bin/python -m mypy src/span_panel_simulator/ --no-error-summary`
Expected: No errors

- [ ] **Step 10: Commit**

```bash
git add -A
git commit -m "Remove old energy balance paths and delete bsee.py"
```

---

## Phase 4: Dead Code Removal

### Task 12: Remove All Dead Code

**Files:**
- Modify: `src/span_panel_simulator/engine.py`
- Modify: `src/span_panel_simulator/circuit.py`
- Possibly modify: `src/span_panel_simulator/dashboard/routes.py`

- [ ] **Step 1: Search for dead references**

Search for all references to removed code:

```bash
# References to old BSEE
.venv/bin/python -m pytest tests/ -v  # ensure clean first
```

Then grep for orphaned references:

- `BatteryStorageEquipment` — should have zero hits outside of git history
- `_bsee` — should have zero hits in engine.py
- `_create_bsee` — should be gone
- `battery_circuit_power` variable in get_snapshot — should be gone
- `signed_battery_before`, `signed_battery_after` — should be gone from modeling if replaced
- `cloned_bsee` — should be gone
- `_forced_grid_offline` checks that duplicate what EnergySystem handles
- `set_solar_excess` on behavior engine — if the energy system handles solar-excess through bus ordering, this may become dead
- `last_battery_direction` on behavior engine — check if still needed after circuit.py update

- [ ] **Step 2: Remove orphaned imports and variables**

Remove any imports, variables, or methods that are no longer referenced after the migration. Check:

- `engine.py`: unused imports, orphaned helper methods, variables only written but never read
- `circuit.py`: `_resolve_battery_direction` if replaced by energy system state
- `behavior_mutable_state.py`: fields that were only used by cloned BSEE

- [ ] **Step 3: Remove the GFE throttling we added to bsee.py earlier in this conversation**

This was the `site_power_w` parameter we added to `BatteryStorageEquipment.update()`. Since `bsee.py` is deleted in Task 11, verify it is indeed gone. If any vestiges remain (e.g., the engine passing `site_power_w`), remove them.

- [ ] **Step 4: Run full test suite**

Run: `.venv/bin/python -m pytest tests/ -v`
Expected: All tests pass

- [ ] **Step 5: Run type checker**

Run: `.venv/bin/python -m mypy src/span_panel_simulator/ --no-error-summary`
Expected: No errors

- [ ] **Step 6: Run linter**

Run: `.venv/bin/python -m ruff check src/span_panel_simulator/`
Expected: No errors (or only pre-existing ones)

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "Remove dead code from energy system migration"
```

---

## Summary

| Phase | Tasks | What it achieves |
|-------|-------|-----------------|
| **Phase 1** | Tasks 1-7 | Energy system built and tested in isolation. Engine untouched. |
| **Phase 2** | Tasks 8-10 | Engine wired to EnergySystem for snapshot, dashboard, and modeling. |
| **Phase 3** | Task 11 | Old energy balance code and bsee.py removed. Single source of truth. |
| **Phase 4** | Task 12 | Dead code sweep. No orphaned references. Clean codebase. |
