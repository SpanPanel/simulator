# Changelog

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
