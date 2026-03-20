# Modeling View Implementation Plan

**Status: COMPLETE** — All 8 tasks implemented and committed. Additional polish applied post-plan.

**Goal:** Build a Before/After energy comparison view with dual charts, range zoom, and per-circuit overlays for evaluating BESS economic impact on historical data.

**Architecture:** Backend computes a read-only simulation pass over recorder data, returning hourly time-series for site power, grid power, PV, battery, and per-circuit power. Frontend caches the full dataset and renders dual Chart.js charts with a noUiSlider range slider for instant client-side zoom/pan. Modeling is a full-view replacement entered from a "Model" button on each panel row.

**Tech Stack:** Python/aiohttp (backend), Chart.js + noUiSlider + vanilla JS (frontend), pytest + pytest-asyncio (tests).

**Spec:** `span_panel_simulator/docs/2026-03-18-modeling-view-design.md`

---

## Commits

| Commit | Description |
|--------|-------------|
| `177b6a5` | feat(recorder): configurable lookback for modeling horizons |
| `88f4265` | fix(recorder): use 'is not None' guard for lookback_days parameter |
| `b8a6419` | feat(engine): add compute_modeling_data for Before/After analysis |
| `23c1c31` | feat(dashboard): add GET /modeling-data endpoint with async callback |
| `cd101f2` | feat(dashboard): add modeling view with dual charts, range slider, and circuit overlays |
| `2e2c1c2` | fix(engine): manual clone of behavior engine to avoid deepcopy of socket-holding recorder |
| `fdca6cc` | feat(modeling): auto-refresh charts on entity/schedule save via engine reload |
| `a5a2aa9` | fix(modeling): only auto-refresh on saves (not GETs), preserve slider zoom position |
| `076b2b2` | fix(modeling): preserve slider zoom across save-triggered refreshes |
| `c7a25ba` | fix(modeling): compute kWh from grid import only, not abs of import+export |
| `b405b59` | feat(modeling): separate import/export kWh — display both with net savings |
| `f6b3f42` | feat(modeling): show net kWh as primary, import/export breakdown in parens |

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `src/span_panel_simulator/recorder.py` | Modified | Store HistoryProvider ref for lookback expansion; added `ensure_lookback()` |
| `src/span_panel_simulator/engine.py` | Modified | Added `compute_modeling_data()` — read-only simulation pass |
| `src/span_panel_simulator/dashboard/__init__.py` | Modified | Added `get_modeling_data` async callback to `DashboardContext` |
| `src/span_panel_simulator/dashboard/routes.py` | Modified | Added `GET /modeling-data` route handler |
| `src/span_panel_simulator/app.py` | Modified | Wired `_get_modeling_data` callback from engine to dashboard |
| `src/span_panel_simulator/dashboard/templates/dashboard.html` | Modified | Added `#modeling-section` container |
| `src/span_panel_simulator/dashboard/templates/partials/modeling_view.html` | Created | Modeling view template (header, horizon, slider, dual charts, legend) |
| `src/span_panel_simulator/dashboard/templates/partials/runtime_controls.html` | Modified | Removed old energy projection section; added view toggle handlers |
| `src/span_panel_simulator/dashboard/templates/partials/panels_list_rows.html` | Modified | Added "Model" button to each panel row |
| `src/span_panel_simulator/dashboard/templates/partials/entity_row.html` | Modified | Added circuit overlay checkbox |
| `src/span_panel_simulator/dashboard/static/dashboard.css` | Modified | Modeling mode layout, chart styling, overlay checkbox appearance |
| `tests/test_modeling.py` | Created | Tests for recorder lookback, engine compute, route handler |

---

### Task 1: Recorder Lookback Expansion — DONE

**Commits:** `177b6a5`, `88f4265`

- [x] Write test for ensure_lookback
- [x] Run test to verify it fails
- [x] Implement recorder lookback expansion
- [x] Run test to verify it passes
- [x] Run type checker
- [x] Commit

---

### Task 2: Engine `compute_modeling_data()` — DONE

**Commit:** `b8a6419`

- [x] Write test for compute_modeling_data
- [x] Run tests to verify they fail
- [x] Implement compute_modeling_data
- [x] Run tests to verify they pass
- [x] Run type checker
- [x] Commit

**Deviation:** Used manual clone of behavior engine instead of `copy.deepcopy` because the recorder holds socket references that can't be deep-copied (`2e2c1c2`).

---

### Task 3: Dashboard Plumbing (Context + Route + App Wiring) — DONE

