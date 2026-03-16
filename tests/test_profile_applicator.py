"""Tests for the usage profile applicator."""

from __future__ import annotations

from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from pathlib import Path

from span_panel_simulator.profile_applicator import apply_usage_profiles


def _clone_config() -> dict[str, object]:
    """Build a minimal clone config with several templates."""
    return {
        "panel_config": {
            "serial_number": "sim-TEST-001-clone",
            "total_tabs": 32,
            "main_size": 200,
        },
        "circuit_templates": {
            "clone_1": {
                "energy_profile": {
                    "mode": "consumer",
                    "power_range": [0.0, 1800.0],
                    "typical_power": 500.0,
                    "power_variation": 0.1,
                },
                "relay_behavior": "controllable",
                "priority": "NEVER",
                "breaker_rating": 15,
            },
            "clone_2": {
                "energy_profile": {
                    "mode": "consumer",
                    "power_range": [0.0, 2400.0],
                    "typical_power": 100.0,
                    "power_variation": 0.1,
                },
                "relay_behavior": "controllable",
                "priority": "OFF_GRID",
                "breaker_rating": 20,
            },
            "clone_30": {
                "energy_profile": {
                    "mode": "producer",
                    "power_range": [-3960.0, 0.0],
                    "typical_power": -2376.0,
                    "power_variation": 0.1,
                },
                "relay_behavior": "controllable",
                "priority": "OFF_GRID",
                "breaker_rating": 20,
                "device_type": "pv",
            },
        },
        "circuits": [
            {"id": "circuit_1", "name": "Bedroom", "template": "clone_1", "tabs": [1]},
            {"id": "circuit_2", "name": "Kitchen", "template": "clone_2", "tabs": [2]},
            {"id": "circuit_30", "name": "Solar", "template": "clone_30", "tabs": [30, 32]},
        ],
        "unmapped_tabs": [],
        "simulation_params": {"update_interval": 5},
    }


