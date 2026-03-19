# Modeling View: Before/After Energy Comparison

Spec for the Modeling tab dual-chart view with horizon selection, range zoom, and per-circuit overlays. Replaces the existing energy projection chart.

---

## Problem

Users need to evaluate the economic impact of adding BESS or changing PV on their panel. The simulator already computes `site_power` (panel lug demand) and `grid_power` (utility meter reading) separately, but there is no way to visualize the comparison or quantify the energy savings over historical data.

## Design Principles

- **Zippy**: Full dataset cached client-side after one fetch. All zoom/pan is instant JS array slicing — no round-trips.
- **Static analysis**: Clock does not tick in modeling mode. This is historical replay, not live simulation.
- **Future-proof**: Endpoint shape supports scenario overrides and optimization without redesign.

---

## Backend

### Endpoint

```
GET /modeling-data?horizon={1mo|3mo|6mo|1yr}
```

Returns the full time-series for the requested horizon from the recorder data source.

### Response Schema

```json
{
  "horizon_start": 1726617600,
  "horizon_end": 1742342400,
  "resolution_s": 3600,
  "time_zone": "America/New_York",
  "timestamps": [1726617600, 1726621200, ...],
  "site_power": [1200, 1150, ...],
  "grid_power": [800, 720, ...],
  "pv_power": [400, 430, ...],
  "battery_power": [0, 0, ...],
  "circuits": {
    "evse": { "name": "EV Charger", "power": [0, 0, 7200, ...] },
    "heat_pump": { "name": "Heat Pump", "power": [1500, 1500, ...] }
  }
}
```

- **Resolution**: Hourly for all horizons. Max 8760 points (1 year).
- **`time_zone`**: Panel IANA timezone for correct X-axis label rendering.
- **`site_power`**: `loads - PV` at each timestamp. Computed from per-circuit replay. This is the "Before" — what the user was paying pre-BESS.
- **`grid_power`**: `site_power - battery_power`. This is the "After" — projected cost with BESS.
- **`pv_power`**: Total PV production (absolute value).
- **`battery_power`**: Net battery contribution. Sign convention matches snapshot: positive = charging (grid import increases), negative = discharging (grid import decreases). Zero when no BESS configured.
- **`circuits`**: Per-circuit power arrays keyed by circuit ID. Included for all circuits so users can overlay any trace.
- **No-BESS case**: `grid_power === site_power`, `battery_power` all zeros. Before and After charts are identical, correctly showing zero savings.

### Error Responses

| Condition | Status | Body |
|-----------|--------|------|
| No running simulation | 503 | `{"error": "No running simulation"}` |
| No recorder data loaded | 400 | `{"error": "No recorder data available"}` |
| Horizon exceeds available data | 200 | Response clamped to available range; `horizon_start`/`horizon_end` reflect actual bounds |

### Recorder Lookback

The `RecorderDataSource` currently loads hourly data with `_HOURLY_LOOKBACK = timedelta(days=90)`. For modeling horizons beyond 90 days, the lookback must be extended. Approach: make `_HOURLY_LOOKBACK` configurable. When the modeling endpoint is hit with a horizon that exceeds the loaded data range, the recorder re-queries with a sufficient lookback and caches the expanded dataset. The HA recorder retains hourly statistics indefinitely, so the data is available — it just wasn't requested.

### Engine Method

New method on `DynamicSimulationEngine`:

```python
async def compute_modeling_data(self, horizon_hours: int) -> dict[str, Any]
```

This performs a **read-only simulation pass** — it does not mutate any runtime circuit state, energy counters, or BSEE state. It creates temporary clones of all stateful objects to iterate across the horizon:

1. **Temporary state**: Clone `RealisticBehaviorEngine` (it mutates `_last_battery_direction`, `_circuit_cycle_states`, `_solar_excess_w` during `get_circuit_power()` calls). Clone `BatteryStorageEquipment` with fresh SOE state if BESS is configured. These clones are discarded after the pass.

