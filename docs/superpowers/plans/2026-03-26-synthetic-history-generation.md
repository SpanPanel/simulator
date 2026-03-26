# Synthetic History Generation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enable cloned panels to replay synthetic historical data from a companion SQLite file, matching HA recorder schema, so panels work without a live HA instance.

**Architecture:** A new `SqliteHistoryProvider` implements the existing `HistoryProvider` protocol, reading from a local SQLite file with HA-compatible schema. A `SyntheticHistoryGenerator` produces 1 year of hourly + 10 days of 5-minute data per circuit using the existing modulation infrastructure (solar, weather, HVAC, cycling, BESS). At startup, `app.py` checks for a companion `_history.db` file alongside the YAML config and uses it when no HA client is available.

**Tech Stack:** Python 3.12, sqlite3 (stdlib), aiohttp (weather fetch), pytest, mypy strict mode

---

## File Structure

| Action | Path | Responsibility |
|--------|------|---------------|
| Create | `src/span_panel_simulator/sqlite_history.py` | `SqliteHistoryProvider` — reads SQLite files via `HistoryProvider` protocol |
| Create | `src/span_panel_simulator/history_generator.py` | `SyntheticHistoryGenerator` — builds companion SQLite from config YAML |
| Modify | `src/span_panel_simulator/config_types.py` | Add `history_db: NotRequired[str]` to `PanelConfig` |
| Modify | `src/span_panel_simulator/app.py` | Add SQLite provider path in `_load_recorder_data()` |
| Modify | `src/span_panel_simulator/dashboard/routes.py` | Invoke generator after clone-from-panel |
| Modify | `src/span_panel_simulator/__main__.py` | No changes needed (generator is invoked by dashboard/clone, not CLI entry) |
| Create | `tests/test_sqlite_history.py` | Tests for `SqliteHistoryProvider` |
| Create | `tests/test_history_generator.py` | Tests for `SyntheticHistoryGenerator` |
| Create | `tests/test_sqlite_app_integration.py` | Integration test: SQLite provider used at panel startup |

---

### Task 1: SqliteHistoryProvider

**Files:**
- Create: `src/span_panel_simulator/sqlite_history.py`
- Create: `tests/test_sqlite_history.py`
- Modify: `tests/test_history.py` (add protocol conformance test)

- [ ] **Step 1: Write failing test — protocol conformance**

Add to `tests/test_history.py`:

```python
from span_panel_simulator.sqlite_history import SqliteHistoryProvider


class TestSqliteHistoryProvider:
    def test_satisfies_protocol(self) -> None:
        from span_panel_simulator.history import HistoryProvider

        provider: HistoryProvider = SqliteHistoryProvider(":memory:")
        assert hasattr(provider, "async_get_statistics")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_history.py::TestSqliteHistoryProvider::test_satisfies_protocol -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'span_panel_simulator.sqlite_history'`

- [ ] **Step 3: Write minimal SqliteHistoryProvider skeleton**

Create `src/span_panel_simulator/sqlite_history.py`:

```python
"""SQLite-backed history provider — reads companion _history.db files.

Implements the ``HistoryProvider`` protocol by querying ``statistics`` and
``statistics_short_term`` tables in the HA-compatible schema written by
``SyntheticHistoryGenerator``.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

# SQL schema for the companion history database.
SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS statistics_meta (
    id INTEGER PRIMARY KEY,
    statistic_id TEXT UNIQUE NOT NULL,
    source TEXT NOT NULL DEFAULT 'simulator',
    unit_of_measurement TEXT,
    has_mean INTEGER DEFAULT 1,
    has_sum INTEGER DEFAULT 0,
    name TEXT
);

CREATE TABLE IF NOT EXISTS statistics (
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

CREATE TABLE IF NOT EXISTS statistics_short_term (
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
"""

# Period name -> table name mapping
_PERIOD_TABLE: dict[str, str] = {
    "hour": "statistics",
    "5minute": "statistics_short_term",
}


class SqliteHistoryProvider:
    """Read-only history provider backed by a local SQLite file.

    The database uses HA's recorder schema: ``statistics_meta`` maps
    statistic IDs to integer keys, and ``statistics`` / ``statistics_short_term``
    store hourly and 5-minute aggregated rows respectively.

    Timestamps are stored as epoch seconds (``start_ts`` column) and returned
    in the same format that ``RecorderDataSource._parse_timestamp`` expects.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)

    async def async_get_statistics(
        self,
        statistic_ids: list[str],
        *,
        period: str = "hour",
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> dict[str, list[dict[str, object]]]:
        """Query statistics from the SQLite database.

        Returns data in the same format as the HA provider: a dict mapping
        statistic IDs to lists of records with ``start``, ``mean``, ``min``,
        ``max`` fields.
        """
        table = _PERIOD_TABLE.get(period)
        if table is None:
            return {}

        if not statistic_ids:
            return {}

        result: dict[str, list[dict[str, object]]] = {}

        try:
            con = sqlite3.connect(self._db_path)
        except sqlite3.Error:
            _LOGGER.warning("Could not open history database: %s", self._db_path)
            return {}

        try:
            cur = con.cursor()

            # Resolve statistic_id -> metadata_id
            placeholders = ",".join("?" for _ in statistic_ids)
            cur.execute(
                f"SELECT id, statistic_id FROM statistics_meta "  # noqa: S608
                f"WHERE statistic_id IN ({placeholders})",
                statistic_ids,
            )
            meta_rows = cur.fetchall()
            meta_map: dict[int, str] = {row[0]: row[1] for row in meta_rows}

            if not meta_map:
                return {}

            for metadata_id, statistic_id in meta_map.items():
                query = (
                    f"SELECT start_ts, mean, min, max FROM {table} "  # noqa: S608
                    f"WHERE metadata_id = ?"
                )
                params: list[object] = [metadata_id]

                if start_time is not None:
                    # start_time is ISO 8601 string; convert to epoch for comparison
                    from datetime import UTC, datetime

                    try:
                        dt = datetime.fromisoformat(start_time)
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=UTC)
                        query += " AND start_ts >= ?"
                        params.append(dt.timestamp())
                    except ValueError:
                        pass

                if end_time is not None:
                    from datetime import UTC, datetime

                    try:
                        dt = datetime.fromisoformat(end_time)
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=UTC)
                        query += " AND start_ts <= ?"
                        params.append(dt.timestamp())
                    except ValueError:
                        pass

                query += " ORDER BY start_ts"
                cur.execute(query, params)

                records: list[dict[str, object]] = []
                for row in cur.fetchall():
                    records.append({
                        "start": row[0],  # epoch seconds (float)
                        "mean": row[1],
                        "min": row[2],
                        "max": row[3],
                    })

                if records:
                    result[statistic_id] = records
        finally:
            con.close()

        return result
```

