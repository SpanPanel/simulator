# Platform Roadmap: Energy Modeling on Real Data

## Executive Summary

The simulator today is a faithful SPAN panel replica. The platform it becomes is fundamentally different in orientation: **real data first, projection second, cost derived.**

The HA energy dashboard already captures complete historical and real-time energy usage for every device in the home — solar inverters, batteries, EV chargers, individual circuits, the grid connection. Rebuilding that observation layer is unnecessary. Instead, this platform consumes HA's energy data as its foundation, establishes a live energy baseline of the home as it actually operates, then lets the user inject virtual devices and project how those additions would change the energy picture. Cost is the final derived layer — once the kWh flows are right, dollars fall out by applying a rate structure.

Three layers, each building on the one below:

1. **Historical + Real-time** — What does this home actually consume and produce? (from HA recorder and live entity state)
2. **Virtual Projection** — What changes if I add a Powerwall / more PV / an EVSE? (simulated devices operating against real patterns)
3. **Cost & Financial** — What does that cost me, and is the investment worth it? (rate structures applied to energy deltas)

---

## The Vision in Three Sentences

Home Assistant already knows everything about the home's energy system — every device, every kWh, every hour of every day. This platform reads that reality, lets the user layer virtual devices on top of it, and projects the energy and financial impact of those additions. The result is investment-grade before/after analysis grounded in observed behavior, not synthetic models.

### Two Dashboards, Two Purposes

The HA energy dashboard and this platform's modeling dashboard are complementary, not competing:

| | HA Energy Dashboard | Modeling Dashboard |
|---|---|---|
| **Purpose** | Observation: what happened, what's happening now | Projection: what would happen if... |
| **Time direction** | Historical and present (backward/now) | Historical replay + forward projection |
| **Data** | Observed reality only | Observed reality + virtual device overlay |
| **Interaction** | Navigate, filter, drill down | Configure virtual devices, compare scenarios, iterate |
| **Output** | kWh consumed, produced, exported | Delta: "adding X changes your grid import by Y kWh, saving $Z/year" |
| **Who owns it** | HA core frontend | This add-on's dashboard |

The HA energy dashboard is the **ground truth reference**. Users already trust it, already know how to read it. The modeling dashboard is where they go to ask "what if?" and get answers grounded in that ground truth.

### Why the Panel Alone Is Not Enough

The SPAN panel sees current flowing through its breakers. It does not see:

- **Solar production details** — an Enphase or SolarEdge system feeding the main bus appears only as reduced grid import, not as measured generation. HA's inverter integration provides the actual production curve.
- **EV charging context** — a Tesla Wall Connector on a dedicated circuit shows up as load, but without the charger's integration the model cannot distinguish charging sessions from other 240V loads. HA's EVSE integrations provide session data, SOC targets, and scheduling.
- **Heat pump efficiency and mode** — the panel sees compressor power draw but not whether it's heating or cooling, its COP, or defrost cycles. HA's climate integrations provide operating mode and efficiency context.
- **Utility billing reality** — the panel measures energy; the utility bills in dollars with TOU periods, tiers, demand charges, and net metering. Opower via HA provides actual billed cost and consumption history.
- **Weather context** — cloud cover, temperature, and wind affect both solar production and HVAC load. HA weather integrations correlate these with observed energy patterns.

HA already integrates all of these. The platform's job is not to re-integrate them — it's to consume what HA provides and layer projection and cost analysis on top.

---

## Current State

### Panel Simulation (complete)

| Capability | Status | Notes |
|---|---|---|
| Panel simulation (circuits, power, energy) | Complete | Geographic solar, BSEE with efficiency/SOE, HVAC seasonal, cycling, TOD profiles |
| Virtual device types (PV, BESS, EVSE) | Complete | 4 battery charge modes, schedule-based EVSE, nameplate PV |
| Panel cloning (eBus scrape) | Complete | Self-contained pipeline: scraper → translator → config |
| Time acceleration | Complete | 1x–360x with chart scaling |
| Dashboard | Complete | HTMX + Jinja2 + Chart.js + noUiSlider |

