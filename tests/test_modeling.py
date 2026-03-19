"""Tests for modeling view backend: recorder lookback, engine compute, route."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from span_panel_simulator.engine import DynamicSimulationEngine
from span_panel_simulator.recorder import RecorderDataSource

if TYPE_CHECKING:
    from pathlib import Path


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


@pytest.fixture
def simple_config(tmp_path: Path) -> Path:
    """Minimal YAML config with a consumer, a PV producer, and a battery."""
    config = tmp_path / "test_panel.yaml"
    config.write_text("""\
panel_config:
  serial_number: "SIM-MODEL-TEST"
  total_tabs: 8
  main_size: 100
  latitude: 40.0
  longitude: -74.0

circuit_templates:
  lighting:
    energy_profile:
      mode: "consumer"
      power_range: [100.0, 500.0]
      typical_power: 300.0
      power_variation: 0.05
    relay_behavior: "controllable"
    priority: "MUST_HAVE"
  solar:
    energy_profile:
      mode: "producer"
      power_range: [0.0, 5000.0]
      typical_power: 3000.0
      power_variation: 0.05
    relay_behavior: "non_controllable"
    priority: "MUST_HAVE"
  battery:
    energy_profile:
      mode: "bidirectional"
      power_range: [0.0, 5000.0]
      typical_power: 3000.0
      power_variation: 0.0
    relay_behavior: "non_controllable"
    priority: "MUST_HAVE"
    battery_behavior:
      enabled: true
      charge_mode: "custom"
      charge_hours: [10, 11, 12, 13, 14]
      discharge_hours: [17, 18, 19, 20, 21]
      nameplate_capacity_kwh: 13.5
      backup_reserve_pct: 20

circuits:
  - id: "lights"
    name: "Lights"
    template: "lighting"
    tabs: [1]
    recorder_entity: "sensor.lights_power"
  - id: "pv"
    name: "Solar PV"
    template: "solar"
    tabs: [3, 5]
    recorder_entity: "sensor.pv_power"
  - id: "batt"
    name: "Battery"
    template: "battery"
    tabs: [2, 4]

unmapped_tabs: []

simulation_params:
  update_interval: 5
  time_acceleration: 1.0
  noise_factor: 0.0
  enable_realistic_behaviors: true
""")
    return config


@pytest.fixture
def recorder_for_engine() -> RecorderDataSource:
    """RecorderDataSource with hourly data for lights and PV sensors."""
    ds = RecorderDataSource()
    now = datetime.now(UTC).timestamp()
    start = now - 60 * 86400  # 60 days back

    lights_series: list[tuple[float, float]] = []
    pv_series: list[tuple[float, float]] = []

    t = start
    while t <= now:
        lights_series.append((t, 300.0))
        hour = datetime.fromtimestamp(t, tz=UTC).hour
        pv_w = 3000.0 if 8 <= hour <= 16 else 0.0
        pv_series.append((t, pv_w))
        t += 3600

    ds._series["sensor.lights_power"] = lights_series
    ds._series["sensor.pv_power"] = pv_series
    return ds


async def test_compute_modeling_data_returns_expected_structure(
    simple_config: Path,
    recorder_for_engine: RecorderDataSource,
) -> None:
    """compute_modeling_data returns the correct response schema."""
    engine = DynamicSimulationEngine(
        config_path=simple_config,
        recorder=recorder_for_engine,
    )
    await engine.initialize_async()

    result = await engine.compute_modeling_data(horizon_hours=168)  # 1 week

    assert result is not None
    assert "error" not in result
    assert "timestamps" in result
    assert "site_power" in result
    assert "grid_power" in result
    assert "pv_power" in result
    assert "battery_power" in result
    assert "circuits" in result
    assert "resolution_s" in result
    assert result["resolution_s"] == 3600
    assert "time_zone" in result
    assert "horizon_start" in result
    assert "horizon_end" in result

    n = len(result["timestamps"])
    assert n > 0
    assert len(result["site_power"]) == n
    assert len(result["grid_power"]) == n
    assert len(result["pv_power"]) == n
    assert len(result["battery_power"]) == n

    for _cid, cdata in result["circuits"].items():
        assert "name" in cdata
        assert "power" in cdata
        assert len(cdata["power"]) == n


async def test_compute_modeling_data_no_recorder() -> None:
    """Returns error dict when no recorder data is loaded."""
    engine = DynamicSimulationEngine(
        config_data={
            "panel_config": {"serial_number": "TEST", "total_tabs": 4, "main_size": 100},
            "circuit_templates": {
                "generic": {
                    "energy_profile": {
                        "mode": "consumer",
                        "power_range": [0.0, 100.0],
                        "typical_power": 50.0,
                        "power_variation": 0.0,
                    },
                    "relay_behavior": "controllable",
                    "priority": "MUST_HAVE",
                },
            },
            "circuits": [
                {"id": "c1", "name": "C1", "template": "generic", "tabs": [1]},
            ],
            "unmapped_tabs": [],
            "simulation_params": {"update_interval": 5, "time_acceleration": 1.0},
        }
    )
    await engine.initialize_async()

    result = await engine.compute_modeling_data(horizon_hours=730)
    assert result is not None
    assert result.get("error") == "No recorder data available"


async def test_compute_modeling_does_not_mutate_runtime_state(
    simple_config: Path,
    recorder_for_engine: RecorderDataSource,
) -> None:
    """The modeling pass must not change the runtime behavior engine state."""
    engine = DynamicSimulationEngine(
        config_path=simple_config,
        recorder=recorder_for_engine,
    )
    await engine.initialize_async()

    be = engine._behavior_engine
    assert be is not None
    direction_before = be._last_battery_direction
    excess_before = be._solar_excess_w
    cycle_keys_before = set(be._circuit_cycle_states.keys())

    await engine.compute_modeling_data(horizon_hours=168)

    assert be._last_battery_direction == direction_before
    assert be._solar_excess_w == excess_before
    assert set(be._circuit_cycle_states.keys()) == cycle_keys_before
