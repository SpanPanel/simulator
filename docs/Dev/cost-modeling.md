# Cost Modeling and Monte Carlo Simulation

## Architectural Vision

The simulator add-on is the natural home for both simulation and modeling. The dashboard already separates these concerns -- simulation produces physically accurate per-circuit power and energy time series; modeling layers cost analysis, scenario comparison, and Monte Carlo projection on top without modifying the physics. This document extends that separation to the system architecture itself: the add-on owns the full workflow from panel cloning through financial modeling, while the HA integration stays focused as a device driver.

### Add-on as Platform, Integration as Device Driver

The SPAN integration's job is to make the panel a first-class HA device: discovery, entity creation, state sync, control. The add-on's job is broader -- it is a platform service with visibility across the entire HA energy ecosystem. The modeling layer needs this whole-home view because cost projections depend on data the SPAN integration cannot see: utility billing (opower), solar inverters from other manufacturers, EV chargers, weather, and custom template sensors the homeowner has built.

```
span-panel integration (device driver)
  - Panel discovery, entity creation, state sync, control
  - Scoped to SPAN panel entities only
  - No knowledge of simulation or modeling

simulator add-on (simulation + modeling platform)
  - Clone: scrapes real panel directly via eBus
  - Data acquisition: Supervisor API -> HA device registry,
    recorder statistics, opower, energy dashboard, any entity
  - Simulation: physics engine, MQTT publisher, Homie v5
  - Modeling: cost engine, scenarios, Monte Carlo
  - Single dashboard UI for the full workflow
```

This is not just a convenience -- it is an architectural requirement. The integration is scoped to the SPAN panel's entities. The add-on, running as an HA service with Supervisor API access, can query any integration's entities, any recorder history, any device registry entry. The modeling layer belongs where the data scope is broadest.

### Clone Viability Assessment

**Question**: Can the add-on own the panel clone without depending on the integration?

**Answer**: Yes. The clone pipeline is already self-contained in the simulator codebase. `scraper.py` explicitly declares no HA integration dependencies ("intentionally self-contained: it does not import span-panel-api or any HA integration code"). The full pipeline -- `scraper.py` (register + eBus scrape), `clone.py` (translate to YAML config), `profile_applicator.py` (merge usage profiles) -- runs entirely within the add-on today. The integration merely triggers it via Socket.IO.

What the add-on needs to fully replace the integration's role in the clone:

| Capability | Integration provides today | Add-on alternative |
|---|---|---|
| Panel IP | User enters in HA config flow | User enters in add-on dashboard |
| Passphrase | User enters in HA config flow | User enters in add-on dashboard |
| Location (lat/lon) | HA's `zone.home` forwarded via Socket.IO | Supervisor API -> `zone.home` entity |
| Circuit-to-entity mapping | Integration knows which HA entity corresponds to each circuit (it created them) | Supervisor API -> device registry: query SPAN panel device, enumerate entities, match by unique_id or device attributes |
| Recorder statistics | Integration queries `recorder/statistics_during_period` and computes profiles | Add-on queries the same endpoint via Supervisor API using discovered entity IDs |

**No blockers exist.** The scraper authenticates directly with the panel via HTTP (`/api/v2/auth/register`), connects to its MQTTS broker, and collects retained eBus topics. None of this touches HA. The translation and profile application are pure data transformations.

The circuit-to-entity mapping deserves attention. The integration is the authority on this mapping because it created the HA entities from the panel's eBus data. However, the add-on can reconstruct this mapping by querying HA's device registry for the SPAN panel device and enumerating its entities. This is a standard Supervisor API operation. The mapping must be rediscovered on any panel reconfiguration regardless of who holds it, so the add-on's discovery-based approach is no less robust than a cached mapping from the integration.

**User experience benefit**: Moving the clone into the add-on eliminates the UI seam where users currently start in the HA integration config flow, then switch to the simulator dashboard. The entire workflow -- discover panel, clone, tune profiles, import energy data, model scenarios, run Monte Carlo -- happens in one place.

### Data Acquisition Strategy

With the clone owned by the add-on, the data acquisition layer consolidates there as well. The add-on uses the Supervisor API to pull from across HA's ecosystem:

