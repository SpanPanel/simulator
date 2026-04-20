# Changelog

## 1.0.11 — 2026-04-19

### Fixes

- Homie schema served over `GET /api/v2/homie/schema` now declares the correct panel size per panel. Previously the `space` property's `format` field was hardcoded to `"1:32:1"` regardless of the panel's configured `total_tabs`, causing span-panel-api (and downstream span-card) to render 40-tab panels as 32-tab
- `typesSchemaHash` is now content-derived (same algorithm span-panel-api uses) and changes with panel size, replacing a hardcoded value that did not reflect schema contents
- `DynamicSimulationEngine.total_tabs` raises instead of silently returning 32 when accessed before `initialize_async()`
- Panels configured with `total_tabs` outside the supported model set (16, 24, 32, 40, 48) now fail loudly at panel-add time instead of being silently labeled `MAIN_32`; validation runs before panel registration so unsupported configs no longer leave an orphan tick task
- `PanelInstance.total_tabs` property added, mirroring the existing `serial_number` lifecycle guard
- Config-directory reload no longer aborts when a single panel fails to start — each config is processed independently, errors are recorded per filename, and successful panels continue to start, stop, or reload as expected
- Failed configs' file hashes are withheld so the next reload automatically retries after the user fixes the config
- Dashboard panel list surfaces per-panel start errors as a badge next to the failing filename, with the full error message available on hover

## 1.0.10 — 2026-04-02

### Features

- Dedicated BESS card in dashboard replaces entity-based battery editing — add, configure, and remove battery storage from a single panel-level view
- Consolidated BESS settings and schedule into a single edit view with immediate schedule display on add
- Rate-aware TOU dispatch: BESS charge/discharge schedule derived automatically from the active URDB electricity rate plan
- Default post-solar discharge schedule applied when switching to TOU mode with an empty schedule
- Modeling Before pass now built from original clone-time config snapshot for accurate baseline comparison
- 32-tab EV charger template renamed to SPAN Drive for consistency with 40-tab panel naming

### Fixes

- Savings display shows absolute value without sign prefix; negative savings labeled "more expensive"
- Savings sign convention corrected in modeling cost comparison
- Empty BESS slots skipped during clone instead of creating phantom battery entries
- Modeling chart refreshes automatically when BESS card settings change
- Duplicate HTML IDs eliminated across dashboard views

## 1.0.9 — 2026-03-30

### Features

- Rate plan selection UI in modeling view with URDB utility and plan discovery
- Cost comparison columns in modeling summary cards (Before vs After estimated cost)
- Opower billing integration: actual billed cost from utility account used for Before baseline when available
- Opower account discovery and selection with utility-filtered URDB rate list
- URDB rate plans filtered to latest version per plan name

### Fixes

- Cost engine units corrected (power arrays are Watts, not kW)

## 1.0.8 — 2026-03-30

### Features

- Time-of-Use rate engine: hourly power-to-cost calculation using URDB schedule matrix lookup
- Per-chart energy summary tables in modeling view

### Fixes

- Modeling energy difference uses imported energy instead of net
- Only BESS circuit excluded from load power calculation (was incorrectly excluding all bidirectional circuits)
- Sanitize address input in TLS cert generation to prevent syntax errors

## 1.0.7 — 2026-03-28

### Features

- BESS charge modes: Self-Consumption (default), Time-of-Use, and Backup Only replace the old solar-gen/solar-excess/custom modes
- Self-Consumption mode: battery automatically discharges to offset grid import and charges from surplus solar — no schedule needed
- Backup Only mode: battery holds at full SOC and only discharges during grid outages
- BESS operates at full inverter rate like a real system — GFE throttle limits discharge to actual load deficit
- Hybrid inverter support: PV stays online during islanding when co-located with BESS
- PV curtailment during islanding: hybrid inverter reduces output to match load + BESS charge capacity when grid is disconnected, matching real MPPT setpoint behavior

### Improvements

- Battery editor shows only installation-relevant fields (nameplate, reserve, inverter rates, charge mode)
- Modeling Before/After labels always show imported/exported breakdown
- Schedule controls hidden when charge mode doesn't use them
- Clone dialog pre-fills a safe filename with auto-suffix when the target already exists; overwrite requires explicit confirmation via modal

### Fixes

- Page-level busy cursor during slow operations (start/stop/restart panel, clone, modeling setup) prevents unintended clicks
- Clone from panel prompts before overwriting an existing config file
- PV curtailment during islanding now reflected in circuit snapshots so dashboard power readings stay consistent
- BESS discharge no longer pushes grid power negative (GFE constraint)
- Modeling Before graph no longer shows user-added circuits that didn't exist in the baseline
- Grid-offline mode respects charge mode instead of forcing unconditional discharge

