"""Tests for SqliteHistoryProvider."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from span_panel_simulator.sqlite_history import SCHEMA_SQL, SqliteHistoryProvider


def _create_test_db(path: Path, entity_id: str, rows: list[tuple[float, float]]) -> None:
    """Create a test SQLite DB with statistics_meta and statistics rows."""
    con = sqlite3.connect(str(path))
    con.executescript(SCHEMA_SQL)
    con.execute(
        "INSERT INTO statistics_meta (id, statistic_id, unit_of_measurement) VALUES (1, ?, 'W')",
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
            start_time="1970-01-01T01:00:00+00:00",
        )

        assert entity in result
        assert len(result[entity]) == 2

    @pytest.mark.asyncio
    async def test_unknown_entity_returns_empty(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        _create_test_db(db_path, "sensor.real", [(1000.0, 100.0)])

        provider = SqliteHistoryProvider(db_path)
        result = await provider.async_get_statistics(["sensor.does_not_exist"], period="hour")

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