| Data Source | HA Path | What It Provides |
|---|---|---|
| SPAN panel (eBus) | Direct scrape (no HA needed) | Circuit topology, breaker sizing, live power/energy, relay state |
| SPAN recorder stats | Supervisor API -> `recorder/statistics_during_period` | Per-circuit `typical_power`, `hour_factors`, `duty_cycle`, `monthly_factors` |
| Opower | Supervisor API -> `sensor.opower_*` entities | Historical utility consumption, actual billed cost, implicit rate structure |
| Energy dashboard | Supervisor API -> energy entities | Whole-home production/consumption aggregates |
| Solar inverters | Supervisor API -> inverter entities (Enphase, SolarEdge, etc.) | Production data from systems not behind the panel |
| EV chargers | Supervisor API -> EVSE entities (Tesla, ChargePoint, etc.) | Charging patterns, consumption |
| Weather | Supervisor API -> weather entities | Solar production correlation, weather-adjusted modeling |

The add-on treats all data sources uniformly -- they are HA entities with recorder history. The SPAN panel is one source among many, distinguished only by the eBus clone providing circuit-level topology that other integrations lack.

---

## Overview

This document describes subsystems that layer modeling on top of the existing simulation engine without modifying its physics:

1. **Utility rate modeling** -- data structures for TOU tariffs, tiered pricing, demand charges, and net metering.
2. **Cost accounting engine** -- a post-tick observer that accumulates cost from power flows and a rate schedule.
3. **Monte Carlo framework** -- headless batch runner that sweeps uncertain parameters across N simulation trials, collecting distribution statistics for financial and operational metrics.

Together these enable the core use case: clone a real panel, import its historical usage profiles, pull utility cost data, define one or more expansion scenarios (add PV, add BESS, add EVSE), and quantify the financial impact with confidence intervals.

## Data Foundation

The add-on acquires realistic usage data from two complementary paths:

**Panel-direct (eBus clone)**: The scraper captures per-circuit topology, breaker sizing, and live energy counters directly from the panel. This provides the structural foundation -- what circuits exist, how they are wired, and their instantaneous state.

**HA ecosystem (Supervisor API)**: The add-on queries HA's recorder for the statistical profiles that make cost projections meaningful:

- **`typical_power`** (watts): Mean power draw derived from recorder `mean` statistic per circuit.
- **`hour_factors`** (0-23 mapped to 0.0-1.0): Hourly load shape from hourly statistics, normalized to peak hour. The engine applies these in `RealisticBehaviorEngine._apply_time_of_day_modulation`.
- **`duty_cycle`** (0.0-1.0): Ratio of `mean/max` from recorder statistics. The engine derives on/off durations from this in `_apply_cycling_behavior`.
- **`monthly_factors`** (1-12 mapped to 0.0-1.0): Seasonal load variation from monthly statistics. Applied in `_apply_seasonal_modulation`.
- **`initial_consumed_energy_wh` / `initial_produced_energy_wh`**: Seed values from the panel's energy counters, establishing correct starting baselines.

These parameters flow into clone configs via `profile_applicator.py` (`apply_usage_profiles`), which merges profiles into `circuit_templates` entries. The engine already consumes all of these parameters -- no physics changes are needed.