### Refactor

- Component-based energy system replaces scattered inline energy balance calculations

## 1.0.6 — 2026-03-26

### Features

- Synthetic history: cloned panels generate a companion SQLite database so modeling works without Home Assistant
- Standalone CLI for synthetic history generation

### Fixes

- Row buttons in dashboard now auto-switch the active config so the entity list stays in sync
- Delete companion history database when a cloned config is removed
- Battery schedule (BSEE) always enforces discharge/idle hours in the After modeling pass
- SYN→REC toggle now works for template-cloned configs
- After modeling pass skips BSEE when battery is unchanged; fix SOE sign handling
- Before/After chart battery sign conventions are now consistent
- Before chart only includes BESS when battery was present in the original recorder baseline
- Fall through to SQLite when HA returns no recorder data
- Derive recorder entity mappings for configs without HA; generate history on simple clone
- Synthetic history now produces identical results across runs for the same config (deterministic seed)

## 1.0.5 — 2026-03-23

### Features

- Dashboard Getting started card with Clone, Model, Purge; template vs editable workflow
- Purge feedback: entity count, none found, no HA, or error in flash

### Improvements

- Only cloned configs appear in HA; default templates excluded from discovery
- README Workflow and Panel Management sections aligned with dashboard
- Clarify purge: removes recorder history written by simulator when panel added to HA

### Fixes

- Fix Hone → Home Assistant typo

## 1.0.4 — 2026-03-22

### Features

- Purge button to clear HA recorder history for stopped profiles
- Recorder data automatically purged when deleting a simulator profile
- Per-entity restore to original recorder state
- Editable PV inverter type, efficiency, battery charge/discharge parameters
- Loading indicators and disabled controls during all dashboard operations

### Fixes

- Fix modeling baseline comparison and per-panel config resolution
- Resume explicitly-started panels on reboot, including defaults
- Clear last config when user explicitly stops a panel
- Require passphrase for clone-from-panel
- Refresh panels list immediately after start/stop/restart/delete
- Fix unsaved-changes tracking across entity and PV editors
- Fix PV chart overflow and clipping in entity editor
- Keep recorder association when editing PV/battery entities
- Remove duplicate Open-Meteo source label in weather status

## 1.0.3 — 2026-03-21

### Features

- Clickable panel rows with hover styling in panel selector
- Unsaved-changes guard when switching panels or loading defaults

## 1.0.2 — 2026-03-21

### Features

- Per-panel HTTP servers with automatic port allocation
- Configurable base HTTP port for add-on and CLI
- Per-panel HTTP port shown in dashboard panel list
- Automatic Supervisor Discovery registration for add-on panels

### Fixes

- Fix panel discovery when running as HA add-on
- Restore battery circuit entries removed when tabs were stripped
- Auto-rename duplicate serials when cloned configs run concurrently
- Fix 16-tab default config missing unmapped tabs

## 1.0.1 — 2026-03-20

### Translations

- Add add-on config translations for German, French, Spanish, Dutch, and Brazilian Portuguese

## 1.0.0 — 2026-03-20

Initial public release.

### Simulation Engine

- Multi-panel support with on-demand reload and per-panel network discovery
- Schema-driven architecture with breaker rating support and circuit templates
- Realistic appliance templates with time-of-day cycling and seasonal HVAC modulation
- Timezone-aware simulation from panel latitude/longitude
- Configurable tick interval and time acceleration

### Solar, Battery & DER

- Solar production model with grid interaction and load shedding
- Battery SOE tracking with charge modes, backup reserve, and depletion bounds
- Battery entities decoupled from breaker tabs (panel-lug connection model)
- EVSE scheduling with Level 2 charger profiles

### Panel Cloning

- Clone a live SPAN panel via eBus scrape-translate-write pipeline
- mDNS panel discovery for standalone and HA environments
- HA entity discovery and usage profile import

### Recorder Replay & Modeling

- Replay Home Assistant recorder history through the simulator
- Before/after modeling view with dual charts and range slider
- Configurable lookback horizon for modeling comparisons
- Per-circuit overlays with toggleable PV/Battery legend
- Import/export kWh breakdown with net savings display

### Dashboard

- Full configuration dashboard with dark mode
- Engine lifecycle controls (start, stop, reload)
- Entity and circuit editors with preset system (YAML-backed registry)
- Day-of-week selectors, date slider, and time controls
- Default template protection (read-only, clone-to-edit workflow)
- Idle startup mode when no config is selected

### Home Assistant Add-on

- Ingress-based dashboard access within HA UI
- Automatic config seeding from bundled templates
- Supervisor token integration for recorder access
- Host networking with mDNS advertisement