- [ ] **Step 4: Run protocol conformance test**

Run: `python -m pytest tests/test_history.py::TestSqliteHistoryProvider -v`
Expected: PASS

- [ ] **Step 5: Write test — reads hourly data from pre-populated DB**

Create `tests/test_sqlite_history.py`:

```python
"""Tests for SqliteHistoryProvider."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from span_panel_simulator.sqlite_history import SCHEMA_SQL, SqliteHistoryProvider


def _create_test_db(path: Path, entity_id: str, rows: list[tuple[float, float]]) -> None:
    """Create a test SQLite DB with statistics_meta and statistics rows."""
    con = sqlite3.connect(str(path))
    con.executescript(SCHEMA_SQL)
    con.execute(
        "INSERT INTO statistics_meta (id, statistic_id, unit_of_measurement) "
        "VALUES (1, ?, 'W')",
        (entity_id,),
    )
    for start_ts, mean in rows:
        con.execute(
            "INSERT INTO statistics (metadata_id, created_ts, start_ts, mean, min, max) "
            "VALUES (1, ?, ?, ?, ?, ?)",
            (start_ts, start_ts, mean, mean * 0.9, mean * 1.1),
        )
    con.commit()
    con.close()


class TestSqliteHistoryProvider:
    @pytest.mark.asyncio
    async def test_reads_hourly_data(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        entity = "sensor.sim_panel_kitchen_power"
        rows = [(1000.0, 500.0), (4600.0, 600.0), (8200.0, 550.0)]
        _create_test_db(db_path, entity, rows)

        provider = SqliteHistoryProvider(db_path)
        result = await provider.async_get_statistics([entity], period="hour")

        assert entity in result
        assert len(result[entity]) == 3
        assert result[entity][0]["start"] == 1000.0
        assert result[entity][0]["mean"] == 500.0

    @pytest.mark.asyncio
    async def test_reads_short_term_data(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        entity = "sensor.sim_panel_kitchen_power"
        con = sqlite3.connect(str(db_path))
        con.executescript(SCHEMA_SQL)
        con.execute(
            "INSERT INTO statistics_meta (id, statistic_id) VALUES (1, ?)",
            (entity,),
        )
        con.execute(
            "INSERT INTO statistics_short_term "
            "(metadata_id, created_ts, start_ts, mean, min, max) "
            "VALUES (1, 1000.0, 1000.0, 200.0, 180.0, 220.0)",
        )
        con.commit()
        con.close()

        provider = SqliteHistoryProvider(db_path)
        result = await provider.async_get_statistics([entity], period="5minute")

        assert entity in result
        assert len(result[entity]) == 1
        assert result[entity][0]["mean"] == 200.0

    @pytest.mark.asyncio
    async def test_filters_by_start_time(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        entity = "sensor.test_power"
        rows = [(1000.0, 100.0), (5000.0, 200.0), (9000.0, 300.0)]
        _create_test_db(db_path, entity, rows)

        provider = SqliteHistoryProvider(db_path)
        result = await provider.async_get_statistics(
            [entity],
            period="hour",
            start_time="1970-01-01T01:00:00+00:00",  # 3600 epoch
        )

        assert entity in result
        assert len(result[entity]) == 2  # only 5000 and 9000

    @pytest.mark.asyncio
    async def test_unknown_entity_returns_empty(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        _create_test_db(db_path, "sensor.real", [(1000.0, 100.0)])

        provider = SqliteHistoryProvider(db_path)
        result = await provider.async_get_statistics(
            ["sensor.does_not_exist"], period="hour"
        )

        assert result == {}

    @pytest.mark.asyncio
    async def test_missing_db_returns_empty(self, tmp_path: Path) -> None:
        provider = SqliteHistoryProvider(tmp_path / "nonexistent.db")
        result = await provider.async_get_statistics(["sensor.x"], period="hour")
        assert result == {}

    @pytest.mark.asyncio
    async def test_unknown_period_returns_empty(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        _create_test_db(db_path, "sensor.x", [(1000.0, 100.0)])

        provider = SqliteHistoryProvider(db_path)
        result = await provider.async_get_statistics(["sensor.x"], period="month")
        assert result == {}
```

- [ ] **Step 6: Run all SqliteHistoryProvider tests**

Run: `python -m pytest tests/test_sqlite_history.py -v`
Expected: All PASS

- [ ] **Step 7: Run mypy**

Run: `python -m mypy src/span_panel_simulator/sqlite_history.py --strict`
Expected: PASS with no errors

- [ ] **Step 8: Commit**

```bash
git add src/span_panel_simulator/sqlite_history.py tests/test_sqlite_history.py tests/test_history.py
git commit -m "feat: add SqliteHistoryProvider for local history replay"
```

---

### Task 2: Add `history_db` to PanelConfig TypedDict

**Files:**
- Modify: `src/span_panel_simulator/config_types.py:22-33`

- [ ] **Step 1: Add the field**

In `src/span_panel_simulator/config_types.py`, add `history_db` to `PanelConfig`:

```python
class PanelConfig(TypedDict):
    """Panel configuration."""

    serial_number: str
    total_tabs: int
    main_size: int  # Main breaker size in Amps
    latitude: NotRequired[float]  # degrees north, default 37.7
    longitude: NotRequired[float]  # degrees east, default -122.4
    soc_shed_threshold: NotRequired[float]  # SOC % below which SOC_THRESHOLD circuits are shed
    postal_code: NotRequired[str]  # ZIP / postal code, default "94103"
    time_zone: NotRequired[str]  # IANA timezone, default "America/Los_Angeles"
    history_db: NotRequired[str]  # path to companion SQLite history file (overrides convention)
```

- [ ] **Step 2: Run mypy on config_types**

Run: `python -m mypy src/span_panel_simulator/config_types.py --strict`
Expected: PASS

- [ ] **Step 3: Run existing tests to confirm no regressions**

Run: `python -m pytest tests/ -x -q`
Expected: All PASS

- [ ] **Step 4: Commit**

```bash
git add src/span_panel_simulator/config_types.py
git commit -m "feat: add history_db field to PanelConfig for explicit SQLite path"
```

---

### Task 3: Wire SqliteHistoryProvider into app.py startup

**Files:**
- Modify: `src/span_panel_simulator/app.py:366-419`
- Create: `tests/test_sqlite_app_integration.py`

- [ ] **Step 1: Write failing integration test**

Create `tests/test_sqlite_app_integration.py`:

