"""Integration test: SqliteHistoryProvider used at panel startup."""

from __future__ import annotations

import sqlite3
import time
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from span_panel_simulator.app import SimulatorApp
from span_panel_simulator.history_generator import SyntheticHistoryGenerator
from span_panel_simulator.recorder import RecorderDataSource
from span_panel_simulator.sqlite_history import SCHEMA_SQL, SqliteHistoryProvider


class TestSqliteRecorderRoundTrip:
    """Verify that SqliteHistoryProvider feeds RecorderDataSource correctly."""

    @pytest.mark.asyncio
    async def test_load_and_get_power(self, tmp_path: Path) -> None:
        """Generate rows, load via SqliteHistoryProvider, query via RecorderDataSource."""
        db_path = tmp_path / "panel_history.db"
        entity = "sensor.sim_panel_kitchen_power"

        con = sqlite3.connect(str(db_path))
        con.executescript(SCHEMA_SQL)
        con.execute(
            "INSERT INTO statistics_meta (id, statistic_id, unit_of_measurement) "
            "VALUES (1, ?, 'W')",
            (entity,),
        )
        # Use a base timestamp within the past 90 days so it falls inside
        # the lookback window when recorder.load() computes start_time.
        base_ts = time.time() - 7 * 86400  # 7 days ago
        base_ts = base_ts - (base_ts % 3600)  # align to hour boundary
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

        # At i=12: mean = 500 + 12*10 = 620 W; at i=13: mean = 630 W
        # Query exactly at i=12 to get 620 W
        mid_ts = base_ts + 12 * 3600
        power = recorder.get_power(entity, mid_ts)
        assert power is not None
        assert 619.0 < power < 621.0

    @pytest.mark.asyncio
    async def test_no_db_file_returns_none(self, tmp_path: Path) -> None:
        provider = SqliteHistoryProvider(tmp_path / "missing.db")
        recorder = RecorderDataSource()
        loaded = await recorder.load(provider, ["sensor.x"], lookback_days=365)
        assert loaded == 0


class TestResolveHistoryDb:
    def test_convention_path(self, tmp_path: Path) -> None:
        config_path = tmp_path / "my_panel.yaml"
        config_path.write_text("panel_config:\n  serial_number: x\n")
        db_path = tmp_path / "my_panel_history.db"
        db_path.write_text("")

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
        (tmp_path / "my_panel_history.db").write_text("")
        custom = tmp_path / "custom.db"
        custom.write_text("")

        raw = {"panel_config": {"history_db": "custom.db"}}
        result = SimulatorApp._resolve_history_db(config_path, raw)
        assert result == custom


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
        """Full pipeline: generate DB -> load via SqliteHistoryProvider -> query power values."""
        import yaml

        config_path = tmp_path / "roundtrip.yaml"
        config_path.write_text(yaml.dump(_ROUNDTRIP_CONFIG))

        anchor = 1_700_000_000.0
        entity = "sensor.sim_roundtrip_kitchen_power"

        # Step 1: Generate
        gen = SyntheticHistoryGenerator()
        db_path = await gen.generate(config_path, anchor_time=anchor)
        assert db_path.exists()

        # Step 2: Load — compute lookback to cover anchor (fixed in past)
        import time as _time

        days_since_anchor = int((_time.time() - anchor) / 86400) + 400
        provider = SqliteHistoryProvider(db_path)
        recorder = RecorderDataSource()
        loaded = await recorder.load(provider, [entity], lookback_days=days_since_anchor)
        assert loaded == 1

        # Step 3: Query
        bounds = recorder.time_bounds()
        assert bounds is not None
        start, end = bounds

        # Coverage spans the hourly window (365 - 10 short-term days = 355 days)
        coverage_days = (end - start) / 86400
        assert coverage_days > 350

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
