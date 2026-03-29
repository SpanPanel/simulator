"""ToU rate integration — OpenEI URDB rate plans and cost calculation."""

from span_panel_simulator.rates.cache import RateCache
from span_panel_simulator.rates.cost_engine import compute_costs
from span_panel_simulator.rates.openei import (
    OpenEIError,
    fetch_rate_detail,
    fetch_rate_plans,
    fetch_utilities,
)
from span_panel_simulator.rates.resolver import resolve_rate
from span_panel_simulator.rates.types import (
    AttributionMeta,
    CostLedger,
    OpenEIConfig,
    RateCacheEntry,
    RatePlanSummary,
    URDBRateTier,
    URDBRecord,
    UtilitySummary,
)

__all__ = [
    "AttributionMeta",
    "CostLedger",
    "OpenEIConfig",
    "OpenEIError",
    "RateCache",
    "RateCacheEntry",
    "RatePlanSummary",
    "URDBRateTier",
    "URDBRecord",
    "UtilitySummary",
    "compute_costs",
    "fetch_rate_detail",
    "fetch_rate_plans",
    "fetch_utilities",
    "resolve_rate",
]
