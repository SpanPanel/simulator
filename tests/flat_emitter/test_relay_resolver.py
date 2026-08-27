import pytest

from span_panel_simulator.flat_emitter.relay_resolver import (
    RelayRequester,
    RelayResolver,
    RelayState,
)


def test_default_state_is_closed_unknown() -> None:
    rr = RelayResolver()
    rr.register("c1", always_on=False)
    assert rr.state("c1") == (RelayState.CLOSED, RelayRequester.UNKNOWN)


def test_always_on_resolves_closed_never() -> None:
    rr = RelayResolver()
    rr.register("dryer", always_on=True)
    assert rr.state("dryer") == (RelayState.CLOSED, RelayRequester.NEVER)


def test_user_override_open_wins_over_default() -> None:
    rr = RelayResolver()
    rr.register("c1", always_on=False)
    rr.set_user_override("c1", RelayState.OPEN)
    assert rr.state("c1") == (RelayState.OPEN, RelayRequester.USER)


def test_user_override_closed_wins_over_shed() -> None:
    rr = RelayResolver()
    rr.register("c1", always_on=False)
    rr.set_shed("c1", open_relay=True)
    rr.set_user_override("c1", RelayState.CLOSED)
    # Operator commanded CLOSED while shed wants OPEN: operator wins.
    assert rr.state("c1") == (RelayState.CLOSED, RelayRequester.USER)


def test_user_override_open_persists_across_shed_clear() -> None:
    rr = RelayResolver()
    rr.register("c1", always_on=False)
    rr.set_user_override("c1", RelayState.OPEN)
    rr.set_shed("c1", open_relay=True)
    rr.clear_all_shed()
    # /set persists; only shed was cleared
    assert rr.state("c1") == (RelayState.OPEN, RelayRequester.USER)


def test_clear_user_override_falls_back_to_shed() -> None:
    rr = RelayResolver()
    rr.register("c1", always_on=False)
    rr.set_shed("c1", open_relay=True)
    rr.set_user_override("c1", RelayState.CLOSED)
    rr.clear_user_override("c1")
    # Without /set override, shed re-asserts.
    assert rr.state("c1") == (RelayState.OPEN, RelayRequester.BACKUP)


def test_shed_only_no_override_resolves_open_backup() -> None:
    rr = RelayResolver()
    rr.register("c1", always_on=False)
    rr.set_shed("c1", open_relay=True)
    assert rr.state("c1") == (RelayState.OPEN, RelayRequester.BACKUP)


def test_always_on_ignores_user_override_open() -> None:
    rr = RelayResolver()
    rr.register("dryer", always_on=True)
    rr.set_user_override("dryer", RelayState.OPEN)
    # Silently dropped. Always-on remains CLOSED with NEVER requester.
    assert rr.state("dryer") == (RelayState.CLOSED, RelayRequester.NEVER)


def test_always_on_ignores_shed() -> None:
    rr = RelayResolver()
    rr.register("dryer", always_on=True)
    rr.set_shed("dryer", open_relay=True)
    assert rr.state("dryer") == (RelayState.CLOSED, RelayRequester.NEVER)


def test_clear_all_shed_resets_only_shed() -> None:
    rr = RelayResolver()
    rr.register("c1", always_on=False)
    rr.register("c2", always_on=False)
    rr.set_shed("c1", open_relay=True)
    rr.set_shed("c2", open_relay=True)
    rr.set_user_override("c1", RelayState.OPEN)
    rr.clear_all_shed()
    assert rr.state("c1") == (RelayState.OPEN, RelayRequester.USER)
    assert rr.state("c2") == (RelayState.CLOSED, RelayRequester.UNKNOWN)


def test_unregistered_instance_set_user_override_raises() -> None:
    rr = RelayResolver()
    with pytest.raises(KeyError, match="unregistered"):
        rr.set_user_override("ghost", RelayState.OPEN)


def test_unregistered_instance_set_shed_raises() -> None:
    rr = RelayResolver()
    with pytest.raises(KeyError, match="unregistered"):
        rr.set_shed("ghost", open_relay=True)


def test_register_idempotent_keeps_state() -> None:
    rr = RelayResolver()
    rr.register("c1", always_on=False)
    rr.set_user_override("c1", RelayState.OPEN)
    rr.register("c1", always_on=False)  # re-register
    # /set override should persist.
    assert rr.state("c1") == (RelayState.OPEN, RelayRequester.USER)


def test_register_can_change_always_on() -> None:
    rr = RelayResolver()
    rr.register("c1", always_on=False)
    rr.set_user_override("c1", RelayState.OPEN)
    rr.register("c1", always_on=True)
    # Newly always-on: /set is silently dropped on next set; existing override
    # remains in the map but state() honors always-on precedence.
    assert rr.state("c1") == (RelayState.CLOSED, RelayRequester.NEVER)


def test_never_backup_shed_is_attributed_to_the_commissioning_lock() -> None:
    """A locked circuit sheds because an installer commissioned it not to be
    backed up, so the actor is the configuration — ``NEVER_BACKUP`` in the flat
    enum, which the eBus migration guide maps onto v1.0 ``CONFIGURATION``."""
    rr = RelayResolver()
    rr.register("hot_tub", always_on=False, never_backup=True)
    rr.set_shed("hot_tub", open_relay=True)
    assert rr.state("hot_tub") == (RelayState.OPEN, RelayRequester.NEVER_BACKUP)


def test_never_backup_does_not_lock_the_relay() -> None:
    """The lock is on the priority, not the relay: an operator can still open
    and close a never-backup circuit."""
    rr = RelayResolver()
    rr.register("hot_tub", always_on=False, never_backup=True)
    rr.set_user_override("hot_tub", RelayState.OPEN)
    assert rr.state("hot_tub") == (RelayState.OPEN, RelayRequester.USER)


def test_never_backup_defaults_to_backup_attribution() -> None:
    rr = RelayResolver()
    rr.register("c1", always_on=False)
    rr.set_shed("c1", open_relay=True)
    assert rr.state("c1") == (RelayState.OPEN, RelayRequester.BACKUP)


def test_known_returns_true_after_register() -> None:
    rr = RelayResolver()
    assert rr.known("c1") is False
    rr.register("c1", always_on=False)
    assert rr.known("c1") is True
