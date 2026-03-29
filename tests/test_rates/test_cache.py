"""Tests for the rate cache manager."""

from __future__ import annotations

from typing import TYPE_CHECKING

from span_panel_simulator.rates.cache import RateCache

if TYPE_CHECKING:
    from pathlib import Path

SAMPLE_URDB_RECORD: dict = {
    "label": "abc123",
    "utility": "Pacific Gas & Electric Co",
    "name": "E-TOU-C",
    "energyratestructure": [[{"rate": 0.25}]],
    "energyweekdayschedule": [[0] * 24 for _ in range(12)],
    "energyweekendschedule": [[0] * 24 for _ in range(12)],
}


class TestRateCache:
    """Rate cache load/save/get/set operations."""

    def test_empty_cache_returns_none(self, tmp_path: Path) -> None:
        cache = RateCache(tmp_path / "rates_cache.yaml")
        assert cache.get_cached_rate("nonexistent") is None

    def test_cache_and_retrieve(self, tmp_path: Path) -> None:
        cache = RateCache(tmp_path / "rates_cache.yaml")
        cache.cache_rate("abc123", SAMPLE_URDB_RECORD)
        entry = cache.get_cached_rate("abc123")
        assert entry is not None
        assert entry.record["label"] == "abc123"
        assert entry.record["utility"] == "Pacific Gas & Electric Co"
        assert entry.source == "openei_urdb"
        assert entry.attribution.license == "CC0"

    def test_persistence_across_instances(self, tmp_path: Path) -> None:
        path = tmp_path / "rates_cache.yaml"
        cache1 = RateCache(path)
        cache1.cache_rate("abc123", SAMPLE_URDB_RECORD)

        cache2 = RateCache(path)
        entry = cache2.get_cached_rate("abc123")
        assert entry is not None
        assert entry.record["name"] == "E-TOU-C"

    def test_current_rate_label(self, tmp_path: Path) -> None:
        cache = RateCache(tmp_path / "rates_cache.yaml")
        assert cache.get_current_rate_label() is None
        cache.set_current_rate_label("abc123")
        assert cache.get_current_rate_label() == "abc123"

    def test_current_rate_label_persists(self, tmp_path: Path) -> None:
        path = tmp_path / "rates_cache.yaml"
        cache1 = RateCache(path)
        cache1.set_current_rate_label("abc123")

        cache2 = RateCache(path)
        assert cache2.get_current_rate_label() == "abc123"

    def test_list_cached_rates(self, tmp_path: Path) -> None:
        cache = RateCache(tmp_path / "rates_cache.yaml")
        cache.cache_rate("abc123", SAMPLE_URDB_RECORD)
        cache.cache_rate(
            "def456",
            {
                "label": "def456",
                "utility": "SoCal Edison",
                "name": "TOU-D-PRIME",
            },
        )
        summaries = cache.list_cached_rates()
        assert len(summaries) == 2
        labels = {s["label"] for s in summaries}
        assert labels == {"abc123", "def456"}

    def test_openei_config_defaults(self, tmp_path: Path) -> None:
        cache = RateCache(tmp_path / "rates_cache.yaml")
        config = cache.get_openei_config()
        assert config.api_url == "https://api.openei.org/utility_rates"
        assert config.api_key == ""

    def test_openei_config_set_and_get(self, tmp_path: Path) -> None:
        cache = RateCache(tmp_path / "rates_cache.yaml")
        cache.set_openei_config("https://custom.api/rates", "my-key-123")
        config = cache.get_openei_config()
        assert config.api_url == "https://custom.api/rates"
        assert config.api_key == "my-key-123"

    def test_openei_config_persists(self, tmp_path: Path) -> None:
        path = tmp_path / "rates_cache.yaml"
        cache1 = RateCache(path)
        cache1.set_openei_config("https://custom.api/rates", "my-key-123")

        cache2 = RateCache(path)
        config = cache2.get_openei_config()
        assert config.api_url == "https://custom.api/rates"
        assert config.api_key == "my-key-123"

    def test_delete_cached_rate(self, tmp_path: Path) -> None:
        cache = RateCache(tmp_path / "rates_cache.yaml")
        cache.cache_rate("abc123", SAMPLE_URDB_RECORD)
        assert cache.get_cached_rate("abc123") is not None
        cache.delete_cached_rate("abc123")
        assert cache.get_cached_rate("abc123") is None
