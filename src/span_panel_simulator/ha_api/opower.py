"""Opower account discovery and cost fetch via HA API.

Composes existing HAClient methods to find opower ELEC accounts
and retrieve daily billed cost statistics from the HA recorder.
Does not modify the HAClient itself.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from span_panel_simulator.ha_api.client import HAClient

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class OpowerAccount:
    """An opower ELEC account discovered from HA."""

    device_id: str
    utility_name: str
    account_number: str
    cost_entity_id: str
    usage_entity_id: str


@dataclass(frozen=True)
class OpowerCostResult:
    """Result of summing daily opower cost statistics."""

    total_cost: float
    days_with_data: int


async def async_discover_opower(client: HAClient) -> list[OpowerAccount]:
    """Find opower ELEC accounts via HA config entries and registries.

    Returns an empty list when opower is not installed or has no
    ELEC accounts.
    """
    config_entries = await client._ws_command_list({"type": "config_entries/get"})
    opower_entry_ids: set[str] = set()
    opower_titles: dict[str, str] = {}
    for entry in config_entries:
        if entry.get("domain") == "opower":
            entry_id = str(entry.get("entry_id", ""))
            opower_entry_ids.add(entry_id)
            opower_titles[entry_id] = str(entry.get("title", ""))

    if not opower_entry_ids:
        return []

    devices = await client._ws_command_list({"type": "config/device_registry/list"})
    elec_devices: list[tuple[str, str, str]] = []
    for dev in devices:
        dev_entries = dev.get("config_entries", [])
        if not isinstance(dev_entries, list):
            continue
        matching_entry = None
        for eid in dev_entries:
            if eid in opower_entry_ids:
                matching_entry = str(eid)
                break
        if matching_entry is None:
            continue

        name = str(dev.get("name", ""))
        if "ELEC" not in name.upper():
            continue

        device_id = str(dev.get("id", ""))
        utility_name = opower_titles.get(matching_entry, "")
        parts = name.split()
        account_number = parts[-1] if len(parts) >= 2 else name

        elec_devices.append((device_id, utility_name, account_number))

    if not elec_devices:
        return []

    entities = await client._ws_command_list({"type": "config/entity_registry/list"})

    accounts: list[OpowerAccount] = []
    for device_id, utility_name, account_number in elec_devices:
        cost_entity_id = ""
        usage_entity_id = ""
        for ent in entities:
            if ent.get("device_id") != device_id:
                continue
            ent_id = str(ent.get("entity_id", ""))
            device_class = str(ent.get("original_device_class", ""))
            if device_class == "monetary" and "cost_to_date" in ent_id:
                cost_entity_id = ent_id
            elif device_class == "energy" and "usage_to_date" in ent_id:
                usage_entity_id = ent_id

        if cost_entity_id and usage_entity_id:
            accounts.append(
                OpowerAccount(
                    device_id=device_id,
                    utility_name=utility_name,
                    account_number=account_number,
                    cost_entity_id=cost_entity_id,
                    usage_entity_id=usage_entity_id,
                )
            )

    return accounts


async def async_get_opower_cost(
    client: HAClient,
    cost_entity_id: str,
    start_time: str,
    end_time: str,
) -> OpowerCostResult | None:
    """Sum daily billed cost from opower statistics over a date range.

    Returns an OpowerCostResult with total cost and days covered,
    or None if no data is available.  Uses the ``change`` field from
    daily statistics, which represents the cost delta for each day
    (opower cost entities use ``state_class: total``).
    """
    stats = await client.async_get_statistics(
        [cost_entity_id],
        period="day",
        start_time=start_time,
        end_time=end_time,
    )
    entries = stats.get(cost_entity_id, [])
    if not entries:
        return None

    total = 0.0
    days_with_data = 0
    for entry in entries:
        change = entry.get("change")
        if isinstance(change, int | float):
            total += float(change)
            days_with_data += 1

    if days_with_data == 0:
        return None

    return OpowerCostResult(total_cost=total, days_with_data=days_with_data)
