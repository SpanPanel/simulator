# Opower Before Cost Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Use actual billed cost from the utility (via HA's Opower integration) for the Before cost in modeling, falling back to URDB calculation when opower is not available.

**Architecture:** A new `ha_api/opower.py` module discovers opower accounts and fetches daily cost statistics via existing HA WebSocket APIs. The rate cache stores the selected opower account. The modeling route checks for opower data before falling back to URDB. The modeling view conditionally shows a "Billing Data" section.

**Tech Stack:** Python 3.14, aiohttp (existing), pytest with AsyncMock. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-03-28-opower-before-cost-design.md`

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `src/span_panel_simulator/ha_api/opower.py` | Create | Opower account discovery and daily cost fetch |
| `tests/test_ha_api/__init__.py` | Create | Test package init |
| `tests/test_ha_api/test_opower.py` | Create | Opower discovery and cost fetch tests |
| `src/span_panel_simulator/rates/cache.py` | Modify | Add opower account get/set methods |
| `tests/test_rates/test_cache.py` | Modify | Add opower account persistence tests |
| `src/span_panel_simulator/dashboard/routes.py` | Modify | Add opower endpoints, modify _attach_costs |
| `src/span_panel_simulator/dashboard/templates/partials/modeling_view.html` | Modify | Conditional Billing Data section, cost display source handling |

---

## Phase 1: Opower Discovery and Cost Fetch

### Task 1: Opower Discovery

**Files:**
- Create: `src/span_panel_simulator/ha_api/opower.py`
- Create: `tests/test_ha_api/__init__.py`
- Create: `tests/test_ha_api/test_opower.py`

- [ ] **Step 1: Create test package and write failing tests**

```bash
mkdir -p tests/test_ha_api
```

Create `tests/test_ha_api/__init__.py`:

```python
```

Create `tests/test_ha_api/test_opower.py`:

```python
"""Tests for opower discovery and cost fetch via HA API."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from span_panel_simulator.ha_api.opower import (
    OpowerAccount,
    async_discover_opower,
    async_get_opower_cost,
)


def _make_client(
    config_entries: list[dict],
    devices: list[dict],
    entities: list[dict],
    statistics: dict | None = None,
) -> AsyncMock:
    """Create a mock HAClient with canned responses."""
    client = AsyncMock()
    client._ws_command_list = AsyncMock(side_effect=_ws_list_router(config_entries, devices, entities))
    if statistics is not None:
        client.async_get_statistics = AsyncMock(return_value=statistics)
    return client


def _ws_list_router(
    config_entries: list[dict],
    devices: list[dict],
    entities: list[dict],
):
    """Return a side_effect function that routes WS list commands."""
    async def route(payload: dict) -> list[dict]:
        cmd_type = payload.get("type", "")
        if cmd_type == "config_entries/get":
            return config_entries
        if cmd_type == "config/device_registry/list":
            return devices
        if cmd_type == "config/entity_registry/list":
            return entities
        return []
    return route


# -- Fixtures: realistic HA registry data --------------------------------

OPOWER_CONFIG_ENTRY = {
    "entry_id": "opower_entry_1",
    "domain": "opower",
    "title": "Pacific Gas and Electric Company (PG&E)",
}

ELEC_DEVICE = {
    "id": "device_elec_1",
    "name": "ELEC account 3021618479",
    "config_entries": ["opower_entry_1"],
    "identifiers": [["opower", "pge_elec_3021618479"]],
}

GAS_DEVICE = {
    "id": "device_gas_1",
    "name": "GAS account 3021618302",
    "config_entries": ["opower_entry_1"],
    "identifiers": [["opower", "pge_gas_3021618302"]],
}

ELEC_ENTITIES = [
    {
        "entity_id": "sensor.opower_pge_elec_cost_to_date",
        "device_id": "device_elec_1",
        "original_device_class": "monetary",
        "entity_category": None,
    },
    {
        "entity_id": "sensor.opower_pge_elec_usage_to_date",
        "device_id": "device_elec_1",
        "original_device_class": "energy",
        "entity_category": None,
    },
    {
        "entity_id": "sensor.opower_pge_elec_forecasted_cost",
        "device_id": "device_elec_1",
        "original_device_class": "monetary",
        "entity_category": None,
    },
    {
        "entity_id": "sensor.opower_pge_elec_forecasted_usage",
        "device_id": "device_elec_1",
        "original_device_class": "energy",
        "entity_category": None,
    },
]

GAS_ENTITIES = [
    {
        "entity_id": "sensor.opower_pge_gas_cost_to_date",
        "device_id": "device_gas_1",
        "original_device_class": "monetary",
        "entity_category": None,
    },
    {
        "entity_id": "sensor.opower_pge_gas_usage_to_date",
        "device_id": "device_gas_1",
        "original_device_class": "energy",
        "entity_category": None,
    },
]


class TestAsyncDiscoverOpower:
    """Discover opower ELEC accounts from HA registries."""

    @pytest.mark.asyncio
    async def test_finds_elec_account(self) -> None:
        client = _make_client(
            config_entries=[OPOWER_CONFIG_ENTRY],
            devices=[ELEC_DEVICE, GAS_DEVICE],
            entities=[*ELEC_ENTITIES, *GAS_ENTITIES],
        )
        accounts = await async_discover_opower(client)
        assert len(accounts) == 1
        assert accounts[0].device_id == "device_elec_1"
        assert accounts[0].utility_name == "Pacific Gas and Electric Company (PG&E)"
        assert accounts[0].account_number == "3021618479"
        assert accounts[0].cost_entity_id == "sensor.opower_pge_elec_cost_to_date"
        assert accounts[0].usage_entity_id == "sensor.opower_pge_elec_usage_to_date"

    @pytest.mark.asyncio
    async def test_ignores_gas_accounts(self) -> None:
        client = _make_client(
            config_entries=[OPOWER_CONFIG_ENTRY],
            devices=[GAS_DEVICE],
            entities=[*GAS_ENTITIES],
        )
        accounts = await async_discover_opower(client)
        assert accounts == []

    @pytest.mark.asyncio
    async def test_no_opower_installed(self) -> None:
        client = _make_client(
            config_entries=[{"entry_id": "other", "domain": "met_eireann", "title": "Weather"}],
            devices=[],
            entities=[],
        )
        accounts = await async_discover_opower(client)
        assert accounts == []

    @pytest.mark.asyncio
    async def test_opower_installed_no_devices(self) -> None:
        client = _make_client(
            config_entries=[OPOWER_CONFIG_ENTRY],
            devices=[],
            entities=[],
        )
        accounts = await async_discover_opower(client)
        assert accounts == []

    @pytest.mark.asyncio
    async def test_multiple_elec_accounts(self) -> None:
        elec2 = {
            "id": "device_elec_2",
            "name": "ELEC account 9999999999",
            "config_entries": ["opower_entry_1"],
            "identifiers": [["opower", "pge_elec_9999999999"]],
        }
        elec2_entities = [
            {
                "entity_id": "sensor.opower_pge_elec_cost_to_date_2",
                "device_id": "device_elec_2",
                "original_device_class": "monetary",
                "entity_category": None,
            },
            {
                "entity_id": "sensor.opower_pge_elec_usage_to_date_2",
                "device_id": "device_elec_2",
                "original_device_class": "energy",
                "entity_category": None,
            },
        ]
        client = _make_client(
            config_entries=[OPOWER_CONFIG_ENTRY],
            devices=[ELEC_DEVICE, elec2],
            entities=[*ELEC_ENTITIES, *elec2_entities],
        )
        accounts = await async_discover_opower(client)
        assert len(accounts) == 2


class TestAsyncGetOpowerCost:
    """Fetch and sum daily cost statistics."""

    @pytest.mark.asyncio
    async def test_sums_daily_cost(self) -> None:
        stats = {
            "sensor.opower_pge_elec_cost_to_date": [
                {"start": "2026-01-01T00:00:00Z", "change": 3.50},
                {"start": "2026-01-02T00:00:00Z", "change": 4.20},
                {"start": "2026-01-03T00:00:00Z", "change": 2.80},
            ]
        }
        client = _make_client([], [], [], statistics=stats)
        result = await async_get_opower_cost(
            client, "sensor.opower_pge_elec_cost_to_date",
            "2026-01-01T00:00:00Z", "2026-01-04T00:00:00Z",
        )
        assert result is not None
        assert result.total_cost == pytest.approx(10.50)
        assert result.days_with_data == 3

    @pytest.mark.asyncio
    async def test_no_data_returns_none(self) -> None:
        stats: dict = {"sensor.opower_pge_elec_cost_to_date": []}
        client = _make_client([], [], [], statistics=stats)
        result = await async_get_opower_cost(
            client, "sensor.opower_pge_elec_cost_to_date",
            "2026-01-01T00:00:00Z", "2026-01-04T00:00:00Z",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_entity_not_in_response(self) -> None:
        stats: dict = {}
        client = _make_client([], [], [], statistics=stats)
        result = await async_get_opower_cost(
            client, "sensor.opower_pge_elec_cost_to_date",
            "2026-01-01T00:00:00Z", "2026-01-04T00:00:00Z",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_skips_entries_without_change(self) -> None:
        stats = {
            "sensor.opower_pge_elec_cost_to_date": [
                {"start": "2026-01-01T00:00:00Z", "change": 3.50},
                {"start": "2026-01-02T00:00:00Z"},  # no change field
                {"start": "2026-01-03T00:00:00Z", "change": None},
                {"start": "2026-01-04T00:00:00Z", "change": 2.00},
            ]
        }
        client = _make_client([], [], [], statistics=stats)
        result = await async_get_opower_cost(
            client, "sensor.opower_pge_elec_cost_to_date",
            "2026-01-01T00:00:00Z", "2026-01-05T00:00:00Z",
        )
        assert result is not None
        assert result.total_cost == pytest.approx(5.50)
        assert result.days_with_data == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_ha_api/test_opower.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'span_panel_simulator.ha_api.opower'`

- [ ] **Step 3: Write the opower module**

Create `src/span_panel_simulator/ha_api/opower.py`:

```python
"""Opower account discovery and cost fetch via HA API.

