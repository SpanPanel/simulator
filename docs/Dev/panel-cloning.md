# Panel Cloning & Usage Modeling

## Context

The simulator supports BESS/SPAN Drive simulation, breaker ratings, and full Homie v5 eBus publishing. Given credentials for a **real** SPAN panel, the simulator connects to its eBus, scrapes every retained topic, and translates the result into a simulator YAML config — a faithful starting point that can then be tuned.

The goal is to provide a basis for downstream modeling of changes to BESS, solar, or EVSE infrastructure for upgrades. The clone captures the panel's topology and sizing; the usage modeling features below build on that foundation to produce realistic energy profiles.

---

## Implemented: WSS Clone Pipeline

### Transport: WSS over TLS

The clone WebSocket runs over TLS (WSS), reusing the simulator's `CertificateBundle`. The WSS endpoint runs on its own dedicated port (`CLONE_WSS_PORT`, default 19443), separate from the dashboard.

### Sequence

```text
Integration / Dashboard        Simulator                        Real Panel
        |                          |                                |
        |-- WSS: clone_panel ----->|                                |
        |   {host, passphrase}     |                                |
        |                          |-- POST /api/v2/auth/register ->|
        |                          |<-- {mqtt_creds, serial} -------|
        |                          |-- GET /api/v2/certificate/ca ->|
        |                          |<-- PEM cert -------------------|
        |<-- WSS: "registering"    |                                |
        |                          |== MQTTS connect ===============|
        |                          |-- SUB ebus/5/{serial}/# ------>|
        |                          |<-- $state, $description -------|
        |                          |<-- retained property msgs -----|
        |<-- WSS: "scraping"       |                                |
        |                          |   (collect until stable)       |
        |                          |== MQTT disconnect =============|
        |<-- WSS: "translating"    |                                |
        |                          |-- parse $description           |
        |                          |-- map properties -> YAML       |
        |                          |-- write configs/{serial}-clone.yaml
        |                          |-- trigger reload               |
        |<-- WSS: "done"           |                                |
        |   {serial, filename}     |                                |
```

### WebSocket Contract

- **Endpoint**: `wss://{simulator_host}:{clone_wss_port}/ws/clone`
- **Request**: `{"type": "clone_panel", "host": "...", "passphrase": "..."}`
- **Status phases**: `registering` → `connecting` → `scraping` → `translating` → `writing` → `done`
- **Result**: `{"type": "result", "status": "ok", "serial": "...", "clone_serial": "...", "filename": "...", "circuits": N, "has_bess": bool, "has_pv": bool, "has_evse": bool}`
- **Error**: `{"type": "result", "status": "error", "phase": "...", "message": "..."}`

### eBus Scrape Strategy

1. `POST http://{host}/api/v2/auth/register` with `{"name": "sim-clone-{uuid4}", "hopPassphrase": passphrase}`
2. Extract `ebusBrokerUsername`, `ebusBrokerPassword`, `ebusBrokerMqttsPort`, `serialNumber`
3. `GET http://{host}/api/v2/certificate/ca` for TLS trust
4. Connect via MQTTS, subscribe to `ebus/5/{serial}/#`, collect all retained messages
5. Stability gate: stop after 5 seconds with no new topics; max 30 seconds total

### eBus-to-YAML Translation

- Panel config: serial (`-clone` suffix), main breaker, panel size (derived from max space)
- Per-circuit templates (`clone_{space}`): energy profile mode, power range, typical power, relay behavior, priority, breaker rating
- Energy profile mode inferred from device node `feed` cross-references (PV → producer, BESS/EVSE → bidirectional, else → consumer)
- BESS: nameplate capacity, default charge/discharge schedule, 20% backup reserve
- PV: nameplate capacity, production profile
- EVSE: night-charging time-of-day profile

### Files

- `scraper.py` — eBus scraper (auth + MQTT collection)
- `clone.py` — Translation layer (eBus → YAML)
- `clone_handler.py` — WebSocket handler (glue)
- `discovery.py` — mDNS advertisement with `cloneWssPort`
- `const.py` — Port constants

