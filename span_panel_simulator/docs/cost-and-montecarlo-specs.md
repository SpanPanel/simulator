# Cost & Monte Carlo Implementation Specs

Implementation-level reference for the rate model, cost accumulator, scenario definitions, and Monte Carlo framework. These specs support Phases 3–5 of the [platform roadmap](platform-roadmap.md). The roadmap owns the architectural framing and phasing; this document owns the schemas, dataclasses, and math.

---

## Utility Rate Modeling

### Rate Structure Schema

Rates are defined in YAML, either inline in a scenario config or as a separate file referenced by path. The schema supports composition of multiple rate components — real utility tariffs layer TOU periods, tiers, demand charges, and credits simultaneously.

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

- `resolve_import_rate(timestamp, cumulative_kwh) -> float` — $/kWh for the current TOU period, tier, and season.
- `resolve_export_rate(timestamp) -> float` — $/kWh credit for export (net metering).
- `resolve_demand_charge(peak_demand_kw) -> float` — $ for billing period demand component.
- `resolve_fixed_charges() -> float` — monthly fixed delivery charges.

The resolver is stateless — it does not track cumulative usage. That is the cost accumulator's job.

---

## Cost Accumulator

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

### Accumulation Logic

Initialized with a `RateSchedule`, the accumulator processes an energy model (baseline or projected) and maintains:

- **Per-interval cost**: Import cost, export credit, demand contribution for each hourly interval.
- **Billing period totals**: Cumulative import kWh (for tier tracking), cumulative export kWh, rolling peak demand kW, total cost.
- **Annual totals**: Sum across billing periods.

For each hourly interval in the energy model:

1. Read grid power (positive = import, negative = export).
2. Compute energy delta in kWh.
3. For imports: look up the import rate via `RateResolver` (passing timestamp and cumulative kWh for tier resolution). Accumulate `delta_kwh * rate`.
4. For exports: look up the export rate. Accumulate credit.
5. Update the rolling demand peak: track the maximum average power (kW) within each demand window.
6. At billing period boundaries: finalize demand charges, apply fixed charges, emit a `BillingPeriodSummary`.

---

## Equipment Cost Metadata

Each equipment template includes cost metadata for financial projection:

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

---

## Scenario Definition Schema

```yaml
scenarios:
  baseline:
    rate_schedule: pge-e-tou-c.yaml

  add_battery:
    extends: baseline
    additions:
      - template: powerwall_13_5
        name: "Tesla Powerwall"

  add_solar_battery:
    extends: add_battery
    additions:
      - template: pv_7600
        name: "Additional PV Array"

  add_evse:
    extends: baseline
    additions:
      - template: evse_48a
        name: "Level 2 EVSE"
```

In the projection model, additions are virtual devices overlaid on the real energy baseline — not circuits added to a panel config. The scenario builder composes `baseline_energy_model + virtual_devices → projected_energy_model`.

---

## Monte Carlo Framework

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

### Compressed Simulation (288 ticks/year)

Running 1000 trials over 20 years at fine granularity is infeasible. The compressed mode samples 12 representative days per year (mid-month), computing 24 hourly intervals per day:

- Hourly load shape from the baseline energy model's per-month daily curves.
- Solar production from the baseline's observed inverter data or geographic solar model for virtual PV.
- Weather variation applied as a multiplier to solar and HVAC load.

Monthly energy: `daily_energy * days_in_month`. Annual energy: sum of 12 monthly totals. This preserves the hourly granularity needed for TOU cost calculation while keeping each trial fast.

### Execution Model

Each trial:

1. Sample parameter values from distributions.
2. Perturb the baseline energy model (weather scaling, usage drift, degraded equipment capacity).
3. Run the projection engine with virtual devices against the perturbed baseline.
4. Apply the cost accumulator with the scenario's rate schedule (adjusted for rate escalation).
5. Compute annual net cost for each year of the time horizon.
6. Compute derived metrics.

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

Aggregation computes P10/P25/P50/P75/P90 for each metric, probability of positive NPV, and annual cost trajectories with confidence bands.

---

## Output Metrics

| Metric | Formula | Unit |
|--------|---------|------|
| Net Present Value (NPV) | Sum of discounted annual savings minus equipment cost | $ |
| Simple Payback | Year at which cumulative savings exceed equipment cost | years |
| Internal Rate of Return (IRR) | Discount rate at which NPV = 0 | % |
| Levelized Cost of Energy (LCOE) | Total lifecycle cost / total energy produced | $/kWh |
| Peak Demand Reduction | (baseline peak − scenario peak) / baseline peak | % |
| Self-Consumption Ratio | PV consumed on-site / total PV produced | ratio |
| Grid Independence | Hours where grid import = 0 / total hours | % |
| Emissions Avoided | (baseline grid kWh − scenario grid kWh) × grid_emission_factor | kg CO₂ |

The grid emission factor defaults to the EPA eGRID regional average and can be overridden per-config.

---

## Runtime Dependencies

New dependency: `numpy` for Monte Carlo statistical aggregation and distribution sampling.
