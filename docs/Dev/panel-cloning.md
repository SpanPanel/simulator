# Panel Cloning & Usage Modeling

## Overview

The simulator clones a real SPAN panel by connecting to its eBus, scraping every retained topic, and translating the result into a simulator YAML config. The clone captures the panel's topology, breaker sizing, and energy state — a faithful starting point for modeling infrastructure changes (BESS, solar, EVSE upgrades).

Usage modeling layers on top of the clone: the HA recorder provides historical per-circuit energy data that the add-on transforms into usage profiles. The clone provides the topology; the profiles provide the behavioral shape.

### Data source separation

The simulator draws from two independent data sources with distinct responsibilities:

| Source | Provides | Standalone? |
|--------|----------|-------------|
| **eBus** (direct panel connection) | Topology: circuit names, breaker ratings, dipole, shed priority, relay behavior, energy seeds, nameplate capacities, panel size. The authoritative source for *what the panel is*. | Yes — full simulation without HA |
| **HA recorder** (via integration) | History: hourly/monthly power statistics per circuit. The source for *how the panel behaves over time*. | No — requires HA with SPAN integration |

The HA integration's role is narrowly scoped: it provides the **circuit-to-entity key mapping** that lets the add-on query the HA recorder for the right statistic IDs. It does not replicate topology data — that comes from eBus. This keeps the integration thin and the simulator self-contained.

**Why eBus owns topology**: Circuit names, breaker ratings, shed priorities, and energy accumulators are real-time panel state. eBus exposes them directly and authoritatively. Duplicating this data through HA would add a dependency without adding value — and would break standalone operation.

**Why HA owns history**: eBus exposes no historical data. SPAN's cloud warehouse has it but offers no public API. The HA recorder is the pragmatic second-best — it stores long-term statistics from the integration's sensors. If eBus ever exposes historical data directly, the HA dependency for profiles becomes optional.

**Standalone operation**: The simulator runs without HA by scraping eBus and using the resulting YAML config as-is. Profiles default to flat curves or presets. When HA is available, profiles are layered on top — an enhancement, not a requirement.

---

## Clone Pipeline

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
     |                              |-- (async) reload completes     |
     |                              |-- panel registered             |
     |<-- clone_ready {} -----------|                                |
     |                              |                                |
     |-- apply_usage_profiles ----->|  (if profiles available)       |
     |   {clone_serial, profiles}   |                                |
     |<-- ack ----------------------|                                |
     |   {status, templates_updated}|                                |
```

### Socket.IO Contract

- **Namespace**: `/v1/panel`
- **Event**: `clone_panel`
- **Payload**: `{"host": "...", "passphrase": "...", "latitude": float, "longitude": float}`
- **Result (ack)**: `{"status": "ok", "serial": "...", "clone_serial": "...", "filename": "...", "circuits": N, "has_bess": bool, "has_pv": bool, "has_evse": bool, "time_zone": "..."}`
- **Error (ack)**: `{"status": "error", "phase": "...", "message": "..."}`
- **Server event**: `clone_ready {}` — emitted to the same SID after the async reload completes and the clone panel is registered in the simulator's panel registry. Clients that intend to send `apply_usage_profiles` should wait for this event rather than sending immediately after the ack, since the panel may not yet be registered when the ack arrives.

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

### sim- Serial Prefix

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

## Credential Persistence & Energy Seeding

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
  origin_serial: "nj-2316-XXXXX"   # real panel's serial (immutable provenance)
  host: "192.168.1.100"
  passphrase: "..."                 # null for door-bypass
  last_synced: "2026-03-15T10:30:00"
```

`origin_serial` is the real panel's serial number — distinct from the clone's `panel_config.serial_number` (which has the `-clone` suffix). The passphrase is the panel's proximity code, not a cloud credential. It lives alongside the config it produced, on the same local machine that already has direct network access to the panel.

