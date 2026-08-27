"""Per-circuit shed priority, and the never-backup commissioning lock.

A circuit's shed priority has two inputs, and they are not the same kind of
thing:

1. **The commissioned value** — ``default-priority`` in the manifest, one of
   the flat enum ``UNKNOWN``, ``OFF_GRID``, ``SOC_THRESHOLD``, ``NEVER``.
   ``NEVER`` means "never shed this circuit"; it is an ordinary value on an
   ordinary circuit, and Home Assistant shows it as "Stays on in an outage".
2. **The installer's lock** — ``never-backup``, a separate commissioning flag.
   The eBus schema migration guide calls ``never-backup``, ``always-on`` and
   ``sheddable`` "independent commissioning inputs stored as separate fields in
   each circuit's commissioning state", and maps this one to the Homie
   ``$settable`` attribute on ``load-shed/priority``: "Published with
   ``$settable = !never-backup``. Locked-priority circuits (commissioned
   permanently OFF_GRID) appear as ``priority = OFF_GRID, $settable = false``;
   user-configurable circuits appear as ``$settable = true``."

So the lock carries its own value: a locked circuit is *commissioned
permanently OFF_GRID*, and the resolver holds that here rather than asking the
configuration to state OFF_GRID a second time next to the flag. The simulator's
priority lives on a shared circuit *template* while the lock is per-circuit, so
a configuration that had to agree with itself could not express one locked
circuit on a template two circuits share; deriving the value from the flag
makes the contradiction unrepresentable instead of merely rejected.

Precedence:

    never-backup lock > /set override > commissioned default

The lock's refusal is silent, mirroring ``RelayResolver.set_user_override``
dropping a relay ``/set`` on an always-on circuit: the panel publishes the
property as not-settable and ignores writes to it rather than erroring back at
a client that has no error channel to receive them.
"""

from __future__ import annotations

#: The priority a never-backup circuit is commissioned with. The lock and the
#: value are one commissioning act — see the module docstring.
LOCKED_PRIORITY = "OFF_GRID"


class PriorityResolver:
    """Maintains shed priority per circuit instance.

    Construct empty, register each circuit with its commissioned default and
    its never-backup flag, then apply ``/set`` overrides; query ``effective()``
    for the priority the panel reports and sheds on."""

    def __init__(self) -> None:
        # instance_id -> the commissioned priority (already forced to
        # LOCKED_PRIORITY for a locked circuit).
        self._defaults: dict[str, str] = {}
        # instance_id -> bool (manifest declaration; the lock)
        self._locked: dict[str, bool] = {}
        # instance_id -> operator /set value (absent = no override)
        self._overrides: dict[str, str] = {}

    def register(self, instance_id: str, *, default_priority: str, never_backup: bool) -> None:
        """Idempotent — re-registering with a different lock updates the
        manifest declaration (typical use: emitter restart with an edited
        manifest). Locking a circuit discards any override it had accumulated,
        because the lock outranks the operator."""
        self._defaults[instance_id] = LOCKED_PRIORITY if never_backup else default_priority
        self._locked[instance_id] = never_backup
        if never_backup:
            self._overrides.pop(instance_id, None)

    def set_override(self, instance_id: str, priority: str) -> None:
        """Operator ``/set``. Never-backup circuits silently drop it — their
        priority is published as not settable."""
        if instance_id not in self._locked:
            raise KeyError(f"set_override for unregistered instance_id={instance_id!r}")
        if self._locked[instance_id]:
            return
        self._overrides[instance_id] = priority.upper()

    def clear_override(self, instance_id: str) -> None:
        """Drop any operator override, restoring the commissioned default."""
        if instance_id not in self._locked:
            raise KeyError(f"clear_override for unregistered instance_id={instance_id!r}")
        self._overrides.pop(instance_id, None)

    def locked(self, instance_id: str) -> bool:
        """True when the circuit was commissioned never-backup."""
        return self._locked.get(instance_id, False)

    def effective(self, instance_id: str) -> str:
        """The priority the panel reports on the wire and sheds on."""
        if instance_id in self._overrides:
            return self._overrides[instance_id]
        return self._defaults[instance_id]

    def all_effective(self) -> dict[str, str]:
        """Every registered circuit's effective priority, for the shed pass."""
        return {cid: self.effective(cid) for cid in self._defaults}

    def known(self, instance_id: str) -> bool:
        return instance_id in self._locked