Composes existing HAClient methods to find opower ELEC accounts
and retrieve daily billed cost statistics from the HA recorder.
Does not modify the HAClient itself.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from span_panel_simulator.ha_api.client import HAClient

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class OpowerAccount:
    """An opower ELEC account discovered from HA."""

    device_id: str
    utility_name: str
    account_number: str
    cost_entity_id: str
    usage_entity_id: str


@dataclass(frozen=True)
class OpowerCostResult:
    """Result of summing daily opower cost statistics."""

    total_cost: float
    days_with_data: int


async def async_discover_opower(client: HAClient) -> list[OpowerAccount]:
    """Find opower ELEC accounts via HA config entries and registries.

    Returns an empty list when opower is not installed or has no
    ELEC accounts.
    """
    # 1. Find opower config entries
    config_entries = await client._ws_command_list({"type": "config_entries/get"})
    opower_entry_ids: set[str] = set()
    opower_titles: dict[str, str] = {}  # entry_id -> title (utility name)
    for entry in config_entries:
        if entry.get("domain") == "opower":
            entry_id = str(entry.get("entry_id", ""))
            opower_entry_ids.add(entry_id)
            opower_titles[entry_id] = str(entry.get("title", ""))

    if not opower_entry_ids:
        return []

    # 2. Find ELEC devices belonging to opower entries
    devices = await client._ws_command_list({"type": "config/device_registry/list"})
    elec_devices: list[tuple[str, str, str]] = []  # (device_id, utility_name, account_number)
    for dev in devices:
        dev_entries = dev.get("config_entries", [])
        if not isinstance(dev_entries, list):
            continue
        matching_entry = None
        for eid in dev_entries:
            if eid in opower_entry_ids:
                matching_entry = str(eid)
                break
        if matching_entry is None:
            continue

        name = str(dev.get("name", ""))
        if "ELEC" not in name.upper():
            continue

        device_id = str(dev.get("id", ""))
        utility_name = opower_titles.get(matching_entry, "")
        # Extract account number from device name like "ELEC account 3021618479"
        parts = name.split()
        account_number = parts[-1] if len(parts) >= 2 else name

        elec_devices.append((device_id, utility_name, account_number))

    if not elec_devices:
        return []

    # 3. Find cost and usage entities for each ELEC device
    entities = await client._ws_command_list({"type": "config/entity_registry/list"})

    accounts: list[OpowerAccount] = []
    for device_id, utility_name, account_number in elec_devices:
        cost_entity_id = ""
        usage_entity_id = ""
        for ent in entities:
            if ent.get("device_id") != device_id:
                continue
            ent_id = str(ent.get("entity_id", ""))
            device_class = str(ent.get("original_device_class", ""))
            # Cost entity: monetary class with "cost_to_date" in name
            if device_class == "monetary" and "cost_to_date" in ent_id:
                cost_entity_id = ent_id
            # Usage entity: energy class with "usage_to_date" in name
            elif device_class == "energy" and "usage_to_date" in ent_id:
                usage_entity_id = ent_id

        if cost_entity_id and usage_entity_id:
            accounts.append(OpowerAccount(
                device_id=device_id,
                utility_name=utility_name,
                account_number=account_number,
                cost_entity_id=cost_entity_id,
                usage_entity_id=usage_entity_id,
            ))

    return accounts


