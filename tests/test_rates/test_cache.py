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


class TestOpowerAccount:
    """Opower account selection persistence."""

    def test_no_opower_account_returns_none(self, tmp_path: Path) -> None:
        cache = RateCache(tmp_path / "rates_cache.yaml")
        assert cache.get_opower_account() is None

    def test_set_and_get_opower_account(self, tmp_path: Path) -> None:
        cache = RateCache(tmp_path / "rates_cache.yaml")
        cache.set_opower_account(
            device_id="device_elec_1",
            utility_name="PG&E",
            account_number="3021618479",
            cost_entity_id="sensor.opower_pge_elec_cost_to_date",
            usage_entity_id="sensor.opower_pge_elec_usage_to_date",
        )
        account = cache.get_opower_account()
        assert account is not None
        assert account["device_id"] == "device_elec_1"
        assert account["utility_name"] == "PG&E"
        assert account["cost_entity_id"] == "sensor.opower_pge_elec_cost_to_date"

    def test_opower_account_persists(self, tmp_path: Path) -> None:
        path = tmp_path / "rates_cache.yaml"
        cache1 = RateCache(path)
        cache1.set_opower_account(
            device_id="device_elec_1",
            utility_name="PG&E",
            account_number="3021618479",
            cost_entity_id="sensor.opower_pge_elec_cost_to_date",
            usage_entity_id="sensor.opower_pge_elec_usage_to_date",
        )
        cache2 = RateCache(path)
        account = cache2.get_opower_account()
        assert account is not None
        assert account["device_id"] == "device_elec_1"

    def test_clear_opower_account(self, tmp_path: Path) -> None:
        cache = RateCache(tmp_path / "rates_cache.yaml")
        cache.set_opower_account(
            device_id="d1",
            utility_name="U",
            account_number="A",
            cost_entity_id="c",
            usage_entity_id="u",
        )
        assert cache.get_opower_account() is not None
        cache.clear_opower_account()
        assert cache.get_opower_account() is None
