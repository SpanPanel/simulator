# Synthetic History Generation for Cloned Panels

## Problem

When a user clones a template to create a panel config and no HA recorder data is available, the simulator has no historical data to replay. The simulator
should project what the history would have been using the circuit data, BESS, and EVSE configuration it already has, producing a year of synthetic recorder data
at the same granularity a real HA instance would retain.

## Architecture

### Data Source Abstraction

The recorder playback layer (`RecorderDataSource`) consumes data through a provider interface (`HistoryProvider` protocol). The provider abstracts the source
from the playback. Two peer provider implementations exist:

- **HA provider** -- reads from a live Home Assistant instance via its statistics API
- **SQLite provider** (`SqliteHistoryProvider`) -- reads from a local SQLite file

Both implement the same interface. Which one is used is a configuration choice, not a quality or priority distinction. The playback layer receives a provider
and replays data identically regardless of source.

```
                    +---------------------+
                    |  RecorderDataSource  |   <- single playback abstraction
                    |  (get_power, merge,  |
                    |   interpolation)     |
                    +---------+-----------+
                              |
                    +---------+-----------+
                    |   HistoryProvider    |   <- provider interface
                    +-----+--------+------+
                          |        |
                   +------+--+  +--+----------+
                   | HA      |  | SQLite      |
                   | Provider |  | Provider    |
                   +---------+  +-------------+
```

**Changes to `RecorderDataSource`:** The `_HOURLY_LOOKBACK` constant (currently 90 days) must be configurable or increased to 365 days when loading from SQLite,
since the generated history spans a full year. Without this, `RecorderDataSource.load()` would silently discard 9 months of generated data. The cleanest
approach is to accept a `lookback_days` parameter at load time, defaulting to 90 for HA (matching current behavior) and 365 for SQLite.

No changes to `RealisticBehaviorEngine.get_power()` or `engine.py` playback logic.

### Startup Source Selection

```
1. Load panel config YAML
2. Determine configured data source:
   - If recorder_entity mappings point to HA -> use HA provider
   - If history_db configured/discovered -> use SQLite provider
   - If neither -> no recorder data, synthetic per-tick generation (existing fallback)
```

No precedence between sources. The config determines which provider is used.

## New Components

### 1. SqliteHistoryProvider

Implements the existing `HistoryProvider` protocol. Reads from a companion SQLite file using HA's recorder schema. Returns data in the same format as the live
HA provider.

- Reads `statistics` table for hourly data
- Reads `statistics_short_term` table for 5-minute data
- Returns records with `start` as epoch seconds (float) matching SQLite's `start_ts` column directly -- `RecorderDataSource._parse_timestamp` already handles
  this format
- `RecorderDataSource` merges the two tiers using its existing logic

### 2. SyntheticHistoryGenerator

Standalone module that takes a panel config YAML, runs the projection, and writes the companion SQLite.

**Inputs:**

- Completed panel config YAML (circuits, templates, BESS config, EVSE profiles)
- Panel latitude/longitude (for solar model and weather data)
- Generation anchor: "now" (or configurable timestamp for testing)

**Time windows generated:**

- `[anchor - 1 year, anchor - 10 days]`: hourly rows written to `statistics` table
- `[anchor - 10 days, anchor]`: 5-minute rows written to `statistics_short_term` table

This mirrors HA's actual retention model: hourly data is permanent, 5-minute data exists only for the most recent 10 days.

**Per-circuit generation strategy:**

| Circuit Type        | Generation Approach                                                                                                                  |
| ------------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| Consumer (loads)    | `typical_power` x time-of-day profile x monthly/seasonal factors x noise                                                             |
| Producer (solar/PV) | Solar production model x weather degradation (Open-Meteo) x panel capacity                                                           |
| BESS                | Charge/discharge/idle schedule from `battery_behavior` config, respecting `max_charge_power`, `max_discharge_power`, SOE constraints |
| EVSE                | `time_of_day_profile.hour_factors` x rated power, with session randomization                                                         |
| HVAC                | Temperature-aware seasonal model (existing `hvac_type` logic) x duty cycle                                                           |
| Cycling loads       | `cycling_pattern` (duty cycle or on/off durations) applied per period                                                                |

**Per statistics row, fields populated:**

- `start_ts`: period start epoch
- `mean`: computed power for that period
- `min`: `mean x (1 - noise_factor)`
- `max`: `mean x (1 + noise_factor)`
- `created_ts`: `start_ts` (synthetic but plausible)
- `sum`: NULL for v1 (power sensors use `mean`/`min`/`max`; energy accumulation via `sum` can be added later if dashboard kWh charts require it)

Reuses the existing modulation infrastructure from `RealisticBehaviorEngine`: solar curves (`solar.py`), weather degradation (`weather.py`), seasonal/monthly
factors, time-of-day profiles, cycling patterns, and HVAC modeling.