async def async_get_opower_cost(
    client: HAClient,
    cost_entity_id: str,
    start_time: str,
    end_time: str,
) -> OpowerCostResult | None:
    """Sum daily billed cost from opower statistics over a date range.

    Returns an OpowerCostResult with total cost and days covered,
    or None if no data is available.  Uses the ``change`` field from
    daily statistics, which represents the cost delta for each day
    (opower cost entities use ``state_class: total``).
    """
    stats = await client.async_get_statistics(
        [cost_entity_id],
        period="day",
        start_time=start_time,
        end_time=end_time,
    )
    entries = stats.get(cost_entity_id, [])
    if not entries:
        return None

    total = 0.0
    days_with_data = 0
    for entry in entries:
        change = entry.get("change")
        if change is not None:
            total += float(change)
            days_with_data += 1

    if days_with_data == 0:
        return None

    return OpowerCostResult(total_cost=total, days_with_data=days_with_data)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_ha_api/test_opower.py -v`
Expected: All 9 tests PASS

- [ ] **Step 5: Run mypy**

Run: `uv run mypy src/span_panel_simulator/ha_api/opower.py`
Expected: No errors

- [ ] **Step 6: Commit**

```bash
git add src/span_panel_simulator/ha_api/opower.py tests/test_ha_api/
git commit -m "Add opower discovery and daily cost fetch via HA API"
```

---

## Phase 2: Rate Cache Extension

### Task 2: Opower Account Persistence

**Files:**
- Modify: `src/span_panel_simulator/rates/cache.py`
- Modify: `tests/test_rates/test_cache.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_rates/test_cache.py`:

```python
class TestOpowerAccount:
    """Opower account selection persistence."""

    def test_no_opower_account_returns_none(self, tmp_path: Path) -> None:
        cache = RateCache(tmp_path / "rates_cache.yaml")
        assert cache.get_opower_account() is None

    def test_set_and_get_opower_account(self, tmp_path: Path) -> None:
        cache = RateCache(tmp_path / "rates_cache.yaml")
        cache.set_opower_account(
            device_id="device_elec_1",
            utility_name="PG&E",
            account_number="3021618479",
            cost_entity_id="sensor.opower_pge_elec_cost_to_date",
            usage_entity_id="sensor.opower_pge_elec_usage_to_date",
        )
        account = cache.get_opower_account()
        assert account is not None
        assert account["device_id"] == "device_elec_1"
        assert account["utility_name"] == "PG&E"
        assert account["cost_entity_id"] == "sensor.opower_pge_elec_cost_to_date"

    def test_opower_account_persists(self, tmp_path: Path) -> None:
        path = tmp_path / "rates_cache.yaml"
        cache1 = RateCache(path)
        cache1.set_opower_account(
            device_id="device_elec_1",
            utility_name="PG&E",
            account_number="3021618479",
            cost_entity_id="sensor.opower_pge_elec_cost_to_date",
            usage_entity_id="sensor.opower_pge_elec_usage_to_date",
        )
        cache2 = RateCache(path)
        account = cache2.get_opower_account()
        assert account is not None
        assert account["device_id"] == "device_elec_1"

    def test_clear_opower_account(self, tmp_path: Path) -> None:
        cache = RateCache(tmp_path / "rates_cache.yaml")
        cache.set_opower_account(
            device_id="d1", utility_name="U", account_number="A",
            cost_entity_id="c", usage_entity_id="u",
        )
        assert cache.get_opower_account() is not None
        cache.clear_opower_account()
        assert cache.get_opower_account() is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_rates/test_cache.py::TestOpowerAccount -v`
Expected: FAIL with `AttributeError: 'RateCache' object has no attribute 'get_opower_account'`

- [ ] **Step 3: Add opower account methods to RateCache**

Add to `src/span_panel_simulator/rates/cache.py`, after the OpenEI configuration section:

```python
    # -- Opower account selection ----------------------------------------

    def get_opower_account(self) -> dict[str, str] | None:
        """Return the saved opower account selection, or None."""
        account = self._data.get("opower_account")
        if not account or not isinstance(account, dict):
            return None
        if not account.get("device_id"):
            return None
        return {
            "device_id": str(account.get("device_id", "")),
            "utility_name": str(account.get("utility_name", "")),
            "account_number": str(account.get("account_number", "")),
            "cost_entity_id": str(account.get("cost_entity_id", "")),
            "usage_entity_id": str(account.get("usage_entity_id", "")),
        }

    def set_opower_account(
        self,
        device_id: str,
        utility_name: str,
        account_number: str,
        cost_entity_id: str,
        usage_entity_id: str,
    ) -> None:
        """Save the opower account selection."""
        self._data["opower_account"] = {
            "device_id": device_id,
            "utility_name": utility_name,
            "account_number": account_number,
            "cost_entity_id": cost_entity_id,
            "usage_entity_id": usage_entity_id,
        }
        self._save()

    def clear_opower_account(self) -> None:
        """Remove the saved opower account selection."""
        self._data.pop("opower_account", None)
        self._save()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_rates/test_cache.py -v`
Expected: All tests PASS (existing + 4 new)

- [ ] **Step 5: Commit**

```bash
git add src/span_panel_simulator/rates/cache.py tests/test_rates/test_cache.py
git commit -m "Add opower account persistence to rate cache"
```

---

## Phase 3: API Endpoints and Modeling Integration

### Task 3: Opower API Endpoints

**Files:**
- Modify: `src/span_panel_simulator/dashboard/routes.py`

- [ ] **Step 1: Add opower route handlers**

Add imports near top of `routes.py`:

```python
from span_panel_simulator.ha_api.opower import (
    async_discover_opower,
    async_get_opower_cost,
)
```

Add route handlers (near the other rate handlers):

```python
async def handle_get_opower_accounts(request: web.Request) -> web.Response:
    """GET /rates/opower-accounts — discover opower ELEC accounts from HA."""
    ctx = _ctx(request)
    if ctx.ha_client is None:
        return web.json_response([])
    from span_panel_simulator.ha_api.client import HAClient

    if not isinstance(ctx.ha_client, HAClient):
        return web.json_response([])
    try:
        accounts = await async_discover_opower(ctx.ha_client)
    except Exception:
        _LOGGER.exception("Failed to discover opower accounts")
        return web.json_response([])
    return web.json_response([
        {
            "device_id": a.device_id,
            "utility_name": a.utility_name,
            "account_number": a.account_number,
            "cost_entity_id": a.cost_entity_id,
            "usage_entity_id": a.usage_entity_id,
        }
        for a in accounts
    ])


