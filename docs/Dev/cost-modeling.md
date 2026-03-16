# Cost Modeling and Monte Carlo Simulation

## Overview

The simulator today produces physically accurate per-circuit power and energy time series from cloned SPAN panel configurations enriched with HA-derived usage profiles. What it does not do is assign a dollar value to those energy flows or answer forward-looking questions like "what is the ROI of adding a 13.5 kWh battery to this panel?"

This document describes three new subsystems that layer on top of the existing simulation engine without modifying its physics:

1. **Utility rate modeling** -- data structures for TOU tariffs, tiered pricing, demand charges, and net metering.
2. **Cost accounting engine** -- a post-tick observer that accumulates cost from power flows and a rate schedule.
3. **Monte Carlo framework** -- headless batch runner that sweeps uncertain parameters across N simulation trials, collecting distribution statistics for financial and operational metrics.

Together these enable the core use case: clone a real panel, import its historical usage profiles from HA, define one or more expansion scenarios (add PV, add BESS, add EVSE), and quantify the financial impact with confidence intervals.

## Data Foundation

The HA integration already provides the realistic usage data that makes cost projections meaningful:

- **`typical_power`** (watts): Mean power draw derived from HA recorder `mean` statistic per circuit.
- **`hour_factors`** (0-23 mapped to 0.0-1.0): Hourly load shape from HA hourly statistics, normalized to peak hour. The engine applies these in `RealisticBehaviorEngine._apply_time_of_day_modulation`.
- **`duty_cycle`** (0.0-1.0): Ratio of `mean/max` from HA statistics. The engine derives on/off durations from this in `_apply_cycling_behavior`.
- **`monthly_factors`** (1-12 mapped to 0.0-1.0): Seasonal load variation from HA monthly statistics. Applied in `_apply_seasonal_modulation`.
- **`initial_consumed_energy_wh` / `initial_produced_energy_wh`**: Seed values from the real panel's energy counters, establishing correct starting baselines.

These parameters flow into clone configs via `profile_applicator.py` (`apply_usage_profiles`), which merges HA-derived profiles into `circuit_templates` entries. The engine already consumes all of these parameters -- no physics changes are needed.

The HA integration is itself a sound foundation for cost modeling. The SPAN cloud warehouse would provide a more robust historical data source (see issue #TBD), but the HA recorder path delivers sufficient profile fidelity today -- especially with a month or more of accumulated statistics.

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
    config: nj-2316-005k6-clone.yaml
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
```

### Dependencies

New runtime dependency: `numpy` for Monte Carlo statistical aggregation and distribution sampling.

### Implementation Sequence

1. **Rate model** (`cost/rate_model.py`, `config_types.py`): TypedDicts and `RateResolver`. Pure data + logic, fully testable in isolation.
2. **Cost accumulator** (`cost/accumulator.py`, `cost/metrics.py`): Processes snapshots into cost ledgers. Test against synthetic snapshot sequences.
3. **Dashboard cost summary**: Wire accumulator into the snapshot loop, add cost summary partial and route.
4. **Scenario builder** (`scenarios/builder.py`, `scenarios/equipment.py`): Config composition from baseline + additions. Test against existing clone configs.
5. **Compressed simulation** (`montecarlo/compressed_sim.py`): Headless engine mode producing annual energy/cost from 288 representative ticks.
6. **Monte Carlo runner** (`montecarlo/runner.py`, `montecarlo/distributions.py`, `montecarlo/aggregation.py`): Parameter sampling, batch execution, statistical aggregation.
7. **Dashboard scenario/MC views**: Comparison tables, distribution plots, confidence bands.

Each phase is independently shippable and testable. The cost accumulator can run alongside the live simulation before scenarios or Monte Carlo exist.