---

## Implemented: Credential Persistence & Energy Seeding

### Clone-time energy seeding

The clone pipeline maps scraped energy topics into the clone YAML:

| eBus topic           | YAML target                                     |
| -------------------- | ----------------------------------------------- |
| `{uuid}/imported-energy` | `circuit_templates[].energy_profile.initial_consumed_energy_wh` |
| `{uuid}/exported-energy` | `circuit_templates[].energy_profile.initial_produced_energy_wh` |

The engine reads these at startup to seed the per-circuit `consumed_energy_wh` and `produced_energy_wh` accumulators. When present, these values take precedence over the annual estimate. This makes the clone YAML self-contained — accumulators start at realistic absolute values matching the real panel.

### Credential storage & provenance

The source panel's connection details are stored in the clone YAML:

```yaml
panel_source:
  origin_serial: "nj-2316-005k6"   # real panel's serial (immutable provenance)
  host: "192.168.65.70"
  passphrase: "..."                 # null for door-bypass
  last_synced: "2026-03-15T10:30:00"
```

`origin_serial` is the real panel's serial number — distinct from the clone's `panel_config.serial_number` (which has the `-clone` suffix). The passphrase is the panel's proximity code, not a cloud credential. It lives alongside the config it produced, on the same local machine that already has direct network access to the panel.

The clone is treated as an independent modeling baseline from the moment it's created. There is no automatic startup refresh — the point-in-time snapshot from cloning captures topology and sizing, which is what matters for modeling. Usage patterns are better calibrated via HA historical import (see below) than another point-in-time scrape.

### Dashboard: update from panel

The dashboard shows provenance when a `panel_source` block is present:

- Display: "Cloned from **nj-2316-005k6** at 192.168.65.70 — last synced 2026-03-15 10:30"
- Action: "Update from panel" — re-scrapes and overwrites `typical_power` and energy seeds with current values. Confirmation dialog warns: *"This will overwrite typical_power and energy values with current readings from the real panel. Any manual customizations to those values will be lost."*

Configs without `panel_source` (hand-authored or future detached clones) show no provenance section.

### Files

- `clone.py`: Energy seed mapping in `_translate_circuit`; `panel_source` block written when `host` is provided; `update_config_from_scrape()` for on-demand refresh
- `circuit.py`: Prefers `initial_*_energy_wh` seeds over annual estimate when present
- `config_types.py`: `PanelSource` TypedDict; `initial_consumed_energy_wh` / `initial_produced_energy_wh` in `EnergyProfileExtended`; `panel_source` in `SimulationConfig`
- `validation.py`: `validate_panel_source()` for the `panel_source` block
- `clone_handler.py`: Passes `host`/`passphrase` through to `translate_scraped_panel`
- `dashboard/routes.py`: `GET /panel-source`, `POST /sync-panel-source`
- `dashboard/templates/partials/panel_source.html`: Provenance display with update action
- `dashboard/config_store.py`: `get_panel_source()`, `get_origin_serial()`

---

## Feature: Dashboard Clone UI

### Problem

Cloning currently requires the HA SPAN integration to initiate the WSS handshake. This creates a dependency on a separate codebase and makes the simulator less self-contained. Users should be able to clone directly from the simulator dashboard.

### Design

#### mDNS panel browser

The simulator already has `zeroconf` / `AsyncZeroconf` for advertising. Add a `ServiceBrowser` that discovers `_span._tcp.local.` services on the LAN.

- Real SPAN panels advertise `_span._tcp` with their serial number in the TXT record
- Filter out the simulator's own entries (check for `cloneWssPort` in TXT, or match against known simulator serials)
- Maintain a live dict of discovered panels: `{serial: {host, ip, port, txt_properties}}`
- Panels that disappear from mDNS are removed after a grace period

#### Dashboard route