The clone is treated as an independent modeling baseline from the moment it's created. There is no automatic startup refresh — the point-in-time snapshot from cloning captures topology and sizing, which is what matters for modeling.

### Dashboard: Update eBus Energy

The dashboard shows provenance when a `panel_source` block is present:

- Display: "Cloned from **nj-2316-XXXXX** at 192.168.1.100 — last synced 2026-03-15 10:30"
- Action: "Update eBus Energy" — re-scrapes and overwrites energy seed values with current readings from the real panel

eBus provides real-time state only — it does not expose historical data from SPAN's cloud warehouse. The energy seed refresh is useful for resyncing cumulative counters, but `typical_power` and usage profiles are not updated here since an instantaneous active-power snapshot is not representative of actual consumption patterns.

Configs without `panel_source` (hand-authored or future detached clones) show no provenance section.

### Files

- `clone.py`: Energy seed mapping in `_translate_circuit`; `panel_source` block written when `host` is provided; `update_config_from_scrape()` for on-demand energy refresh
- `circuit.py`: Prefers `initial_*_energy_wh` seeds over annual estimate when present
- `config_types.py`: `PanelSource` TypedDict; `initial_consumed_energy_wh` / `initial_produced_energy_wh` in `EnergyProfileExtended`; `panel_source` in `SimulationConfig`
- `validation.py`: `validate_panel_source()` for the `panel_source` block
- `app.py`: Passes `host`/`passphrase` through to `translate_scraped_panel` in `_clone_panel()`
- `dashboard/routes.py`: `GET /panel-source`, `POST /sync-panel-source`
- `dashboard/templates/partials/panel_source.html`: Provenance display with energy refresh action
- `dashboard/config_store.py`: `get_panel_source()`, `get_origin_serial()`

---

## HA Usage Profile Import

### Data Source Limitations

eBus exposes real-time circuit state but no historical data. SPAN's cloud warehouse stores high-resolution, long-lived per-circuit history that would be ideal for profile derivation, but there is no public API to access it. The HA integration works around this by querying the HA recorder's long-term statistics — a pragmatic second-best that depends on how long HA has been collecting data for the panel.

### Architecture: eBus for Topology, HA for History

The data sources are deliberately separated:

- **eBus** provides the complete panel topology — circuit names, breaker ratings, dipole flags, shed priorities, relay behavior, energy seeds, device nameplate capacities. This is what `translate_scraped_panel()` consumes to produce the clone YAML. It works standalone without HA.
- **HA integration** provides the **circuit-to-entity key mapping** — the bridge that lets the add-on query the HA recorder for per-circuit historical statistics. It does not replicate topology data.

The integration exposes a thin manifest service (`span_panel.export_circuit_manifest`) that maps each circuit to its HA power sensor entity ID and clone template name. The add-on calls this service once, then queries the HA recorder directly for hourly/monthly statistics and computes usage profiles locally. This keeps the integration's role narrow (key mapping) while the add-on owns the statistical logic.

This componentization means:
- The manifest service is **small and stable** — it only needs to return `{serial, host, entity_id, template, device_type, tabs}` per circuit. No topology fields to keep in sync.
- The **profile computation is portable** — if eBus ever exposes historical data, the add-on can query it directly using the same `profile_builder.py` logic, bypassing HA entirely.
- The **simulator runs standalone** — eBus scrape produces a complete config; HA profiles are an optional enhancement layered on top.

#### Multi-panel design

SPAN installations commonly have multiple panels (main panel, sub-panels). Each panel is a separate HA config entry with its own entity set. The service returns all panels in a single call — the add-on matches each panel's serial to its local clone config via `panel_source.origin_serial` and processes each independently.

