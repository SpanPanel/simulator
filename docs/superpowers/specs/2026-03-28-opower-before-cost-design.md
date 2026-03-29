# Opower Before Cost Integration Design

Use actual billed cost from the utility (via HA's Opower integration) for the Before cost in the modeling view, replacing the URDB-calculated estimate when available.

---

## Scope

**In scope:**
- Discover Opower ELEC accounts via HA config entries and device/entity registry
- Fetch daily cost statistics from HA recorder for a given date range
- Use opower billed cost as Before cost when available
- Fall back to URDB calculation when opower is not available
- Opower account selection UI in modeling view (conditional on HA + opower)
- Pre-filter URDB utility list using opower utility name

**Out of scope:**
- Rate plan inference from opower cost/usage data (future)
- Standalone opower usage (no HA = no opower)
- Gas account support (ELEC only)
- Opower data caching beyond the modeling session
- Export/import cost split from opower (only net cost available)

---

## Before Cost Source Priority

```
1. Opower daily cost sum (HA connected + opower account selected + data available)
2. URDB calculation against recorder power arrays (fallback)
```

After cost is always URDB (proposed rate if set, otherwise current rate). This is unchanged.

---

## Architecture

### HA API Layer

New file `src/span_panel_simulator/ha_api/opower.py` — composes existing `HAClient` methods, does not modify the client itself.

**Discovery:**

```python
@dataclass(frozen=True)
class OpowerAccount:
    """An opower ELEC account discovered from HA."""
    device_id: str
    utility_name: str
    account_number: str
    cost_entity_id: str
    usage_entity_id: str

async def async_discover_opower(client: HAClient) -> list[OpowerAccount]:
    """Find opower ELEC accounts via HA config entries and device registry."""
```

Logic:
1. Query config entries via WebSocket `config_entries/get`
2. Filter for entries with `domain: "opower"`
3. For each opower config entry, find devices via `config/device_registry/list` where `config_entries` contains the entry ID
4. Filter devices to ELEC accounts (device name or identifiers contain "ELEC")
5. For each ELEC device, find entities via `config/entity_registry/list`
6. Identify the cost entity (device_class `monetary` or entity_id containing `cost_to_date`) and usage entity
7. Return `OpowerAccount` for each

**Statistics fetch:**

```python
async def async_get_opower_cost(
    client: HAClient,
    cost_entity_id: str,
    start_time: str,
    end_time: str,
) -> float | None:
    """Sum daily billed cost from opower statistics over a date range.

    Returns the total cost in dollars, or None if no data is available.
    """
```

Logic:
1. Call `client.async_get_statistics([cost_entity_id], period="day", start_time=..., end_time=...)`
2. The opower cost entity uses `state_class: total` — the recorder stores cumulative values. Each daily statistic entry has a `change` field representing the cost delta for that day.
3. Sum the `change` values across the date range to get total cost
4. Return the total, or None if the response is empty or the entity has no data

### Rate Cache Extension

Two new methods on `RateCache`:

```python
def get_opower_account(self) -> dict[str, str] | None:
    """Return the saved opower account selection, or None."""

def set_opower_account(
    self, device_id: str, utility_name: str,
    account_number: str, cost_entity_id: str, usage_entity_id: str,
) -> None:
    """Save the opower account selection."""
```

Stored in `rates_cache.yaml`:

```yaml
opower:
  device_id: "abc123def456"
  utility_name: "Pacific Gas and Electric Company (PG&E)"
  account_number: "3021618479"
  cost_entity_id: "sensor.opower_pge_elec_cost_to_date"
  usage_entity_id: "sensor.opower_pge_elec_usage_to_date"
```

### Modeling Route Changes

**New endpoints:**

| Method | Path | Description |
|--------|------|-------------|
| GET | `/rates/opower-accounts` | Discover opower ELEC accounts from HA |
| PUT | `/rates/opower-account` | Save selected opower account |
| GET | `/rates/opower-account` | Get saved opower account |

**Modified `_attach_costs`:**

Before cost source selection:
1. Check if opower account is saved in rate cache
2. If saved AND HA client is available, fetch daily cost via `async_get_opower_cost` for the horizon date range
3. If opower returns a cost, use it:
   ```json
   "before_costs": {
       "source": "opower",
       "net_cost": 342.00
   }
   ```
4. If opower is not available or returns None, fall back to URDB:
   ```json
   "before_costs": {
       "source": "urdb",
       "import_cost": 185.10,
       "export_credit": 42.30,
       "fixed_charges": 30.00,
       "net_cost": 142.80
   }
   ```

After cost is always URDB (unchanged):
```json
"after_costs": {
    "source": "urdb",
    "import_cost": 120.50,
    "export_credit": 55.10,
    "fixed_charges": 30.00,
    "net_cost": 65.40
}
```

**Opower cost is fetched once** when `_attach_costs` is first called in a modeling session. The result is held on the request or passed through — not re-fetched on every horizon change. (The HA WebSocket call is local, but the data only updates every 48 hours from the utility, so repeated fetching is wasteful.)

---

## Modeling View UI

### Conditional Billing Data Section

Only appears when HA is connected and opower ELEC account(s) are discovered.

**Layout when opower is available:**

```
Billing Data (Opower)
PG&E -- ELEC account 3021618479           [Change]  (only if multiple accounts)
Before cost: actual billed amount

Current Rate                               Proposed Rate
PG&E -- E-TOU-C (2024)  [Change] [Refresh] Using current rate  [Set Proposed Rate]
After cost: calculated from selected rate
```

**Layout when opower is NOT available:**

Same as current — no Billing Data section, URDB for both Before and After.

### On Modeling Mode Entry

1. Fetch `GET /rates/opower-accounts`
2. If accounts found and none saved → auto-select if single account, show picker if multiple
3. If account saved → show it in the Billing Data section
4. If no accounts found → hide Billing Data section entirely

### Before Summary Card Display

**With opower:**
```
Full Horizon    Imported 2.8k kWh / Exported 636 kWh    Cost: $342.00
Visible Range   Imported 1.2k kWh / Exported 280 kWh    --
```

**With URDB (no opower):**
```
Full Horizon    Imported 2.8k kWh ($185.10) / Exported 636 kWh ($42.30)    Net: $142.80
Visible Range   Imported 1.2k kWh / Exported 280 kWh                       --
```

Visible range never shows cost for Before (daily cost data cannot be meaningfully sliced to a sub-horizon range).

### URDB Utility Pre-filter

When the opower account is set, its `utility_name` is used to pre-populate the utility dropdown in the OpenEI dialog. The user can still change it, but the default match saves a step.

---

## Error Handling

| Scenario | Behavior |
|----------|----------|
| HA not connected (standalone) | Opower section hidden, URDB for both |
| HA connected but opower not installed | Opower section hidden, URDB for both |
| Opower installed but no ELEC account | Opower section hidden |
| Opower ELEC found but no statistics yet | Show section, Before cost: "Awaiting billing data", fall back to URDB |
| Statistics don't cover full horizon | Use available data, note: "Cost: $342.00 (3 of 6 months)" |
| Saved device_id no longer in HA | Clear saved selection, re-prompt discovery |
| Multiple ELEC accounts | Show picker |
| Single ELEC account | Auto-select, no picker |

---

## Testing Strategy

**Unit tests (`tests/test_ha_api/test_opower.py`):**
- `async_discover_opower`: mocked config entries + device/entity registry — finds ELEC, ignores GAS, handles no opower
- `async_get_opower_cost`: mocked statistics — daily sum, partial data, empty data returns None

**Unit tests (`tests/test_rates/test_cache.py`):**
- `get_opower_account` / `set_opower_account` — persistence and retrieval

**Route integration:**
- `_attach_costs` with opower source — `before_costs` has `source: "opower"` and `net_cost` only
- `_attach_costs` without opower — existing URDB behavior unchanged
- `GET /rates/opower-accounts` — returns accounts or empty list

---

## Future Extensions

- **Rate plan inference** -- compare opower hourly cost/usage against URDB rate schedules to suggest which plan the user is on
- **Gas account support** -- extend to GAS opower accounts for dual-fuel modeling
- **Standalone opower** -- use opower library directly (with utility credentials) when HA is not available

---

## References

- [2026-03-28-tou-rate-integration-design.md](2026-03-28-tou-rate-integration-design.md) -- Parent rate integration spec
- [Home Assistant Opower Integration](https://www.home-assistant.io/integrations/opower/)
- [tronikos/opower](https://github.com/tronikos/opower) -- Standalone Python opower library
- HA WebSocket API: `config_entries/get`, `config/device_registry/list`, `config/entity_registry/list`, `recorder/statistics_during_period`
