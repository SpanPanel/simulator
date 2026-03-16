# Panel Cloning & Usage Modeling

## Context

The simulator supports BESS/SPAN Drive simulation, breaker ratings, and full Homie v5 eBus publishing. Given credentials for a **real** SPAN panel, the simulator connects to its eBus, scrapes every retained topic, and translates the result into a simulator YAML config — a faithful starting point that can then be tuned.

The goal is to provide a basis for downstream modeling of changes to BESS, solar, or EVSE infrastructure for upgrades. The clone captures the panel's topology and sizing; the usage modeling features below build on that foundation to produce realistic energy profiles.

---

## Implemented: Clone Pipeline

### Transport: Socket.IO

The HA integration triggers cloning via the Socket.IO `/v1/panel` namespace using the `clone_panel` event. This reuses the existing Socket.IO connection the integration maintains for pushing location data.

### Sequence

```text
HA Integration                   Simulator                        Real Panel
     |                              |                                |
     |== Socket.IO /v1/panel ======>|                                |
     |                              |                                |
     |-- clone_panel -------------->|                                |
     |   {host, passphrase,         |                                |
     |    latitude, longitude}      |                                |
     |                              |-- POST /api/v2/auth/register ->|
     |                              |<-- {mqtt_creds, serial} -------|
     |                              |-- GET /api/v2/certificate/ca ->|
     |                              |<-- PEM cert -------------------|
     |                              |== MQTTS connect ===============|
     |                              |-- SUB ebus/5/{serial}/# ------>|
     |                              |<-- $state, $description -------|
     |                              |<-- retained property msgs -----|
     |                              |   (collect until stable)       |
     |                              |== MQTT disconnect =============|
     |                              |-- parse $description           |
     |                              |-- map properties -> YAML       |
     |                              |-- write configs/{serial}-clone.yaml
     |                              |-- apply lat/lon + timezone     |
     |                              |-- trigger reload               |
     |<-- ack ----------------------|                                |
     |   {clone_serial, circuits,   |                                |
     |    time_zone, ...}           |                                |
```

### Socket.IO Contract

- **Namespace**: `/v1/panel`
- **Event**: `clone_panel`
- **Payload**: `{"host": "...", "passphrase": "...", "latitude": float, "longitude": float}`
- **Result**: `{"status": "ok", "serial": "...", "clone_serial": "...", "filename": "...", "circuits": N, "has_bess": bool, "has_pv": bool, "has_evse": bool, "time_zone": "..."}`
- **Error**: `{"status": "error", "phase": "...", "message": "..."}`

### eBus Scrape Strategy

1. `POST http://{host}/api/v2/auth/register` with `{"name": "sim-clone-{uuid4}", "hopPassphrase": passphrase}`
2. Extract `ebusBrokerUsername`, `ebusBrokerPassword`, `ebusBrokerMqttsPort`, `serialNumber`
3. `GET http://{host}/api/v2/certificate/ca` for TLS trust
4. Connect via MQTTS, subscribe to `ebus/5/{serial}/#`, collect all retained messages
5. Stability gate: stop after 5 seconds with no new topics; max 30 seconds total

### eBus-to-YAML Translation

- Panel config: serial (`sim-{original}-clone`), main breaker, panel size (derived from max space)
- Per-circuit templates (`clone_{space}`): energy profile mode, power range, typical power, relay behavior, priority, breaker rating
- Energy profile mode inferred from device node `feed` cross-references (PV → producer, BESS/EVSE → bidirectional, else → consumer)
- BESS: nameplate capacity, default charge/discharge schedule, 20% backup reserve
- PV: nameplate capacity, production profile
- EVSE: night-charging time-of-day profile

### sim- serial prefix

All simulated panels are prefixed with `sim-` so the HA integration can distinguish simulators from real hardware:

- **Engine**: auto-prefixes `sim-` at config load if missing
- **Clone pipeline**: clone serial is `sim-{original_serial}-clone`
- **Stock configs**: use `sim-` prefix (e.g., `sim-40t-001`)

