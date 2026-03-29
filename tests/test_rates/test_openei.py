"""Tests for the OpenEI URDB API client (mocked HTTP)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from span_panel_simulator.rates.openei import (
    OpenEIError,
    fetch_rate_detail,
    fetch_rate_plans,
    fetch_utilities,
)

API_URL = "https://api.openei.org/utility_rates"
API_KEY = "test-key"


class TestFetchUtilities:
    """Fetch utilities by lat/lon."""

    @pytest.mark.asyncio
    async def test_returns_utility_summaries(self) -> None:
        response_data = {
            "items": [
                {"utility_name": "Pacific Gas & Electric Co", "eia": "14328"},
                {"utility_name": "City of Palo Alto", "eia": "14328"},
            ]
        }
        with patch("span_panel_simulator.rates.openei._get_json", return_value=response_data):
            result = await fetch_utilities(37.7, -122.4, API_URL, API_KEY)
        assert len(result) >= 1
        assert result[0].utility_name == "City of Palo Alto"  # sorted alphabetically

    @pytest.mark.asyncio
    async def test_empty_result(self) -> None:
        with patch("span_panel_simulator.rates.openei._get_json", return_value={"items": []}):
            result = await fetch_utilities(0.0, 0.0, API_URL, API_KEY)
        assert result == []

    @pytest.mark.asyncio
    async def test_deduplicates_utilities(self) -> None:
        response_data = {
            "items": [
                {"utility_name": "PG&E", "eia": "14328"},
                {"utility_name": "PG&E", "eia": "14328"},
            ]
        }
        with patch("span_panel_simulator.rates.openei._get_json", return_value=response_data):
            result = await fetch_utilities(37.7, -122.4, API_URL, API_KEY)
        assert len(result) == 1


class TestFetchRatePlans:
    """Fetch rate plans for a utility."""

    @pytest.mark.asyncio
    async def test_returns_plan_summaries(self) -> None:
        response_data = {
            "items": [
                {
                    "label": "abc123",
                    "name": "E-TOU-C",
                    "startdate": 1672531200,
                    "enddate": None,
                    "description": "Time of use residential",
                },
                {
                    "label": "def456",
                    "name": "E-TOU-D",
                    "startdate": 1672531200,
                    "enddate": 1704067200,
                    "description": "Legacy TOU",
                },
            ]
        }
        with patch("span_panel_simulator.rates.openei._get_json", return_value=response_data):
            result = await fetch_rate_plans("Pacific Gas & Electric Co", API_URL, API_KEY)
        assert len(result) == 2
        assert result[0].label == "abc123"
        assert result[0].name == "E-TOU-C"
        assert result[1].enddate == 1704067200


class TestFetchRateDetail:
    """Fetch full rate detail by label."""

    @pytest.mark.asyncio
    async def test_returns_full_record(self) -> None:
        response_data = {
            "items": [
                {
                    "label": "abc123",
                    "utility": "PG&E",
                    "name": "E-TOU-C",
                    "energyratestructure": [[{"rate": 0.25}]],
                    "energyweekdayschedule": [[0] * 24 for _ in range(12)],
                    "energyweekendschedule": [[0] * 24 for _ in range(12)],
                }
            ]
        }
        with patch("span_panel_simulator.rates.openei._get_json", return_value=response_data):
            result = await fetch_rate_detail("abc123", API_URL, API_KEY)
        assert result["label"] == "abc123"
        assert result["energyratestructure"] == [[{"rate": 0.25}]]

    @pytest.mark.asyncio
    async def test_label_not_found_raises(self) -> None:
        with (
            patch("span_panel_simulator.rates.openei._get_json", return_value={"items": []}),
            pytest.raises(OpenEIError, match="not found"),
        ):
            await fetch_rate_detail("nonexistent", API_URL, API_KEY)

    @pytest.mark.asyncio
    async def test_api_error_raises(self) -> None:
        with (
            patch(
                "span_panel_simulator.rates.openei._get_json",
                side_effect=OpenEIError("HTTP 401: Unauthorized"),
            ),
            pytest.raises(OpenEIError, match="401"),
        ):
            await fetch_rate_detail("abc123", API_URL, API_KEY)
