"""Tests for the history provider abstraction."""

from __future__ import annotations

import pytest

from span_panel_simulator.history import (
    EBusHistoryProvider,
    HistoryProvider,
    NullHistoryProvider,
)


class TestNullHistoryProvider:
    @pytest.mark.asyncio
    async def test_returns_empty(self) -> None:
        provider = NullHistoryProvider()
        result = await provider.async_get_statistics(["sensor.test"])
        assert result == {}

    def test_satisfies_protocol(self) -> None:
        provider: HistoryProvider = NullHistoryProvider()
        assert hasattr(provider, "async_get_statistics")


class TestEBusHistoryProvider:
    @pytest.mark.asyncio
    async def test_returns_empty(self) -> None:
        provider = EBusHistoryProvider()
        result = await provider.async_get_statistics(["sensor.test"], period="month")
        assert result == {}

    def test_satisfies_protocol(self) -> None:
        provider: HistoryProvider = EBusHistoryProvider()
        assert hasattr(provider, "async_get_statistics")