### Files

- `scraper.py` — eBus scraper (auth + MQTT collection)
- `clone.py` — Translation layer (eBus → YAML); `sim-` prefix in serial construction
- `sio_handler.py` — Socket.IO `clone_panel` event handler
- `app.py` — `_clone_panel()` pipeline orchestrator
- `engine.py` — `sim-` prefix enforcement at config load
- `discovery.py` — mDNS advertisement

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
- `app.py`: Passes `host`/`passphrase` through to `translate_scraped_panel` in `_clone_panel()`
- `dashboard/routes.py`: `GET /panel-source`, `POST /sync-panel-source`
- `dashboard/templates/partials/panel_source.html`: Provenance display with update action
- `dashboard/config_store.py`: `get_panel_source()`, `get_origin_serial()`

---

## Feature: Dashboard Clone UI

### Problem

Cloning currently requires the HA SPAN integration to trigger the Socket.IO `clone_panel` event. This creates a dependency on a separate codebase and makes the simulator less self-contained. Users should be able to clone directly from the simulator dashboard.

### Design

#### mDNS panel browser

The simulator already has `zeroconf` / `AsyncZeroconf` for advertising. Add a `ServiceBrowser` that discovers `_span._tcp.local.` services on the LAN.

- Real SPAN panels advertise `_span._tcp` with their serial number in the TXT record
- Filter out the simulator's own entries (match against known simulator serials, or check for `sim-` prefix)
- Maintain a live dict of discovered panels: `{serial: {host, ip, port, txt_properties}}`
- Panels that disappear from mDNS are removed after a grace period

#### Dashboard route

`GET /admin/discovered-panels` returns the current set of discovered panels as JSON. The dashboard polls this on an interval while the clone dialog is open.

`POST /admin/clone` accepts `{host, passphrase}` and drives the scrape-translate-write pipeline directly — the same `register_with_panel` → `scrape_ebus` → `translate_scraped_panel` → `write_clone_config` → reload sequence that `app._clone_panel()` orchestrates, but invoked via HTTP rather than Socket.IO.

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

#### Scope

- `discovery.py`: Add `ServiceBrowser` for `_span._tcp.local.`, maintain discovered panel dict
- `dashboard/routes.py`: Add `GET /admin/discovered-panels` and `POST /admin/clone`
- `dashboard/templates/`: New clone dialog partial (HTMX)
- `dashboard/templates/base.html`: "Clone Panel" button in nav

---

## Feature: HA Usage Profile Import (Integration-Driven)

### Problem

A cloned panel has accurate topology but synthetic energy behavior. The simulator's engine generates power values from `typical_power`, time-of-day profiles, and noise — useful for exercising the eBus interface, but not representative of actual consumption patterns. For modeling infrastructure changes (adding battery capacity, upgrading solar, sizing an EVSE), the simulation needs to reflect how the household actually uses power.

### Goal

Import historical usage data from Home Assistant to derive per-circuit energy profiles that drive the simulation engine. The goal is **not** to replay history verbatim — it is to extract the usage *shape* (daily patterns, seasonal variation, per-circuit relative load) and use that to parameterize the simulator's existing engine so that forward-looking modeling scenarios are grounded in real behavior.

### Architecture: Integration Does the Heavy Lifting

The HA SPAN integration already has access to the HA recorder, knows the circuit entity IDs, and drives the clone flow over Socket.IO. Rather than giving the simulator its own HA API client, the integration queries HA statistics, derives per-circuit profiles, and delivers pre-computed values to the simulator via a dedicated Socket.IO event.