```python
"""Integration test: SqliteHistoryProvider used at panel startup."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from span_panel_simulator.recorder import RecorderDataSource
from span_panel_simulator.sqlite_history import SCHEMA_SQL, SqliteHistoryProvider


class TestSqliteRecorderRoundTrip:
    """Verify that SqliteHistoryProvider feeds RecorderDataSource correctly."""

    @pytest.mark.asyncio
    async def test_load_and_get_power(self, tmp_path: Path) -> None:
        """Generate rows, load via SqliteHistoryProvider, query via RecorderDataSource."""
        db_path = tmp_path / "panel_history.db"
        entity = "sensor.sim_panel_kitchen_power"

        # Create DB with 24 hourly rows
        con = sqlite3.connect(str(db_path))
        con.executescript(SCHEMA_SQL)
        con.execute(
            "INSERT INTO statistics_meta (id, statistic_id, unit_of_measurement) "
            "VALUES (1, ?, 'W')",
            (entity,),
        )
        base_ts = 1_700_000_000.0  # ~Nov 2023
        for i in range(24):
            ts = base_ts + i * 3600
            mean = 500.0 + i * 10.0
            con.execute(
                "INSERT INTO statistics (metadata_id, created_ts, start_ts, mean, min, max) "
                "VALUES (1, ?, ?, ?, ?, ?)",
                (ts, ts, mean, mean * 0.9, mean * 1.1),
            )
        con.commit()
        con.close()

        provider = SqliteHistoryProvider(db_path)
        recorder = RecorderDataSource()
        loaded = await recorder.load(provider, [entity], lookback_days=365)

        assert loaded == 1
        assert recorder.has_entity(entity)

        # Query a timestamp in the middle — should interpolate
        mid_ts = base_ts + 12 * 3600
        power = recorder.get_power(entity, mid_ts)
        assert power is not None
        assert 600.0 < power < 640.0  # 500 + 12*10 = 620, interpolation close

    @pytest.mark.asyncio
    async def test_no_db_file_returns_none(self, tmp_path: Path) -> None:
        """When companion DB does not exist, provider returns empty."""
        provider = SqliteHistoryProvider(tmp_path / "missing.db")
        recorder = RecorderDataSource()
        loaded = await recorder.load(provider, ["sensor.x"], lookback_days=365)
        assert loaded == 0
```

- [ ] **Step 2: Run test to verify it passes (provider already works)**

Run: `python -m pytest tests/test_sqlite_app_integration.py -v`
Expected: PASS (this validates the round-trip, no app.py changes needed yet)

- [ ] **Step 3: Modify `_load_recorder_data` in app.py**

Replace the existing `_load_recorder_data` method in `src/span_panel_simulator/app.py` (lines 366-419). The new version checks for a companion SQLite file when no HA client is available:

```python
    async def _load_recorder_data(self, config_path: Path) -> RecorderDataSource | None:
        """Create and populate a RecorderDataSource from config + history source.

        Source selection:
          1. If HA client is available and config has recorder_entity mappings → HA provider
          2. If a companion ``_history.db`` file exists (or ``history_db`` is set) → SQLite provider
          3. Otherwise → None (engine uses synthetic per-tick generation)

        Failures are logged and swallowed so the panel still starts in synthetic mode.
        """
        try:
            raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        except Exception:
            return None

        if not isinstance(raw, dict):
            return None

        templates = raw.get("circuit_templates")
        if not isinstance(templates, dict):
            return None

        entity_ids: list[str] = []
        for tmpl in templates.values():
            if isinstance(tmpl, dict):
                entity_id = tmpl.get("recorder_entity")
                if isinstance(entity_id, str) and entity_id:
                    entity_ids.append(entity_id)

        if not entity_ids:
            return None

        # Source 1: HA client available → use HA provider
        if self._ha_client is not None:
            _LOGGER.info(
                "Loading recorder data for %s (%d entities) from HA",
                config_path.name,
                len(entity_ids),
            )
            recorder = RecorderDataSource()
            try:
                loaded = await recorder.load(self._ha_client, entity_ids)
            except Exception:
                _LOGGER.warning(
                    "Recorder data loading failed for %s — using synthetic",
                    config_path.name,
                    exc_info=True,
                )
                return None

            if loaded == 0:
                _LOGGER.warning(
                    "Recorder returned no data for %s — using synthetic",
                    config_path.name,
                )
            return recorder if loaded > 0 else None

        # Source 2: companion SQLite file
        db_path = self._resolve_history_db(config_path, raw)
        if db_path is not None:
            from span_panel_simulator.sqlite_history import SqliteHistoryProvider

            _LOGGER.info(
                "Loading recorder data for %s (%d entities) from %s",
                config_path.name,
                len(entity_ids),
                db_path.name,
            )
            provider = SqliteHistoryProvider(db_path)
            recorder = RecorderDataSource()
            try:
                loaded = await recorder.load(provider, entity_ids, lookback_days=365)
            except Exception:
                _LOGGER.warning(
                    "SQLite history loading failed for %s — using synthetic",
                    config_path.name,
                    exc_info=True,
                )
                return None

            if loaded == 0:
                _LOGGER.warning(
                    "SQLite history returned no data for %s — using synthetic",
                    config_path.name,
                )
            return recorder if loaded > 0 else None

        return None

    @staticmethod
    def _resolve_history_db(config_path: Path, raw: dict[str, object]) -> Path | None:
        """Find the companion SQLite history DB for a config file.

        Checks explicit ``panel_config.history_db`` first, then falls back
        to the convention: ``<config_stem>_history.db`` in the same directory.
        """
        panel_config = raw.get("panel_config")
        if isinstance(panel_config, dict):
            explicit = panel_config.get("history_db")
            if isinstance(explicit, str) and explicit:
                explicit_path = Path(explicit)
                if not explicit_path.is_absolute():
                    explicit_path = config_path.parent / explicit_path
                if explicit_path.exists():
                    return explicit_path

        convention_path = config_path.with_name(config_path.stem + "_history.db")
        if convention_path.exists():
            return convention_path

        return None
```

- [ ] **Step 4: Write test for _resolve_history_db**

Add to `tests/test_sqlite_app_integration.py`:

