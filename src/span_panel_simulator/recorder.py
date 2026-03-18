"""Recorder data source — cached time-series for replay.

Loads per-circuit power statistics from a ``HistoryProvider`` and provides
synchronous lookups by entity ID + timestamp.  After loading, the data
source is backend-agnostic: it depends only on the ``HistoryProvider``
protocol defined in ``history.py``, not on any HA-specific module.

Designed for injection into the simulation engine so that cloned panels
can replay recorded power instead of synthesising from statistical
summaries.
"""

from __future__ import annotations

import bisect
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from span_panel_simulator.history import HistoryProvider

_LOGGER = logging.getLogger(__name__)


def _parse_timestamp(value: object) -> float | None:
    """Convert a recorder timestamp to epoch seconds.

    Handles three formats returned by the HA recorder API:
      - Unix epoch in *seconds* (float < 1 e12)
      - Unix epoch in *milliseconds* (float >= 1 e12)
      - ISO 8601 string
    """
    if value is None:
        return None
    if isinstance(value, int | float):
        ts = float(value)
        if ts > 1e12:
            ts /= 1000.0
        return ts
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt.timestamp()
        except ValueError:
            return None
    return None


class RecorderDataSource:
    """Cached per-entity power time-series for recorder replay.

    Usage::

        ds = RecorderDataSource()
        await ds.load(history_provider, ["sensor.a", "sensor.b"])
        power = ds.get_power("sensor.a", some_epoch_timestamp)
    """

    def __init__(self) -> None:
        # entity_id -> sorted list of (epoch_seconds, mean_power_watts)
        self._series: dict[str, list[tuple[float, float]]] = {}

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    async def load(
        self,
        history: HistoryProvider,
        entity_ids: list[str],
    ) -> int:
        """Pre-fetch statistics for *entity_ids* from *history*.

        Queries both hourly (indefinite retention) and 5-minute (short-term)
        statistics, merges them into a single sorted series per entity, and
        stores the result for synchronous lookup during simulation ticks.

        Returns the number of entities for which data was loaded.
        """
        if not entity_ids:
            return 0

        loaded = 0

        # Fetch hourly stats (indefinite retention — no time bounds)
        hourly = await history.async_get_statistics(entity_ids, period="hour")

        # Fetch 5-minute stats (~10-day retention)
        short_term = await history.async_get_statistics(entity_ids, period="5minute")

        for entity_id in entity_ids:
            points = self._merge_records(
                hourly.get(entity_id, []),
                short_term.get(entity_id, []),
            )
            if points:
                self._series[entity_id] = points
                loaded += 1

        if loaded:
            bounds = self.time_bounds()
            _LOGGER.info(
                "Recorder loaded %d/%d entities, %s -> %s",
                loaded,
                len(entity_ids),
                datetime.fromtimestamp(bounds[0], tz=UTC).isoformat() if bounds else "?",
                datetime.fromtimestamp(bounds[1], tz=UTC).isoformat() if bounds else "?",
            )
        else:
            _LOGGER.warning("Recorder loaded 0/%d entities — no data available", len(entity_ids))

        return loaded

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def get_power(self, entity_id: str, timestamp: float) -> float | None:
        """Return recorded mean power at *timestamp*, or ``None``.

        Uses linear interpolation between the two nearest data points.
        Returns ``None`` if the entity has no data or the timestamp is
        outside the recorded window.
        """
        series = self._series.get(entity_id)
        if not series:
            return None

        # Outside recorded window → let caller fall back to synthetic
        if timestamp < series[0][0] or timestamp > series[-1][0]:
            return None

        # Binary search for the insertion point
        idx = bisect.bisect_right(series, (timestamp, float("inf"))) - 1
        if idx < 0:
            idx = 0

        if idx >= len(series) - 1:
            return series[-1][1]

        t0, v0 = series[idx]
        t1, v1 = series[idx + 1]

        if t1 == t0:
            return v0

        frac = (timestamp - t0) / (t1 - t0)
        return v0 + frac * (v1 - v0)

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    def time_bounds(self) -> tuple[float, float] | None:
        """Return ``(earliest, latest)`` epoch seconds across all entities.

        Returns ``None`` if no data is loaded.
        """
        if not self._series:
            return None
        earliest = min(s[0][0] for s in self._series.values())
        latest = max(s[-1][0] for s in self._series.values())
        return (earliest, latest)

    @property
    def is_loaded(self) -> bool:
        """Whether any recorder data has been loaded."""
        return bool(self._series)

    @property
    def entity_count(self) -> int:
        """Number of entities with loaded data."""
        return len(self._series)

    def has_entity(self, entity_id: str) -> bool:
        """Whether data is available for *entity_id*."""
        return entity_id in self._series

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _merge_records(
        hourly_records: list[dict[str, object]],
        short_term_records: list[dict[str, object]],
    ) -> list[tuple[float, float]]:
        """Merge hourly and short-term records into a sorted series.

        Short-term (5-minute) records are more granular and take precedence
        over hourly records for overlapping time ranges.  The result is a
        sorted list of ``(epoch_seconds, mean_power_watts)`` tuples with
        no duplicate timestamps.
        """
        points: dict[float, float] = {}

        # Add hourly points first
        for record in hourly_records:
            ts = _parse_timestamp(record.get("start"))
            mean = record.get("mean")
            if ts is not None and mean is not None and isinstance(mean, int | float | str):
                try:
                    points[ts] = float(mean)
                except (ValueError, TypeError):
                    continue

        # Overlay short-term points (overwrite hourly for same timestamp)
        for record in short_term_records:
            ts = _parse_timestamp(record.get("start"))
            mean = record.get("mean")
            if ts is not None and mean is not None and isinstance(mean, int | float | str):
                try:
                    points[ts] = float(mean)
                except (ValueError, TypeError):
                    continue

        if not points:
            return []

        return sorted(points.items())
