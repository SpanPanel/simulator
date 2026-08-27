import pytest

from span_panel_simulator.flat_emitter.priority_resolver import (
    LOCKED_PRIORITY,
    PriorityResolver,
)


def test_default_is_the_commissioned_priority() -> None:
    pr = PriorityResolver()
    pr.register("c1", default_priority="NEVER", never_backup=False)
    assert pr.effective("c1") == "NEVER"
    assert pr.locked("c1") is False


def test_override_wins_over_the_commissioned_default() -> None:
    pr = PriorityResolver()
    pr.register("c1", default_priority="NEVER", never_backup=False)
    pr.set_override("c1", "OFF_GRID")
    assert pr.effective("c1") == "OFF_GRID"


def test_override_is_upper_cased() -> None:
    pr = PriorityResolver()
    pr.register("c1", default_priority="NEVER", never_backup=False)
    pr.set_override("c1", "soc_threshold")
    assert pr.effective("c1") == "SOC_THRESHOLD"


def test_clear_override_restores_the_commissioned_default() -> None:
    pr = PriorityResolver()
    pr.register("c1", default_priority="NEVER", never_backup=False)
    pr.set_override("c1", "OFF_GRID")
    pr.clear_override("c1")
    assert pr.effective("c1") == "NEVER"


def test_never_backup_circuit_reads_off_grid_whatever_was_commissioned() -> None:
    """The lock carries the value: "Locked-priority circuits (commissioned
    permanently OFF_GRID) appear as ``priority = OFF_GRID, $settable = false``"."""
    pr = PriorityResolver()
    pr.register("c1", default_priority="NICE_TO_HAVE", never_backup=True)
    assert pr.effective("c1") == LOCKED_PRIORITY == "OFF_GRID"
    assert pr.locked("c1") is True


def test_never_backup_circuit_drops_an_override() -> None:
    pr = PriorityResolver()
    pr.register("c1", default_priority="NICE_TO_HAVE", never_backup=True)
    pr.set_override("c1", "NEVER")
    assert pr.effective("c1") == "OFF_GRID"


def test_locking_a_circuit_discards_an_override_it_already_had() -> None:
    """Re-commissioning is the installer overruling the operator."""
    pr = PriorityResolver()
    pr.register("c1", default_priority="NICE_TO_HAVE", never_backup=False)
    pr.set_override("c1", "NEVER")
    pr.register("c1", default_priority="NICE_TO_HAVE", never_backup=True)
    assert pr.effective("c1") == "OFF_GRID"


def test_unlocking_a_circuit_restores_its_commissioned_priority() -> None:
    pr = PriorityResolver()
    pr.register("c1", default_priority="SOC_THRESHOLD", never_backup=True)
    pr.register("c1", default_priority="SOC_THRESHOLD", never_backup=False)
    assert pr.effective("c1") == "SOC_THRESHOLD"
    assert pr.locked("c1") is False


def test_all_effective_covers_every_registered_circuit() -> None:
    pr = PriorityResolver()
    pr.register("c1", default_priority="NEVER", never_backup=False)
    pr.register("c2", default_priority="NICE_TO_HAVE", never_backup=True)
    pr.set_override("c1", "OFF_GRID")
    assert pr.all_effective() == {"c1": "OFF_GRID", "c2": "OFF_GRID"}


def test_unregistered_instance_raises() -> None:
    pr = PriorityResolver()
    assert pr.known("nope") is False
    with pytest.raises(KeyError):
        pr.set_override("nope", "NEVER")
    with pytest.raises(KeyError):
        pr.clear_override("nope")
