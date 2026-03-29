"""Core types for ToU rate integration.

URDBRecord mirrors the OpenEI URDB API response schema.  Records are
stored verbatim — never modified after fetch.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypedDict

# -- URDB schema types (read-only mirrors of API response) ---------------


class URDBRateTier(TypedDict, total=False):
    """Single tier within a rate period."""

    rate: float  # $/kWh
    max: float  # kWh ceiling for this tier (absent on last tier)
    unit: str  # e.g. "kWh"
    adj: float  # adjustment factor


class URDBRecord(TypedDict, total=False):
    """Subset of the OpenEI URDB v3 rate record we consume.

    Stored verbatim from the API — all fields are optional because
    different rate plans populate different subsets.
    """

    label: str
    utility: str
    name: str
    uri: str
    startdate: int  # epoch seconds
    enddate: int  # epoch seconds
    sector: str
    description: str
    source: str

    # Energy charges
    energyratestructure: list[list[URDBRateTier]]
    energyweekdayschedule: list[list[int]]  # 12 x 24 (month x hour)
    energyweekendschedule: list[list[int]]  # 12 x 24

    # Export / sell rates
    sell: list[list[URDBRateTier]]  # same shape as energyratestructure
    usenetmetering: bool

    # Fixed charges
    fixedmonthlycharge: float
    minmonthlycharge: float
    annualmincharge: float

    # Demand charges (flat)
    flatdemandstructure: list[list[URDBRateTier]]
    flatdemandmonths: list[list[int]]  # 12-element, period per month

    # Demand charges (time-based) — stored but not used in v1
    demandratestructure: list[list[URDBRateTier]]
    demandweekdayschedule: list[list[int]]
    demandweekendschedule: list[list[int]]


# -- Metadata and cache types -------------------------------------------


@dataclass(frozen=True)
class AttributionMeta:
    """Provenance metadata for a cached rate record."""

    provider: str
    url: str
    license: str
    api_version: int


@dataclass(frozen=True)
class RateCacheEntry:
    """A cached URDB record with its metadata envelope."""

    source: str
    retrieved_at: str  # ISO 8601
    attribution: AttributionMeta
    record: dict[str, Any]  # raw URDB JSON — typed access via URDBRecord


@dataclass(frozen=True)
class OpenEIConfig:
    """User-configurable OpenEI API settings."""

    api_url: str = "https://api.openei.org/utility_rates"
    api_key: str = ""


# -- Cost calculation result --------------------------------------------


@dataclass(frozen=True)
class CostLedger:
    """Result of applying a rate schedule to a power time-series."""

    import_cost: float  # $ over horizon
    export_credit: float  # $ over horizon
    fixed_charges: float  # $ over horizon (monthly fixed + flat demand)
    net_cost: float  # import_cost - export_credit + fixed_charges


# -- API response summaries ---------------------------------------------


@dataclass(frozen=True)
class UtilitySummary:
    """Minimal info for a utility returned by URDB search."""

    utility_name: str
    eia_id: str


@dataclass(frozen=True)
class RatePlanSummary:
    """Minimal info for a rate plan in a utility's offerings."""

    label: str
    name: str
    startdate: int  # epoch seconds
    enddate: int | None  # epoch seconds or None if open-ended
    description: str