async def handle_get_opower_account(request: web.Request) -> web.Response:
    """GET /rates/opower-account — get saved opower account."""
    account = _rate_cache(request).get_opower_account()
    if account is None:
        return web.json_response({"configured": False})
    return web.json_response({**account, "configured": True})


async def handle_put_opower_account(request: web.Request) -> web.Response:
    """PUT /rates/opower-account — save selected opower account."""
    body = await request.json()
    device_id = body.get("device_id", "").strip()
    if not device_id:
        return web.json_response({"error": "device_id is required"}, status=400)
    _rate_cache(request).set_opower_account(
        device_id=device_id,
        utility_name=body.get("utility_name", ""),
        account_number=body.get("account_number", ""),
        cost_entity_id=body.get("cost_entity_id", ""),
        usage_entity_id=body.get("usage_entity_id", ""),
    )
    return web.json_response({"ok": True})
```

- [ ] **Step 2: Register the routes in setup_routes**

Add after the existing rate routes:

```python
    # Opower account management
    app.router.add_get("/rates/opower-accounts", handle_get_opower_accounts)
    app.router.add_get("/rates/opower-account", handle_get_opower_account)
    app.router.add_put("/rates/opower-account", handle_put_opower_account)
```

- [ ] **Step 3: Commit**

```bash
git add src/span_panel_simulator/dashboard/routes.py
git commit -m "Add opower account discovery and selection endpoints"
```

---

### Task 4: Modify _attach_costs for Opower Before Cost

**Files:**
- Modify: `src/span_panel_simulator/dashboard/routes.py`

- [ ] **Step 1: Update handle_modeling_data to pass HA client**

Change `handle_modeling_data` to pass the HA client through:

```python
async def handle_modeling_data(request: web.Request) -> web.Response:
    """Return time-series for Before/After energy comparison."""
    ctx = _ctx(request)
    horizon_key = request.query.get("horizon", "1mo")
    horizon_hours = _HORIZON_MAP.get(horizon_key, 730)

    config_file = resolve_modeling_config_filename(ctx, request.query.get("config"))
    result = await ctx.get_modeling_data(horizon_hours, config_file)
    if result is None:
        return web.json_response({"error": "No running simulation"}, status=503)
    if "error" in result:
        return web.json_response(result, status=400)

    # Attach cost data if rate cache is available
    cache = _rate_cache(request)
    proposed_label = request.query.get("proposed_rate_label")
    await _attach_costs(result, cache, proposed_label, ctx.ha_client)

    return web.json_response(result)