```python
from span_panel_simulator.app import SimulatorApp


class TestResolveHistoryDb:
    def test_convention_path(self, tmp_path: Path) -> None:
        config_path = tmp_path / "my_panel.yaml"
        config_path.write_text("panel_config:\n  serial_number: x\n")
        db_path = tmp_path / "my_panel_history.db"
        db_path.write_text("")  # just needs to exist

        result = SimulatorApp._resolve_history_db(config_path, {})
        assert result == db_path

    def test_explicit_path(self, tmp_path: Path) -> None:
        config_path = tmp_path / "my_panel.yaml"
        config_path.write_text("")
        db_path = tmp_path / "custom.db"
        db_path.write_text("")

        raw = {"panel_config": {"history_db": "custom.db"}}
        result = SimulatorApp._resolve_history_db(config_path, raw)
        assert result == db_path

    def test_no_db_returns_none(self, tmp_path: Path) -> None:
        config_path = tmp_path / "my_panel.yaml"
        config_path.write_text("")

        result = SimulatorApp._resolve_history_db(config_path, {})
        assert result is None

    def test_explicit_overrides_convention(self, tmp_path: Path) -> None:
        config_path = tmp_path / "my_panel.yaml"
        config_path.write_text("")
        # Both exist
        (tmp_path / "my_panel_history.db").write_text("")
        custom = tmp_path / "custom.db"
        custom.write_text("")

        raw = {"panel_config": {"history_db": "custom.db"}}
        result = SimulatorApp._resolve_history_db(config_path, raw)
        assert result == custom
```

- [ ] **Step 5: Run all integration tests**

Run: `python -m pytest tests/test_sqlite_app_integration.py -v`
Expected: All PASS

- [ ] **Step 6: Run full test suite**

Run: `python -m pytest tests/ -x -q`
Expected: All PASS

- [ ] **Step 7: Run mypy on app.py**

Run: `python -m mypy src/span_panel_simulator/app.py --strict`
Expected: PASS (or only pre-existing issues)

- [ ] **Step 8: Commit**

```bash
git add src/span_panel_simulator/app.py tests/test_sqlite_app_integration.py
git commit -m "feat: wire SqliteHistoryProvider into panel startup"
```

---

### Task 4: SyntheticHistoryGenerator — core generation logic

**Files:**
- Create: `src/span_panel_simulator/history_generator.py`
- Create: `tests/test_history_generator.py`

This is the largest task. The generator reads a panel config YAML, computes per-circuit power for every time step, and writes the companion SQLite DB.

- [ ] **Step 1: Write failing test — generator produces correct row counts**

Create `tests/test_history_generator.py`:

```python
"""Tests for SyntheticHistoryGenerator."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import yaml

from span_panel_simulator.history_generator import SyntheticHistoryGenerator

# Minimal config with one consumer circuit
_MINIMAL_CONFIG: dict[str, object] = {
    "panel_config": {
        "serial_number": "sim-test-gen",
        "total_tabs": 16,
        "main_size": 200,
        "latitude": 37.7,
        "longitude": -122.4,
    },
    "circuit_templates": {
        "kitchen": {
            "energy_profile": {
                "mode": "consumer",
                "power_range": [0, 2400],
                "typical_power": 800.0,
                "power_variation": 0.1,
            },
            "relay_behavior": "controllable",
            "priority": "MUST_HAVE",
            "recorder_entity": "sensor.sim_test_gen_kitchen_power",
        },
    },
    "circuits": [
        {"id": "circuit_1", "name": "Kitchen", "template": "kitchen", "tabs": [1]},
    ],
    "unmapped_tabs": [2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16],
    "simulation_params": {
        "update_interval": 5,
        "time_acceleration": 1.0,
        "noise_factor": 0.02,
        "enable_realistic_behaviors": True,
    },
}


class TestSyntheticHistoryGenerator:
    @pytest.mark.asyncio
    async def test_generates_correct_tables(self, tmp_path: Path) -> None:
        config_path = tmp_path / "test_panel.yaml"
        config_path.write_text(yaml.dump(_MINIMAL_CONFIG))

        gen = SyntheticHistoryGenerator()
        db_path = await gen.generate(config_path)

        assert db_path.exists()
        assert db_path.name == "test_panel_history.db"

        con = sqlite3.connect(str(db_path))
        # Check statistics_meta has the entity
        meta = con.execute(
            "SELECT statistic_id FROM statistics_meta"
        ).fetchall()
        assert len(meta) == 1
        assert meta[0][0] == "sensor.sim_test_gen_kitchen_power"

        # Check hourly rows exist (roughly 365 days * 24 hours - 10 days * 24)
        hourly_count = con.execute(
            "SELECT COUNT(*) FROM statistics"
        ).fetchone()[0]
        # ~355 days * 24 = 8520, allow some tolerance
        assert hourly_count > 8000
        assert hourly_count < 9000

        # Check short-term rows exist (10 days * 24 hours * 12 per hour)
        short_count = con.execute(
            "SELECT COUNT(*) FROM statistics_short_term"
        ).fetchone()[0]
        # 10 days * 288 five-minute slots = 2880
        assert short_count > 2800
        assert short_count < 3000

        con.close()

    @pytest.mark.asyncio
    async def test_deterministic_output(self, tmp_path: Path) -> None:
        """Same config + anchor produces identical DBs."""
        config_path = tmp_path / "test_panel.yaml"
        config_path.write_text(yaml.dump(_MINIMAL_CONFIG))

        anchor = 1_700_000_000.0  # fixed anchor

        gen = SyntheticHistoryGenerator()
        db1 = await gen.generate(config_path, anchor_time=anchor)

        # Rename to avoid overwrite
        db1_copy = tmp_path / "db1.db"
        db1.rename(db1_copy)

        db2 = await gen.generate(config_path, anchor_time=anchor)

        con1 = sqlite3.connect(str(db1_copy))
        con2 = sqlite3.connect(str(db2))

        rows1 = con1.execute(
            "SELECT start_ts, mean FROM statistics ORDER BY start_ts"
        ).fetchall()
        rows2 = con2.execute(
            "SELECT start_ts, mean FROM statistics ORDER BY start_ts"
        ).fetchall()

        assert rows1 == rows2
        con1.close()
        con2.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_history_generator.py::TestSyntheticHistoryGenerator::test_generates_correct_tables -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement SyntheticHistoryGenerator**

Create `src/span_panel_simulator/history_generator.py`:

```python
"""Synthetic history generator — builds companion SQLite databases.

Given a panel config YAML, generates a year of synthetic power statistics
matching HA's recorder schema.  The output SQLite file can be read by
``SqliteHistoryProvider`` and fed to ``RecorderDataSource`` for replay.

Time windows:
  - ``[anchor - 1 year, anchor - 10 days]``: hourly rows in ``statistics``
  - ``[anchor - 10 days, anchor]``: 5-minute rows in ``statistics_short_term``

Uses the same modulation infrastructure as the live simulation engine:
solar curves, weather degradation, HVAC seasonal model, time-of-day
profiles, cycling patterns, and monthly factors.
"""