```text
HA Integration                    Add-on (Simulator)                HA Recorder
     |                                  |                                |
     |                                  |-- POST services/               |
     |<-- span_panel/                   |   span_panel/                  |
     |    export_circuit_manifest ------|   export_circuit_manifest      |
     |                                  |                                |
     |-- manifest response ------------>|                                |
     |   {panels: [                     |                                |
     |     {serial, circuits: {...}},   |                                |
     |     {serial, circuits: {...}},   |                                |
     |   ]}                             |                                |
     |                                  |                                |
     |                                  |  for each panel:               |
     |                                  |    match serial →              |
     |                                  |      clone config              |
     |                                  |      (panel_source.            |
     |                                  |       origin_serial)           |
     |                                  |                                |
     |                                  |-- recorder/                    |
     |                                  |   statistics_during_period --->|
     |                                  |   (entity_ids from manifest)   |
     |                                  |<-- hourly + monthly stats -----|
     |                                  |                                |
     |                                  |-- compute profiles per panel   |
     |                                  |   (keyed by template name      |
     |                                  |    from manifest)              |
     |                                  |-- apply to each clone YAML     |
```

**Why all panels in one call**: The add-on doesn't know how many panels exist or what their serials are until it asks. A single call avoids discovery round-trips and gives the add-on the complete system topology. The add-on iterates locally, matching manifests to clone configs by serial.

**Why a service, not Socket.IO**: The HA service API is a standard, well-documented mechanism. The add-on already has an HA API client (`ha_api/client.py`) that can call services via REST. No proprietary protocol needed.

**Why the add-on queries recorder directly**: The add-on already has the `HAClient` with WebSocket support for `recorder/statistics_during_period`, and it already has the full profile derivation logic in `profile_builder.py`. The integration doesn't need to compute profiles — it just needs to export the mapping that only it can authoritatively provide.

**Why separate events still work**: The Socket.IO `clone_panel` and `apply_usage_profiles` events remain for the integration-initiated flow (config flow clone). The service-based flow is an independent path the add-on can use on its own schedule — profile refresh without re-cloning, or initial profile import after clone.

### HA Side: Circuit Manifest Service (integration repo)

#### Service definition

The integration registers a standard HA service: `span_panel.export_circuit_manifest`.

**Service schema**: No required fields. The service iterates all SPAN panel config entries and returns a manifest for each.

**Response**: returned via the service response mechanism (HA 2024.8+ supports service responses).

```json
{
  "panels": [
    {
      "serial": "nj-2316-XXXXX",
      "host": "192.168.1.100",
      "circuits": [
        {
          "entity_id": "sensor.span_panel_kitchen_disposal_power",
          "template": "clone_2",
          "device_type": "circuit",
          "tabs": [2, 3]
        },
        {
          "entity_id": "sensor.span_panel_solar_inverter_power",
          "template": "clone_8",
          "device_type": "pv",
          "tabs": [8]
        },
        {
          "entity_id": "sensor.span_panel_home_battery_power",
          "template": "clone_14",
          "device_type": "battery",
          "tabs": [14, 16]
        }
      ]
    },
    {
      "serial": "nj-2316-YYYYY",
      "host": "192.168.1.101",
      "circuits": [
        {
          "entity_id": "sensor.span_panel_2_garage_outlets_power",
          "template": "clone_1",
          "device_type": "circuit",
          "tabs": [1, 3]
        },
        {
          "entity_id": "sensor.span_panel_2_ev_charger_power",
          "template": "clone_5",
          "device_type": "evse",
          "tabs": [5, 7]
        }
      ]
    }
  ]
}
```

**Per-panel fields**:

| Field | Type | Description |
|---|---|---|
| `serial` | `str` | Real panel serial — add-on matches this to `panel_source.origin_serial` in clone configs |
| `host` | `str` | Panel IP/hostname from the HA config entry — used by the add-on for direct eBus scraping without manual IP entry |

**Per-circuit fields**:

