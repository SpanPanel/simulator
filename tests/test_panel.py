"""Tests for PanelInstance lifecycle."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from span_panel_simulator.panel import PanelInstance


@pytest.fixture
def simple_config(tmp_path: Path) -> Path:
    """Write a minimal YAML config and return its path."""
    config = tmp_path / "test_panel.yaml"
    config.write_text("""\
panel_config:
  serial_number: "SIM-PANEL-A"
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
""")
    return config


class TestPanelInstance:
    """PanelInstance start/stop/reload lifecycle."""

    @pytest.mark.asyncio
    async def test_start_and_stop(self, simple_config: Path) -> None:
        publish = AsyncMock()
        panel = PanelInstance(simple_config, publish)

        serial = await panel.start()
        assert serial == "SIM-PANEL-A"
        assert panel.is_running
        assert panel.engine is not None
        assert panel.publisher is not None

        # Should have published $state=init, $description, properties, $state=ready
        assert publish.call_count > 0

        await panel.stop()
        assert not panel.is_running

    @pytest.mark.asyncio
    async def test_reload_restarts(self, simple_config: Path) -> None:
        publish = AsyncMock()
        panel = PanelInstance(simple_config, publish)

        await panel.start()
        assert panel.is_running

        serial = await panel.reload()
        assert serial == "SIM-PANEL-A"
        assert panel.is_running

        await panel.stop()

    @pytest.mark.asyncio
    async def test_serial_before_start_raises(self, simple_config: Path) -> None:
        panel = PanelInstance(simple_config, AsyncMock())
        with pytest.raises(RuntimeError, match="not initialised"):
            _ = panel.serial_number


class TestEngineTotalTabsProperty:
    """total_tabs property behavior before and after initialization."""

    def test_raises_before_initialize(self) -> None:
        """total_tabs must raise if accessed before initialize_async()."""
        from span_panel_simulator.engine import DynamicSimulationEngine

        engine = DynamicSimulationEngine(config_path=None, recorder=None)
        with pytest.raises(RuntimeError, match="initialize_async"):
            _ = engine.total_tabs

    @pytest.mark.asyncio
    async def test_returns_config_value_after_initialize(self, simple_config: Path) -> None:
        """total_tabs returns the configured value after initialization."""
        from span_panel_simulator.engine import DynamicSimulationEngine

        engine = DynamicSimulationEngine(config_path=simple_config, recorder=None)
        await engine.initialize_async()
        assert engine.total_tabs == 8


@pytest.fixture
def main40_config(tmp_path: Path) -> Path:
    """Write a 40-tab YAML config and return its path."""
    config = tmp_path / "test_main40.yaml"
    config.write_text("""\
panel_config:
  serial_number: "SIM-40T-TEST"
  total_tabs: 40
  main_size: 200

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
  - id: "c_low"
    name: "Low Tab"
    template: "lighting"
    tabs: [1]
  - id: "c_high"
    name: "High Tab"
    template: "lighting"
    tabs: [39]

unmapped_tabs: []

simulation_params:
  update_interval: 5
  time_acceleration: 1.0
  noise_factor: 0.0
  enable_realistic_behaviors: false
""")
    return config


class TestEngineSnapshotPanelSize:
    """Snapshot reflects configured panel size and derived panel model."""

    @pytest.mark.asyncio
    async def test_40_tab_snapshot(self, main40_config: Path) -> None:
        from span_panel_simulator.engine import DynamicSimulationEngine

        engine = DynamicSimulationEngine(config_path=main40_config, recorder=None)
        await engine.initialize_async()

        assert engine.total_tabs == 40

        snapshot = await engine.get_snapshot()
        assert snapshot.panel_size == 40
        assert snapshot.panel_model == "MAIN_40"
