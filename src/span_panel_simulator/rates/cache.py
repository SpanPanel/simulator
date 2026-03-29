"""Simulator-wide rate cache backed by a YAML file.

Stores URDB records verbatim, keyed by their label.  Also manages
the current rate selection and OpenEI API configuration.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

import yaml

from span_panel_simulator.rates.types import (
    AttributionMeta,
    OpenEIConfig,
    RateCacheEntry,
)

_DEFAULT_ATTRIBUTION = AttributionMeta(
    provider="OpenEI Utility Rate Database",
    url="https://openei.org/wiki/Utility_Rate_Database",
    license="CC0",
    api_version=3,
)


class RateCache:
    """Manages the simulator-wide rate cache YAML file."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._data = self._load()

    # -- Cache operations ------------------------------------------------

    def get_cached_rate(self, label: str) -> RateCacheEntry | None:
        """Return a cached rate entry by URDB label, or None."""
        rates = self._data.get("rates", {})
        entry = rates.get(label)
        if entry is None:
            return None
        attr_data = entry.get("attribution", {})
        return RateCacheEntry(
            source=entry.get("source", "openei_urdb"),
            retrieved_at=entry.get("retrieved_at", ""),
            attribution=AttributionMeta(
                provider=attr_data.get("provider", _DEFAULT_ATTRIBUTION.provider),
                url=attr_data.get("url", _DEFAULT_ATTRIBUTION.url),
                license=attr_data.get("license", _DEFAULT_ATTRIBUTION.license),
                api_version=attr_data.get("api_version", _DEFAULT_ATTRIBUTION.api_version),
            ),
            record=entry.get("record", {}),
        )

    def cache_rate(self, label: str, urdb_record: dict[str, Any]) -> None:
        """Store a URDB record in the cache."""
        if "rates" not in self._data:
            self._data["rates"] = {}
        self._data["rates"][label] = {
            "source": "openei_urdb",
            "retrieved_at": datetime.now(UTC).isoformat(),
            "attribution": {
                "provider": _DEFAULT_ATTRIBUTION.provider,
                "url": _DEFAULT_ATTRIBUTION.url,
                "license": _DEFAULT_ATTRIBUTION.license,
                "api_version": _DEFAULT_ATTRIBUTION.api_version,
            },
            "record": urdb_record,
        }
        self._save()

    def delete_cached_rate(self, label: str) -> None:
        """Remove a rate from the cache."""
        rates = self._data.get("rates", {})
        rates.pop(label, None)
        self._save()

    def list_cached_rates(self) -> list[dict[str, Any]]:
        """Return summary dicts for all cached rates."""
        rates = self._data.get("rates", {})
        summaries = []
        for label, entry in rates.items():
            record = entry.get("record", {})
            summaries.append(
                {
                    "label": label,
                    "utility": record.get("utility", ""),
                    "name": record.get("name", ""),
                    "retrieved_at": entry.get("retrieved_at", ""),
                }
            )
        return summaries

    # -- Current rate selection ------------------------------------------

    def get_current_rate_label(self) -> str | None:
        """Return the simulator-wide current rate label, or None."""
        label = self._data.get("current_rate_label")
        return label if label else None

    def set_current_rate_label(self, label: str) -> None:
        """Set the simulator-wide current rate selection."""
        self._data["current_rate_label"] = label
        self._save()

    # -- OpenEI configuration -------------------------------------------

    def get_openei_config(self) -> OpenEIConfig:
        """Return the stored OpenEI API settings."""
        cfg = self._data.get("openei", {})
        return OpenEIConfig(
            api_url=cfg.get("api_url", OpenEIConfig.api_url),
            api_key=cfg.get("api_key", OpenEIConfig.api_key),
        )

    def set_openei_config(self, api_url: str, api_key: str) -> None:
        """Update the OpenEI API settings."""
        self._data["openei"] = {
            "api_url": api_url,
            "api_key": api_key,
        }
        self._save()

    # -- Opower account selection ----------------------------------------

    def get_opower_account(self) -> dict[str, str] | None:
        """Return the saved opower account selection, or None."""
        account = self._data.get("opower_account")
        if not account or not isinstance(account, dict):
            return None
        if not account.get("device_id"):
            return None
        return {
            "device_id": str(account.get("device_id", "")),
            "utility_name": str(account.get("utility_name", "")),
            "account_number": str(account.get("account_number", "")),
            "cost_entity_id": str(account.get("cost_entity_id", "")),
            "usage_entity_id": str(account.get("usage_entity_id", "")),
        }

    def set_opower_account(
        self,
        device_id: str,
        utility_name: str,
        account_number: str,
        cost_entity_id: str,
        usage_entity_id: str,
    ) -> None:
        """Save the opower account selection."""
        self._data["opower_account"] = {
            "device_id": device_id,
            "utility_name": utility_name,
            "account_number": account_number,
            "cost_entity_id": cost_entity_id,
            "usage_entity_id": usage_entity_id,
        }
        self._save()

    def clear_opower_account(self) -> None:
        """Remove the saved opower account selection."""
        self._data.pop("opower_account", None)
        self._save()

    # -- Persistence -----------------------------------------------------

    def _load(self) -> dict[str, Any]:
        if self._path.exists():
            with open(self._path) as f:
                data = yaml.safe_load(f)
            return data if isinstance(data, dict) else {}
        return {}

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "w") as f:
            yaml.dump(
                self._data,
                f,
                default_flow_style=False,
                sort_keys=False,
                allow_unicode=True,
            )
