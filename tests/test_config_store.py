"""Tests for ConfigStore dirty-state tracking."""

from __future__ import annotations

from textwrap import dedent
from typing import TYPE_CHECKING

from span_panel_simulator.dashboard.config_store import ConfigStore

if TYPE_CHECKING:
    from pathlib import Path

MINIMAL_YAML = dedent("""\
    panel_config:
      serial_number: "TEST-001"
      total_tabs: 16
      main_size: 200
      latitude: 37.7
      longitude: -122.4
    circuit_templates:
      lighting:
        energy_profile:
          mode: consumer
          power_range: [0.0, 500.0]
          typical_power: 80.0
          power_variation: 0.1
        relay_behavior: controllable
        priority: NEVER
    circuits:
      - id: light_1
        name: Light 1
        template: lighting
        tabs: [1]
    simulation_params:
      update_interval: 5
      time_acceleration: 1.0
      noise_factor: 0.02
      enable_realistic_behaviors: true
""")


class TestDirtyFlag:
    def test_starts_clean(self) -> None:
        store = ConfigStore()
        assert store.dirty is False

    def test_load_from_yaml_clears_dirty(self) -> None:
        store = ConfigStore()
        store.load_from_yaml(MINIMAL_YAML)
        assert store.dirty is False

    def test_load_from_file_clears_dirty(self, tmp_path: Path) -> None:
        f = tmp_path / "test.yaml"
        f.write_text(MINIMAL_YAML)
        store = ConfigStore()
        store.load_from_file(f)
        assert store.dirty is False

    def test_update_panel_config_sets_dirty(self) -> None:
        store = ConfigStore()
        store.load_from_yaml(MINIMAL_YAML)
        store.update_panel_config({"serial_number": "CHANGED"})
        assert store.dirty is True

    def test_update_simulation_params_sets_dirty(self) -> None:
        store = ConfigStore()
        store.load_from_yaml(MINIMAL_YAML)
        store.update_simulation_params({"update_interval": 10})
        assert store.dirty is True

    def test_add_entity_sets_dirty(self) -> None:
        store = ConfigStore()
        store.load_from_yaml(MINIMAL_YAML)
        store.add_entity("circuit")
        assert store.dirty is True

    def test_delete_entity_sets_dirty(self) -> None:
        store = ConfigStore()
        store.load_from_yaml(MINIMAL_YAML)
        store.delete_entity("light_1")
        assert store.dirty is True

    def test_update_entity_sets_dirty(self) -> None:
        store = ConfigStore()
        store.load_from_yaml(MINIMAL_YAML)
        store.update_entity("light_1", {"name": "New Name"})
        assert store.dirty is True

    def test_update_entity_profile_sets_dirty(self) -> None:
        store = ConfigStore()
        store.load_from_yaml(MINIMAL_YAML)
        store.update_entity_profile("light_1", {h: 0.5 for h in range(24)})
        assert store.dirty is True

    def test_save_to_file_clears_dirty(self, tmp_path: Path) -> None:
        store = ConfigStore()
        store.load_from_yaml(MINIMAL_YAML)
        store.update_panel_config({"serial_number": "CHANGED"})
        assert store.dirty is True
        out = tmp_path / "out.yaml"
        store.save_to_file(out)
        assert store.dirty is False
        assert out.exists()

    def test_load_after_dirty_clears_flag(self) -> None:
        store = ConfigStore()
        store.load_from_yaml(MINIMAL_YAML)
        store.update_panel_config({"serial_number": "CHANGED"})
        assert store.dirty is True
        store.load_from_yaml(MINIMAL_YAML)
        assert store.dirty is False


class TestDirtyStateAfterMutation:
    """Tests for dirty flag transitions across mutations and loads."""

    def test_clean_store_reports_not_dirty(self) -> None:
        store = ConfigStore()
        store.load_from_yaml(MINIMAL_YAML)
        assert store.dirty is False

    def test_mutated_store_reports_dirty(self) -> None:
        store = ConfigStore()
        store.load_from_yaml(MINIMAL_YAML)
        store.update_panel_config({"serial_number": "X"})
        assert store.dirty is True


class TestSaveToFile:
    """Tests for save_to_file round-trip."""

    def test_saved_file_is_valid_yaml(self, tmp_path: Path) -> None:
        store = ConfigStore()
        store.load_from_yaml(MINIMAL_YAML)
        store.update_panel_config({"serial_number": "SAVED"})
        out = tmp_path / "saved.yaml"
        store.save_to_file(out)

        # Reload and verify
        store2 = ConfigStore()
        store2.load_from_file(out)
        assert store2.get_panel_config()["serial_number"] == "SAVED"
        assert store2.dirty is False