### HA Integration (partial)

| Capability | Status | Notes |
|---|---|---|
| Per-circuit profile enrichment (recorder) | Complete | hour_factors, duty_cycle, monthly_factors from SPAN circuit entities |
| Entity discovery (SPAN device) | Complete | Maps circuit templates to HA entity/statistic IDs |
| Supervisor API client | Not started | Required for all direct HA data access |
| Ecosystem device discovery (non-SPAN) | Not started | Solar inverters, EV chargers, batteries, HVAC via HA |
| Opower (utility billing history) | Not started | Supervisor API path identified |
| Weather correlation | Not started | HA weather entities available |

### Financial Modeling (designed, not implemented)

| Capability | Status | Notes |
|---|---|---|
| Rate structure modeling | Designed | Full spec in cost-modeling.md |
| Cost accounting engine | Designed | Observer pattern spec |
| Scenario builder | Designed | YAML composition spec |
| Monte Carlo framework | Designed | Compressed sim + aggregation spec |

---

## Phase 1: Data Foundation & Ecosystem Discovery

**Goal**: Establish the plumbing to read the home's complete energy picture from HA — historical and real-time — without depending on the HA integration as intermediary.

The HA energy dashboard draws from recorder statistics and live entity state. This phase builds the add-on's ability to access the same data directly via the Supervisor API, so the modeling engine has the full picture of what the home actually does.

**Deliverables**:

1. **Supervisor API client** (`ha_api/supervisor.py`)
   - Authenticated client for device registry, entity enumeration, recorder statistics, entity state
   - Foundation for all HA data access — panel and non-panel devices alike
   - WebSocket or polling subscription for real-time entity state changes

2. **Ecosystem device discovery** (`ha_api/ecosystem_discovery.py`)
   - Discover all energy-relevant devices registered in HA:
     - **Solar inverters** (Enphase, SolarEdge, Fronius) — production power/energy entities
     - **Battery systems** (Powerwall, Enphase IQ, Victron) — SOC, charge/discharge power, operating mode
     - **EV chargers** (Tesla Wall Connector, ChargePoint, Wallbox) — session data, charging power
     - **Heat pumps / HVAC** — operating mode, COP, compressor power
     - **Grid connection** — import/export power and energy entities
     - **Weather integrations** — temperature, cloud cover, wind
   - Discovery by HA device class, entity domain, and known integration identifiers
   - Produces a unified device inventory: what the home has, where the data lives, what recorder history exists
   - User-confirmable: dashboard presents discovered devices for include/exclude

3. **Historical data acquisition**
   - Pull recorder statistics for all discovered energy entities
   - Per-circuit profiles from SPAN entities (existing capability, now via Supervisor API path)
   - Solar production curves from inverter entities
   - Battery charge/discharge patterns from battery entities
   - EV charging patterns from charger entities
   - HVAC duty cycles from climate entities
   - Grid import/export totals
   - Build a **historical energy model**: the home's energy behavior over the last N months, at hourly granularity

4. **Opower data acquisition**
   - Query `sensor.opower_*` entities for historical utility consumption and billed cost
   - Infer effective rate from billed cost ÷ consumption (rate validation for later phases)
   - Seasonal baseline from 12+ months of utility data

5. **Clone wizard in dashboard** (`dashboard/templates/partials/clone_wizard.html`)
   - User enters panel IP + passphrase directly in the add-on dashboard
   - Runs existing scraper → clone pipeline
   - Eliminates the integration-to-addon handoff

6. **Device-to-circuit mapping**
   - Map ecosystem devices to panel circuits where applicable (e.g., "Enphase system → backfeed breaker on tabs 31-32")
   - Establishes accounting boundaries for double-counting prevention: if a device maps to a known circuit, the panel measurement is authoritative; if not, the HA entity is
   - User-assistable: dashboard allows manual mapping confirmation

