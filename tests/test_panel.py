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