from __future__ import annotations

import hashlib
import logging
import math
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import yaml

from span_panel_simulator.hvac import hvac_seasonal_factor
from span_panel_simulator.solar import daily_weather_factor, solar_production_factor
from span_panel_simulator.sqlite_history import SCHEMA_SQL
from span_panel_simulator.weather import fetch_historical_weather, get_cached_weather

if TYPE_CHECKING:
    from span_panel_simulator.config_types import (
        CircuitTemplateExtended,
        SimulationConfig,
    )

_LOGGER = logging.getLogger(__name__)

_SECONDS_PER_HOUR = 3600
_SECONDS_PER_5MIN = 300
_DAYS_SHORT_TERM = 10
_DAYS_TOTAL = 365


def _deterministic_noise(panel_serial: str, circuit_id: str, start_ts: float) -> float:
    """Deterministic per-row noise in [-1, 1], seeded from identity + timestamp."""
    raw = f"{panel_serial}:{circuit_id}:{start_ts}".encode()
    h = int(hashlib.sha256(raw).hexdigest()[:8], 16)
    return (h % 20000 - 10000) / 10000.0


def _resolve_timezone(config: dict[str, object]) -> ZoneInfo:
    """Resolve panel timezone from config, matching engine logic."""
    panel = config.get("panel_config", {})
    if not isinstance(panel, dict):
        return ZoneInfo("America/Los_Angeles")

    explicit = panel.get("time_zone")
    if isinstance(explicit, str) and explicit:
        try:
            return ZoneInfo(explicit)
        except (KeyError, ValueError):
            pass

    lat = panel.get("latitude")
    lon = panel.get("longitude")
    if lat is not None and lon is not None:
        from timezonefinder import TimezoneFinder

        tz_name = TimezoneFinder().timezone_at(lat=float(lat), lng=float(lon))
        if tz_name is not None:
            return ZoneInfo(tz_name)

    return ZoneInfo("America/Los_Angeles")


