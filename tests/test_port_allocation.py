"""Tests for SimulatorApp port allocation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from span_panel_simulator.app import SimulatorApp

if TYPE_CHECKING:
    from pathlib import Path


def _make_app(tmp_path: Path, base_http_port: int = 9000) -> SimulatorApp:
    """Create a SimulatorApp with the given base port."""
    return SimulatorApp(config_dir=tmp_path, base_http_port=base_http_port)


class TestPortAllocation:
    """Port allocation and release logic."""

    def test_allocate_sequential_ports(self, tmp_path: Path) -> None:
        """Ports are sequential from base."""
        app = _make_app(tmp_path, base_http_port=9000)
        p1 = app._allocate_port()
        p2 = app._allocate_port()
        p3 = app._allocate_port()
        assert p1 == 9000
        assert p2 == 9001
        assert p3 == 9002

    def test_release_and_reuse(self, tmp_path: Path) -> None:
        """Released ports are reused (lowest available)."""
        app = _make_app(tmp_path, base_http_port=9000)
        p1 = app._allocate_port()
        p2 = app._allocate_port()
        p3 = app._allocate_port()
        assert (p1, p2, p3) == (9000, 9001, 9002)

        # Release the middle port
        app._release_port(9001)

        # Next allocation should reuse 9001 (lowest available)
        p4 = app._allocate_port()
        assert p4 == 9001

    def test_release_nonexistent_port(self, tmp_path: Path) -> None:
        """Releasing unknown port is a no-op."""
        app = _make_app(tmp_path, base_http_port=9000)
        # Should not raise
        app._release_port(12345)

    def test_rapid_allocate_release_cycle(self, tmp_path: Path) -> None:
        """No port leaks after cycles."""
        app = _make_app(tmp_path, base_http_port=9000)

        # Allocate 5 ports
        ports = [app._allocate_port() for _ in range(5)]
        assert ports == [9000, 9001, 9002, 9003, 9004]

        # Release all
        for p in ports:
            app._release_port(p)

        # All should be reusable, starting from base
        ports2 = [app._allocate_port() for _ in range(5)]
        assert ports2 == [9000, 9001, 9002, 9003, 9004]

        # No leaks — used_ports should have exactly 5 entries
        assert len(app._used_ports) == 5
