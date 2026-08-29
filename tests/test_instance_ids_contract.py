"""The circuit-uuid derivation is a contract shared with PanelBench.

PanelBench emits the same panel over the same broker namespace, and stopping one
to start the other is how a firmware upgrade is rehearsed on a single panel. If
the two derived a circuit's device id differently, every circuit would change
identity at the swap — so both repositories pin the same literal, and a change
made on one side alone fails the other side's suite.
"""

from span_panel_simulator.emitter_adapter.instance_ids import stable_circuit_uuid

#: Cross-repo contract with panelbench: the same panel serial and circuit id must
#: derive this exact value there. Do not regenerate it to match a code change —
#: a mismatch means the derivation moved, which is the thing being guarded.
SOLAR_INVERTER_ON_SIM_40T_001 = "be87c32bda4f5cd9abbf6d3995ae28c0"


def test_derivation_matches_the_panelbench_contract() -> None:
    assert stable_circuit_uuid("sim-40t-001", "solar_inverter") == SOLAR_INVERTER_ON_SIM_40T_001


def test_the_same_circuit_id_on_two_panels_gets_two_ids() -> None:
    """The collision this scoping exists to remove: two panels in one add-on
    share a broker, and a circuit id is a YAML key both panel configs reuse."""
    first = stable_circuit_uuid("sim-40t-001", "solar_inverter")
    second = stable_circuit_uuid("sim-40t-002", "solar_inverter")
    assert first != second
