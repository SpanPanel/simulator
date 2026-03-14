"""Tests for SimulatorApp multi-panel config discovery and reload."""

from __future__ import annotations

from typing import TYPE_CHECKING

from span_panel_simulator.app import _discover_configs, _file_hash

if TYPE_CHECKING:
    from pathlib import Path

_SIMPLE_CONFIG = """\
panel_config:
  serial_number: "{serial}"
  total_tabs: 8
  main_size: 100

circuit_templates:
  lighting:
    energy_profile:
      mode: "consumer"
      power_range: [5.0, 50.0]
      typical_power: 25.0
      power_variation: 0.1
    relay_behavior: "controllable"
    priority: "NEVER"

circuits:
  - id: "test_circuit"
    name: "Test Circuit"
    template: "lighting"
    tabs: [1]

unmapped_tabs: []

simulation_params:
  update_interval: 5
  time_acceleration: 1.0
  noise_factor: 0.0
  enable_realistic_behaviors: false
"""


def _write_config(config_dir: Path, name: str, serial: str) -> Path:
    path = config_dir / name
    path.write_text(_SIMPLE_CONFIG.format(serial=serial))
    return path


class TestConfigDiscovery:
    """Config directory scanning."""

    def test_finds_yaml_files(self, tmp_path: Path) -> None:
        _write_config(tmp_path, "a.yaml", "PANEL-A")
        _write_config(tmp_path, "b.yml", "PANEL-B")
        (tmp_path / "not_config.txt").write_text("ignored")

        configs = _discover_configs(tmp_path)
        assert len(configs) == 2
        names = {p.name for p in configs}
        assert names == {"a.yaml", "b.yml"}

    def test_empty_directory(self, tmp_path: Path) -> None:
        configs = _discover_configs(tmp_path)
        assert len(configs) == 0

    def test_hash_changes_on_modification(self, tmp_path: Path) -> None:
        path = _write_config(tmp_path, "panel.yaml", "PANEL-A")
        hash1 = _file_hash(path)

        path.write_text(_SIMPLE_CONFIG.format(serial="PANEL-B"))
        hash2 = _file_hash(path)

        assert hash1 != hash2

    def test_hash_stable_for_same_content(self, tmp_path: Path) -> None:
        path = _write_config(tmp_path, "panel.yaml", "PANEL-A")
        assert _file_hash(path) == _file_hash(path)
