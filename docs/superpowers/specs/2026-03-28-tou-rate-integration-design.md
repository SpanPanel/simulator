# ToU Rate Integration Design

Design for integrating Time-of-Use (ToU) rate plans from the OpenEI Utility Rate Database (URDB) into the SPAN Panel Simulator, enabling cost-based Before/After comparisons in the modeling view.

---

## Scope

**In scope:**
- URDB API client: fetch utilities by lat/lon, fetch rate plans, fetch full rate detail
- Simulator-wide rate cache storing verbatim URDB records with attribution metadata
- Rate resolver: timestamp + URDB record -> import/export rates
- Cost engine: hourly power arrays + rate record -> cost ledger
- Two rate plan slots: current (simulator-wide) and proposed (per-modeling comparison)
- Modeling view UI: rate display, OpenEI config/retrieval dialog, attribution button
- Cost figures on existing energy summary cards

**Out of scope:**
- Rate editing (URDB records are read-only)
- Tiered rate calculation (use tier 1 for v1)
- Monte Carlo optimization
- ROI / capitalization / financial analysis
- Manual rate entry fallback
- Demand charge time-series calculation (included as fixed monthly cost only)

---

## Data Model

### Simulator-Wide Rate Cache

A new file `data/rates_cache.yaml` stores all fetched URDB records, shared across simulator profiles.

```yaml
rates:
  "539f6a22ec4f024411ec8bf9":        # keyed by URDB label
    source: "openei_urdb"
    retrieved_at: "2026-03-28T14:30:00Z"
    attribution:
      provider: "OpenEI Utility Rate Database"
      url: "https://openei.org/wiki/Utility_Rate_Database"
      license: "CC0"
      api_version: 3
    record: { ... }                   # Full URDB JSON, stored verbatim
  "539f6a22ec4f024411ec8c01":
    ...

current_rate_label: "539f6a22ec4f024411ec8bf9"
```

Key properties:
- `record` contains the exact URDB API response for `detail=full` -- never modified
- `current_rate_label` is the simulator-wide "what I'm on" selection
- Proposed rate is a transient modeling-view selection referencing a cached label
- Cache is fetch-on-demand with user-triggered refresh; no TTL

### OpenEI Configuration

Stored simulator-wide (alongside the rate cache or in simulator config):

```yaml
openei:
  api_url: "https://api.openei.org/utility_rates"
  api_key: "your-key-here"
```

Both fields are user-configurable via a dialog in the modeling view.

---

## Package Structure

```
src/span_panel_simulator/rates/
  __init__.py
  types.py          # Typed structures for URDB records and metadata
  openei.py         # URDB API client
  resolver.py       # Timestamp -> rate lookup
  cost_engine.py    # Hourly power + rates -> CostLedger
```

### types.py

Typed wrappers around the URDB schema and cost calculation results.

**URDBRecord** -- TypedDict matching the URDB response fields used by the resolver and cost engine:
- `label`, `utility`, `name`, `uri`, `startdate`, `enddate`
- `energyratestructure` -- list of list of dicts (period -> tier -> {rate, max, unit, adj})
- `energyweekdayschedule` -- 12x24 matrix (month x hour -> period index)
- `energyweekendschedule` -- 12x24 matrix (month x hour -> period index)
- `sell` -- export rate structure (same format as energyratestructure), optional
- `usenetmetering` -- bool
- `fixedmonthlycharge` -- float, optional
- `flatdemandstructure` -- flat demand charges, optional
- `flatdemandmonths` -- month applicability for flat demand, optional
- `demandratestructure` -- time-based demand charges, optional
- `sector`, `description`, `source`

**RateCacheEntry** -- metadata envelope:
- `source: str` ("openei_urdb")
- `retrieved_at: str` (ISO 8601)
- `attribution: AttributionMeta`
- `record: URDBRecord`

**AttributionMeta**:
- `provider: str`
- `url: str`
- `license: str`
- `api_version: int`

**CostLedger** -- result of cost calculation:
- `import_cost: float` ($ over horizon)
- `export_credit: float` ($ over horizon)
- `net_cost: float` (import_cost - export_credit + fixed_charges)
- `fixed_charges: float` ($ over horizon, from fixedmonthlycharge + flat demand)

### openei.py

URDB API client. All methods accept `api_url` and `api_key` parameters from the stored configuration.

```python
async def fetch_utilities(
    lat: float, lon: float,
    api_url: str, api_key: str,
) -> list[UtilitySummary]:
    """Fetch utilities near a lat/lon from URDB."""

async def fetch_rate_plans(
    utility: str,
    api_url: str, api_key: str,
    sector: str = "Residential",
) -> list[RatePlanSummary]:
    """Fetch available rate plans for a utility."""

async def fetch_rate_detail(
    label: str,
    api_url: str, api_key: str,
) -> URDBRecord:
    """Fetch full rate detail by URDB label."""
```

