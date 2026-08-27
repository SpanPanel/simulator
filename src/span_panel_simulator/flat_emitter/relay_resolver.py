"""Per-circuit relay state, with strict precedence over commands from multiple sources.

The emitter now owns relay state across ticks. Commands arrive from three sources:

1. **Manifest declaration** — ``relay-behavior == "always-on"`` or ``always-on=true``
   metadata. Absolute: the relay can never be opened, regardless of /set or
   load-shedding decisions.
2. **/set commands** — operator-driven via Homie ``circuit/.../switch/relay/set``
   topic. Authoritative for non-always-on circuits, no debounce.
3. **Load shedding** — emitter's ``LoadSheddingDevice`` decisions. Applies only
   when there's no /set override.

Precedence (highest wins):

    always-on > /set override > load-shed > default-CLOSED

``relay_requester`` reflects the source of the active decision:
- ``NEVER`` for always-on (the relay is physically incapable of opening)
- ``USER`` for /set
- ``BACKUP`` for load-shed
- ``NEVER_BACKUP`` for load-shed on a circuit an installer commissioned
  never-backup: the shed is the commissioning lock taking effect, not a
  configurable policy, which is why the eBus schema migration guide maps this
  flat value onto v1.0's ``CONFIGURATION`` ("the commissioning lock is now
  expressed structurally via ``load-shed/priority = OFF_GRID`` with
  ``$settable = false``; ``CONFIGURATION`` captures the source attribution")
- ``UNKNOWN`` for the default-CLOSED state

The producer never sees /set commands. ``Emitter`` registers internal handlers
for ``circuit.switch/relay``, ``circuit.priority/shed-priority``, and
``circuit.info/name``; those handlers call ``RelayResolver.set_user_override``
(and the priority equivalent on a sibling state map)."""

from __future__ import annotations

from enum import StrEnum


class RelayState(StrEnum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class RelayRequester(StrEnum):
    NEVER = "NEVER"  # always-on circuit; cannot open
    USER = "USER"  # /set override active
    BACKUP = "BACKUP"  # load-shed in effect
    NEVER_BACKUP = "NEVER_BACKUP"  # load-shed on a never-backup circuit
    UNKNOWN = "UNKNOWN"  # default-CLOSED, no decision-maker


class RelayResolver:
    """Maintains relay state per circuit instance.

    Construct empty, register each circuit with its always-on flag, then update
    overrides and shed decisions; query ``state()`` for the resolved final
    state."""

    def __init__(self) -> None:
        # always_on map: instance_id -> bool (manifest declaration; immutable post-register)
        self._always_on: dict[str, bool] = {}
        # never_backup map: instance_id -> bool (manifest declaration). Does not
        # gate the relay — it only attributes a shed to the commissioning lock.
        self._never_backup: dict[str, bool] = {}
        # /set override map: instance_id -> RelayState | None (None = no override)
        self._user_overrides: dict[str, RelayState | None] = {}
        # load-shed decision map: instance_id -> bool (True = wants OPEN)
        self._shed: dict[str, bool] = {}

    def register(self, instance_id: str, *, always_on: bool, never_backup: bool = False) -> None:
        """Idempotent — re-registering with a different always_on value updates
        the manifest declaration (typical use: emitter restart with edited manifest)."""
        self._always_on[instance_id] = always_on
        self._never_backup[instance_id] = never_backup
        self._user_overrides.setdefault(instance_id, None)
        self._shed.setdefault(instance_id, False)

    def set_user_override(self, instance_id: str, state: RelayState | None) -> None:
        """Operator /set or explicit clear. ``state=None`` clears the override
        and lets load-shed (or default-CLOSED) take effect.

        Always-on circuits silently drop the override — operator cannot open them."""
        if instance_id not in self._always_on:
            raise KeyError(f"set_user_override for unregistered instance_id={instance_id!r}")
        if self._always_on[instance_id]:
            return  # absolute: always-on ignores /set
        self._user_overrides[instance_id] = state

    def clear_user_override(self, instance_id: str) -> None:
        self.set_user_override(instance_id, None)

    def set_shed(self, instance_id: str, *, open_relay: bool) -> None:
        """Load-shedding decision. ``open_relay=True`` means the load-shedding
        policy wants this circuit OPEN.

        Always-on circuits silently drop the request."""
        if instance_id not in self._always_on:
            raise KeyError(f"set_shed for unregistered instance_id={instance_id!r}")
        if self._always_on[instance_id]:
            return
        self._shed[instance_id] = open_relay

    def clear_all_shed(self) -> None:
        """Reset every shed decision to False. Called by the emitter at the
        start of each tick before re-running ``LoadSheddingDevice``."""
        for k in self._shed:
            self._shed[k] = False

    def state(self, instance_id: str) -> tuple[RelayState, RelayRequester]:
        """Resolve the final state for ``instance_id``."""
        if self._always_on.get(instance_id, False):
            return RelayState.CLOSED, RelayRequester.NEVER
        override = self._user_overrides.get(instance_id)
        if override is not None:
            return override, RelayRequester.USER
        if self._shed.get(instance_id, False):
            if self._never_backup.get(instance_id, False):
                return RelayState.OPEN, RelayRequester.NEVER_BACKUP
            return RelayState.OPEN, RelayRequester.BACKUP
        return RelayState.CLOSED, RelayRequester.UNKNOWN

    def known(self, instance_id: str) -> bool:
        return instance_id in self._always_on