```text
HA Integration                                 Simulator
     |                                            |
     |-- clone_panel ---------------------------->|  (existing)
     |<-- {clone_serial, circuits, ...} ----------|
     |                                            |
     |-- recorder/statistics_during_period ------>| (HA internal,
     |<-- hourly stats per circuit entity --------|  not the simulator)
     |                                            |
     |-- derive profiles per circuit              |
     |   (typical_power, hour_factors,            |
     |    power_variation, duty_cycle,            |
     |    monthly_factors)                        |
     |                                            |
     |-- apply_usage_profiles ------------------->|  (new Socket.IO event)
     |   {clone_serial, profiles: {               |
     |     "clone_2": {...},                      |
     |     "clone_15": {...},                     |
     |   }}                                       |
     |<-- {status: ok, circuits_updated: N} ------|
```

**Why separate events**: `clone_panel` and `apply_usage_profiles` are independent operations. Profiles can be re-imported without re-cloning — the topology rarely changes, but usage patterns may be worth refreshing before a new modeling session. Both are thin Socket.IO RPC calls over the existing `/v1/panel` namespace.

**Why the integration, not the simulator**: The integration runs inside HA and has native access to `recorder/statistics_during_period`. The simulator has no HA credentials, no HA API client, and no reason to acquire either. The integration computes the profiles; the simulator applies them to its config. Each side does what it already knows how to do.

### HA Side: Profile Derivation (integration repo)

#### Statistics source

HA's recorder stores per-circuit sensor data published by the SPAN integration:

- `sensor.span_{device}_{circuit_name}_power` — instantaneous watts
- `sensor.span_{device}_{circuit_name}_energy_consumed` — monotonic Wh counter
- `sensor.span_{device}_{circuit_name}_energy_produced` — monotonic Wh counter

The recorder's **long-term statistics** (`recorder/statistics_during_period`) retain hourly aggregates (mean, min, max, state change sum) indefinitely. Short-term state history is purged after the configured retention period, but the hourly buckets persist. A month of data is sufficient; a year gives seasonal resolution.

#### Entity resolution

The integration knows the panel's device entry and circuit entities from its own entity registry. During profile building, it resolves each circuit's power entity by device + suffix. No fragile entity name guessing — the integration created these entities and owns their registry entries.

Circuit-to-template mapping uses the clone response: the clone serial encodes the original panel serial, and circuit spaces map directly to template names (`circuit_{space}` -> `clone_{space}`).

#### Profile derivation

From the hourly statistics, derive per-circuit profiles:

**Typical power** — The overall mean power across all hours replaces the point-in-time `typical_power` from the clone snapshot.

**Power variation** — The coefficient of variation (stddev / mean) across hours replaces the default 0.1 `power_variation`, capturing how peaky or steady the circuit is.

**Time-of-day factors** — For each circuit, compute the average power per hour-of-day across the retrieval window. Normalize to produce 24 `hour_factors` values (0.0-1.0 relative to peak hour). This maps directly to the engine's existing `time_of_day_profile.hour_factors`.

**Cycling detection** — Circuits with high max/mean ratios (e.g., refrigerator, HVAC) and bimodal distributions can have cycling parameters derived: duty cycle = mean/max, period estimated from state-change frequency if available.

**Monthly factors** (requires > 3 months of data) — Compare monthly averages to detect seasonal swing. Normalize to produce 12 `monthly_factors` values (1.0 = peak month). Takes precedence over latitude-based `hvac_type` model in the engine.

#### Scope (integration repo: `~/projects/HA/span`)

- **New: `profile_builder.py`** — Queries `recorder/statistics_during_period` for circuit power entities over a configurable window (default 30 days). Derives `typical_power`, `power_variation`, `hour_factors`, `duty_cycle`, `monthly_factors` per circuit. Returns `dict[str, CircuitProfile]` keyed by template name.
- **Extended: `simulation_utils.py`** — New `send_usage_profiles()` function sends `apply_usage_profiles` Socket.IO event. Orchestration function `clone_and_profile()` calls `execute_clone_via_simulator()` then `send_usage_profiles()`.
- **Extended: `config_flow.py`** — Clone step gains an option to include usage profiles (default on if sufficient recorder history exists). Calls `clone_and_profile()` instead of bare `execute_clone_via_simulator()`.

### Simulator Side: Profile Application

