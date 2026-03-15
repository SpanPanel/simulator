"""Tests for the clone WebSocket handler (clone_handler.py)."""

from __future__ import annotations

import json

from span_panel_simulator.clone_handler import _parse_request


class TestParseRequest:
    """Tests for _parse_request() validation."""

    def test_valid_request(self) -> None:
        data = json.dumps(
            {
                "type": "clone_panel",
                "host": "192.168.1.100",
                "passphrase": "secret",
            }
        )
        result = _parse_request(data)
        assert result == ("192.168.1.100", "secret")

    def test_null_passphrase(self) -> None:
        data = json.dumps(
            {
                "type": "clone_panel",
                "host": "192.168.1.100",
                "passphrase": None,
            }
        )
        result = _parse_request(data)
        assert result == ("192.168.1.100", None)

    def test_no_passphrase(self) -> None:
        data = json.dumps(
            {
                "type": "clone_panel",
                "host": "192.168.1.100",
            }
        )
        result = _parse_request(data)
        assert result == ("192.168.1.100", None)

    def test_wrong_type(self) -> None:
        data = json.dumps({"type": "something_else", "host": "192.168.1.100"})
        assert _parse_request(data) is None

    def test_missing_host(self) -> None:
        data = json.dumps({"type": "clone_panel"})
        assert _parse_request(data) is None

    def test_empty_host(self) -> None:
        data = json.dumps({"type": "clone_panel", "host": ""})
        assert _parse_request(data) is None

    def test_invalid_json(self) -> None:
        assert _parse_request("not json") is None

    def test_non_dict(self) -> None:
        assert _parse_request(json.dumps([1, 2, 3])) is None

    def test_non_string_passphrase(self) -> None:
        data = json.dumps(
            {
                "type": "clone_panel",
                "host": "192.168.1.100",
                "passphrase": 12345,
            }
        )
        assert _parse_request(data) is None