`GET /admin/discovered-panels` returns the current set of discovered panels as JSON. The dashboard polls this on an interval while the clone dialog is open.

`POST /admin/clone` accepts `{host, passphrase}` and drives the scrape-translate-write pipeline directly — the same `register_with_panel` → `scrape_ebus` → `translate_scraped_panel` → `write_clone_config` → reload sequence that `clone_handler.py` orchestrates, but invoked as a function call rather than over WSS.

#### UI

A "Clone Panel" button in the dashboard navigation opens an HTMX dialog:

```
┌─────────────────────────────────┐
│  Clone from SPAN Panel          │
│                                 │
│  Panel:  [▾ nj-2316-005k6    ] │
│          192.168.65.70          │
│                                 │
│  Passphrase: [________________] │
│                                 │
│  [Cancel]            [Clone]    │
└─────────────────────────────────┘
```

- The dropdown is populated from `/admin/discovered-panels`
- On submit, POST to `/admin/clone`
- Progress feedback via HTMX polling against a task-status endpoint, or SSE stream
- On completion: redirect to the new panel's dashboard, or display a success message with the clone serial and circuit count

#### Relationship to WSS endpoint

The WSS endpoint (`/ws/clone`) remains for the HA integration's use. The dashboard route calls the same underlying functions. No business logic is duplicated — `clone_handler.py` and the dashboard route are both thin orchestrators over `scraper.py` and `clone.py`.

#### Scope

- `discovery.py`: Add `ServiceBrowser` for `_span._tcp.local.`, maintain discovered panel dict
- `dashboard/routes.py`: Add `GET /admin/discovered-panels` and `POST /admin/clone`
- `dashboard/templates/`: New clone dialog partial (HTMX)
- `dashboard/templates/base.html`: "Clone Panel" button in nav

---

## Feature: HA Usage Profile Import

### Problem

A cloned panel has accurate topology but synthetic energy behavior. The simulator's engine generates power values from `typical_power`, time-of-day profiles, and noise — useful for exercising the eBus interface, but not representative of actual consumption patterns. For modeling infrastructure changes (adding battery capacity, upgrading solar, sizing an EVSE), the simulation needs to reflect how the household actually uses power.

### Goal

Import historical usage data from Home Assistant to derive per-circuit energy profiles that drive the simulation engine. The goal is **not** to replay history verbatim — it is to extract the usage *shape* (daily patterns, seasonal variation, per-circuit relative load) and use that to parameterize the simulator's existing engine so that forward-looking modeling scenarios are grounded in real behavior.

### Why HA

HA's recorder stores per-circuit sensor data published by the SPAN integration:

- `sensor.span_{serial}_{circuit_id}_instant_power` — instantaneous watts
- `sensor.span_{serial}_{circuit_id}_consumed_energy` — monotonic Wh counter
- `sensor.span_{serial}_{circuit_id}_produced_energy` — monotonic Wh counter

The recorder's **long-term statistics** (`recorder/statistics_during_period`) retain hourly aggregates (mean, min, max, state change sum) indefinitely. Short-term state history is purged after 10 days, but the hourly buckets persist. A month of data is sufficient; a year gives seasonal resolution.

### Design

#### HA connection

Store HA connection details in the clone YAML:

```yaml
ha_source:
  url: "http://homeassistant.local:8123"
  token: "eyJ..."                        # long-lived access token
  entity_prefix: "sensor.span_panel_"    # matches integration entity naming
```

The token is a long-lived HA access token created by the user via their HA profile. It grants read access to the history API.

#### Statistics retrieval

Call the HA WebSocket API `recorder/statistics_during_period` to pull hourly statistics for all circuit power entities over a configurable window (default: 30 days).

Response shape per entity per hour:

```json
{
  "start": "2026-02-15T00:00:00Z",
  "mean": 145.3,
  "min": 0.0,
  "max": 1200.0,
  "sum": 580.0,
  "state": 145.3
}
```

#### Profile derivation