```

- [ ] **Step 2: Update _attach_costs to check opower first**

Replace the existing `_attach_costs` function:

```python
async def _attach_costs(
    result: dict[str, Any],
    cache: RateCache,
    proposed_rate_label: str | None,
    ha_client: Any,
) -> None:
    """Add before_costs and after_costs to a modeling result dict.

    Before cost priority:
      1. Opower actual billed cost (if HA + opower account configured)
      2. URDB calculation against recorder power arrays
    After cost: always URDB.
    """
    tz_str: str = result["time_zone"]
    ts_list: list[int] = result["timestamps"]

    # -- Before cost -----------------------------------------------------
    before_costs: dict[str, Any] | None = None

    # Try opower first
    opower_acct = cache.get_opower_account()
    if opower_acct is not None and ha_client is not None:
        from span_panel_simulator.ha_api.client import HAClient

        if isinstance(ha_client, HAClient):
            from datetime import datetime, timezone
            from zoneinfo import ZoneInfo

            tz = ZoneInfo(tz_str)
            start_dt = datetime.fromtimestamp(ts_list[0], tz=tz)
            end_dt = datetime.fromtimestamp(ts_list[-1], tz=tz)
            start_iso = start_dt.astimezone(timezone.utc).isoformat()
            end_iso = end_dt.astimezone(timezone.utc).isoformat()

            try:
                opower_result = await async_get_opower_cost(
                    ha_client,
                    opower_acct["cost_entity_id"],
                    start_iso,
                    end_iso,
                )
                if opower_result is not None:
                    # Calculate expected days in horizon for coverage note
                    horizon_days = (end_dt - start_dt).days or 1
                    before_costs = {
                        "source": "opower",
                        "net_cost": round(opower_result.total_cost, 2),
                        "days_with_data": opower_result.days_with_data,
                        "horizon_days": horizon_days,
                    }
            except Exception:
                _LOGGER.exception("Failed to fetch opower cost")

    # Fall back to URDB
    if before_costs is None:
        current_label = cache.get_current_rate_label()
        if current_label is not None:
            current_entry = cache.get_cached_rate(current_label)
            if current_entry is not None:
                costs = compute_costs(ts_list, result["site_power"], current_entry.record, tz_str)
                before_costs = {
                    "source": "urdb",
                    "import_cost": round(costs.import_cost, 2),
                    "export_credit": round(costs.export_credit, 2),
                    "fixed_charges": round(costs.fixed_charges, 2),
                    "net_cost": round(costs.net_cost, 2),
                }

    if before_costs is not None:
        result["before_costs"] = before_costs

    # -- After cost (always URDB) ----------------------------------------
    current_label = cache.get_current_rate_label()
    if current_label is None:
        return
    current_entry = cache.get_cached_rate(current_label)
    if current_entry is None:
        return

    after_record = current_entry.record
    if proposed_rate_label:
        proposed_entry = cache.get_cached_rate(proposed_rate_label)
        if proposed_entry is not None:
            after_record = proposed_entry.record
    after_costs_result = compute_costs(ts_list, result["grid_power"], after_record, tz_str)
    result["after_costs"] = {
        "source": "urdb",
        "import_cost": round(after_costs_result.import_cost, 2),
        "export_credit": round(after_costs_result.export_credit, 2),
        "fixed_charges": round(after_costs_result.fixed_charges, 2),
        "net_cost": round(after_costs_result.net_cost, 2),
    }