class SyntheticHistoryGenerator:
    """Generate companion SQLite history databases from panel config YAMLs."""

    async def generate(
        self,
        config_path: Path,
        *,
        anchor_time: float | None = None,
    ) -> Path:
        """Generate the companion history DB for a config file.

        Args:
            config_path: Path to the panel YAML config.
            anchor_time: Unix epoch for the "now" end of the window.
                Defaults to current time.

        Returns:
            Path to the generated ``_history.db`` file.
        """
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            msg = f"Invalid config: {config_path}"
            raise ValueError(msg)

        anchor = anchor_time if anchor_time is not None else time.time()
        db_path = config_path.with_name(config_path.stem + "_history.db")

        panel_config = raw.get("panel_config", {})
        if not isinstance(panel_config, dict):
            msg = "Missing panel_config"
            raise ValueError(msg)

        serial = str(panel_config.get("serial_number", "unknown"))
        lat = float(panel_config.get("latitude", 37.7))
        lon = float(panel_config.get("longitude", -122.4))
        tz = _resolve_timezone(raw)
        noise_factor = float(
            raw.get("simulation_params", {}).get("noise_factor", 0.02)
            if isinstance(raw.get("simulation_params"), dict)
            else 0.02
        )

        # Fetch weather data for solar degradation (best-effort)
        weather_monthly: dict[int, float] | None = None
        cached = get_cached_weather(lat, lon)
        if cached is not None:
            weather_monthly = cached.monthly_factors
        else:
            try:
                wd = await fetch_historical_weather(lat, lon)
                weather_monthly = wd.monthly_factors
            except Exception:
                _LOGGER.debug("Weather fetch failed; using deterministic model", exc_info=True)

        # Collect circuits with recorder_entity mappings
        templates = raw.get("circuit_templates", {})
        if not isinstance(templates, dict):
            templates = {}

        circuits_to_generate: list[tuple[str, str, dict[str, object]]] = []
        for tmpl_name, tmpl in templates.items():
            if not isinstance(tmpl, dict):
                continue
            entity = tmpl.get("recorder_entity")
            if isinstance(entity, str) and entity:
                circuits_to_generate.append((tmpl_name, entity, tmpl))

        if not circuits_to_generate:
            _LOGGER.warning("No recorder_entity mappings in %s — nothing to generate", config_path.name)
            # Still create DB with schema for consistency
            con = sqlite3.connect(str(db_path))
            con.executescript(SCHEMA_SQL)
            con.close()
            return db_path

        # Compute time boundaries
        hourly_start = anchor - _DAYS_TOTAL * 86400
        short_term_start = anchor - _DAYS_SHORT_TERM * 86400
        hourly_end = short_term_start  # hourly stops where short-term begins

        # Generate
        _LOGGER.info(
            "Generating synthetic history for %s: %d circuits, anchor=%s",
            config_path.name,
            len(circuits_to_generate),
            datetime.fromtimestamp(anchor, tz=UTC).isoformat(),
        )

        con = sqlite3.connect(str(db_path))
        con.executescript(SCHEMA_SQL)

        # Clear any existing data (regeneration case)
        con.execute("DELETE FROM statistics")
        con.execute("DELETE FROM statistics_short_term")
        con.execute("DELETE FROM statistics_meta")

        try:
            for idx, (tmpl_name, entity_id, tmpl) in enumerate(circuits_to_generate, start=1):
                con.execute(
                    "INSERT INTO statistics_meta (id, statistic_id, source, unit_of_measurement, name) "
                    "VALUES (?, ?, 'simulator', 'W', ?)",
                    (idx, entity_id, tmpl_name),
                )

                # Generate hourly rows
                self._generate_rows(
                    con=con,
                    table="statistics",
                    metadata_id=idx,
                    entity_id=entity_id,
                    template=tmpl,
                    start_ts=hourly_start,
                    end_ts=hourly_end,
                    step_seconds=_SECONDS_PER_HOUR,
                    serial=serial,
                    lat=lat,
                    lon=lon,
                    tz=tz,
                    noise_factor=noise_factor,
                    weather_monthly=weather_monthly,
                )

                # Generate 5-minute rows
                self._generate_rows(
                    con=con,
                    table="statistics_short_term",
                    metadata_id=idx,
                    entity_id=entity_id,
                    template=tmpl,
                    start_ts=short_term_start,
                    end_ts=anchor,
                    step_seconds=_SECONDS_PER_5MIN,
                    serial=serial,
                    lat=lat,
                    lon=lon,
                    tz=tz,
                    noise_factor=noise_factor,
                    weather_monthly=weather_monthly,
                )

            con.commit()
        finally:
            con.close()

        _LOGGER.info("Wrote synthetic history to %s", db_path.name)
        return db_path

    def _generate_rows(
        self,
        *,
        con: sqlite3.Connection,
        table: str,
        metadata_id: int,
        entity_id: str,
        template: dict[str, object],
        start_ts: float,
        end_ts: float,
        step_seconds: int,
        serial: str,
        lat: float,
        lon: float,
        tz: ZoneInfo,
        noise_factor: float,
        weather_monthly: dict[int, float] | None,
    ) -> None:
        """Generate statistics rows for one circuit into the given table."""
        ep = template.get("energy_profile", {})
        if not isinstance(ep, dict):
            return

        mode = str(ep.get("mode", "consumer"))
        typical_power = float(ep.get("typical_power", 0.0))
        nameplate_w = ep.get("nameplate_capacity_w")
        nameplate = float(nameplate_w) if nameplate_w is not None else None

        # Time-of-day profile
        tod_profile = template.get("time_of_day_profile", {})
        tod_enabled = isinstance(tod_profile, dict) and tod_profile.get("enabled", False)
        hour_factors: dict[int, float] = {}
        if isinstance(tod_profile, dict):
            raw_hf = tod_profile.get("hour_factors", {})
            if isinstance(raw_hf, dict):
                hour_factors = {int(k): float(v) for k, v in raw_hf.items()}

        # Monthly factors
        monthly_factors: dict[int, float] = {}
        raw_mf = template.get("monthly_factors")
        if isinstance(raw_mf, dict):
            monthly_factors = {int(k): float(v) for k, v in raw_mf.items()}

        # HVAC type
        hvac_type = template.get("hvac_type")
        hvac_type_str = str(hvac_type) if isinstance(hvac_type, str) else None

        # Cycling pattern
        cycling = template.get("cycling_pattern")
        duty_cycle: float | None = None
        cycle_period = 2700
        if isinstance(cycling, dict):
            dc = cycling.get("duty_cycle")
            if dc is not None:
                duty_cycle = float(dc)
            else:
                on_dur = cycling.get("on_duration")
                off_dur = cycling.get("off_duration")
                if on_dur is not None and off_dur is not None:
                    total = int(on_dur) + int(off_dur)
                    if total > 0:
                        duty_cycle = int(on_dur) / total
                        cycle_period = total
            cp = cycling.get("period")
            if cp is not None:
                cycle_period = int(cp)

        # Active days from time_of_day_profile
        active_days: list[int] = []
        if isinstance(tod_profile, dict):
            ad = tod_profile.get("active_days", [])
            if isinstance(ad, list):
                active_days = [int(d) for d in ad]

        # Power range for clamping
        power_range = ep.get("power_range", [0, 10000])
        if isinstance(power_range, list) and len(power_range) == 2:
            min_power, max_power = float(power_range[0]), float(power_range[1])
        else:
            min_power, max_power = 0.0, 10000.0

        # Mean of hour factors for normalisation
        mean_hf = (
            sum(hour_factors.values()) / len(hour_factors)
            if hour_factors
            else 1.0
        )

        # Mean of monthly factors for normalisation
        mean_mf = (
            sum(monthly_factors.values()) / len(monthly_factors)
            if monthly_factors
            else 1.0
        )

        batch: list[tuple[object, ...]] = []
        ts = start_ts
        while ts < end_ts:
            power = self._compute_power_at(
                ts=ts,
                mode=mode,
                typical_power=typical_power,
                nameplate=nameplate,
                lat=lat,
                lon=lon,
                tz=tz,
                serial=serial,
                hour_factors=hour_factors,
                mean_hf=mean_hf,
                tod_enabled=tod_enabled,
                monthly_factors=monthly_factors,
                mean_mf=mean_mf,
                hvac_type=hvac_type_str,
                duty_cycle=duty_cycle,
                cycle_period=cycle_period,
                active_days=active_days,
                weather_monthly=weather_monthly,
            )

            # Apply deterministic noise
            noise = _deterministic_noise(serial, entity_id, ts)
            noisy_power = power * (1.0 + noise * noise_factor)

            # Clamp
            if mode == "producer":
                noisy_power = max(0.0, min(abs(min_power), noisy_power))
            else:
                noisy_power = max(min_power, min(max_power, noisy_power))

            mean_val = noisy_power
            min_val = mean_val * (1.0 - noise_factor)
            max_val = mean_val * (1.0 + noise_factor)

            batch.append((metadata_id, ts, ts, mean_val, min_val, max_val))

            if len(batch) >= 1000:
                con.executemany(
                    f"INSERT INTO {table} "  # noqa: S608
                    "(metadata_id, created_ts, start_ts, mean, min, max) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    batch,
                )
                batch.clear()

            ts += step_seconds

        if batch:
            con.executemany(
                f"INSERT INTO {table} "  # noqa: S608
                "(metadata_id, created_ts, start_ts, mean, min, max) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                batch,
            )

    def _compute_power_at(
        self,
        *,
        ts: float,
        mode: str,
        typical_power: float,
        nameplate: float | None,
        lat: float,
        lon: float,
        tz: ZoneInfo,
        serial: str,
        hour_factors: dict[int, float],
        mean_hf: float,
        tod_enabled: bool,
        monthly_factors: dict[int, float],
        mean_mf: float,
        hvac_type: str | None,
        duty_cycle: float | None,
        cycle_period: int,
        active_days: list[int],
        weather_monthly: dict[int, float] | None,
    ) -> float:
        """Compute synthetic power for one time step."""
        dt = datetime.fromtimestamp(ts, tz=tz)
        hour = dt.hour
        weekday = dt.weekday()
        month = dt.month

        # Check active days
        if active_days and weekday not in active_days:
            return 0.0

        base = typical_power

        # Mode-specific modulation
        if mode == "producer":
            # Solar: use nameplate or typical_power as scale
            scale = abs(nameplate) if nameplate is not None and nameplate > 0 else abs(base)
            solar = solar_production_factor(ts, lat, lon)
            weather = daily_weather_factor(
                ts, seed=hash(serial), monthly_factors=weather_monthly
            )
            return scale * solar * weather

        # Time-of-day for consumers
        if hour_factors and tod_enabled:
            factor = hour_factors.get(hour, 0.0)
            if mean_hf > 0:
                base = typical_power / mean_hf * factor
            else:
                base = 0.0
        elif tod_enabled:
            # Basic peak/off-peak
            if hour >= 22 or hour <= 6:
                base = typical_power * 0.3
            elif hour in range(7, 22):
                base = typical_power

        # Monthly/seasonal modulation
        if monthly_factors:
            mf = monthly_factors.get(month, 1.0)
            if mean_mf > 0:
                base = base / mean_mf * mf
        elif hvac_type is not None:
            base = base * hvac_seasonal_factor(ts, lat, hvac_type, tz=tz)

        # Cycling: reduce by duty cycle
        if duty_cycle is not None and duty_cycle < 1.0:
            # For hourly/5min aggregation, duty cycle reduces mean power
            base = base * duty_cycle

        return base
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_history_generator.py -v`
Expected: All PASS

- [ ] **Step 5: Write test — solar circuit produces day/night pattern**

Add to `tests/test_history_generator.py`:

```python
    @pytest.mark.asyncio
    async def test_solar_circuit_has_day_night_pattern(self, tmp_path: Path) -> None:
        """Solar circuits should produce zero power at night, nonzero during day."""
        solar_config = {
            **_MINIMAL_CONFIG,
            "circuit_templates": {
                "solar": {
                    "energy_profile": {
                        "mode": "producer",
                        "power_range": [-5000, 0],
                        "typical_power": -3000.0,
                        "power_variation": 0.05,
                        "nameplate_capacity_w": 5000.0,
                    },
                    "relay_behavior": "non_controllable",
                    "priority": "NEVER",
                    "recorder_entity": "sensor.sim_test_gen_solar_power",
                },
            },
            "circuits": [
                {"id": "circuit_1", "name": "Solar", "template": "solar", "tabs": [1]},
            ],
        }

        config_path = tmp_path / "solar_panel.yaml"
        config_path.write_text(yaml.dump(solar_config))

        gen = SyntheticHistoryGenerator()
        # Use a fixed anchor in summer for reliable daylight
        db_path = await gen.generate(config_path, anchor_time=1_719_792_000.0)  # ~Jul 2024

        con = sqlite3.connect(str(db_path))
        rows = con.execute(
            "SELECT start_ts, mean FROM statistics ORDER BY start_ts LIMIT 48"
        ).fetchall()
        con.close()

        # Among the first 48 hourly rows, some should be zero (night)
        # and some should be nonzero (day)
        values = [r[1] for r in rows]
        assert any(v == 0.0 for v in values), "Expected some zero (nighttime) rows"
        assert any(v > 0.0 for v in values), "Expected some nonzero (daytime) rows"