**Dependencies**: None. This is the foundation everything else builds on.

**Dashboard impact**: Low. Device discovery and clone wizard are forms and tables — HTMX's sweet spot. No framework pressure.

---

## Phase 2: Energy Baseline

**Goal**: Present the home's real energy picture in the modeling dashboard — a simplified, purpose-built view that establishes the "before" against which virtual devices will be measured.

This is not a clone of the HA energy dashboard. It is a **baseline summary** optimized for comparison: what are the key energy flows, what are the patterns, and what are the numbers that a virtual device would change?

**Deliverables**:

1. **Baseline energy model** (`baseline/model.py`)
   - Aggregate historical data into a representative energy profile:
     - Daily grid import/export curves (hourly resolution, per-month)
     - Solar production curve (if PV exists)
     - Battery throughput (if BESS exists)
     - EV charging pattern (if EVSE exists)
     - Total home consumption curve
   - This is the "typical day" for each month — the same 288-point (12 months × 24 hours) representation the compressed simulation uses
   - Derived entirely from HA recorder data, not from synthetic profiles

2. **Real-time energy state** (`baseline/realtime.py`)
   - Live snapshot of current energy flows from HA entity state:
     - Grid: importing or exporting, how much
     - Solar: current production
     - Battery: charging, discharging, SOC
     - EV: charging or idle, power draw
     - Home: total consumption
   - Polled or WebSocket-subscribed via Supervisor API
   - This is the "right now" view — what the home is doing at this moment

3. **Modeling dashboard: baseline view**
   - **Energy balance summary**: daily/monthly/annual grid import, solar production, battery throughput, self-consumption ratio, self-sufficiency
   - **Time navigation**: day/week/month/year views over historical data (from recorder)
   - **Current state indicator**: live power flows right now
   - **Key numbers for comparison**: the metrics that virtual devices would change — grid import is the primary target, since that's what costs money
   - This view exists so the user can see their baseline before configuring what-if scenarios. It provides the "before" half of the before/after

4. **Baseline validation**
   - Cross-validate recorder-derived baseline against Opower utility data (if available)
   - Flag discrepancies: if the model's grid import doesn't approximately match the utility-metered consumption, something is misconfigured
   - Confidence indicator: how much historical data is available, how representative is the baseline

**Dependencies**: Phase 1 (Supervisor API, ecosystem discovery, historical data).

**Dashboard impact**: Moderate. The baseline view is mostly charts and summary numbers. Chart.js handles this. Time navigation is HTMX-friendly (server renders the selected period). The real-time state indicator is a polled partial. Still comfortable on the current stack.

---

## Phase 3: Virtual Device Projection

**Goal**: Let the user inject virtual devices into the real energy model and see how they change the picture — replaying historical patterns with the virtual device operating against observed reality.

This is where the simulation engine's role shifts. It is no longer simulating a fake panel. It is answering: "given your home's actual energy patterns, what would a Powerwall / additional PV / EVSE do to your grid import?"

**Deliverables**:

1. **Projection engine** (`projection/engine.py`)
   - Takes the baseline energy model (Phase 2) as input
   - Accepts one or more virtual device definitions (BESS, PV, EVSE)
   - Replays historical energy patterns with virtual devices operating against them:
     - A virtual battery charges from observed excess solar, discharges during observed peak consumption
     - A virtual PV array adds production using the location's solar model, offset against observed consumption
     - A virtual EVSE adds load on a configurable schedule
   - Produces a **projected energy model**: the same 288-point structure as the baseline, but with virtual device effects applied
   - Reuses existing simulation physics (BSEE charge/discharge, solar model, cycling) but driven by real historical data instead of synthetic profiles

