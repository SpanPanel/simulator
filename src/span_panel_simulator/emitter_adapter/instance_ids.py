"""Stable ID derivation for emitter manifest entries.

Lifted from publisher.py so the simulator's UUID derivation matches what the legacy
publisher produced. UUID v5 with a fixed namespace ensures the same circuit always
produces the same UUID across simulator restarts.

A circuit's uuid is derived from ``<panel-serial>/<circuit-id>``, not from the
circuit id alone. A circuit id is a YAML key (``solar_inverter``, ``oven``) that
every panel config reuses, so an unscoped derivation gave two panels the same
circuit uuid.

**This derivation is a contract with PanelBench and must match it byte for byte.**
PanelBench publishes circuits flat, at ``ebus/5/<circuit-uuid>/…`` with no serial
in the path, so an unscoped id collided there on the wire; it scopes by the panel
serial to fix that. Stopping this simulator and starting PanelBench is how a
firmware upgrade is rehearsed on a single panel, and Home Assistant keys a circuit
entity on the uuid the panel publishes — so if the two repositories derived a
circuit's id differently, every circuit would change identity at the swap and
strand the entities that carry its history and automations. That is why this
module changed, and why ``tests/test_instance_ids_contract.py`` pins a literal
PanelBench pins too.

Scoping is also correct here in its own right: a circuit id shared across panels
is one identity to any consumer keyed on the node id alone. It was not, however,
a collision on this simulator's own wire. ``wire/mapping/circuit.yaml`` places a
circuit ``node-on-parent`` with ``device_id_source: parent`` and
``$description_owner: parent``, so a circuit is a node on the panel device rather
than a device of its own, every topic is rooted at the panel's serial, and two
simulated panels never overwrote each other's retained values.

The panel serial threaded in here must be the one the manifest publishes —
``panel_config.serial_number`` — and must reach every call site from that single
key. Deriving it a second way (a config file name, a container hostname, a CLI
override read before the ``sim-`` prefix is applied) yields a different uuid for
the same circuit, which breaks the PanelBench contract without colliding with
anything.
"""

from __future__ import annotations

import uuid

_CIRCUIT_NAMESPACE = uuid.UUID("a1b2c3d4-e5f6-7890-abcd-ef1234567890")


def stable_circuit_uuid(panel_id: str, circuit_id: str) -> str:
    """Deterministic dashless UUID for a circuit, scoped to the panel that owns it."""
    return str(uuid.uuid5(_CIRCUIT_NAMESPACE, f"{panel_id}/{circuit_id}")).replace("-", "")
