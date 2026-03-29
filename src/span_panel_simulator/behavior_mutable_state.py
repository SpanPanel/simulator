"""Mutable tick state for :class:`RealisticBehaviorEngine`.

Modeling and cloning copy this snapshot instead of reaching into private
attributes on the behavior engine.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class BehaviorEngineMutableState:
    """Immutable snapshot of fields that change during simulation ticks."""

    circuit_cycle_states: dict[str, dict[str, Any]]
    last_battery_direction: str
    grid_offline: bool