**Noise model:** Per-row noise is deterministic, seeded from a hash of `(panel_serial, circuit_id, start_ts)`. This ensures regenerating the DB produces
identical data, matching the approach already used by `daily_weather_factor` in `solar.py`.

**Timezone handling:** The generator uses the panel's configured timezone (`panel_config.time_zone`, or derived from lat/lon) to convert UTC epoch timestamps to
local time when applying time-of-day profiles and BESS charge/discharge schedules.

**BESS SOE tracking:** BESS circuits are generated in strict chronological order, carrying state-of-energy forward across all rows. Initial SOE starts at
`backup_reserve_pct`. This means BESS generation cannot be parallelized per-circuit.

### 3. Provider Selection Logic

In `PanelInstance`/`app.py` startup, the configured data source determines which provider is instantiated and passed to `RecorderDataSource`.

**Changes to existing files:**

- **`app.py`**: `_load_recorder_data()` currently only creates a `RecorderDataSource` when an HA client is available. A second code path is needed: when a
  companion `_history.db` exists, instantiate `SqliteHistoryProvider` and pass it to `RecorderDataSource` with `lookback_days=365`.
- **`config_types.py`**: Add `history_db: NotRequired[str]` to the `PanelConfig` TypedDict for explicit path override (convention-based discovery is the
  default).

## SQLite Schema

File convention: `configs/<panel_name>_history.db` alongside the panel YAML.

Discovery: `SqliteHistoryProvider` receives the DB path at construction. `PanelInstance` resolves it by convention (swap `.yaml` to `_history.db`) or from an
explicit `history_db` field in `panel_config`.

```sql
CREATE TABLE statistics_meta (
    id INTEGER PRIMARY KEY,
    statistic_id TEXT UNIQUE NOT NULL,
    source TEXT NOT NULL DEFAULT 'simulator',
    unit_of_measurement TEXT,
    has_mean INTEGER DEFAULT 1,
    has_sum INTEGER DEFAULT 0,
    name TEXT
);

CREATE TABLE statistics (
    id INTEGER PRIMARY KEY,
    metadata_id INTEGER NOT NULL REFERENCES statistics_meta(id),
    created_ts REAL NOT NULL,
    start_ts REAL NOT NULL,
    mean REAL,
    min REAL,
    max REAL,
    last_reset_ts REAL,
    state REAL,
    sum REAL,
    UNIQUE(metadata_id, start_ts)
);

CREATE TABLE statistics_short_term (
    id INTEGER PRIMARY KEY,
    metadata_id INTEGER NOT NULL REFERENCES statistics_meta(id),
    created_ts REAL NOT NULL,
    start_ts REAL NOT NULL,
    mean REAL,
    min REAL,
    max REAL,
    last_reset_ts REAL,
    state REAL,
    sum REAL,
    UNIQUE(metadata_id, start_ts)
);
```

Entity naming in `statistics_meta`: `sensor.<panel_serial>_<circuit_name>_power` -- matches what the SPAN HA integration produces, so `recorder_entity` mappings
work identically.

## Clone Pipeline Integration

After `translate_scraped_panel()` produces the config dict and `write_clone_config()` writes the YAML:

1. Call `await SyntheticHistoryGenerator.generate(config_path)` -- async because weather data fetching (`weather.py`) uses async HTTP
2. Generator reads the YAML, runs the projection, writes the companion `_history.db`
3. Clone output includes both files
4. If generation fails (e.g., network unavailable for Open-Meteo), the clone still succeeds with the YAML. The generator falls back to the deterministic weather
   model in `solar.py` (no-network fallback). The SQLite is still produced, just with less accurate weather variation.

Generation is invoked from both the dashboard clone endpoint and the CLI clone command.

## Standalone CLI

```
python -m span_panel_simulator.history_generator configs/my_panel.yaml
```

- Reads the YAML, generates (or regenerates) the companion SQLite
- Useful for hand-authored configs, regenerating after config edits, testing
- Optional flags: `--anchor-time` (default: now), `--years` (default: 1)

## Testing Strategy

**Unit tests:**

- `SyntheticHistoryGenerator`: given a known config, verify correct number of rows, correct time ranges, power values within expected bounds per circuit type
- `SqliteHistoryProvider`: given a pre-built SQLite, verify it returns data in the same format as `HistoryProvider`
- Round-trip: generate -> load via `SqliteHistoryProvider` -> verify `RecorderDataSource.get_power()` returns interpolated values consistent with generation
  inputs

**Integration tests:**

- Clone a template panel -> verify companion `_history.db` is created alongside YAML
- Start a `PanelInstance` from that config -> verify it replays synthetic history identically to how it would replay HA data

**CLI test:**

- Run the standalone generator against a test config, verify the SQLite output schema and row counts
