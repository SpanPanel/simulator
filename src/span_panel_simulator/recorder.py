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
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from span_panel_simulator.history import HistoryProvider

_LOGGER = logging.getLogger(__name__)

# How far back to query for each statistics period.
_HOURLY_LOOKBACK = timedelta(days=90)
_SHORT_TERM_LOOKBACK = timedelta(days=10)

# When the simulation clock overshoots the recorder window by less than
# this many seconds, fall back to synthetic instead of wrapping.  This
# prevents a 5-minute data lag at 1x speed from triggering a wrap to
# the start of the window (months ago).  At accelerated speeds (e.g.
# 360x) this threshold is exceeded in ~10 s of wall time, after which
# looping playback engages.
_FALLBACK_THRESHOLD_S = 3600.0


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

        # HA's recorder/statistics_during_period requires start_time.
        # For hourly stats we request the last year (indefinite retention
        # means all data is available, but we still need a start bound).
        # For 5-minute stats we request the last 10 days (short-term
        # retention window).
        now = datetime.now(UTC)
        hourly_start = (now - _HOURLY_LOOKBACK).isoformat()
        short_term_start = (now - _SHORT_TERM_LOOKBACK).isoformat()

        hourly = await history.async_get_statistics(
            entity_ids,
            period="hour",
            start_time=hourly_start,
        )

        short_term = await history.async_get_statistics(
            entity_ids,
            period="5minute",
            start_time=short_term_start,
        )

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
                "Recorder loaded %d/%d entities (%d total points), %s -> %s",
                loaded,
                len(entity_ids),
                sum(len(s) for s in self._series.values()),
                datetime.fromtimestamp(bounds[0], tz=UTC).isoformat() if bounds else "?",
                datetime.fromtimestamp(bounds[1], tz=UTC).isoformat() if bounds else "?",
            )
            # Log per-entity summary at debug for troubleshooting
            for eid, pts in self._series.items():
                _LOGGER.debug(
                    "  %s: %d points, %s -> %s",
                    eid,
                    len(pts),
                    datetime.fromtimestamp(pts[0][0], tz=UTC).isoformat(),
                    datetime.fromtimestamp(pts[-1][0], tz=UTC).isoformat(),
                )
            # Log entities that had no data
            missing = [eid for eid in entity_ids if eid not in self._series]
            if missing:
                _LOGGER.warning("Recorder: no data for %d entities: %s", len(missing), missing)
        else:
            _LOGGER.warning("Recorder loaded 0/%d entities — no data available", len(entity_ids))

        return loaded

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def get_power(self, entity_id: str, timestamp: float) -> float | None:
        """Return recorded mean power at *timestamp*, or ``None``.

        Uses linear interpolation between the two nearest data points.

        Boundary behaviour:

        * **Within window** — direct interpolation.
        * **Past the end by less than one window duration** — returns
          ``None`` so the engine falls back to synthetic.  This covers
          real-time tracking at 1x speed where the simulation clock is
          just minutes ahead of the most recent data point.
        * **Past the end by one full window or more** — wraps via modular
          arithmetic for looping playback at accelerated speeds.
        * **Before the start** — wraps via modular arithmetic.

        Returns ``None`` when the entity has no data or the timestamp is
        in the synthetic-fallback zone just past the window end.
        """
        series = self._series.get(entity_id)
        if not series:
            return None

        window_start = series[0][0]
        window_end = series[-1][0]
        window_duration = window_end - window_start
        if window_duration <= 0:
            return series[0][1]

        # Timestamps just past the window end: clamp to the end rather
        # than wrapping.  At 1x speed the simulation clock is always
        # slightly ahead of the last recorded data point (HA writes
        # 5-minute stats periodically).  Wrapping would jump to the
        # start of the window — months ago — producing wildly wrong
        # values.  Clamping returns the most recent recorded value,
        # which is the best approximation until new data arrives.
        #
        # The threshold is generous (1 hour) so wrapping only engages
        # during genuinely accelerated playback, not during real-time
        # tracking with minor data lag.
        overshoot = timestamp - window_end
        if 0 < overshoot < _FALLBACK_THRESHOLD_S:
            timestamp = window_end

        # Outside the window by a full cycle or more, or before start
        # → wrap via modular arithmetic for looping playback.
        if timestamp < window_start or timestamp > window_end:
            timestamp = window_start + ((timestamp - window_start) % window_duration)

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
