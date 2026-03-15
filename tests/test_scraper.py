"""Tests for the eBus scraper (scraper.py)."""

from __future__ import annotations

import pytest

from span_panel_simulator.scraper import (
    PanelCredentials,
    ScrapeError,
    _validate_required_topics,
)

_SERIAL = "nj-2316-TEST"
_PREFIX = f"ebus/5/{_SERIAL}"


class TestScrapeError:
    """Tests for the ScrapeError exception."""

    def test_phase_stored(self) -> None:
        err = ScrapeError("connecting", "timeout")
        assert err.phase == "connecting"
        assert str(err) == "timeout"


class TestPanelCredentials:
    """Tests for the PanelCredentials dataclass."""

    def test_frozen(self) -> None:
        creds = PanelCredentials(
            username="user",
            password="pass",
            serial_number=_SERIAL,
            mqtts_port=8883,
            broker_host="192.168.1.1",
        )
        assert creds.serial_number == _SERIAL
        with pytest.raises(AttributeError):
            creds.username = "other"  # frozen dataclass


class TestValidateRequiredTopics:
    """Tests for _validate_required_topics()."""

    def _valid_collected(self) -> dict[str, str]:
        return {
            f"{_PREFIX}/$state": "ready",
            f"{_PREFIX}/$description": "{}",
            f"{_PREFIX}/core/serial-number": _SERIAL,
            f"{_PREFIX}/aaa111/name": "Test Circuit",
        }

    def _valid_description(self) -> dict[str, dict[str, dict[str, str]]]:
        return {
            "nodes": {
                "aaa111": {"type": "energy.ebus.device.circuit"},
            }
        }

    def test_valid_passes(self) -> None:
        _validate_required_topics(
            self._valid_collected(),
            self._valid_description(),
            _SERIAL,
        )

    def test_missing_description_raises(self) -> None:
        with pytest.raises(ScrapeError, match="No \\$description"):
            _validate_required_topics(self._valid_collected(), {}, _SERIAL)

    def test_missing_state_raises(self) -> None:
        collected = self._valid_collected()
        del collected[f"{_PREFIX}/$state"]
        with pytest.raises(ScrapeError, match="No \\$state"):
            _validate_required_topics(collected, self._valid_description(), _SERIAL)

    def test_missing_serial_raises(self) -> None:
        collected = self._valid_collected()
        del collected[f"{_PREFIX}/core/serial-number"]
        with pytest.raises(ScrapeError, match="No core/serial-number"):
            _validate_required_topics(collected, self._valid_description(), _SERIAL)

    def test_no_circuit_nodes_raises(self) -> None:
        desc: dict[str, dict[str, dict[str, str]]] = {
            "nodes": {
                "core": {"type": "energy.ebus.device.distribution-enclosure.core"},
            }
        }
        with pytest.raises(ScrapeError, match="No circuit nodes"):
            _validate_required_topics(self._valid_collected(), desc, _SERIAL)