| Field | Type | Description |
|---|---|---|
| `entity_id` | `str` | Power sensor entity ID — the `statistic_id` for recorder queries. Resolved by the integration via entity registry lookup (`build_circuit_unique_id` → `async_get_entity_id`). |
| `template` | `str` | Clone template name (`clone_{min_tab}`) — used to key profiles for `profile_applicator` |
| `device_type` | `str` | `"circuit"`, `"pv"`, `"battery"`, `"evse"` — add-on uses this to skip hardware-driven profiles |
| `tabs` | `list[int]` | Breaker tab numbers — informational, confirms template derivation |

**Why a list, not a keyed dict**: The add-on needs `entity_id` for recorder queries and `template` for profile application. These are the two operational identifiers. There is no need for a third "circuit key" concept — that was an artifact of the add-on reverse-engineering entity IDs from the states API. The integration resolves entity IDs authoritatively via its unique_id → entity registry lookup, and template names via `min(tabs)`. The add-on consumes both directly.

#### Entity resolution (integration-side)

The integration resolves each circuit's power sensor entity via the entity registry using the unique ID pattern `span_{serial}_{circuit_uuid}_power` (via `build_circuit_unique_id`). No fragile entity name guessing — the integration created these entities and owns their registry entries.

Circuit-to-template mapping: for each circuit in `SpanPanelSnapshot.circuits`, `min(circuit.tabs)` → `clone_{tab}` template name. This matches the clone pipeline's naming convention.

The service includes all circuits — including PV, BESS, and EVSE. The add-on decides what to skip based on `device_type`, not the integration. This keeps the service a pure data export.

#### Multi-panel iteration

The service handler iterates `hass.config_entries.async_entries(DOMAIN)` to find all configured SPAN panels. For each entry, it retrieves the panel's data coordinator, resolves circuit entity IDs via the entity registry, and builds the per-panel manifest. Panels that are not loaded or have no circuit data are silently omitted.

#### Host field

Each config entry stores the panel's IP/hostname in `entry.data["host"]` (set during integration setup when the user adds the panel). The service includes this as the `host` field in each panel's manifest entry. This is the same address HA uses to communicate with the panel — it is authoritative and current.

The add-on uses this field to populate the clone card's panel selector, eliminating manual IP entry. The address is the panel's direct IP, not the HA instance's — the add-on connects to the panel's eBus directly for scraping.

#### Files (integration repo: `~/projects/HA/span`)

- **`services.yaml`** — Service schema definition for `export_circuit_manifest`
- **`__init__.py`** — Service registration via `hass.services.async_register()` with `supports_response=SupportsResponse.ONLY`. Handler iterates all config entries.
- **`simulator_profile_builder.py`** — Existing file; entity resolution logic reused by the service handler. The profile computation portions become unused by the integration (moved to add-on) but can remain for backward compatibility with the Socket.IO flow.
- **`simulation_utils.py`** — `CloneResult`, `ProfileResult` dataclasses; `clone_with_profiles()` manages a single Socket.IO session (clone → wait for `clone_ready` → send profiles); `discover_clone_simulators()` for mDNS discovery.
- **`config_flow.py`** — Options flow builds profiles via `_build_profiles_best_effort()`, then passes them to `clone_with_profiles()`. Profile delivery is transparent and best-effort.

### Add-on Side: Service-Driven Profile Import

#### Multi-panel flow

1. **Call manifest service**: `POST /api/services/span_panel/export_circuit_manifest` → get all panels with their circuits, entity IDs, template names, and device types
2. **Match panels to clone configs**: For each panel in the manifest, find the clone config file whose `panel_source.origin_serial` matches the manifest's `serial`. Unmatched panels are logged and skipped (no local clone for that panel yet).
3. **Per panel** (iterate independently):
   a. **Filter circuits**: Skip `device_type` in `{"pv", "battery", "evse"}` — their power profiles are hardware-driven
   b. **Query recorder**: Pass power `entity_id` values as `statistic_ids` to `recorder/statistics_during_period` (hourly 30d, monthly 12mo) — same queries as today
   c. **Build profiles**: `profile_builder.py` computes `typical_power`, `power_variation`, `hour_factors`, `duty_cycle`, `monthly_factors` per circuit, keyed by `template` name — same statistical logic as today
   d. **Apply profiles**: Pass template-keyed profiles to `profile_applicator.py` with the matched config path
