"""OpenEI URDB API client.

Fetches utility and rate plan data from the OpenEI Utility Rate
Database.  All functions accept api_url and api_key so the base URL
and credentials are caller-configurable.
"""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

from span_panel_simulator.rates.types import RatePlanSummary, UtilitySummary

_LOG = logging.getLogger(__name__)


class OpenEIError(Exception):
    """Raised when the URDB API returns an error or unexpected response."""


async def _get_json(url: str, params: dict[str, str]) -> dict[str, Any]:
    """Issue a GET request and return the parsed JSON response."""
    async with aiohttp.ClientSession() as session, session.get(url, params=params) as resp:
        if resp.status != 200:
            body = await resp.text()
            raise OpenEIError(f"HTTP {resp.status}: {body[:200]}")
        data: dict[str, Any] = await resp.json(content_type=None)
        return data


async def fetch_utilities(
    lat: float,
    lon: float,
    api_url: str,
    api_key: str,
) -> list[UtilitySummary]:
    """Fetch utilities near a lat/lon from URDB.

    Returns de-duplicated utilities sorted by name.
    """
    params = {
        "version": "3",
        "format": "json",
        "api_key": api_key,
        "lat": str(lat),
        "lon": str(lon),
        "sector": "Residential",
        "detail": "minimal",
    }
    data = await _get_json(api_url, params)
    items = data.get("items", [])

    seen: set[str] = set()
    utilities: list[UtilitySummary] = []
    for item in items:
        name = item.get("utility_name", item.get("utility", ""))
        if not name or name in seen:
            continue
        seen.add(name)
        utilities.append(
            UtilitySummary(
                utility_name=name,
                eia_id=str(item.get("eia", "")),
            )
        )
    utilities.sort(key=lambda u: u.utility_name)
    return utilities


async def fetch_rate_plans(
    utility: str,
    api_url: str,
    api_key: str,
    sector: str = "Residential",
) -> list[RatePlanSummary]:
    """Fetch available rate plans for a utility."""
    params = {
        "version": "3",
        "format": "json",
        "api_key": api_key,
        "ratesforutility": utility,
        "sector": sector,
        "detail": "minimal",
    }
    data = await _get_json(api_url, params)
    items = data.get("items", [])

    plans: list[RatePlanSummary] = []
    for item in items:
        plans.append(
            RatePlanSummary(
                label=item.get("label", ""),
                name=item.get("name", ""),
                startdate=item.get("startdate", 0),
                enddate=item.get("enddate"),
                description=item.get("description", ""),
            )
        )
    return plans


async def fetch_rate_detail(
    label: str,
    api_url: str,
    api_key: str,
) -> dict[str, Any]:
    """Fetch the full rate record for a URDB label.

    Returns the raw URDB record dict (to be stored verbatim).
    Raises OpenEIError if the label is not found.
    """
    params = {
        "version": "3",
        "format": "json",
        "api_key": api_key,
        "getpage": label,
        "detail": "full",
    }
    data = await _get_json(api_url, params)
    items = data.get("items", [])
    if not items:
        raise OpenEIError(f"Rate plan '{label}' not found in URDB")
    record: dict[str, Any] = items[0]
    return record
