"""History provider abstraction for circuit power statistics.

Defines a Protocol for statistics retrieval so the profile builder can
work with any backend — HA recorder, future eBus history, or a no-op
stub for standalone operation.

The interface mirrors the subset of HAClient that profile_builder.py
actually uses: ``async_get_statistics(statistic_ids, period, start_time,
end_time)``.  HAClient already satisfies this protocol with no changes.
"""

from __future__ import annotations

from typing import Protocol


class HistoryProvider(Protocol):
    """Interface for circuit power history retrieval.

    Each backend implements this to supply aggregated power statistics
    for profile building.  The output format matches what HA's recorder
    returns: a dict mapping statistic IDs to lists of records, each
    containing ``start``, ``mean``, ``min``, ``max`` fields.
    """

    async def async_get_statistics(
        self,
        statistic_ids: list[str],
        *,
        period: str = "hour",
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> dict[str, list[dict[str, object]]]:
        """Return aggregated statistics keyed by statistic ID."""
        ...


class NullHistoryProvider:
    """No-op provider for standalone operation without history data.

    Returns empty results for all queries.  Profile builder will
    produce no profiles, and circuit configs retain their defaults
    or hand-authored values.
    """

    async def async_get_statistics(
        self,
        statistic_ids: list[str],
        *,
        period: str = "hour",
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> dict[str, list[dict[str, object]]]:
        return {}


class EBusHistoryProvider:
    """Stub for future eBus-based history retrieval.

    SPAN panels do not currently expose historical data via eBus.
    When they do, this provider will connect to the panel's MQTT
    broker and query historical statistics directly — bypassing HA
    entirely.

    For now, returns empty results identical to NullHistoryProvider.
    """

    async def async_get_statistics(
        self,
        statistic_ids: list[str],
        *,
        period: str = "hour",
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> dict[str, list[dict[str, object]]]:
        return {}
