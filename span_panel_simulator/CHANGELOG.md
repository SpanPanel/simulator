# Changelog

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