2. **Per-timestamp loop** (hourly steps across the recorder window, clamped to `horizon_hours` from the end):
   - **Pass 1**: For each non-solar-excess circuit, call the cloned behavior engine's `get_circuit_power()`. Classify: consumer → `total_consumption`, producer → `total_production`, bidirectional → `raw_battery_power` (unsigned).
   - **Pass 2** (solar-excess only): Compute `excess = pv_total - load_total`, set on cloned engine, then compute solar-excess battery circuits.
   - Compute `site_power = total_consumption - total_production`.

3. **Battery/BSEE pass**: Step the temporary BSEE through the horizon hourly, tracking SOE depletion and saturation across hours/days. This ensures the "After" curve reflects realistic battery limits (e.g., battery empties overnight, can't discharge further until recharged). The BSEE clamps `raw_battery_power` to SOE bounds and determines direction (charging/discharging/idle).

4. **Grid power with signed battery convention**: The modeling pass uses signed battery power throughout: positive = charging (increases grid import), negative = discharging (decreases grid import). This gives the single formula:

   ```
   grid_power = site_power - signed_battery_power
   ```

   When discharging (`signed_battery_power < 0`): `site - (-discharge) = site + discharge` → grid goes down (battery covers some load). When charging (`signed_battery_power > 0`): `site - charge` → grid goes up (grid supplies loads AND battery). This is the physically correct relationship.

   **Note**: The engine's `get_snapshot()` currently uses unsigned `battery_circuit_power` in its formula, which produces incorrect `grid_power` during charging. The modeling pass uses the correct signed convention. A follow-up fix to `get_snapshot()` should align it with the modeling pass formula.

5. **Per-circuit arrays**: Each circuit's power at each timestamp is stored for the circuit overlay feature.

### Dashboard Access Path

New async callback on `DashboardContext`:

```python
get_modeling_data: Callable[[int], Awaitable[dict[str, Any] | None]] = ...
```

The method and callback are `async` because the recorder may need to expand its lookback (async re-query via `history.async_get_statistics`) before computing. `SimulatorApp` wires this to `engine.compute_modeling_data()`. The route handler `await`s `context.get_modeling_data(horizon_hours)` — no direct engine access, preserving the clean boundary.

### Future Extension

The endpoint shape supports scenario overrides without redesign:

```
POST /modeling-data
{
  "horizon": "6mo",
  "overrides": {
    "evse": { "discharge_hours": [16,17,18,19] },
    "battery": { "charge_mode": "solar-excess" }
  }
}
```

And optimization:

```
POST /modeling-optimize → recommended overrides + resulting dataset
```

Neither is built now. The point is nothing in the current design precludes them.

---

## Frontend

### Layout (top to bottom)

1. **Horizon dropdown** — Last Month / Last 3 Months / Last 6 Months / Last Year. Changing triggers a new fetch.

2. **Range slider** — Double-ended noUiSlider. Handles map to epoch offsets within the horizon. Dragging either handle or the bar between them re-slices the cached dataset and updates both charts instantly. Date range labels at the ends.

3. **Before chart** — Chart.js line chart. Traces:
   - Grid/Site power (red, `#ef4444`) — in the Before view this is `site_power`
   - Solar (amber, `#f59e0b`) — `pv_power`
   - Optional per-circuit overlays (unique colors)
   - Label line: `Before (Site Power — no BESS)  4,283 kWh`

4. **After chart** — Chart.js line chart, stacked below Before. Traces:
   - Grid power (red, `#ef4444`) — `grid_power`
   - Solar (amber, `#f59e0b`) — `pv_power` (same as Before)
   - Battery (green, `#10b981`) — `battery_power`
   - Optional per-circuit overlays (same selection as Before, synced)
   - Label line: `After (Grid Power — with BESS)  2,917 kWh  Savings: 1,366 kWh (31.9%)`

5. **Shared legend** below the After chart.

### Chart Synchronization

- Both charts share the same Y-axis max (computed from the greater of the two datasets across the visible range).
- Both charts share the same X-axis range (driven by the range slider).
- X-axis ticks are adaptive based on visible range:
  - `> 30 days`: month labels (Jan, Feb, Mar...)
  - `7–30 days`: date labels every few days
  - `1–7 days`: daily labels with midnight gridlines
  - `< 1 day`: hour labels (00:00, 03:00...) with vertical gridlines
- Both charts update together via `chart.update('none')` (no animation).

### Energy Summaries

- kWh values are computed client-side by summing the power array across the visible range: `sum(power[i] * resolution_s / 3_600_000)` for each visible index (watts * seconds → kWh).
- Horizon totals use the full array. Visible totals use the slider-selected subset.
- Savings = Before kWh - After kWh, displayed as absolute and percentage.
- Dollar values are a future addition — space is reserved inline after the kWh values.

### Circuit Overlay Checkboxes

- Each circuit row in the entity list gets a checkbox, visible only when the Modeling tab is active.
- Toggling a checkbox adds/removes that circuit's power trace to both charts simultaneously.
- Colors assigned from a fixed palette distinct from red/amber/green: `['#3b82f6', '#8b5cf6', '#ec4899', '#14b8a6', '#f97316', '#6366f1', '#84cc16', '#e879f9']`.
- Checkbox state is ephemeral (not persisted).
- Use case: spotting large loads (EVSE, heat pump, pool) whose schedules are candidates for optimization.

### Data Flow

1. User clicks "Modeling" tab → runtime polling stops, modeling view renders with a loading spinner.
2. Initial fetch: `GET /modeling-data?horizon=1mo` → spinner replaced with charts, response cached in JS variable.
3. User changes horizon dropdown → new fetch, replace cached data, reset slider to full range, re-render both charts.
4. User drags range slider → JS slices cached `timestamps`/power arrays by index range → update both charts.
5. User toggles circuit checkbox → add/remove dataset from both charts → update.
6. User clicks "Runtime" tab → modeling charts destroyed, runtime polling resumes.

### Range Slider Behavior

- noUiSlider with `connect: true` (bar between handles highlighted).
- Handles snap to hourly intervals (matching data resolution).
- Minimum visible range: 24 hours (24 data points) to avoid rendering single-dot charts.
- Dragging the bar between handles pans the window without changing zoom level.
- Pip label density adapts to horizon: 1yr → 12 monthly pips, 6mo → 6, 3mo → ~6 bi-weekly, 1mo → ~4 weekly.

---

## Integration

### Files Modified

| File | Changes |
|------|---------|
| `dashboard/__init__.py` | Add `get_modeling_data` callback to `DashboardContext` |
| `dashboard/routes.py` | Add `GET /modeling-data` handler |
| `engine.py` | Add `compute_modeling_data()` method |
| `recorder.py` | Make `_HOURLY_LOOKBACK` configurable for extended horizons |
| `app.py` | Wire `get_modeling_data` callback from engine to dashboard context |
| `dashboard/templates/partials/runtime_controls.html` | Replace modeling section with dual-chart layout, range slider, adaptive ticks, circuit checkbox wiring. Remove existing energy projection JS (lines 668-775). |
| `dashboard/static/dashboard.css` | Range slider styling, circuit checkbox appearance, modeling-specific layout rules |

### No New Dependencies

Chart.js and noUiSlider are already loaded in `base.html`. No additional libraries.

### Existing Modeling View

The current modeling view (energy projection bar chart with week/month/year period buttons) is replaced entirely. The energy projection endpoint (`GET /energy-projection`) remains available but is no longer used by the dashboard.

---

## Out of Scope

- Dollar cost overlay (future — rate schedule integration)
- Scenario overrides / "what-if" tweaking (future — POST endpoint)
- Engine optimization (future — `POST /modeling-optimize`)
- Mouse wheel zoom (double-ended slider is sufficient)
- Clock ticking in modeling mode