`UtilitySummary` contains utility name and EIA ID. `RatePlanSummary` contains label, name, startdate, enddate, and description.

### resolver.py

Resolves a single timestamp to import and export rates using the URDB schedule matrices.

```python
def resolve_rate(
    timestamp: int,       # epoch seconds
    tz: str,              # IANA timezone
    record: URDBRecord,
) -> tuple[float, float]:
    """Return (import_rate_per_kwh, export_rate_per_kwh) for a timestamp."""
```

Logic:
1. Convert epoch to local datetime using `tz`
2. Determine month (0-11) and hour (0-23)
3. Select weekday or weekend schedule based on day-of-week
4. Look up period index: `schedule[month][hour]`
5. Import rate: `energyratestructure[period][0]["rate"]` (tier 1)
6. Export rate: `sell[period][0]["rate"]` if `sell` exists, else 0.0
7. Return `(import_rate, export_rate)`

### cost_engine.py

Batch cost calculation over a modeling horizon.

```python
def compute_costs(
    timestamps: list[int],    # epoch seconds, hourly
    power_kw: list[float],    # positive = import, negative = export
    record: URDBRecord,
    tz: str,
    resolution_s: int = 3600,
) -> CostLedger:
    """Compute total costs over a horizon."""
```

Logic:
1. For each timestamp/power pair:
   - `resolve_rate(timestamp, tz, record)` -> `(import_rate, export_rate)`
   - `energy_kwh = power_kw * resolution_s / 3600`
   - If `energy_kwh > 0`: accumulate `import_cost += energy_kwh * import_rate`
   - If `energy_kwh < 0`: accumulate `export_credit += abs(energy_kwh) * export_rate`
2. Count distinct months in the horizon
3. `fixed_charges = months * (fixedmonthlycharge + flat_demand)`
4. `net_cost = import_cost - export_credit + fixed_charges`
5. Return `CostLedger`

---

## Config Store Integration

New methods in `config_store.py` for the simulator-wide rate cache:

```python
# Cache management
get_rates_cache() -> dict                          # Full cache
get_cached_rate(label: str) -> RateCacheEntry | None
cache_rate(label: str, urdb_response: URDBRecord) -> None
delete_cached_rate(label: str) -> None

# Current rate selection (simulator-wide)
get_current_rate_label() -> str | None
set_current_rate_label(label: str) -> None

# OpenEI configuration
get_openei_config() -> OpenEIConfig
set_openei_config(api_url: str, api_key: str) -> None
```

The rates cache file (`data/rates_cache.yaml`) is independent of per-profile config files.

---

## API Endpoints

### Rate Discovery and Fetching

| Method | Path | Description |
|--------|------|-------------|
| GET | `/rates/utilities?lat=&lon=` | Utilities near location (from URDB) |
| GET | `/rates/plans?utility=&sector=Residential` | Rate plans for a utility |
| POST | `/rates/fetch` `{label}` | Fetch full detail from URDB, cache it |
| POST | `/rates/refresh` `{label}` | Re-fetch a cached rate from URDB |

### Cache and Selection

| Method | Path | Description |
|--------|------|-------------|
| GET | `/rates/cache` | List cached rate summaries |
| GET | `/rates/current` | Current rate label + summary |
| PUT | `/rates/current` `{label}` | Set simulator-wide current rate |
| GET | `/rates/detail/{label}` | Full cached record |
| GET | `/rates/attribution/{label}` | Attribution metadata |

### OpenEI Configuration

| Method | Path | Description |
|--------|------|-------------|
| GET | `/rates/openei-config` | Current API URL and key |
| PUT | `/rates/openei-config` `{api_url, api_key}` | Update API URL and key |

### Modeling Integration

The existing `GET /modeling-data` endpoint gains:
- Optional query param: `proposed_rate_label`
- New response fields:

```json
{
  "...existing fields...",
  "before_costs": {
    "import_cost": 185.10,
    "export_credit": 42.30,
    "net_cost": 142.80,
    "fixed_charges": 30.00
  },
  "after_costs": {
    "import_cost": 120.50,
    "export_credit": 55.10,
    "net_cost": 65.40,
    "fixed_charges": 30.00
  }
}
```

- Before costs use the simulator-wide current rate
- After costs use `proposed_rate_label` if provided, otherwise also the current rate
- Cost fields are omitted entirely when no current rate is configured

---

## Modeling View UI

