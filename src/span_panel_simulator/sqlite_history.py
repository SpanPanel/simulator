"""SQLite-backed history provider — reads companion _history.db files.

Implements the ``HistoryProvider`` protocol by querying ``statistics`` and
``statistics_short_term`` tables in the HA-compatible schema written by
``SyntheticHistoryGenerator``.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import UTC, datetime
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

    def _sync_get_statistics(
        self,
        statistic_ids: list[str],
        *,
        table: str,
        start_time: str | None,
        end_time: str | None,
    ) -> dict[str, list[dict[str, object]]]:
        """Synchronous SQLite query for statistics data.

        Intended to be called via ``asyncio.to_thread``.
        """
        result: dict[str, list[dict[str, object]]] = {}

        if self._db_path != ":memory:" and not Path(self._db_path).exists():
            _LOGGER.warning("History database not found: %s", self._db_path)
            return {}

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
                f"SELECT id, statistic_id FROM statistics_meta "
                f"WHERE statistic_id IN ({placeholders})",
                statistic_ids,
            )
            meta_rows = cur.fetchall()
            meta_map: dict[int, str] = {row[0]: row[1] for row in meta_rows}

            if not meta_map:
                return {}

            for metadata_id, statistic_id in meta_map.items():
                query = f"SELECT start_ts, mean, min, max FROM {table} WHERE metadata_id = ?"
                params: list[object] = [metadata_id]

                if start_time is not None:
                    try:
                        dt = datetime.fromisoformat(start_time)
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=UTC)
                        query += " AND start_ts >= ?"
                        params.append(dt.timestamp())
                    except ValueError:
                        pass

                if end_time is not None:
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
                    records.append(
                        {
                            "start": row[0],
                            "mean": row[1],
                            "min": row[2],
                            "max": row[3],
                        }
                    )

                if records:
                    result[statistic_id] = records
        finally:
            con.close()

        return result

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

        return await asyncio.to_thread(
            self._sync_get_statistics,
            statistic_ids,
            table=table,
            start_time=start_time,
            end_time=end_time,
        )