4. **Trigger reload**: Single reload after all panels are processed

#### Matching manifests to clone configs

The link between a manifest panel and a local clone config is `panel_source.origin_serial` in the YAML. The add-on scans all config files in its config directory and builds a `dict[str, Path]` mapping `origin_serial → config_path`. Each manifest panel's `serial` is looked up in this map.

Edge cases:
- **No match**: Panel exists in HA but hasn't been cloned yet. Logged as info, skipped.
- **Multiple matches**: Shouldn't happen (each clone has a unique origin serial). If it does, first match wins with a warning.
- **No `panel_source`**: Hand-authored configs without provenance. Not eligible for service-driven profile import.

#### HAClient addition

Add `async_call_service()` to `ha_api/client.py`:

```python
async def async_call_service(
    self,
    domain: str,
    service: str,
    service_data: dict[str, object] | None = None,
    *,
    return_response: bool = False,
) -> dict[str, object]:
```

Uses `POST /api/services/{domain}/{service}` with the `return_response` header for service responses.

#### Manifest consumer

New module `ha_api/manifest.py` — thin wrapper that calls the service and returns typed dataclasses:

```python
@dataclass(frozen=True, slots=True)
class CircuitManifestEntry:
    entity_id: str
    template: str
    device_type: str
    tabs: list[int]

@dataclass(frozen=True, slots=True)
class PanelManifest:
    serial: str
    host: str
    circuits: list[CircuitManifestEntry]

    def profile_circuits(self) -> list[CircuitManifestEntry]:
        """Circuits eligible for profile building (excludes hardware-driven)."""
        return [c for c in self.circuits
                if c.device_type not in ("pv", "battery", "evse")]

    def profile_entity_ids(self) -> list[str]:
        """Entity IDs for circuits eligible for profile building."""
        return [c.entity_id for c in self.profile_circuits()]


async def fetch_all_manifests(client: HAClient) -> list[PanelManifest]:
    """Call export_circuit_manifest and parse the multi-panel response."""
    ...
```

The top-level `fetch_all_manifests()` returns a list of `PanelManifest` — one per panel in the HA installation. The caller matches each to a local config file by serial.

#### entity_discovery.py disposition

Replaced by `manifest.py`. The states-API pattern-matching approach becomes a fallback for environments where the integration doesn't have the service (older versions). Eventually removed.

#### profile_builder.py changes

Reworked. Currently accepts `list[CircuitEntityMapping]` with circuit_key-based output — change to accept `list[CircuitManifestEntry]` instead. Each entry carries both `entity_id` (for recorder queries) and `template` (for output keying). The statistical computation is unchanged.

Output is keyed directly by **template name** — no intermediate circuit_key or re-keying step. The profile builder queries recorder using `entity_id`, computes the profile, and stores it under the circuit's `template` name. This output feeds directly into `profile_applicator.py`.

### Profile Derivation (shared logic)

#### Statistics source

HA's recorder stores per-circuit sensor data published by the SPAN integration. The recorder's long-term statistics (`recorder/statistics_during_period`) retain hourly aggregates (mean, min, max) indefinitely. Short-term state history is purged after the configured retention period, but the hourly buckets persist. A month of data is sufficient; a year gives seasonal resolution.

#### Recorder queries

Two targeted queries per profile build:

1. **Hourly stats, last 30 days** (~720 points/circuit): derives `typical_power`, `power_variation`, `hour_factors`, `duty_cycle`
2. **Monthly stats, last 12 months** (~12 points/circuit): derives `monthly_factors`

Circuits with fewer than 24 hourly data points are skipped.

#### Profile derivation

