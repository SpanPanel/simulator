"""Shared test fixtures for the simulator test suite."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from span_panel_simulator.models import (
    SpanBatterySnapshot,
    SpanCircuitSnapshot,
    SpanPanelSnapshot,
    SpanPVSnapshot,
)
from span_panel_simulator.publisher import HomiePublisher


@pytest.fixture
def sample_snapshot() -> SpanPanelSnapshot:
    """A minimal but complete snapshot for testing."""
    circuits = {
        "living_room_lights": SpanCircuitSnapshot(
            circuit_id="living_room_lights",
            name="Living Room Lights",
            relay_state="CLOSED",
            instant_power_w=150.0,
            produced_energy_wh=0.0,
            consumed_energy_wh=5000.0,
            tabs=[1],
            priority="MUST_HAVE",
            is_user_controllable=True,
            is_sheddable=False,
            is_never_backup=False,
        ),
        "kitchen_outlets": SpanCircuitSnapshot(
            circuit_id="kitchen_outlets",
            name="Kitchen Outlets",
            relay_state="CLOSED",
            instant_power_w=800.0,
            produced_energy_wh=0.0,
            consumed_energy_wh=25000.0,
            tabs=[3, 5],
            priority="NICE_TO_HAVE",
            is_user_controllable=True,
            is_sheddable=True,
            is_never_backup=False,
            is_240v=True,
            current_a=6.7,
            breaker_rating_a=20.0,
        ),
        "unmapped_tab_2": SpanCircuitSnapshot(
            circuit_id="unmapped_tab_2",
            name="Unmapped Tab 2",
            relay_state="CLOSED",
            instant_power_w=0.0,
            produced_energy_wh=0.0,
            consumed_energy_wh=0.0,
            tabs=[2],
            priority="UNKNOWN",
            is_user_controllable=False,
            is_sheddable=False,
            is_never_backup=False,
        ),
    }

    return SpanPanelSnapshot(
        serial_number="SPAN-TEST-001",
        firmware_version="spanos2/sim/01",
        main_relay_state="CLOSED",
        instant_grid_power_w=950.0,
        feedthrough_power_w=0.0,
        main_meter_energy_consumed_wh=100000.0,
        main_meter_energy_produced_wh=0.0,
        feedthrough_energy_consumed_wh=0.0,
        feedthrough_energy_produced_wh=0.0,
        dsm_state="DSM_ON_GRID",
        current_run_config="PANEL_ON_GRID",
        door_state="CLOSED",
        proximity_proven=True,
        uptime_s=3600,
        eth0_link=True,
        wlan_link=False,
        wwan_link=False,
        panel_size=8,
        dominant_power_source="GRID",
        grid_islandable=False,
        l1_voltage=121.3,
        l2_voltage=119.8,
        main_breaker_rating_a=200,
        power_flow_grid=950.0,
        power_flow_site=950.0,
        power_flow_pv=0.0,
        power_flow_battery=0.0,
        upstream_l1_current_a=3.96,
        upstream_l2_current_a=3.96,
        downstream_l1_current_a=0.0,
        downstream_l2_current_a=0.0,
        circuits=circuits,
        battery=SpanBatterySnapshot(),
        pv=SpanPVSnapshot(),
    )


@pytest.fixture
def publish_mock() -> AsyncMock:
    """Mock publish function that records calls."""
    return AsyncMock()


@pytest.fixture
def publisher(publish_mock: AsyncMock) -> HomiePublisher:
    """HomiePublisher wired to a mock publish function."""
    return HomiePublisher(
        serial_number="SPAN-TEST-001",
        publish_fn=publish_mock,
    )