```

- [ ] **Step 6: Run all generator tests**

Run: `python -m pytest tests/test_history_generator.py -v`
Expected: All PASS

- [ ] **Step 7: Run mypy**

Run: `python -m mypy src/span_panel_simulator/history_generator.py --strict`
Expected: PASS (or only pre-existing issues from imported modules)

- [ ] **Step 8: Commit**

```bash
git add src/span_panel_simulator/history_generator.py tests/test_history_generator.py
git commit -m "feat: add SyntheticHistoryGenerator for offline history creation"
```

---

### Task 5: Standalone CLI entry point for generator

**Files:**
- Modify: `src/span_panel_simulator/history_generator.py` (add `__main__` block at bottom)

- [ ] **Step 1: Add CLI entry point to history_generator.py**

Append to the bottom of `src/span_panel_simulator/history_generator.py`:

```python
async def _cli_main() -> None:
    """CLI entry point for standalone generation."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Generate synthetic history DB from a panel config YAML",
    )
    parser.add_argument("config", type=Path, help="Path to the panel YAML config")
    parser.add_argument(
        "--anchor-time",
        type=float,
        default=None,
        help="Unix epoch for the anchor (default: now)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    gen = SyntheticHistoryGenerator()
    db_path = await gen.generate(args.config, anchor_time=args.anchor_time)
    print(f"Generated: {db_path}")


if __name__ == "__main__":
    import asyncio

    asyncio.run(_cli_main())
```

- [ ] **Step 2: Test the CLI manually**

Run: `python -m span_panel_simulator.history_generator configs/default_MAIN_16.yaml --anchor-time 1700000000`
Expected: Prints `Generated: configs/default_MAIN_16_history.db` (or warns about no recorder_entity mappings if the default config lacks them)

- [ ] **Step 3: Commit**

```bash
git add src/span_panel_simulator/history_generator.py
git commit -m "feat: add standalone CLI for synthetic history generation"
```

---

### Task 6: Integrate generator into clone-from-panel flow

**Files:**
- Modify: `src/span_panel_simulator/dashboard/routes.py` (in `handle_clone_from_panel`)

- [ ] **Step 1: Write test — clone flow generates companion DB**

Add to `tests/test_history_generator.py`:

```python
class TestCloneIntegration:
    @pytest.mark.asyncio
    async def test_generate_after_clone_config(self, tmp_path: Path) -> None:
        """After writing a clone config, the generator should produce a companion DB."""
        # Write a clone-like config with recorder_entity mappings
        config = {
            "panel_config": {
                "serial_number": "sim-ABC123-clone",
                "total_tabs": 32,
                "main_size": 200,
                "latitude": 37.7,
                "longitude": -122.4,
            },
            "circuit_templates": {
                "clone_1": {
                    "energy_profile": {
                        "mode": "consumer",
                        "power_range": [0, 2400],
                        "typical_power": 500.0,
                        "power_variation": 0.1,
                    },
                    "relay_behavior": "controllable",
                    "priority": "MUST_HAVE",
                    "recorder_entity": "sensor.span_panel_kitchen_power",
                },
                "clone_3": {
                    "energy_profile": {
                        "mode": "producer",
                        "power_range": [-5000, 0],
                        "typical_power": -3000.0,
                        "power_variation": 0.05,
                        "nameplate_capacity_w": 5000.0,
                    },
                    "relay_behavior": "non_controllable",
                    "priority": "NEVER",
                    "recorder_entity": "sensor.span_panel_solar_power",
                },
            },
            "circuits": [
                {"id": "circuit_1", "name": "Kitchen", "template": "clone_1", "tabs": [1]},
                {"id": "circuit_3", "name": "Solar", "template": "clone_3", "tabs": [3]},
            ],
            "unmapped_tabs": list(range(4, 33)),
            "simulation_params": {
                "update_interval": 5,
                "time_acceleration": 1.0,
                "noise_factor": 0.02,
                "enable_realistic_behaviors": True,
            },
        }

        config_path = tmp_path / "ABC123-clone.yaml"
        config_path.write_text(yaml.dump(config))

        gen = SyntheticHistoryGenerator()
        db_path = await gen.generate(config_path, anchor_time=1_700_000_000.0)

        assert db_path.exists()

        # Verify both entities are in the DB
        con = sqlite3.connect(str(db_path))
        meta = con.execute("SELECT statistic_id FROM statistics_meta ORDER BY id").fetchall()
        assert len(meta) == 2
        assert meta[0][0] == "sensor.span_panel_kitchen_power"
        assert meta[1][0] == "sensor.span_panel_solar_power"

        # Both should have hourly data
        for entity_idx in (1, 2):
            count = con.execute(
                "SELECT COUNT(*) FROM statistics WHERE metadata_id = ?",
                (entity_idx,),
            ).fetchone()[0]
            assert count > 8000
        con.close()
```

- [ ] **Step 2: Run test**

Run: `python -m pytest tests/test_history_generator.py::TestCloneIntegration -v`
Expected: PASS

- [ ] **Step 3: Add generator call to handle_clone_from_panel in routes.py**

In `src/span_panel_simulator/dashboard/routes.py`, find the `handle_clone_from_panel` function. After the line `clone_path = write_clone_config(config, ctx.config_dir, scraped.serial_number)` (around line 1558), add the generator invocation:

```python
    clone_path = write_clone_config(config, ctx.config_dir, scraped.serial_number)

    # Generate synthetic history companion DB for offline replay
    try:
        from span_panel_simulator.history_generator import SyntheticHistoryGenerator

        gen = SyntheticHistoryGenerator()
        history_db = await gen.generate(clone_path)
        _LOGGER.info("Generated synthetic history: %s", history_db.name)
    except Exception:
        _LOGGER.warning("Synthetic history generation failed — panel will use per-tick synthesis", exc_info=True)
```

- [ ] **Step 4: Run existing clone tests to verify no regressions**

Run: `python -m pytest tests/test_clone.py -v`
Expected: All PASS

- [ ] **Step 5: Run full test suite**

Run: `python -m pytest tests/ -x -q`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add src/span_panel_simulator/dashboard/routes.py tests/test_history_generator.py
git commit -m "feat: generate synthetic history DB on clone-from-panel"
```

---

### Task 7: End-to-end round-trip test

**Files:**
- Modify: `tests/test_sqlite_app_integration.py`

- [ ] **Step 1: Write end-to-end test — generate then load via provider**

Add to `tests/test_sqlite_app_integration.py`:

```python
from span_panel_simulator.history_generator import SyntheticHistoryGenerator

# Same minimal config as test_history_generator
_ROUNDTRIP_CONFIG: dict[str, object] = {
    "panel_config": {
        "serial_number": "sim-roundtrip",
        "total_tabs": 16,
        "main_size": 200,
        "latitude": 37.7,
        "longitude": -122.4,
    },
    "circuit_templates": {
        "kitchen": {
            "energy_profile": {
                "mode": "consumer",
                "power_range": [0, 2400],
                "typical_power": 800.0,
                "power_variation": 0.1,
            },
            "relay_behavior": "controllable",
            "priority": "MUST_HAVE",
            "recorder_entity": "sensor.sim_roundtrip_kitchen_power",
        },
    },
    "circuits": [
        {"id": "circuit_1", "name": "Kitchen", "template": "kitchen", "tabs": [1]},
    ],
    "unmapped_tabs": list(range(2, 17)),
    "simulation_params": {
        "update_interval": 5,
        "time_acceleration": 1.0,
        "noise_factor": 0.02,
        "enable_realistic_behaviors": True,
    },
}


class TestEndToEndRoundTrip:
    @pytest.mark.asyncio
    async def test_generate_then_load_then_query(self, tmp_path: Path) -> None:
        """Full pipeline: generate DB -> load via SqliteHistoryProvider -> query via RecorderDataSource."""
        import yaml

        config_path = tmp_path / "roundtrip.yaml"
        config_path.write_text(yaml.dump(_ROUNDTRIP_CONFIG))

        anchor = 1_700_000_000.0
        entity = "sensor.sim_roundtrip_kitchen_power"

        # Step 1: Generate
        gen = SyntheticHistoryGenerator()
        db_path = await gen.generate(config_path, anchor_time=anchor)
        assert db_path.exists()

        # Step 2: Load
        provider = SqliteHistoryProvider(db_path)
        recorder = RecorderDataSource()
        loaded = await recorder.load(provider, [entity], lookback_days=365)
        assert loaded == 1

        # Step 3: Query
        bounds = recorder.time_bounds()
        assert bounds is not None
        start, end = bounds

        # Coverage should be close to 365 days
        coverage_days = (end - start) / 86400
        assert coverage_days > 360

        # Query multiple points — all should return non-None
        import random
        rng = random.Random(42)
        for _ in range(100):
            ts = rng.uniform(start, end)
            power = recorder.get_power(entity, ts)
            assert power is not None
            assert power >= 0.0  # consumer circuit, always >= 0

    @pytest.mark.asyncio
    async def test_convention_discovery_works(self, tmp_path: Path) -> None:
        """Verify that _resolve_history_db finds the generated companion file."""
        import yaml

        config_path = tmp_path / "discovery.yaml"
        config_path.write_text(yaml.dump(_ROUNDTRIP_CONFIG))

        gen = SyntheticHistoryGenerator()
        db_path = await gen.generate(config_path, anchor_time=1_700_000_000.0)

        # The generated file should match the convention
        assert db_path.name == "discovery_history.db"

        # SimulatorApp._resolve_history_db should find it
        result = SimulatorApp._resolve_history_db(config_path, _ROUNDTRIP_CONFIG)
        assert result == db_path
```

- [ ] **Step 2: Run end-to-end tests**

Run: `python -m pytest tests/test_sqlite_app_integration.py -v`
Expected: All PASS

- [ ] **Step 3: Run full test suite + mypy**

Run: `python -m pytest tests/ -x -q && python -m mypy src/span_panel_simulator/sqlite_history.py src/span_panel_simulator/history_generator.py src/span_panel_simulator/app.py --strict`
Expected: All PASS

- [ ] **Step 4: Commit**

```bash
git add tests/test_sqlite_app_integration.py
git commit -m "test: add end-to-end round-trip test for synthetic history pipeline"
```

---

### Task 8: Run ruff and final cleanup

- [ ] **Step 1: Run ruff on all new/modified files**

Run: `python -m ruff check src/span_panel_simulator/sqlite_history.py src/span_panel_simulator/history_generator.py src/span_panel_simulator/app.py src/span_panel_simulator/config_types.py src/span_panel_simulator/dashboard/routes.py`
Expected: No errors (fix any that appear)

- [ ] **Step 2: Run full test suite one final time**

Run: `python -m pytest tests/ -v`
Expected: All PASS

- [ ] **Step 3: Commit any cleanup**

```bash
git add -u
git commit -m "chore: lint and cleanup for synthetic history feature"
```