**Commit:** `23c1c31`

- [x] Write test for the modeling route
- [x] Run tests to verify they fail
- [x] Add callback to DashboardContext
- [x] Add route handler
- [x] Wire callback in app.py
- [x] Run tests to verify they pass
- [x] Run type checker on modified files
- [x] Commit

---

### Task 4: Remove Old Modeling Section + Add Modeling View Template — DONE

**Commit:** `cd101f2` (combined with Tasks 5-7 into one frontend commit)

- [x] Remove old modeling HTML from runtime_controls.html
- [x] Remove old modeling JS from runtime_controls.html
- [x] Create modeling_view.html template
- [x] Update dashboard.html to include modeling view
- [x] Verify the app loads without errors
- [x] Commit

---

### Task 5: Panel List Model Button + View Mode Transition — DONE

**Commit:** `cd101f2`

- [x] Add Model button to panel rows
- [x] Expose chartTextColor/chartGridColor globally
- [x] Update view toggle to handle modeling mode transition
- [x] Verify the dashboard renders and buttons appear
- [x] Commit

---

### Task 6: Circuit Overlay Checkboxes — DONE

**Commit:** `cd101f2`

- [x] Add overlay checkbox to entity rows
- [x] Commit

---

### Task 7: CSS Styling — DONE

**Commit:** `cd101f2`

- [x] Add modeling mode layout rules
- [x] Verify styling renders correctly
- [x] Commit

---

### Task 8: Integration Testing and Polish — DONE

**Commits:** `fdca6cc`, `a5a2aa9`, `076b2b2`, `c7a25ba`, `b405b59`, `f6b3f42`

- [x] Run the full test suite
- [x] Run type checker on all modified files
- [x] Manual smoke test
- [x] Fix issues found (see post-plan polish below)
- [x] Final commits

---

## Post-Plan Polish

Several issues were discovered and fixed during manual testing after the initial 8 tasks:

1. **Auto-refresh on save** (`fdca6cc`): Modeling charts now auto-refresh when entities or schedules are saved, by reloading the engine and re-fetching modeling data.

2. **Refresh scope** (`a5a2aa9`): Auto-refresh was triggering on GET requests too (e.g. polling); scoped to POST/save operations only.

3. **Slider zoom preservation** (`076b2b2`, `a5a2aa9`): Range slider position was resetting on auto-refresh; now preserves the visible range across data reloads.

4. **kWh computation** (`c7a25ba`): Was using `Math.abs()` on all power values for kWh summation, which double-counted export. Changed to separate grid import from export.

5. **Import/export breakdown** (`b405b59`, `f6b3f42`): Added separate import and export kWh display with net savings shown as primary metric: `"1,234 kWh (1,500 import / 266 export)"`.

## Implementation Notes

### Site Power vs Grid Power

The engine now correctly separates `site_power` and `grid_power` at the snapshot level (`ae5bbd7`), not just in the modeling pass:

- **`site_power`** = net demand at the panel lugs (loads - solar), independent of BESS
- **`grid_power`** = utility meter reading (loads - solar ± battery)

The modeling view's Before chart shows `site_power` (what you'd pay without BESS) and the After chart shows `grid_power` (what you'd pay with BESS). See [SPAN API Client Docs](https://github.com/spanio/SPAN-API-Client-Docs) for the panel's physical topology.

### Grid Power Formula

With SPAN sign convention (positive = charging, negative = discharging):

```
grid_power = site_power + signed_battery_power
```

- Charging (positive): grid must supply site demand PLUS battery charge → grid increases
- Discharging (negative): battery covers some demand → grid decreases

### Behavior Engine Cloning

The original plan called for `copy.deepcopy(self._behavior_engine)`. This failed at runtime because the recorder holds socket/connection references that can't be pickled. The fix (`2e2c1c2`) uses a manual clone that copies only the mutable state fields (`_circuit_cycle_states`, `_last_battery_direction`, `_solar_excess_w`).

### Runtime Polling

When entering modeling mode, runtime polling (3s `pollPower`, 250ms interpolation) continues running but the runtime charts are hidden. Since the runtime section is `display: none`, Chart.js updates are cheap (no rendering). Stopping/restarting intervals adds complexity with negligible benefit.

### Chart Theme Colors

The `chartTextColor()` and `chartGridColor()` functions from `runtime_controls.html` are exposed globally via `window.chartTextColor` and `window.chartGridColor`. The modeling view JS falls back to CSS custom properties if these aren't available.
