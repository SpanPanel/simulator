# Recorder Replay: Design & Implementation Plan

## Problem

The clone profile enrichment pipeline compresses detailed HA recorder
time-series into summary statistics (`typical_power`, `hour_factors`,
`duty_cycle`, `monthly_factors`), then the engine reconstructs synthetic
power from those summaries.  This is inherently lossy — a 300 W always-on
load gets modeled with a 72 % duty cycle that periodically drops to 0 W, a
spa's 5 AM heating surge lands at the wrong hour, and bursty loads produce
unrealistic noise.

The HA recorder already has the actual per-circuit power history at hourly
granularity (indefinite retention) and 5-minute granularity (10-day
retention).  Rather than approximating from statistics, the simulator
should replay the recorded values directly.

## Design Principles

1. **Recorder replay is the default when HA is available.**  Every circuit
   replays its recorded power unless the user explicitly overrides it.

2. **Per-circuit override.**  Editing a circuit's profile in the dashboard
   switches that circuit from replay to synthetic.  All other circuits
   continue replaying.  This enables "what-if" scenarios: change the EV
   charging schedule, keep everything else as recorded reality.

3. **Panel-level values are always derived.**  Grid power, lug current,
   total consumption/production are computed from individual circuit
   values (whether recorded or synthetic), never read from panel-level
   recorder entities.  This ensures overrides propagate correctly.

4. **Real BESS is opaque.**  A real battery's recorded power is replayed
   as-is — we cannot reverse-engineer its decision logic (manual
   interventions, mode changes, app overrides).  Virtual batteries
   operate on the residual grid power after all recorded circuits.

5. **Looping playback.**  When the simulation clock reaches the end of
   recorded history, it wraps to the beginning — like replaying a song.
   The time controls (date slider, time-of-day, speed) scrub through the
   recorded window.

6. **Standalone mode unaffected.**  Without HA, everything works as today:
   eBus scrape, YAML templates, synthetic engine.  Recorder replay is an
   HA-gated capability.

## Data Available

HA recorder provides two tables per entity:

| Table | Granularity | Retention | Fields |
|---|---|---|---|
| `statistics` | Hourly | Indefinite | `start_ts`, `mean`, `min`, `max`, `sum` |
| `statistics_short_term` | 5-minute | ~10 days | Same fields |

For a 32-circuit panel over the available history (~83 days hourly, 10
days 5-minute), the total data is ~6 MB — trivially small.

## Per-Circuit Source Selection

Each circuit tick resolves its power source in priority order:

```
1. User override?  → synthetic (template-driven, existing engine)
2. Recorder data available for this timestamp?  → replay recorded mean
3. Neither?  → synthetic fallback (template-driven)
```

A circuit is considered "user overridden" when its template has been
edited in the dashboard after the initial clone/profile enrichment.
This is tracked by a `user_modified` flag on the template.

## Virtual Device Behaviour

### Virtual BESS — "follow-real" charge mode

When a real BESS exists in the recorded data, a virtual battery shadows
its direction:

- **Real BESS charging** → virtual battery also charges (conditions are
  favourable — excess solar, off-peak rates, etc.)
- **Real BESS discharging AND grid still importing** → virtual battery
  discharges to offset remaining grid import
- **Real BESS discharging but grid at zero or exporting** → virtual
  battery stays idle (nothing to offset)

The virtual battery has its own SOC, capacity, and charge/discharge
rate limits.  It borrows the real battery's timing intelligence without
needing to know its configuration.

When no real BESS exists, the virtual battery falls back to its
configured charge mode (solar-excess, custom schedule, etc.) operating
against the recorded load and production data.

### Virtual PV

Additional PV capacity operates the same way as virtual BESS — it layers
on top of recorded reality:

- The existing real PV's recorded production is replayed as-is
- The virtual PV array uses the geographic solar model to produce power
  at the panel's coordinates
- Virtual PV production offsets grid import

**Known gap:** adding virtual PV would have changed the real BESS's
decisions in practice (more excess solar → more charging).  Since the
real BESS is opaque, its recorded behaviour remains constant.  The
virtual PV only reduces grid import directly — it does not cause the
real BESS to charge more.  This is an acknowledged limitation;
documenting it is more honest than modelling it incorrectly.

If the user wants to model "more PV + battery response," they can
override the BESS circuit (switching it to synthetic with a configured
charge mode) so the simulated battery reacts to the additional PV.
This trades replay fidelity for projection flexibility — an explicit
user choice.

## Looping Playback

When the simulation clock reaches the end of the recorded window:

- The clock wraps to the start of the recorded window
- A seamless transition — no pause, no reset of virtual device state
  (SOC carries over)
- The date slider reflects the current position within the recorded
  window
- At high speed (360x), the full recorded window plays through
  repeatedly