Beyond per-circuit profiles, the add-on can enrich the model with data the integration cannot see: opower billing history for rate validation, solar inverter production for cross-correlation, and whole-home energy dashboard aggregates for baseline verification. The SPAN cloud warehouse would add long-lived per-circuit historical depth (see [issue #3](https://github.com/SpanPanel/simulator/issues/3)); the HA recorder path delivers sufficient profile fidelity today with a month or more of accumulated statistics.

---

## Utility Rate Modeling

### Rate Structure Schema

Rates are defined in YAML alongside the simulation config, either inline or as a separate file referenced by path. The schema supports composition of multiple rate components -- real utility tariffs layer TOU periods, tiers, demand charges, and credits simultaneously.

```yaml
rate_schedule:
  name: "PG&E E-TOU-C"
  currency: "USD"
  billing_cycle_day: 1

  tou_periods:
    peak:
      hours: [16, 17, 18, 19, 20]
      rate_kwh: 0.49
    off_peak:
      hours: [0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,21,22,23]
      rate_kwh: 0.38
    summer_peak:
      months: [6, 7, 8, 9]
      hours: [16, 17, 18, 19, 20]
      rate_kwh: 0.54

  tiered_pricing:
    baseline_kwh: 400
    tiers:
      - { up_to_kwh: 400, rate_kwh: 0.30 }
      - { up_to_kwh: 800, rate_kwh: 0.38 }
      - { rate_kwh: 0.45 }

  demand_charges:
    enabled: true
    rate_kw: 12.50
    window_minutes: 15

  net_metering:
    enabled: true
    export_rate_kwh: 0.08
    annual_true_up: true
    nbc_kwh: 0.03

  fixed_charges:
    monthly: 15.00
```

### Rate Resolution

A `RateResolver` class accepts a `RateSchedule` and provides:

- `resolve_import_rate(timestamp, cumulative_kwh) -> float` -- $/kWh for the current TOU period, tier, and season.
- `resolve_export_rate(timestamp) -> float` -- $/kWh credit for export (net metering).
- `resolve_demand_charge(peak_demand_kw) -> float` -- $ for billing period demand component.
- `resolve_fixed_charges() -> float` -- monthly fixed delivery charges.

The resolver is stateless with respect to the simulation -- it does not track cumulative usage. That is the cost accumulator's job.

---

## Cost Accounting Engine

### Architecture

The cost engine wraps the existing `DynamicSimulationEngine` as an observer. It does not subclass or modify the physics engine. Instead, it reads each `SpanPanelSnapshot` after the engine produces it and accumulates cost.

```
DynamicSimulationEngine.get_snapshot()
        |
        v
  SpanPanelSnapshot (power, energy flows)
        |
        v
  CostAccumulator.process_snapshot(snapshot, timestamp)
        |
        v
  CostLedger (running totals)
```

### CostAccumulator

Initialized with a `RateSchedule`, the accumulator maintains:

- **Per-tick cost**: Import cost, export credit, demand contribution for this tick interval.
- **Billing period totals**: Cumulative import kWh (for tier tracking), cumulative export kWh, rolling peak demand kW, total cost.
- **Annual totals**: Sum across billing periods.

On each tick:

1. Read `instant_grid_power_w` from the snapshot. Positive = import, negative = export.
2. Compute energy delta: `power_w * tick_interval_hours` (kWh).
3. For imports: look up the import rate via `RateResolver` (passing timestamp and cumulative kWh for tier resolution). Accumulate `delta_kwh * rate`.
4. For exports: look up the export rate. Accumulate credit.
5. Update the rolling demand peak: track the maximum average power (kW) within each demand window.
6. At billing period boundaries: finalize demand charges, apply fixed charges, emit a `BillingPeriodSummary`.

### CostLedger

```python
@dataclass(frozen=True, slots=True)
class CostLedger:
    timestamp: float
    import_kwh: float
    export_kwh: float
    import_cost: float
    export_credit: float
    demand_charge: float
    fixed_charges: float
    net_cost: float             # import_cost - export_credit + demand + fixed
    peak_demand_kw: float
    billing_periods: list[BillingPeriodSummary]
```

### Integration Point

The cost accumulator attaches to the engine's snapshot loop in `app.py` where `PanelInstance` already calls `engine.get_snapshot()` on each tick. The accumulator's `process_snapshot` call is added after the snapshot is produced but before publishing. Purely observational -- cannot affect MQTT output or circuit behavior.

---

## Expansion Scenario Builder

### Concept

An expansion scenario is a YAML config variant that adds equipment to an existing panel clone. The scenario builder takes a baseline config (the cloned panel) and produces derived configs with additional circuits.

### Scenario Definition

```yaml
scenarios:
  baseline:
    config: panel-clone.yaml
    rate_schedule: pge-e-tou-c.yaml

  add_battery:
    extends: baseline
    additions:
      - template: powerwall_13_5
        circuit_id: circuit_33
        name: "Tesla Powerwall"
        tabs: [27, 28]

  add_solar_battery:
    extends: add_battery
    additions:
      - template: pv_7600
        circuit_id: circuit_34
        name: "Additional PV Array"
        tabs: [27]

  add_evse:
    extends: baseline
    additions:
      - template: evse_48a
        circuit_id: circuit_35
        name: "Level 2 EVSE"
        tabs: [27, 28]
```

### Equipment Template Library

Pre-defined templates for common expansion equipment, stored in `configs/equipment/`:

- **BESS**: Powerwall, Enphase IQ 5P, LG Chem RESU -- with realistic charge/discharge curves, efficiencies, degradation rates.
- **PV**: System sizes from 3 kW to 15 kW with region-appropriate production factors.
- **EVSE**: Level 1 (12A/120V), Level 2 (48A/240V), Level 2 with load management.

Each template includes equipment cost metadata:

```yaml
equipment_cost:
  installed_cost: 15000
  annual_maintenance: 150
  warranty_years: 10
  expected_life_years: 20
  degradation_rate_annual: 0.005
  federal_itc_eligible: true
  itc_rate: 0.30
```

### ScenarioBuilder

Takes a baseline `SimulationConfig`, applies additions, and returns a new `SimulationConfig`. Validates tab conflicts, breaker capacity, and panel slot availability. The builder does not run simulations -- it produces configs that the engine or Monte Carlo runner consumes.

---

## Monte Carlo Framework

### Purpose

Single-point simulations give a false sense of precision. Real energy costs depend on uncertain or stochastic variables: weather variation, usage pattern drift, rate escalation, equipment degradation. The Monte Carlo framework quantifies this uncertainty by running N simulations per scenario with parameter perturbation.

### Parameter Distributions

```yaml
monte_carlo:
  trials: 1000
  seed: 42
  time_horizon_years: 20

  distributions:
    weather_variation:
      type: normal
      mean: 1.0
      std: 0.12

    usage_drift:
      type: normal
      mean: 0.02
      std: 0.01

    rate_escalation:
      type: normal
      mean: 0.04
      std: 0.02

    equipment_degradation:
      type: uniform
      low: 0.003
      high: 0.007

    discount_rate:
      type: fixed
      value: 0.05
```

### Execution Model

The Monte Carlo runner operates headless -- no MQTT publishing, no dashboard. It reuses the engine's physics (circuit power computation, BSEE charge/discharge, solar model) but bypasses the Homie publisher entirely. Each trial:

1. Sample parameter values from distributions.
2. Construct a perturbed engine config (adjusted weather factors, load scaling, degraded equipment capacity).
3. Run a compressed simulation: instead of real-time ticks, advance through representative days (one per month, 24 hourly steps = 288 ticks per simulated year). This leverages `hour_factors` and `monthly_factors` which already encode the load shape.
4. Feed each tick's snapshot through `CostAccumulator` with the scenario's `RateSchedule` (adjusted for rate escalation).
5. Compute annual net cost for each year of the time horizon.
6. Compute derived metrics: NPV, payback period, LCOE.

### Compressed Simulation

Running 1000 trials at 5-second real-time ticks for 20 years is computationally infeasible. The compressed mode samples 12 representative days per year (mid-month), computing 24 hourly snapshots per day:

- `hour_factors[h]` provides the hourly load shape.
- `monthly_factors[m]` provides the seasonal multiplier.
- `solar_production_factor(timestamp, lat, lon)` provides the solar curve.
- `daily_weather_factor(timestamp, seed)` provides weather variation.

Monthly energy: `daily_energy * days_in_month`. Annual energy: sum of 12 monthly totals. This reuses the existing modulation pipeline at hourly granularity needed for TOU cost calculation.

### Trial Result

```python
@dataclass(frozen=True, slots=True)
class TrialResult:
    trial_id: int
    annual_costs: list[float]
    annual_production_kwh: list[float]
    annual_consumption_kwh: list[float]
    annual_peak_demand_kw: list[float]
    npv: float
    simple_payback_years: float | None
    irr: float | None
    lcoe: float
    self_consumption_ratio: float
    peak_demand_reduction_pct: float
```

### Aggregation

After N trials, compute:

- **Mean, median, P10, P25, P75, P90** for each metric.
- **Probability of positive NPV** (fraction of trials showing profitable investment).
- **Payback period distribution** (histogram).
- **Annual cost trajectories** (confidence bands over time horizon).

```python
@dataclass(frozen=True, slots=True)
class ScenarioResult:
    scenario_name: str
    baseline_name: str
    trials: int
    npv: StatSummary
    payback_years: StatSummary
    lcoe: StatSummary
    annual_savings: list[StatSummary]
    self_consumption: StatSummary
    peak_demand_reduction: StatSummary
    positive_npv_probability: float
```

---

## Output Metrics

| Metric | Formula | Unit |
|--------|---------|------|
| Net Present Value (NPV) | Sum of discounted annual savings minus equipment cost | $ |
| Simple Payback | Year at which cumulative savings exceed equipment cost | years |
| Internal Rate of Return (IRR) | Discount rate at which NPV = 0 | % |
| Levelized Cost of Energy (LCOE) | Total lifecycle cost / total energy produced | $/kWh |
| Peak Demand Reduction | (baseline peak - scenario peak) / baseline peak | % |
| Self-Consumption Ratio | PV consumed on-site / total PV produced | ratio |
| Grid Independence | Hours where grid import = 0 / total hours | % |
| Emissions Avoided | (baseline grid kWh - scenario grid kWh) * grid_emission_factor | kg CO2 |

The grid emission factor defaults to the EPA eGRID regional average and can be overridden per-config.

---

## Dashboard Integration

### Cost Summary Panel

`partials/cost_summary.html` -- current billing period import cost, export credit, demand charge, net cost. Rate schedule name and active TOU period indicator.

### Scenario Comparison View

`partials/scenario_comparison.html` -- side-by-side table of baseline vs. expansion scenarios with key metrics (annual cost, NPV, payback, peak demand) and delta columns showing savings.

### Monte Carlo Results

`partials/monte_carlo_results.html` -- NPV distribution histogram, payback period distribution, annual cost trajectory with P10/P90 confidence bands, probability of positive ROI.

### New Routes

```
GET  /cost-summary              -> Cost summary partial
GET  /scenarios                 -> Scenario list/comparison
POST /scenarios/run             -> Trigger Monte Carlo batch
GET  /scenarios/{id}/results    -> Monte Carlo results
GET  /rate-schedules            -> Rate schedule editor
PUT  /rate-schedules            -> Update rate schedule
```

---

## File Layout

```
src/span_panel_simulator/
  ha_api/
    __init__.py
    supervisor.py              # Supervisor API client (device registry, recorder, entities)
    entity_discovery.py        # SPAN device -> circuit entity mapping via registry
    profile_builder.py         # Compute usage profiles from recorder stats (replaces integration's role)

  cost/
    __init__.py
    rate_model.py              # RateSchedule types, RateResolver
    accumulator.py             # CostAccumulator, CostLedger
    metrics.py                 # NPV, IRR, LCOE, payback computation

  scenarios/
    __init__.py
    builder.py                 # ScenarioBuilder -- config composition
    equipment.py               # Equipment template loader + cost metadata

  montecarlo/
    __init__.py
    runner.py                  # MonteCarloRunner -- batch execution
    distributions.py           # Parameter distribution sampling
    compressed_sim.py          # Compressed simulation (288 ticks/year)
    aggregation.py             # StatSummary, ScenarioResult

  dashboard/
    templates/partials/
      clone_wizard.html        # Panel clone flow (IP, passphrase, clone, profile import)
      cost_summary.html
      scenario_comparison.html
      monte_carlo_results.html

configs/
  rates/
    pge-e-tou-c.yaml
    pge-e-tou-d.yaml
    sce-tou-d-prime.yaml
  equipment/
    powerwall_13_5.yaml
    enphase_iq5p.yaml
    pv_5kw.yaml
    pv_7_6kw.yaml
    pv_10kw.yaml
    evse_48a.yaml

tests/
  test_rate_model.py
  test_cost_accumulator.py
  test_scenario_builder.py
  test_monte_carlo.py
  test_compressed_sim.py
  test_entity_discovery.py
  test_profile_builder.py
```

### Dependencies

New runtime dependency: `numpy` for Monte Carlo statistical aggregation and distribution sampling.

### Implementation Sequence

1. **Clone in dashboard** (`dashboard/clone_wizard.html`, dashboard routes): Move the clone initiation into the add-on dashboard. User enters panel IP and passphrase, add-on runs the existing `scraper.py` + `clone.py` pipeline directly. Eliminates the integration-to-addon handoff.
2. **Supervisor API client** (`ha_api/supervisor.py`): Authenticated client for HA's Supervisor API. Provides access to device registry, entity enumeration, and `recorder/statistics_during_period`.
3. **Entity discovery** (`ha_api/entity_discovery.py`): Query the SPAN panel's HA device, enumerate its power/energy sensor entities, and map circuit template names to HA entity/statistic IDs. Rediscovery on demand when panel configuration changes.
4. **Profile builder** (`ha_api/profile_builder.py`): Compute `typical_power`, `hour_factors`, `duty_cycle`, `monthly_factors` from recorder statistics. This is the logic currently in the HA integration, moved to the add-on where it has access to the full entity surface.
5. **Rate model** (`cost/rate_model.py`, `config_types.py`): TypedDicts and `RateResolver`. Pure data + logic, fully testable in isolation.
6. **Cost accumulator** (`cost/accumulator.py`, `cost/metrics.py`): Processes snapshots into cost ledgers. Test against synthetic snapshot sequences.
7. **Dashboard cost summary**: Wire accumulator into the snapshot loop, add cost summary partial and route.
8. **Scenario builder** (`scenarios/builder.py`, `scenarios/equipment.py`): Config composition from baseline + additions. Test against existing clone configs.
9. **Compressed simulation** (`montecarlo/compressed_sim.py`): Headless engine mode producing annual energy/cost from 288 representative ticks.
10. **Monte Carlo runner** (`montecarlo/runner.py`, `montecarlo/distributions.py`, `montecarlo/aggregation.py`): Parameter sampling, batch execution, statistical aggregation.
11. **Dashboard scenario/MC views**: Comparison tables, distribution plots, confidence bands.

Each phase is independently shippable and testable. Phases 1-4 establish the add-on as a self-sufficient platform with its own clone and data acquisition. The cost accumulator (phase 6) can run alongside the live simulation before scenarios or Monte Carlo exist. The Socket.IO channel to the integration becomes optional -- retained for backward compatibility but no longer required for the core workflow.