**Typical power** — Mean of hourly means across all hours. Replaces the point-in-time `typical_power` from the clone snapshot.

**Power variation** — Coefficient of variation (stddev / mean) of hourly means, clamped to [0.0, 1.0]. Replaces the default 0.1 `power_variation`.

**Time-of-day factors** — Group hourly stats by hour-of-day (0–23), average each bucket, normalize so peak hour = 1.0. Maps directly to `time_of_day_profile.hour_factors`.

**Duty cycle** — `mean(hourly means) / mean(hourly maxes)`. Only included if < 0.8 (circuits at or above 0.8 are considered always-on).

**Monthly factors** (requires 3+ distinct months) — Monthly averages normalized to peak month = 1.0. Takes precedence over latitude-based `hvac_type` model in the engine.

### Legacy: Integration-Initiated Profile Delivery (Socket.IO)

The Socket.IO `apply_usage_profiles` event remains as a secondary path. The integration can still compute and deliver profiles directly — useful for the config flow clone sequence where the integration is already orchestrating the operation.

```text
HA Integration                                 Simulator
     |                                            |
     |== Socket.IO /v1/panel ===================>|  (single session)
     |                                            |
     |-- clone_panel ---------------------------->|
     |<-- ack {clone_serial, circuits, ...} ------|
     |                                            |  (async reload)
     |<-- clone_ready {} -------------------------|
     |                                            |
     |-- apply_usage_profiles ------------------->|  (same connection)
     |   {clone_serial, profiles: {               |
     |     "clone_2": {...},                      |
     |     "clone_15": {...},                     |
     |   }}                                       |
     |<-- ack {status: ok, templates_updated: N} -|
     |                                            |
     |== disconnect ==============================|
```

The config flow orchestration is unchanged: `clone_with_profiles()` in `simulation_utils.py` manages a single Socket.IO session (clone → wait for `clone_ready` → send profiles). Profile building and delivery are best-effort.

#### Files (integration repo: `~/projects/HA/span`)

- **`simulator_profile_builder.py`** — Queries `statistics_during_period` for circuit power entities over 30-day (hourly) and 12-month (monthly) windows. Maps circuits to template names via `min(tabs)`. Derives `typical_power`, `power_variation`, `hour_factors`, `duty_cycle`, `monthly_factors` per circuit. Returns `dict[str, dict[str, object]]` keyed by template name.
- **`simulation_utils.py`** — `CloneResult`, `ProfileResult` dataclasses; `clone_with_profiles()` manages a single Socket.IO session (clone → wait for `clone_ready` → send profiles); `discover_clone_simulators()` for mDNS discovery.
- **`config_flow.py`** — Options flow builds profiles via `_build_profiles_best_effort()`, then passes them to `clone_with_profiles()`. Profile delivery is transparent and best-effort.

### Simulator Side: Profile Application

#### Profile applicator (unchanged)

`profile_applicator.py` is a pure function: reads clone config YAML, merges incoming profile dicts into matching `circuit_templates`, writes back. It accepts profiles keyed by **template name** regardless of how they were produced — both the service-driven path (add-on re-keys circuit_key → template) and the Socket.IO path (integration keys by template directly) produce the same shape.

#### Merge rules

Profile application is an additive merge into the clone config's `circuit_templates`:

| Profile field | Target YAML path | Merge behavior |
|---|---|---|
| `typical_power` | `energy_profile.typical_power` | Overwrite; skip producer/bidirectional |
| `power_variation` | `energy_profile.power_variation` | Overwrite; skip producer/bidirectional |
| `hour_factors` | `time_of_day_profile.hour_factors` | Overwrite; sets `enabled: true` |
| `duty_cycle` | `cycling_pattern.duty_cycle` | Overwrite |
| `monthly_factors` | `monthly_factors` | Overwrite |