```

- [ ] **Step 3: Run full test suite**

Run: `uv run pytest tests/ -x -q`
Expected: All tests PASS

- [ ] **Step 4: Commit**

```bash
git add src/span_panel_simulator/dashboard/routes.py
git commit -m "Use opower actual billed cost for Before when available"
```

---

## Phase 4: Modeling View UI

### Task 5: Conditional Billing Data Section and Cost Source Handling

**Files:**
- Modify: `src/span_panel_simulator/dashboard/templates/partials/modeling_view.html`

- [ ] **Step 1: Add Billing Data HTML section**

Insert before the existing `<!-- Rate plan selection -->` div (before `<div id="modeling-rate-section"`):

```html
  <!-- Opower billing data (conditional, only when HA + opower detected) -->
  <div id="modeling-opower-section" style="display:none; margin-bottom:0.75rem">
    <div style="display:flex; gap:1rem; align-items:center; flex-wrap:wrap">
      <div style="font-weight:500; font-size:0.85rem">Billing Data (Opower)</div>
      <div id="opower-account-display" class="text-muted" style="font-size:0.8rem"></div>
      <button type="button" class="btn btn-xs" id="btn-opower-change" style="display:none">Change</button>
    </div>
  </div>

  <!-- Opower account picker dialog -->
  <div id="opower-picker-overlay" style="display:none; position:fixed; inset:0; background:rgba(0,0,0,0.5); z-index:1000; justify-content:center; align-items:center">
    <div class="card" style="width:min(400px,90vw); margin:2rem">
      <h3 style="margin-top:0">Select Electric Account</h3>
      <div id="opower-picker-list" style="margin-bottom:1rem"></div>
      <div style="text-align:right">
        <button type="button" class="btn btn-xs" id="btn-opower-picker-cancel">Cancel</button>
      </div>
    </div>
  </div>