2. **Equipment template library** (`configs/equipment/`)
   - Pre-defined templates: Powerwall, Enphase IQ 5P, common PV sizes, Level 2 EVSE
   - Each includes: capacity, efficiency, degradation, charge modes, cost metadata (installed cost, maintenance, warranty, ITC eligibility)
   - Templates operate against the real baseline: a BESS template's solar-excess mode uses the home's actual observed PV production, not an assumed nameplate

3. **Scenario builder** (`scenarios/builder.py`)
   - Compose baseline + virtual device additions → projected model
   - Multiple scenarios: "add battery only," "add battery + more PV," "add EVSE"
   - YAML scenario definitions with `extends` chain
   - Validates panel capacity constraints where applicable (tab conflicts, breaker sizing)

4. **Modeling dashboard: projection view**
   - **Before/after comparison**: baseline grid import vs. projected grid import with virtual device
   - **Delta display**: "Your daily grid import drops from 16.3 kWh to 8.1 kWh (−50%)"
   - **Energy flow visualization**: where energy comes from (grid, PV real, PV virtual, battery) and where it goes — with virtual devices visually distinguished
   - **Time navigation**: replay any historical period with the virtual overlay applied — "what would last July have looked like with a Powerwall?"
   - **Interactive configuration**: select equipment, adjust sizing, see projections update
   - **Real-time projection**: "right now, with a Powerwall, you'd be discharging instead of importing" — virtual device state computed against live energy flows

5. **Historical replay**
   - For any historical period in the recorder, replay the energy data with virtual devices injected
   - User can zoom in/out (day/week/month/year) to see how virtual devices affect different seasons, weather patterns, usage levels
   - Comparison mode: side-by-side or overlay of baseline vs. projected

**Dependencies**: Phase 2 (baseline energy model, real-time state).

**Dashboard impact**: High. This is the inflection point. Interactive equipment configuration, before/after comparison with instant feedback, time-navigated replay with virtual overlay, and real-time projection are all patterns that push beyond HTMX's request-response model. This is where Svelte islands enter. See [Dashboard Architecture Assessment](#dashboard-architecture-assessment).

---

## Phase 4: Cost & Financial Modeling

**Goal**: Apply rate structures to the energy models — baseline and projected — so every kWh delta carries a dollar value. Cost is derived from the energy picture, not computed independently.

By this phase, the energy model is already validated: the baseline matches observed reality, and the projection engine produces credible energy deltas. Cost is the straightforward application of a rate schedule to those deltas.

**Deliverables**:

1. **Rate model** (`cost/rate_model.py`)
   - `RateSchedule` TypedDict: TOU periods, tiered pricing, demand charges, net metering, fixed charges
   - `RateResolver`: stateless resolution of import/export rates by timestamp + cumulative kWh
   - YAML rate definitions for common tariffs (PG&E E-TOU-C, SCE TOU-D-PRIME, etc.)
   - Unit-testable in isolation against known tariff examples
   - Rate validation against Opower data (Phase 1): does the rate model reproduce the utility's actual billed cost?

2. **Cost accumulator** (`cost/accumulator.py`)
   - Processes the baseline and projected energy models through the rate schedule
   - Produces: annual grid cost (baseline), annual grid cost (projected), annual savings, demand charges, net metering credits
   - No modification to the projection engine — purely a function applied to energy data

3. **Cost metrics** (`cost/metrics.py`)
   - NPV, simple payback, IRR, LCOE computed from annual cost streams and equipment cost metadata
   - Pure functions: `(baseline_cost_stream, projected_cost_stream, equipment_cost) → financial_metrics`

4. **Modeling dashboard: cost layer**
   - Cost annotations on the existing projection view: every energy delta now shows its dollar value
   - **Scenario comparison table**: baseline vs. each scenario with annual cost, savings, NPV, payback, peak demand
   - **Cost timeline**: monthly cost baseline vs. projected, showing seasonal variation
   - **Rate period overlay**: TOU period indicators on energy charts — "you're importing during peak, that costs $0.54/kWh"
   - **Payback visualization**: cumulative savings vs. equipment cost, time to breakeven

