"""Tests for modeling view backend: recorder lookback, engine compute, route."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from span_panel_simulator.recorder import RecorderDataSource


@pytest.fixture
def recorder_with_data() -> RecorderDataSource:
    """RecorderDataSource pre-loaded with 30 days of hourly data."""
    ds = RecorderDataSource()
    now = datetime.now(UTC).timestamp()
    start = now - 30 * 86400  # 30 days back
    # Populate directly for testing (bypass load())
    series: list[tuple[float, float]] = []
    t = start
    while t <= now:
        series.append((t, 1000.0))  # constant 1kW
        t += 3600
    ds._series["sensor.test"] = series
    return ds


async def test_ensure_lookback_noop_when_sufficient(
    recorder_with_data: RecorderDataSource,
) -> None:
    """ensure_lookback does nothing when data already covers the requested range."""
    ds = recorder_with_data
    bounds_before = ds.time_bounds()
    await ds.ensure_lookback(required_days=20)
    assert ds.time_bounds() == bounds_before


async def test_ensure_lookback_noop_without_history(
    recorder_with_data: RecorderDataSource,
) -> None:
    """ensure_lookback does nothing when no HistoryProvider is stored."""
    ds = recorder_with_data
    # No _history set — should be a no-op
    await ds.ensure_lookback(required_days=180)
    bounds = ds.time_bounds()
    assert bounds is not None
    coverage = (bounds[1] - bounds[0]) / 86400
    assert coverage < 35  # still only ~30 days


async def test_ensure_lookback_reloads_when_insufficient() -> None:
    """ensure_lookback calls load() when data doesn't cover the requested range."""
    ds = RecorderDataSource()

    # Simulate initial load with 30 days
    now = datetime.now(UTC)
    start_30d = now - timedelta(days=30)
    entity_ids = ["sensor.a"]

    mock_history = AsyncMock()
    # First load: 30 days of hourly data
    hourly_records_30d = [
        {"start": (start_30d + timedelta(hours=h)).isoformat(), "mean": 500.0}
        for h in range(30 * 24)
    ]
    mock_history.async_get_statistics = AsyncMock(return_value={"sensor.a": hourly_records_30d})

    await ds.load(mock_history, entity_ids)
    bounds = ds.time_bounds()
    assert bounds is not None
    initial_coverage = (bounds[1] - bounds[0]) / 86400

    # Now request 180 days — should trigger reload
    hourly_records_180d = [
        {"start": (now - timedelta(days=180) + timedelta(hours=h)).isoformat(), "mean": 500.0}
        for h in range(180 * 24)
    ]
    mock_history.async_get_statistics = AsyncMock(return_value={"sensor.a": hourly_records_180d})

    await ds.ensure_lookback(required_days=180)
    bounds = ds.time_bounds()
    assert bounds is not None
    new_coverage = (bounds[1] - bounds[0]) / 86400
    assert new_coverage > initial_coverage