```

- [ ] **Step 2: Add opower JavaScript**

Add after the existing rate plan state variables:

```javascript
  // -- Opower state --
  var opowerSectionEl = document.getElementById('modeling-opower-section');
  var opowerDisplayEl = document.getElementById('opower-account-display');
  var opowerChangeBtn = document.getElementById('btn-opower-change');
  var opowerPickerOverlay = document.getElementById('opower-picker-overlay');
  var opowerAccounts = [];

  function loadOpowerAccounts() {
    fetch('rates/opower-accounts')
      .then(function(r) { return r.json(); })
      .then(function(accounts) {
        opowerAccounts = accounts;
        if (accounts.length === 0) {
          opowerSectionEl.style.display = 'none';
          return;
        }
        opowerSectionEl.style.display = '';
        // Check if account already saved
        fetch('rates/opower-account')
          .then(function(r) { return r.json(); })
          .then(function(saved) {
            if (saved.configured) {
              showOpowerAccount(saved);
            } else if (accounts.length === 1) {
              // Auto-select single account
              selectOpowerAccount(accounts[0]);
            } else {
              openOpowerPicker();
            }
          });
      })
      .catch(function() {
        opowerSectionEl.style.display = 'none';
      });
  }

  function showOpowerAccount(account) {
    opowerDisplayEl.textContent = account.utility_name + ' \u2014 ELEC ' + account.account_number;
    opowerChangeBtn.style.display = opowerAccounts.length > 1 ? '' : 'none';
  }

  function selectOpowerAccount(account) {
    fetch('rates/opower-account', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(account),
    }).then(function() {
      showOpowerAccount(account);
      closeOpowerPicker();
      fetchModelingData(horizonSelect.value);
    });
  }

  function openOpowerPicker() {
    var list = document.getElementById('opower-picker-list');
    list.innerHTML = '';
    for (var i = 0; i < opowerAccounts.length; i++) {
      var acct = opowerAccounts[i];
      var btn = document.createElement('button');
      btn.className = 'btn btn-xs';
      btn.style.cssText = 'display:block; width:100%; text-align:left; margin-bottom:0.5rem; padding:0.5rem';
      btn.textContent = acct.utility_name + ' \u2014 ELEC ' + acct.account_number;
      btn.dataset.index = String(i);
      btn.addEventListener('click', function() {
        selectOpowerAccount(opowerAccounts[parseInt(this.dataset.index)]);
      });
      list.appendChild(btn);
    }
    opowerPickerOverlay.style.display = 'flex';
  }

  function closeOpowerPicker() {
    opowerPickerOverlay.style.display = 'none';
  }

  opowerChangeBtn.addEventListener('click', openOpowerPicker);
  document.getElementById('btn-opower-picker-cancel').addEventListener('click', closeOpowerPicker);
```

- [ ] **Step 3: Update enterModelingMode to load opower**

In the `enterModelingMode` function, add before `loadCurrentRate()`:

```javascript
    loadOpowerAccounts();
```

In `exitModelingMode`, add:

```javascript
    opowerSectionEl.style.display = 'none';
```

- [ ] **Step 4: Update populateCostCell to handle opower source**

Replace the existing `populateCostCell` function:

```javascript
  function populateCostCell(el, costs) {
    if (!costs) { el.textContent = ''; return; }
    if (costs.source === 'opower') {
      var text = 'Cost: ' + formatDollar(costs.net_cost) + ' (billed)';
      if (costs.days_with_data && costs.horizon_days && costs.days_with_data < costs.horizon_days * 0.9) {
        var months = Math.round(costs.days_with_data / 30);
        var totalMonths = Math.round(costs.horizon_days / 30);
        text += ' (' + months + ' of ' + totalMonths + ' months)';
      }
      el.textContent = text;
    } else {
      el.textContent = formatDollar(costs.import_cost) + ' imp, ' + formatDollar(costs.export_credit)
        + ' exp \u2014 Net: ' + formatDollar(costs.net_cost);
    }
  }