class TestApplyUsageProfiles:
    """Tests for apply_usage_profiles."""

    def test_basic_profile_merge(self, tmp_path: Path) -> None:
        """Typical power and variation are overwritten."""
        config_path = tmp_path / "clone.yaml"
        config_path.write_text(yaml.dump(_clone_config()))

        count = apply_usage_profiles(
            config_path,
            {
                "clone_1": {
                    "typical_power": 350.0,
                    "power_variation": 0.25,
                },
            },
        )

        assert count == 1
        loaded = yaml.safe_load(config_path.read_text())
        ep = loaded["circuit_templates"]["clone_1"]["energy_profile"]
        assert ep["typical_power"] == 350.0
        assert ep["power_variation"] == 0.25

    def test_hour_factors_with_string_keys(self, tmp_path: Path) -> None:
        """JSON string keys are converted to int keys."""
        config_path = tmp_path / "clone.yaml"
        config_path.write_text(yaml.dump(_clone_config()))

        hour_factors = {str(h): 0.5 for h in range(24)}
        hour_factors["12"] = 1.0

        count = apply_usage_profiles(
            config_path,
            {
                "clone_2": {"hour_factors": hour_factors},
            },
        )

        assert count == 1
        loaded = yaml.safe_load(config_path.read_text())
        tod = loaded["circuit_templates"]["clone_2"]["time_of_day_profile"]
        assert tod["enabled"] is True
        assert tod["hour_factors"][12] == 1.0
        assert tod["hour_factors"][0] == 0.5

    def test_duty_cycle_written(self, tmp_path: Path) -> None:
        """Duty cycle is placed under cycling_pattern."""
        config_path = tmp_path / "clone.yaml"
        config_path.write_text(yaml.dump(_clone_config()))

        count = apply_usage_profiles(
            config_path,
            {
                "clone_1": {"duty_cycle": 0.45},
            },
        )

        assert count == 1
        loaded = yaml.safe_load(config_path.read_text())
        cp = loaded["circuit_templates"]["clone_1"]["cycling_pattern"]
        assert cp["duty_cycle"] == 0.45

    def test_monthly_factors_with_string_keys(self, tmp_path: Path) -> None:
        """Monthly factors string keys are converted to int."""
        config_path = tmp_path / "clone.yaml"
        config_path.write_text(yaml.dump(_clone_config()))

        monthly = {str(m): 0.8 for m in range(1, 13)}
        monthly["7"] = 1.0

        count = apply_usage_profiles(
            config_path,
            {
                "clone_1": {"monthly_factors": monthly},
            },
        )

        assert count == 1
        loaded = yaml.safe_load(config_path.read_text())
        mf = loaded["circuit_templates"]["clone_1"]["monthly_factors"]
        assert mf[7] == 1.0
        assert mf[1] == 0.8

    def test_producer_skips_power_overwrite(self, tmp_path: Path) -> None:
        """Producer circuits do not get typical_power overwritten."""
        config_path = tmp_path / "clone.yaml"
        config_path.write_text(yaml.dump(_clone_config()))

        count = apply_usage_profiles(
            config_path,
            {
                "clone_30": {
                    "typical_power": 999.0,
                    "power_variation": 0.5,
                    "hour_factors": {str(h): 1.0 for h in range(24)},
                },
            },
        )

        assert count == 1
        loaded = yaml.safe_load(config_path.read_text())
        ep = loaded["circuit_templates"]["clone_30"]["energy_profile"]
        # Power fields unchanged
        assert ep["typical_power"] == -2376.0
        assert ep["power_variation"] == 0.1
        # But hour_factors still applied
        assert "time_of_day_profile" in loaded["circuit_templates"]["clone_30"]

    def test_missing_template_skipped(self, tmp_path: Path) -> None:
        """Profiles for non-existent templates are skipped."""
        config_path = tmp_path / "clone.yaml"
        config_path.write_text(yaml.dump(_clone_config()))

        count = apply_usage_profiles(
            config_path,
            {
                "clone_99": {"typical_power": 100.0},
            },
        )

        assert count == 0

    def test_empty_profiles_no_write(self, tmp_path: Path) -> None:
        """Empty profiles dict results in no file modification."""
        config_path = tmp_path / "clone.yaml"
        original = yaml.dump(_clone_config())
        config_path.write_text(original)

        count = apply_usage_profiles(config_path, {})
        assert count == 0

    def test_multiple_templates_updated(self, tmp_path: Path) -> None:
        """Multiple templates can be updated in one call."""
        config_path = tmp_path / "clone.yaml"
        config_path.write_text(yaml.dump(_clone_config()))

        count = apply_usage_profiles(
            config_path,
            {
                "clone_1": {"typical_power": 200.0},
                "clone_2": {"typical_power": 300.0},
            },
        )

        assert count == 2
        loaded = yaml.safe_load(config_path.read_text())
        assert loaded["circuit_templates"]["clone_1"]["energy_profile"]["typical_power"] == 200.0
        assert loaded["circuit_templates"]["clone_2"]["energy_profile"]["typical_power"] == 300.0

    def test_preserves_existing_template_fields(self, tmp_path: Path) -> None:
        """Profile merge does not clobber existing template fields."""
        config_path = tmp_path / "clone.yaml"
        config_path.write_text(yaml.dump(_clone_config()))

        apply_usage_profiles(
            config_path,
            {
                "clone_1": {"typical_power": 200.0},
            },
        )

        loaded = yaml.safe_load(config_path.read_text())
        tmpl = loaded["circuit_templates"]["clone_1"]
        assert tmpl["relay_behavior"] == "controllable"
        assert tmpl["priority"] == "NEVER"
        assert tmpl["breaker_rating"] == 15
        assert tmpl["energy_profile"]["power_range"] == [0.0, 1800.0]

    def test_invalid_config_returns_zero(self, tmp_path: Path) -> None:
        """Non-dict YAML returns 0 without crashing."""
        config_path = tmp_path / "bad.yaml"
        config_path.write_text("just a string\n")

        count = apply_usage_profiles(config_path, {"clone_1": {"typical_power": 100.0}})
        assert count == 0