### Layout (top to bottom, above charts)

1. **Getting Started** (existing)
2. **Rate Plan Section** (new)
3. **Charts** (existing)

### Rate Plan Section

**Current Rate display:**
- When no rate selected: "No rate plan selected" with "Configure" button
- When rate selected: "{utility} -- {plan name} ({start_date})" with "Change" and "Refresh" buttons
- Attribution info icon/button next to the display

**Proposed Rate display:**
- When empty: "Using current rate for comparison" with "Set Proposed Rate" button
- When set: same format as current, with "Change" and "Clear" buttons

### OpenEI Dialog

Opened by "Configure", "Change", or "Set Proposed Rate" buttons. Contains:

**Settings section:**
- API URL input (pre-filled with stored value or default `https://api.openei.org/utility_rates`)
- API key input (pre-filled with stored value)
- Save button
- Link to OpenEI signup page for API key

**Rate selection section:**
- Utility dropdown (auto-populated from lat/lon when dialog opens, user can type to filter)
- Rate plan dropdown (populated when utility is selected, shows name + date range)
- "Use This Rate" button (fetches full detail, caches, assigns to the target slot)

### Attribution Popup

Triggered by info icon. Shows:
- Provider: OpenEI Utility Rate Database
- License: CC0 (Creative Commons Zero)
- URDB Label: {label}
- Retrieved: {date}
- Link to OpenEI

### Cost Display on Summary Cards

Existing energy summaries extended with cost when a current rate is configured:

```
Before: Imported 1,234 kWh ($185.10) / Exported 456 kWh ($42.30) -- Net: $142.80
After:  Imported 987 kWh ($120.50) / Exported 612 kWh ($55.10) -- Net: $65.40
Savings: 247 kWh (20%) / $77.40 (54%)
```

When no rate is configured, the existing energy-only display remains unchanged.

---

## Error Handling

| Scenario | Behavior |
|----------|----------|
| URDB fetch fails (network) | Inline error in dialog: "Could not reach OpenEI -- check connection and API key" |
| Invalid API key | "Invalid API key -- verify at openei.org" |
| No utilities for location | "No utilities found near this location -- try adjusting coordinates in Panel Config" |
| No rate plans for utility | "No residential rate plans found for this utility" |
| Rate has no `sell` field | Export credit = $0; note: "No export/sell rate defined in this plan" |
| Rate has tiered rates | Use tier 1 (lowest bracket) for v1 |
| Demand charges present | Include `flatdemandstructure` in fixed_charges |
| Proposed rate is null | After costs use current rate |
| No current rate selected | Cost fields omitted from modeling response |
| Cached label gone on refresh | Keep cached version, warn: "This rate may no longer be available from OpenEI" |

---

## Timezone Handling

- Rate schedules are interpreted in the panel's configured timezone (`panel_config.time_zone`)
- The resolver converts epoch timestamps to local time using this timezone before looking up month/hour in the schedule matrices
- Modeling timestamps are already produced in the panel timezone

---

## Testing Strategy

**Unit tests:**
- `resolver.py`: known URDB record + timestamps -> expected rates for weekday/weekend, different months, different hours
- `cost_engine.py`: synthetic power arrays + known rates -> expected cost ledger; verify import/export split, fixed charges, month counting
- `openei.py`: mock HTTP responses -> verify parsing of utility list, rate plan list, full record

**Integration tests:**
- End-to-end: cache a rate, run `compute_modeling_data` with rate, verify cost fields in response
- Config store: cache/retrieve/refresh cycle

**Manual verification:**
- Select a known California NEM rate, verify costs against manual calculation
- Verify UI flow: configure API key -> select utility -> select rate -> see costs

---

## Future Extensions

These are explicitly deferred and not part of this implementation:

- **Tiered rate calculation** -- use consumption brackets instead of always tier 1
- **Manual rate entry** -- fallback when URDB lacks the user's tariff
- **Demand charge time-series** -- calculate demand charges based on peak kW per billing period
- **Rate plan optimization** -- Monte Carlo over rate plans to recommend the best one
- **ROI and capitalization** -- equipment cost vs energy savings analysis
- **NEM grandfathering rules** -- model legacy vs current net metering agreements

---

## References

- [2026-03-22-energy-optimization-design.md](../../2026-03-22-energy-optimization-design.md) -- Parent design for rate integration and optimization
- OpenEI URDB API: https://openei.org/services/doc/rest/util_rates/?version=3
- OpenEI URDB License: CC0 (Creative Commons Zero)
- [2026-03-28-component-energy-system-design.md](2026-03-28-component-energy-system-design.md) -- Energy system architecture (produces the power arrays consumed by the cost engine)