```

- [ ] **Step 5: Update populateCostDiffCell to use net_cost from either source**

The existing `populateCostDiffCell` already uses `beforeCosts.net_cost` and `afterCosts.net_cost` — both opower and URDB include `net_cost`, so no change needed. Verify this is the case.

- [ ] **Step 6: Commit**

```bash
git add src/span_panel_simulator/dashboard/templates/partials/modeling_view.html
git commit -m "Add conditional opower billing data section to modeling view"
```

---

## Phase 5: Utility Pre-filter and Final Wiring

### Task 6: Pre-filter URDB Utility from Opower

**Files:**
- Modify: `src/span_panel_simulator/dashboard/templates/partials/modeling_view.html`

- [ ] **Step 1: Update loadUtilities to accept a default utility name**

Modify the `loadUtilities` function to accept an optional utility name parameter:

```javascript
  function loadUtilities(defaultUtility) {
    var latEl = document.querySelector('[name="latitude"]');
    var lonEl = document.querySelector('[name="longitude"]');
    var lat = (latEl && parseFloat(latEl.value)) || 37.7;
    var lon = (lonEl && parseFloat(lonEl.value)) || -122.4;
    utilitySelect.innerHTML = '<option value="">Loading...</option>';
    planSelect.innerHTML = '<option value="">Select a utility first</option>';
    planSelect.disabled = true;
    useRateBtn.disabled = true;

    fetch('rates/utilities?lat=' + lat + '&lon=' + lon)
      .then(function(r) { return r.json(); })
      .then(function(data) {
        if (data.error) {
          utilitySelect.innerHTML = '<option value="">Error: ' + data.error + '</option>';
          return;
        }
        utilitySelect.innerHTML = '<option value="">Select a utility...</option>';
        var matchIndex = -1;
        for (var i = 0; i < data.length; i++) {
          var opt = document.createElement('option');
          opt.value = data[i].utility_name;
          opt.textContent = data[i].utility_name;
          utilitySelect.appendChild(opt);
          if (defaultUtility && data[i].utility_name.indexOf(defaultUtility) !== -1) {
            matchIndex = i + 1;  // +1 for the placeholder option
          }
        }
        if (matchIndex > 0) {
          utilitySelect.selectedIndex = matchIndex;
          loadRatePlans(utilitySelect.value);
        }
      })
      .catch(function() {
        utilitySelect.innerHTML = '<option value="">Error loading utilities</option>';
      });
  }
```

- [ ] **Step 2: Pass opower utility name when opening rate dialog**

Update the `openRateDialog` function to use the opower utility name:

```javascript
  function openRateDialog(target) {
    rateDialogTarget = target;
    dialogError.style.display = 'none';
    dialogOverlay.style.display = 'flex';
    loadOpenEIConfig();
    // Pre-filter by opower utility if available
    var opowerAcct = null;
    var opowerDisplay = opowerDisplayEl.textContent;
    if (opowerDisplay && opowerSectionEl.style.display !== 'none') {
      // Extract utility name portion (before the em-dash)
      var parts = opowerDisplay.split('\u2014');
      if (parts.length > 0) opowerAcct = parts[0].trim();
    }
    loadUtilities(opowerAcct || null);
  }
```

- [ ] **Step 3: Update existing callers of loadUtilities**

The only other caller of `loadUtilities` is in the `btn-rate-save-config` event listener. Update it:

```javascript
  document.getElementById('btn-rate-save-config').addEventListener('click', function() {
    var url = document.getElementById('rate-api-url').value.trim();
    var key = document.getElementById('rate-api-key').value.trim();
    fetch('rates/openei-config', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({api_url: url, api_key: key}),
    }).then(function() { loadUtilities(null); });
  });
```

- [ ] **Step 4: Run full test suite**

Run: `uv run pytest tests/ -x -q`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/span_panel_simulator/dashboard/templates/partials/modeling_view.html
git commit -m "Pre-filter URDB utility list from opower account name"
```

---

### Task 7: Update ha_api Package Exports

**Files:**
- Modify: `src/span_panel_simulator/ha_api/__init__.py`

- [ ] **Step 1: Add opower exports**

```python
"""Home Assistant API client — dual-mode access for add-on and local development."""

from __future__ import annotations

from span_panel_simulator.ha_api.client import HAClient
from span_panel_simulator.ha_api.manifest import (
    CircuitManifestEntry,
    PanelManifest,
    fetch_all_manifests,
)
from span_panel_simulator.ha_api.opower import (
    OpowerAccount,
    OpowerCostResult,
    async_discover_opower,
    async_get_opower_cost,
)

__all__ = [
    "CircuitManifestEntry",
    "HAClient",
    "OpowerAccount",
    "OpowerCostResult",
    "PanelManifest",
    "async_discover_opower",
    "async_get_opower_cost",
    "fetch_all_manifests",
]
```

- [ ] **Step 2: Run full test suite and type checker**

Run: `uv run pytest tests/ -x -q`
Run: `uv run mypy src/span_panel_simulator/ha_api/`

- [ ] **Step 3: Commit**

```bash
git add src/span_panel_simulator/ha_api/__init__.py
git commit -m "Export opower types and functions from ha_api package"
```

---

## Summary

| Phase | Tasks | What it delivers |
|-------|-------|-----------------|
| 1. Discovery & Fetch | 1 | Opower account discovery + daily cost sum from HA |
| 2. Cache Extension | 2 | Opower account persistence in rates_cache.yaml |
| 3. API & Modeling | 3-4 | Endpoints + _attach_costs with opower priority |
| 4. UI | 5 | Conditional billing data section + source-aware cost display |
| 5. Polish | 6-7 | URDB utility pre-filter + package exports |