**Dependencies**: Phases 2-3 (energy baseline, projection engine). Phase 1 Opower data enables rate validation.

**Dashboard impact**: Moderate-to-high. The cost layer adds annotations and tables to the existing projection view. Scenario comparison with interactive toggling is where Svelte islands prove their value. The cost timeline and rate period overlay are chart enhancements.

---

## Phase 5: Monte Carlo & Optimization

**Goal**: Replace single-point projections with probability distributions. Produce defensible ROI assessments with confidence intervals. Then move from "model what the user configures" to "recommend the optimal configuration."

**Deliverables**:

1. **Parameter distributions** (`montecarlo/distributions.py`)
   - Weather variation, usage drift, rate escalation, equipment degradation
   - Normal, uniform, and fixed distribution types
   - Deterministic seeding for reproducibility

2. **Monte Carlo runner** (`montecarlo/runner.py`)
   - N trials per scenario (default 1000)
   - Each trial: perturb baseline energy model → run projection with virtual devices → apply cost → compute metrics
   - Uses the 288-point compressed representation — each trial is fast
   - Parallelizable across trials

3. **Aggregation** (`montecarlo/aggregation.py`)
   - P10/P25/P50/P75/P90 for NPV, payback, LCOE, annual savings
   - Probability of positive NPV
   - Annual cost trajectories with confidence bands

4. **Optimization targets**
   - Minimize annual energy cost
   - Minimize payback period
   - Maximize self-consumption ratio
   - Maximize grid independence

5. **Parameter search**
   - BESS sizing sweep (5–20 kWh)
   - PV sizing sweep (3–15 kW)
   - Battery charge schedule optimization (which hours to charge/discharge given TOU)
   - EVSE charging window optimization (minimize cost given rate structure + solar)

6. **Advisory output**
   - "For your usage pattern and rate plan, a 10 kWh battery with solar-excess charging saves $X/year with Y-year payback (P50)"
   - Sensitivity analysis: which parameters most affect the outcome

7. **Modeling dashboard: Monte Carlo & optimization views**
   - NPV distribution histogram
   - Payback period distribution
   - Annual cost trajectory with P10/P90 confidence bands
   - Probability of positive ROI callout
   - Sensitivity analysis: parameter importance ranking
   - Optimization result: recommended configuration with confidence intervals

**Dependencies**: Phases 3-4 (projection engine, cost model).

**Dashboard impact**: Very high. Distribution histograms, confidence bands, interactive sensitivity sliders, optimization result exploration — these are inherently client-side interactive patterns. Svelte components are essential here.

---

## Dashboard Architecture Assessment

### Two Dashboards

The platform has two distinct dashboard concerns:

1. **The existing simulator dashboard** — controls the panel simulation, displays real-time simulated power/energy, manages configs. This stays on HTMX/Jinja2/Chart.js. It works well and its scope doesn't grow.

2. **The modeling dashboard** — a new dashboard surface (or section) for baseline observation, virtual device projection, cost analysis, and Monte Carlo results. This is where the framework question matters.

### The HA Energy Dashboard Is Not Ours to Modify

The HA energy dashboard is built into HA's Lit/Polymer frontend. It is comprehensive for observation: hourly stacked bar charts, energy distribution flow diagrams, per-device breakdown, Sankey diagrams, self-sufficiency gauges. We do not replicate it, extend it, or build custom cards for it. It remains the user's reference for "what actually happened."

Our modeling dashboard consumes the same underlying data (via Supervisor API) but serves a different purpose: projection and comparison. It is a separate surface within the add-on's own dashboard, served by aiohttp, under our full control.

### Assessment by Phase

