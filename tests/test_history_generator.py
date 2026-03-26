"""Tests for SyntheticHistoryGenerator."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import pytest
import yaml

if TYPE_CHECKING:
    from pathlib import Path

from span_panel_simulator.history_generator import SyntheticHistoryGenerator

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
        meta = con.execute("SELECT statistic_id FROM statistics_meta").fetchall()
        assert len(meta) == 1
        assert meta[0][0] == "sensor.sim_test_gen_kitchen_power"

        hourly_count = con.execute("SELECT COUNT(*) FROM statistics").fetchone()[0]
        # ~355 days * 24 = 8520, allow some tolerance
        assert hourly_count > 8000
        assert hourly_count < 9000

        short_count = con.execute("SELECT COUNT(*) FROM statistics_short_term").fetchone()[0]
        # 10 days * 288 five-minute slots = 2880
        assert short_count > 2800
        assert short_count < 3000

        con.close()

    @pytest.mark.asyncio
    async def test_deterministic_output(self, tmp_path: Path) -> None:
        """Same config + anchor produces identical DBs."""
        config_path = tmp_path / "test_panel.yaml"
        config_path.write_text(yaml.dump(_MINIMAL_CONFIG))

        anchor = 1_700_000_000.0

        gen = SyntheticHistoryGenerator()
        db1 = await gen.generate(config_path, anchor_time=anchor)

        db1_copy = tmp_path / "db1.db"
        db1.rename(db1_copy)

        db2 = await gen.generate(config_path, anchor_time=anchor)

        con1 = sqlite3.connect(str(db1_copy))
        con2 = sqlite3.connect(str(db2))

        rows1 = con1.execute("SELECT start_ts, mean FROM statistics ORDER BY start_ts").fetchall()
        rows2 = con2.execute("SELECT start_ts, mean FROM statistics ORDER BY start_ts").fetchall()

        assert rows1 == rows2
        con1.close()
        con2.close()

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
        db_path = await gen.generate(config_path, anchor_time=1_719_792_000.0)

        con = sqlite3.connect(str(db_path))
        rows = con.execute(
            "SELECT start_ts, mean FROM statistics ORDER BY start_ts LIMIT 48"
        ).fetchall()
        con.close()

        values = [r[1] for r in rows]
        assert any(v == 0.0 for v in values), "Expected some zero (nighttime) rows"
        assert any(v > 0.0 for v in values), "Expected some nonzero (daytime) rows"