Fields not touched by profile merge: `mode`, `power_range`, `relay_behavior`, `priority`, `breaker_rating`, `device_type`, `battery_behavior`, `initial_*_energy_wh`, `nameplate_capacity_w`.

All profile fields are optional per circuit. The simulator merges only the fields present, preserving topology values untouched. String dict keys from JSON (`"0"`, `"1"`) are converted to int keys for YAML compatibility.

#### Socket.IO event (legacy path)

**Event**: `apply_usage_profiles` on `/v1/panel`

**Payload**:

```json
{
  "clone_serial": "sim-nj-2316-XXXXX-clone",
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

**Response**: `{"status": "ok", "templates_updated": N}`

#### Engine integration

The engine already supports all of these parameters — no engine changes were required:

- `time_of_day_profile.hour_factors` — applied in `_apply_time_of_day_modulation()`
- `cycling_pattern.duty_cycle` — applied in `_apply_cycling_behavior()`
- `monthly_factors` — applied in `_apply_seasonal_modulation()` (takes precedence over `hvac_type`)
- `power_variation` — applied as noise in `get_circuit_power()`

#### Files (simulator repo)

- **`ha_api/manifest.py`** — (new) Calls `span_panel.export_circuit_manifest` service, parses multi-panel response into `list[PanelManifest]`. Each `PanelManifest` has serial + `list[CircuitManifestEntry]` with helpers for filtering and entity ID extraction.
- **`ha_api/client.py`** — (modified) Add `async_call_service()` for HA service calls with response support.
- **`ha_api/profile_builder.py`** — (reworked) Accept `list[CircuitManifestEntry]` instead of `list[CircuitEntityMapping]`. Output keyed directly by template name (from manifest) — no intermediate circuit_key. Statistical computation unchanged.
- **`ha_api/entity_discovery.py`** — (deprecated) Retained as fallback for older integration versions without the service. New code paths use `manifest.py`.
- **`profile_applicator.py`** — (unchanged) Pure merge function, accepts template-keyed profiles.
- **`sio_handler.py`** — (unchanged) `SioContext` with `clone_panel` and `apply_usage_profiles` callbacks for the Socket.IO path.
- **`app.py`** — (modified) Add multi-panel profile import orchestration: fetch all manifests → match serials to clone configs via `panel_source.origin_serial` → build and apply profiles per panel → single reload. Socket.IO path unchanged.

### Dashboard: Panel Discovery via Manifest

The "Clone from HA Integration" card uses the manifest service to populate a panel selector — saving the user from typing an IP. The eBus scrape still runs against the selected panel to capture full topology. The passphrase field stays because eBus authentication requires it (blank for door-bypass panels).

#### Flow

1. **`GET /discovered-panels`**: If `ha_client` is connected, calls `fetch_all_manifests()` and returns JSON:
   ```json
   [
     {"serial": "nj-2316-XXXXX", "host": "192.168.1.100", "circuits": 20},
     {"serial": "nj-2316-YYYYY", "host": "192.168.1.101", "circuits": 12}
   ]
   ```
   Returns `[]` if HA is not connected or the service call fails.

2. **Clone card template**: When `ha_available` is true, renders a `<select>` populated from `/discovered-panels`. Selecting a panel auto-fills the host input. The manual IP text input remains for standalone use or when HA is unavailable.

3. **Clone submission**: Unchanged — `POST /clone-from-panel` receives `host` and `passphrase`, scrapes the panel via eBus, builds the YAML config, then optionally imports HA profiles if available.

#### Why the eBus scrape stays

The manifest provides `{serial, host, entity_id, template, device_type, tabs}` — enough for panel discovery and profile key mapping, but not enough to build a simulator config. The eBus scrape provides circuit names, breaker ratings, dipole flags, shed priorities, relay behavior, energy seeds, and device nameplate capacities. These are the panel's real-time state — authoritative and complete.

Keeping eBus as the topology source means:
- The simulator works standalone without HA
- Circuit names come from the panel, not from HA entity naming conventions
- If eBus later exposes history, the HA dependency drops to zero

#### Fallback

When HA is not connected (`ha_available` is false), the panel selector is not rendered. The form shows only the manual IP input and passphrase — full standalone operation.

#### Why manifest for discovery, not mDNS

Real SPAN panels advertise `_span._tcp.local.` via mDNS, so a browser could find them without HA. However:

- The manifest service gives us the serial and circuit count alongside the IP, providing richer context for the selector.
- The HA config entry's `host` is the authoritative, user-verified address (mDNS can advertise stale IPs after network changes).
- No new mDNS browser dependency needed — the add-on already has the HA API client.
- mDNS discovery can be added later as a complementary mechanism for standalone environments.

#### Files

- **`ha_api/manifest.py`**: Add `host` field to `PanelManifest`, parse from service response
- **`dashboard/routes.py`**: New `GET /discovered-panels` route
- **`dashboard/templates/partials/clone_panel.html`**: Panel selector UI
- **Integration repo**: Add `"host": entry.data["host"]` to service response per panel

### Implementation Plan

#### Integration repo (`~/projects/HA/span`)

1. **Define service schema** (`services.yaml`): `export_circuit_manifest` — no required fields
2. **Implement service handler** (`__init__.py`): Iterate `hass.config_entries.async_entries(DOMAIN)`, for each loaded entry resolve circuit entity IDs via entity registry, compute `min(tabs)` → template name, build per-panel manifest. Return `{"panels": [...]}`. Register with `supports_response=SupportsResponse.ONLY`.
3. **Include `host` in response**: Add `"host": entry.data["host"]` per panel — the IP/hostname from the config entry, used by the add-on for direct eBus scraping.
4. **Reuse entity resolution**: The existing `build_circuit_unique_id` and circuit-to-tab mapping from `simulator_profile_builder.py` provide the resolution logic. Factor into a shared helper if needed.

#### Simulator repo (this repo)

1. **`ha_api/client.py`**: Add `async_call_service()` method
2. **`ha_api/manifest.py`**: New module — `fetch_all_manifests()` calls service, parses multi-panel response into `list[PanelManifest]`. Add `host: str` field to `PanelManifest`, parsed from service response.
3. **`ha_api/profile_builder.py`**: Rework to accept `list[CircuitManifestEntry]`, output keyed by template name directly. No circuit_key layer.
4. **Config serial index**: Scan config directory, read `panel_source.origin_serial` from each YAML, build `dict[str, Path]` for manifest matching.
5. **Multi-panel orchestration** (`app.py` or new `ha_api/profile_import.py`): Fetch all manifests → match to configs → per-panel: filter circuits, query recorder, build profiles (keyed by template), apply → single reload.
6. **`dashboard/routes.py`**: New `GET /discovered-panels` route — calls `fetch_all_manifests()`, returns JSON list of `{serial, host, circuits}` for the clone card panel selector.
7. **`dashboard/templates/partials/clone_panel.html`**: Replace manual IP text input with a `<select>` populated from `/discovered-panels` when HA is available. Fall back to manual input when HA is not connected.
8. **`ha_api/entity_discovery.py`**: Mark as deprecated, retain as fallback
9. **Tests**: Unit tests for manifest parsing (including `host` field), multi-panel matching, re-keying, unmatched panel handling, service call mock, `/discovered-panels` route

### Tests

- **`test_profile_applicator.py`** — 10 tests: basic merge, string key conversion, duty cycle, monthly factors, producer skip, missing template, empty profiles, multiple templates, field preservation, invalid config.
- **`test_sio.py`** — 5 tests for `apply_usage_profiles` event validation: valid call, missing serial, empty serial, missing profiles, empty profiles.
- **`test_manifest.py`** — (planned) Manifest parsing, multi-panel response, device_type filtering, serial-to-config matching, unmatched panels, service call error handling.