#### Socket.IO event

**Event**: `apply_usage_profiles` on `/v1/panel`

**Payload**:

```json
{
  "clone_serial": "sim-nj-2316-005k6-clone",
  "profiles": {
    "clone_2": {
      "typical_power": 145.3,
      "power_variation": 0.45,
      "hour_factors": {
        "0": 0.15, "1": 0.12, "8": 0.65, "14": 1.0, "22": 0.30
      },
      "duty_cycle": 0.4,
      "monthly_factors": {
        "1": 0.6, "2": 0.65, "7": 1.0, "8": 0.95, "12": 0.55
      }
    }
  }
}
```

All profile fields are optional per circuit. The simulator merges only the fields present, preserving topology values (breaker_rating, relay_behavior, priority, mode, power_range) untouched.

**Response**: `{"status": "ok", "circuits_updated": N}`

#### Merge rules

Profile application is an **additive merge** into the clone config's `circuit_templates`:

| Profile field | Target YAML path | Merge behavior |
|---|---|---|
| `typical_power` | `energy_profile.typical_power` | Overwrite |
| `power_variation` | `energy_profile.power_variation` | Overwrite |
| `hour_factors` | `time_of_day_profile.hour_factors` | Overwrite; sets `enabled: true` |
| `duty_cycle` | `cycling_pattern.duty_cycle` | Overwrite; enables cycling |
| `monthly_factors` | `monthly_factors` | Overwrite |

Fields **not** touched by profile merge: `mode`, `power_range`, `relay_behavior`, `priority`, `breaker_rating`, `device_type`, `battery_behavior`, `initial_*_energy_wh`, `nameplate_capacity_w`.

#### Engine integration

The engine already supports all of these parameters — no engine changes required:

- `time_of_day_profile.hour_factors` — applied in `_apply_time_of_day_modulation()`
- `cycling_pattern.duty_cycle` — applied in `_apply_cycling_behavior()`
- `monthly_factors` — applied in `_apply_seasonal_modulation()` (takes precedence over `hvac_type`)
- `power_variation` — applied as noise in `get_circuit_power()`

#### Scope (simulator repo)

- **New: `profile_applicator.py`** — Pure function: reads clone config YAML, merges incoming profile dicts into matching `circuit_templates`, writes back. No HA dependency.
- **Extended: `sio_handler.py`** — New `on_apply_usage_profiles` handler with validation.
- **Extended: `app.py`** — New `_apply_usage_profiles` callback in `SioContext`, wires to `profile_applicator`.
- **No changes to `engine.py`** — it already consumes all target parameters.

### Dashboard: Re-import

The dashboard can trigger a profile re-import for configs with `panel_source` (since the integration knows which panel to query). This is a future extension — the Socket.IO event is the same `apply_usage_profiles`, but initiated from the dashboard rather than the config flow. The dashboard would need to signal the integration to re-derive and push, or expose a manual profile upload.

---

## Implementation Priority

| Feature                            | Status      | Value                                                        |
| ---------------------------------- | ----------- | ------------------------------------------------------------ |
| Clone pipeline                     | Done        | Scrape-translate-write from real panel via Socket.IO         |
| Credential persistence + seeding   | Done        | Energy seeds from real panel; provenance + on-demand refresh |
| Socket.IO clone channel + sim-     | Done        | HA integration clones via existing Socket.IO session         |
| HA usage profile import            | Next        | Realistic modeling basis for infrastructure upgrade planning |
| Dashboard clone UI                 | Not started | Self-contained simulator, no HA integration dependency       |

The HA usage profile import is the next priority. It spans two repos:

1. **Integration** (`~/projects/HA/span`): `profile_builder.py` (derivation) + `simulation_utils.py` extension (delivery)
2. **Simulator** (`~/projects/simulator`): `profile_applicator.py` (merge) + `sio_handler.py` extension (reception)

The dashboard clone UI is independent and lower priority — the integration already provides the clone trigger via Socket.IO.