The time controls map to the recorded window:
- **Date slider** → position within recorded history
- **Time-of-day slider** → hour within the selected day
- **Speed** → playback rate (1x–360x)
- **Forward/backward** → direction through recorded timestamps

## Entity ID Mapping

The clone config must store the mapping from circuit template to HA
entity ID so the engine can query the correct recorder data.  This
mapping comes from the manifest during clone:

```yaml
circuit_templates:
  clone_23:
    energy_profile: { ... }
    recorder_entity: "sensor.span_panel_internet_living_room_power"
    # ...
```

When `recorder_entity` is present and HA is connected, the engine
queries that entity's statistics for the current simulation timestamp.
When absent or HA unavailable, the engine falls back to synthetic.

## Architecture

### New Components

1. **`RecorderDataSource`** (`recorder.py` or extend `history.py`)
   - Queries `statistics` and `statistics_short_term` tables via the
     existing `HistoryProvider` interface
   - Caches a window of data in memory to avoid per-tick queries
   - Provides: `get_power(entity_id, timestamp) -> float | None`
   - Handles granularity selection: 5-minute when within short-term
     window, hourly otherwise
   - Interpolates between data points for smooth playback

2. **Per-circuit source selection** (in `engine.py` / `circuit.py`)
   - `SimulatedCircuit.tick()` checks `user_modified` flag and
     `recorder_entity` availability
   - Delegates to recorder or synthetic engine accordingly

3. **Follow-real BESS charge mode** (in `engine.py`)
   - New charge mode for virtual batteries operating alongside a
     recorded real BESS
   - Reads real BESS direction from recorded data, mirrors with own
     constraints

4. **Entity ID persistence** (in `clone.py` / `profile_applicator.py`)
   - Store `recorder_entity` in circuit template during clone
   - Preserve through profile enrichment updates

5. **Playback loop logic** (in `engine.py`)
   - Detect end of recorded window, wrap to start
   - Expose recorded window bounds to dashboard for time slider range

### Modified Components

- **`engine.py`**: per-circuit source selection, follow-real BESS mode,
  playback looping
- **`circuit.py`**: `user_modified` tracking, recorder power injection
- **`clone.py`**: store `recorder_entity` in templates
- **`dashboard/routes.py`**: expose recorded time range for slider
  bounds, override tracking
- **`dashboard/templates`**: time controls adapted to recorded window

### Unchanged Components

- **`scraper.py`**: eBus scrape is independent
- **`publisher.py`**: publishes snapshots regardless of power source
- **`solar.py`**: used for virtual PV, unchanged
- **`models.py`**: snapshot dataclasses unchanged
- **Profile builder/applicator**: still populates templates as fallback

## Implementation Order

### Phase A: Data Plumbing

1. **Entity ID mapping in clone** — store `recorder_entity` per circuit
   template during clone (from manifest entity IDs)
2. **RecorderDataSource** — query statistics by entity ID + timestamp,
   cache a window, handle granularity selection
3. **Basic per-circuit replay** — engine tick reads from recorder when
   available, falls back to synthetic

### Phase B: Playback Controls

4. **Time slider bound to recorded window** — dashboard exposes
   start/end of recorded data, slider scrubs within
5. **Looping playback** — wrap at end of recorded window
6. **Granularity switching** — use 5-minute data when within short-term
   window, hourly otherwise

### Phase C: Overrides & Virtual Devices

7. **User override tracking** — `user_modified` flag set on template
   edit, circuit switches to synthetic
8. **Virtual BESS follow-real mode** — shadow real BESS direction
9. **Virtual PV** — layer additional solar on top of recorded production

### Phase D: Dashboard Integration

10. **Replay mode indicator** — show which circuits are replaying vs
    synthetic vs overridden
11. **Override toggle** — let user explicitly switch a circuit between
    replay and synthetic
12. **Grid delta display** — show the impact of overrides on grid import

## Relationship to Platform Roadmap

This plan implements the core of Phase 2 (Energy Baseline) and Phase 3
(Virtual Device Projection) from the platform roadmap, applied to the
existing simulator dashboard rather than a separate modeling surface.

| Roadmap Phase | This Plan |
|---|---|
| Phase 1: Data Foundation | Entity ID mapping, RecorderDataSource |
| Phase 2: Energy Baseline | Per-circuit replay, playback controls |
| Phase 3: Virtual Projection | Override tracking, virtual BESS/PV |
| Phase 4: Cost & Financial | Future — apply rate structures to replay data |
| Phase 5: Monte Carlo | Future — parameterised replay with virtual devices |

The key architectural difference: the roadmap envisions a separate
modeling dashboard, while this plan integrates replay into the existing
simulator dashboard.  The engine, data source, and override model are
the same either way — the rendering surface is the only difference.