| Phase | HTMX Suitability | Notes |
|---|---|---|
| Phase 1 (Data Foundation) | Excellent | Device discovery, clone wizard — forms and tables |
| Phase 2 (Energy Baseline) | Good | Summary charts, time navigation. Server-rendered periods work. Real-time state is a polled partial |
| Phase 3 (Virtual Projection) | Strained | Interactive equipment config, before/after overlay, real-time projection — these demand client-side reactivity |
| Phase 4 (Cost & Financial) | Strained | Scenario comparison with interactive toggling, rate period overlays, cost annotations — additions to an already reactive view |
| Phase 5 (Monte Carlo) | Inadequate | Distribution visualization, sensitivity sliders, optimization exploration — inherently client-side stateful patterns |

### Recommendation: Svelte Islands Starting at Phase 3

**Phases 1–2: Stay on HTMX.** Device discovery forms, baseline summary charts, time navigation — these are server-rendered patterns where HTMX excels. The zero-build simplicity is a genuine advantage during the data foundation work.

**Phase 2 (late): Add Vite + Svelte build infrastructure.** Set up the build pipeline as prep, without migrating any existing views.

**Phase 3: First Svelte islands.** The projection view — interactive equipment selection, before/after overlay, real-time projection — is the first component that demands client-side reactivity. Svelte components mount into the existing page alongside HTMX partials.

**Phases 4–5: Expand Svelte surface.** Cost annotations, scenario comparison, Monte Carlo visualization, sensitivity analysis — each arrives as a new Svelte island. The HTMX shell handles navigation and simple forms; Svelte handles all interactive visualization.

### Why Svelte

