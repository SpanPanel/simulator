"""Tests for Socket.IO handler and config location update."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
import yaml

from span_panel_simulator.clone import update_config_location
from span_panel_simulator.sio_handler import SioContext, _PanelNamespace

if TYPE_CHECKING:
    from pathlib import Path


def _minimal_config(
    serial: str = "sim-TEST-001",
    lat: float = 37.7,
    lon: float = -122.4,
) -> dict[str, object]:
    """Build a minimal valid YAML config dict."""
    return {
        "panel_config": {
            "serial_number": serial,
            "total_tabs": 32,
            "main_size": 200,
            "latitude": lat,
            "longitude": lon,
        },
        "circuit_templates": {
            "default_consumer": {
                "energy_profile": {
                    "mode": "consumer",
                    "power_range": [0.0, 1800.0],
                    "typical_power": 500.0,
                    "power_variation": 0.1,
                },
                "relay_behavior": "controllable",
                "priority": "MUST_HAVE",
            },
        },
        "circuits": [
            {
                "id": "circuit_1",
                "name": "Test Circuit",
                "template": "default_consumer",
                "tabs": [1],
            },
        ],
        "unmapped_tabs": list(range(2, 33)),
        "simulation_params": {
            "update_interval": 5,
            "time_acceleration": 1.0,
            "noise_factor": 0.02,
            "enable_realistic_behaviors": True,
        },
    }


# ------------------------------------------------------------------
# update_config_location
# ------------------------------------------------------------------


class TestUpdateConfigLocation:
    """Tests for the YAML config location update utility."""

    def test_updates_latitude_longitude(self, tmp_path: Path) -> None:
        """Lat/lon are written to the config file."""
        config_path = tmp_path / "panel.yaml"
        config_path.write_text(yaml.dump(_minimal_config()))

        update_config_location(config_path, 40.7128, -74.0060)

        loaded = yaml.safe_load(config_path.read_text())
        assert loaded["panel_config"]["latitude"] == 40.7128
        assert loaded["panel_config"]["longitude"] == -74.0060

    def test_resolves_timezone(self, tmp_path: Path) -> None:
        """Timezone is resolved from coordinates and written to config."""
        config_path = tmp_path / "panel.yaml"
        config_path.write_text(yaml.dump(_minimal_config()))

        tz = update_config_location(config_path, 40.7128, -74.0060)

        assert tz == "America/New_York"
        loaded = yaml.safe_load(config_path.read_text())
        assert loaded["panel_config"]["time_zone"] == "America/New_York"

    def test_returns_timezone_string(self, tmp_path: Path) -> None:
        """Return value is the resolved IANA timezone."""
        config_path = tmp_path / "panel.yaml"
        config_path.write_text(yaml.dump(_minimal_config()))

        tz = update_config_location(config_path, 51.5074, -0.1278)
        assert tz == "Europe/London"

    def test_southern_hemisphere_timezone(self, tmp_path: Path) -> None:
        """Southern hemisphere coordinates resolve correctly."""
        config_path = tmp_path / "panel.yaml"
        config_path.write_text(yaml.dump(_minimal_config()))

        tz = update_config_location(config_path, -33.8688, 151.2093)
        assert tz == "Australia/Sydney"

    def test_preserves_other_config_fields(self, tmp_path: Path) -> None:
        """Non-location fields in panel_config are preserved."""
        config_path = tmp_path / "panel.yaml"
        config_path.write_text(yaml.dump(_minimal_config()))

        update_config_location(config_path, 40.7128, -74.0060)

        loaded = yaml.safe_load(config_path.read_text())
        assert loaded["panel_config"]["serial_number"] == "sim-TEST-001"
        assert loaded["panel_config"]["total_tabs"] == 32
        assert len(loaded["circuits"]) == 1

    def test_invalid_config_raises(self, tmp_path: Path) -> None:
        """Non-dict YAML raises ValueError."""
        config_path = tmp_path / "bad.yaml"
        config_path.write_text("just a string\n")

        with pytest.raises(ValueError, match="Invalid config"):
            update_config_location(config_path, 40.0, -74.0)


# ------------------------------------------------------------------
# _PanelNamespace event handlers
# ------------------------------------------------------------------


class TestPanelNamespace:
    """Tests for Socket.IO event handlers (unit-tested without server)."""

    def _make_namespace(
        self,
        update_result: dict[str, str] | None = None,
    ) -> tuple[_PanelNamespace, AsyncMock]:
        """Build a namespace instance with a mock context."""
        mock_update = AsyncMock(
            return_value=update_result or {"status": "ok", "time_zone": "America/New_York"}
        )
        mock_clone = AsyncMock(return_value={"status": "ok"})
        mock_profiles = AsyncMock(return_value={"status": "ok", "templates_updated": 0})
        ctx = SioContext(
            update_panel_location=mock_update,
            clone_panel=mock_clone,
            apply_usage_profiles=mock_profiles,
        )
        ns = _PanelNamespace("/v1/panel", ctx)
        return ns, mock_update

    @pytest.mark.asyncio
    async def test_set_location_calls_callback(self) -> None:
        """Valid set_location event invokes the update callback."""
        ns, mock = self._make_namespace()

        result = await ns.on_set_location(
            "sid-1",
            {"serial": "PANEL-001", "latitude": 40.7, "longitude": -74.0},
        )

        mock.assert_awaited_once_with("PANEL-001", 40.7, -74.0)
        assert result["status"] == "ok"
        assert result["time_zone"] == "America/New_York"

    @pytest.mark.asyncio
    async def test_set_location_missing_serial(self) -> None:
        """Missing serial returns error without calling callback."""
        ns, mock = self._make_namespace()

        result = await ns.on_set_location(
            "sid-1",
            {"latitude": 40.7, "longitude": -74.0},
        )

        mock.assert_not_awaited()
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_set_location_missing_latitude(self) -> None:
        """Missing latitude returns error."""
        ns, mock = self._make_namespace()

        result = await ns.on_set_location(
            "sid-1",
            {"serial": "PANEL-001", "longitude": -74.0},
        )

        mock.assert_not_awaited()
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_set_location_missing_longitude(self) -> None:
        """Missing longitude returns error."""
        ns, mock = self._make_namespace()

        result = await ns.on_set_location(
            "sid-1",
            {"serial": "PANEL-001", "latitude": 40.7},
        )

        mock.assert_not_awaited()
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_set_location_integer_coords(self) -> None:
        """Integer coordinates are accepted and converted to float."""
        ns, mock = self._make_namespace()

        result = await ns.on_set_location(
            "sid-1",
            {"serial": "PANEL-001", "latitude": 41, "longitude": -74},
        )

        mock.assert_awaited_once_with("PANEL-001", 41.0, -74.0)
        assert result["status"] == "ok"

    @pytest.mark.asyncio
    async def test_set_location_invalid_serial_type(self) -> None:
        """Non-string serial returns error."""
        ns, mock = self._make_namespace()

        result = await ns.on_set_location(
            "sid-1",
            {"serial": 123, "latitude": 40.7, "longitude": -74.0},
        )

        mock.assert_not_awaited()
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_set_location_empty_serial(self) -> None:
        """Empty string serial returns error."""
        ns, mock = self._make_namespace()

        result = await ns.on_set_location(
            "sid-1",
            {"serial": "", "latitude": 40.7, "longitude": -74.0},
        )

        mock.assert_not_awaited()
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_set_location_string_latitude(self) -> None:
        """String latitude returns error."""
        ns, mock = self._make_namespace()

        result = await ns.on_set_location(
            "sid-1",
            {"serial": "PANEL-001", "latitude": "not-a-number", "longitude": -74.0},
        )

        mock.assert_not_awaited()
        assert result["status"] == "error"

    # apply_usage_profiles event tests

    @pytest.mark.asyncio
    async def test_apply_profiles_calls_callback(self) -> None:
        """Valid apply_usage_profiles event invokes the callback."""
        ns, _ = self._make_namespace()
        profiles = {"clone_1": {"typical_power": 200.0}}

        result = await ns.on_apply_usage_profiles(
            "sid-1",
            {"clone_serial": "sim-TEST-clone", "profiles": profiles},
        )

        ns._ctx.apply_usage_profiles.assert_awaited_once_with("sim-TEST-clone", profiles)
        assert result["status"] == "ok"

    @pytest.mark.asyncio
    async def test_apply_profiles_missing_serial(self) -> None:
        """Missing clone_serial returns error."""
        ns, _ = self._make_namespace()

        result = await ns.on_apply_usage_profiles(
            "sid-1",
            {"profiles": {"clone_1": {"typical_power": 200.0}}},
        )

        ns._ctx.apply_usage_profiles.assert_not_awaited()
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_apply_profiles_empty_serial(self) -> None:
        """Empty clone_serial returns error."""
        ns, _ = self._make_namespace()

        result = await ns.on_apply_usage_profiles(
            "sid-1",
            {"clone_serial": "", "profiles": {"clone_1": {}}},
        )

        ns._ctx.apply_usage_profiles.assert_not_awaited()
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_apply_profiles_missing_profiles(self) -> None:
        """Missing profiles dict returns error."""
        ns, _ = self._make_namespace()

        result = await ns.on_apply_usage_profiles(
            "sid-1",
            {"clone_serial": "sim-TEST-clone"},
        )

        ns._ctx.apply_usage_profiles.assert_not_awaited()
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_apply_profiles_empty_profiles(self) -> None:
        """Empty profiles dict returns error."""
        ns, _ = self._make_namespace()

        result = await ns.on_apply_usage_profiles(
            "sid-1",
            {"clone_serial": "sim-TEST-clone", "profiles": {}},
        )

        ns._ctx.apply_usage_profiles.assert_not_awaited()
        assert result["status"] == "error"