From the hourly statistics, derive per-circuit profiles:

**Time-of-day factors** — For each circuit, compute the average power per hour-of-day across the retrieval window. Normalize to produce 24 `hour_factors` values (0.0–1.0 relative to peak hour). This maps directly to the engine's existing `time_of_day_profile.hour_factors`.

**Typical power** — The overall mean power across all hours replaces the point-in-time `typical_power` from the clone snapshot.

**Power variation** — The coefficient of variation (stddev / mean) across hours replaces the default 0.1 `power_variation`, capturing how peaky or steady the circuit is.

**Cycling detection** — Circuits with high max/mean ratios (e.g., refrigerator, HVAC) and bimodal distributions can have cycling parameters derived: duty cycle ≈ mean/max, period estimated from state-change frequency if available.

**Seasonal adjustment** (requires > 3 months of data) — Compare monthly averages to detect HVAC seasonal swing. If present, tag the circuit with `hvac_type` and seasonal modulation parameters.

#### Engine integration

The derived profiles are written into the clone YAML's `circuit_templates`, replacing or enriching the defaults:

```yaml
clone_15:
  energy_profile:
    mode: consumer
    power_range: [0.0, 9600.0]
    typical_power: 850.0           # from HA mean, not clone snapshot
    power_variation: 0.45          # from HA coefficient of variation
  time_of_day_profile:
    enabled: true
    hour_factors:                   # from HA hourly averages, normalized
      0: 0.15
      1: 0.12
      ...
      14: 1.0
      15: 0.95
      ...
  cycling:
    enabled: true
    duty_cycle: 0.4                # from HA max/mean ratio
    period_minutes: 20             # estimated from state-change frequency
  hvac_type: cooling               # from seasonal pattern detection
```

The engine already supports all of these parameters. The HA import doesn't introduce new simulation mechanics — it calibrates the existing ones.

#### Dashboard integration

An "Import Usage" action on the dashboard, available for configs with `ha_source` or configurable on demand:

1. Prompt for HA URL and token (pre-filled from `ha_source` if present)
2. Auto-detect matching entities by prefix + serial
3. Show a preview: per-circuit summary (avg power, peak hour, profile shape)
4. On confirm, update the circuit templates and write the YAML

This is an explicit user action, not an automatic startup step. Usage profiles are stable over weeks — there's no need to re-import on every restart. The user imports when they want to re-baseline before a modeling session.

#### Entity mapping

The SPAN integration uses a predictable entity naming scheme. Given the clone's original serial and circuit spaces, the mapping is:

```
circuit_id: circuit_{space}
HA entity:  sensor.span_{serial}_{circuit_id}_instant_power
```

Where the serial has hyphens replaced with underscores per HA's entity ID conventions. The import step resolves these automatically and falls back to a manual mapping UI if entities don't match.

#### Scope

- New module: `ha_import.py` — HA statistics retrieval + profile derivation
- `dashboard/routes.py`: Add import usage action
- `dashboard/templates/`: Import dialog partial
- `clone.py` or `validation.py`: Accept `ha_source` config block
- No changes to `engine.py` — it already consumes `time_of_day_profile`, `cycling`, `hvac_type`, and `power_variation`

---

## Implementation Priority

| Feature                            | Status      | Value                                                        |
| ---------------------------------- | ----------- | ------------------------------------------------------------ |
| WSS clone pipeline                 | Done        | Scrape-translate-write from real panel via WSS               |
| Credential persistence + seeding   | Done        | Energy seeds from real panel; provenance + on-demand refresh |
| Dashboard clone UI                 | Not started | Self-contained simulator, no HA integration dependency       |
| HA usage profile import            | Not started | Realistic modeling basis for infrastructure upgrade planning |

The dashboard clone UI and HA import are independent. The HA import is the capstone — it uses historical statistics to calibrate the simulation engine's existing parameters, making forward-looking modeling scenarios grounded in real behavior rather than point-in-time snapshots.