- **Bundle size**: Compiles to vanilla JS with no runtime (~3–8 KB per component vs. React's ~40 KB baseline)
- **Reactivity model**: `$:` reactive declarations map naturally to "data flows in, chart updates"
- **Server coexistence**: Mounts into existing DOM nodes without owning the page — coexists with HTMX
- **Learning curve**: Closest to writing HTML/CSS/JS, smallest leap from the current vanilla approach
- **Charting**: LayerCake (Svelte-native), svelte-chartjs for reactive charts; `svelte-range-slider-pips` for range inputs

### Build Infrastructure

- `package.json` with `svelte`, `vite`, `@sveltejs/vite-plugin-svelte`, `typescript`
- `vite.config.ts` outputting to `src/span_panel_simulator/dashboard/static/components/`
- Components authored in `frontend/src/`, compiled to ES modules
- Python backend unchanged — aiohttp serves compiled bundles alongside existing static assets

---

## The Simulation Engine's Evolving Role

The simulation engine today generates synthetic energy data — it simulates a panel that doesn't physically exist. In the modeling platform, its role shifts:

| Capability | Today | Platform |
|---|---|---|
| Circuit power computation | Synthetic profiles + modulation | Still used for panel simulation mode |
| BSEE charge/discharge | Simulates battery against synthetic load | Projects virtual battery against real observed load |
| Solar model | Geographic production for simulated PV | Geographic production for virtual PV, validated against real inverter data |
| Time acceleration | Speeds up synthetic simulation | Replays historical data at accelerated rates |
| Compressed simulation (288 ticks/year) | Not yet built | Drives Monte Carlo: same physics, real baseline input |

The engine's physics are reusable. What changes is the **input**: instead of synthetic profiles, the engine operates against the home's actual historical energy data. The BSEE model that charges a virtual battery from excess solar doesn't care whether the solar curve comes from a synthetic `solar_production_factor` or from an Enphase inverter's recorder history — the math is the same.

---

## Work Stream Summary

```
Phase 1: Data Foundation ────────────── Backend, HTMX forms
  ├─ Supervisor API client
  ├─ Ecosystem device discovery
  ├─ Historical data acquisition
  ├─ Opower data acquisition
  ├─ Clone wizard (HTMX)
  └─ Device-to-circuit mapping

Phase 2: Energy Baseline ────────────── Backend + summary charts
  ├─ Baseline energy model (288-point)
  ├─ Real-time energy state
  ├─ Modeling dashboard: baseline view
  ├─ Baseline validation (vs. Opower)
  └─ [Infrastructure prep: Vite + Svelte build]

Phase 3: Virtual Device Projection ──── First Svelte islands
  ├─ Projection engine
  ├─ Equipment template library
  ├─ Scenario builder
  ├─ Projection view (Svelte island)
  ├─ Historical replay with overlay
  └─ Real-time projection

Phase 4: Cost & Financial ───────────── Expand Svelte surface
  ├─ Rate model + validation
  ├─ Cost accumulator
  ├─ Cost metrics (NPV, payback, IRR)
  ├─ Cost annotations on projection view
  └─ Scenario comparison (Svelte island)

Phase 5: Monte Carlo & Optimization ─── Svelte-dominant modeling UI
  ├─ Parameter distributions
  ├─ Monte Carlo runner
  ├─ Aggregation + confidence bands
  ├─ Optimization + parameter search
  ├─ Advisory output
  └─ MC/optimization views (Svelte islands)
```

---

## Key Architectural Principles

1. **Real data first, simulation second.** The home's actual energy behavior — from HA recorder and live entity state — is the foundation. Virtual devices project against observed reality, not synthetic models. The simulation engine provides physics for virtual devices; HA provides the ground truth they operate against.

2. **The HA energy dashboard is the observation layer; the modeling dashboard is the projection layer.** They share data (via Supervisor API / recorder) but serve different purposes. We do not replicate or extend the HA energy dashboard. We consume its data and build a distinct tool for "what if?" analysis.

3. **The panel is the foundation, not the boundary.** The SPAN panel provides circuit-level granularity. HA extends the model to every energy-relevant device. The architecture treats the panel as one data source among many — the most detailed one, but not the only one.

4. **HA as the universal integration layer.** The platform does not build direct integrations with Enphase, Tesla, SolarEdge, or any manufacturer. It consumes what HA already integrates. Device coverage scales with the HA ecosystem without per-vendor effort.

5. **Cost is derived, not primary.** Energy accounting (kWh) comes first. Cost is a function applied to energy data. If the energy model is wrong, no rate schedule precision helps. Get the kWh right, then apply dollars.

6. **Backend owns physics and cost computation; frontend visualizes.** The projection engine, cost accumulator, and Monte Carlo runner are Python. The client receives results (JSON) and renders them interactively. Testable, portable, framework-independent.

7. **Observer pattern for cost.** The cost layer never modifies energy flows. It reads energy data and computes cost. This separation means the projection engine is reusable for non-financial analysis (peak shaving, self-consumption optimization, grid independence).

8. **Config-driven scenarios.** Expansion scenarios are YAML diffs against a baseline. The scenario builder is a pure function: `baseline + virtual_devices → projected_model`. No code changes for new equipment types.

9. **Compressed simulation for batch analysis.** The 288-point representation (12 months × 24 hours) enables Monte Carlo: 1000 trials are feasible without a compute cluster. Same physics, same baseline, different parameter samples.

10. **Incremental dashboard migration.** HTMX handles forms, discovery, and simple views. Svelte handles interactive visualization. They coexist on the same page, sharing data through JSON endpoints and `data-` attributes.

---

## What This Document Does Not Cover

- **Specific rate tariff research** — which utilities and tariffs to support first. Product decision informed by user geography.
- **SPAN warehouse API** — if/when SPAN exposes per-circuit historical data via API, it enriches historical depth. The architecture accommodates it without structural changes.
- **Mobile/responsive design** — orthogonal to the modeling architecture.
- **Multi-panel support** — modeling across multiple SPAN panels. The config model supports it; the dashboard UX is not yet designed for it.
- **Direct manufacturer integrations** — the platform consumes HA entities, not manufacturer APIs.
- **Grid services / VPP modeling** — demand response, virtual power plant participation. Natural extensions of the cost model but require utility program data not yet scoped.
- **Financing / loan modeling** — equipment financing, PACE loans, PPA structures. Layer on top of NPV/payback but involve financial instrument modeling outside the energy domain.
