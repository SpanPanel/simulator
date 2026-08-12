"""An EVSE's node id is its drive serial, because that is what firmware publishes.

This simulator named EVSE nodes `evse` / `evse-2` — positional slots no panel emits.
The flat parser keys its snapshot on the node id verbatim and the integration builds
the Home Assistant device identifier from that key, so against real firmware an EVSE
is already serial-identified. v1.0 names the same device `<panel>-<serial>` and its
parser strips that back to the serial, reproducing the flat key exactly — which is
what carries a drive's identity, history and automations across a firmware upgrade.

A positional slot broke that. Upgrading re-keyed every EVSE, so Home Assistant built
new devices and left the old ones stranded with their entities Unavailable — two
"SPAN Drive - Driveway" devices showing the same serial, one live and one dead. The
defect was only ever in this fixture, but this fixture is what the upgrade path is
proved against, so it turned a migration that does not survive into a passing test.

Since the serial is now a topic level rather than only a property value, it has to be
a legal Homie id — which is why the default is lower-case and a configured one is
checked rather than quietly rewritten.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import pytest

from span_panel_simulator.emitter_adapter.spec_generator import (
    _evse_serial_number,
    build_manifest,
)

if TYPE_CHECKING:
    from span_panel_simulator.config_types import SimulationConfig

_HOMIE_ALLOWED = set("abcdefghijklmnopqrstuvwxyz0123456789-")


def _profile(**evse: object) -> SimulationConfig:
    profile: dict[str, Any] = {
        "panel_config": {"serial_number": "sim-40t-001", "total_tabs": 40, "main_size": 200},
        "circuits": [],
        "evse": {"enabled": True, **evse},
    }
    return cast("SimulationConfig", profile)


def test_the_node_id_is_the_serial() -> None:
    """The identity a consumer keys on, asserted as an equality.

    Equality rather than a literal: what matters is that the two agree, because the
    flat parser reads the node id while the v1.0 parser reads the serial. A literal
    would pass even if the two were derived independently and happened to coincide
    for this one config.
    """
    (evse,) = build_manifest(_profile()).of_class("evse")

    assert evse.instance_id == evse.metadata["serial-number"]


def test_the_default_serial_is_a_legal_homie_topic_id() -> None:
    """Lower-case, because it is a topic level now.

    Homie 5 allows only `a`-`z`, `0`-`9` and `-` in a topic-level id. The old default
    was `SIM-EVSE-…`, which was legal as a property value and became an illegal topic
    the moment it also became the node id — the same class of defect as publishing
    `"BESS"` where a device id was required.
    """
    (evse,) = build_manifest(_profile()).of_class("evse")

    assert set(evse.instance_id) <= _HOMIE_ALLOWED, (
        f"node id {evse.instance_id!r} contains characters Homie forbids in a topic level"
    )


def test_a_configured_serial_that_cannot_be_a_topic_id_is_refused() -> None:
    """Refused, not sanitised.

    Rewriting it would publish a node id that no longer matches the
    `info/serial-number` beside it, so a consumer keying identity off the serial would
    silently stop matching across an upgrade — reintroducing, quietly, the exact
    failure this change removes. Better to fail at build time with the reason.
    """
    with pytest.raises(ValueError, match="Homie topic id"):
        _evse_serial_number({"serial_number": "SPN-DRV-001"}, "sim-40t-001", 1)


def test_a_configured_lowercase_serial_is_used_as_given() -> None:
    """The configured value wins; the default only fills a gap."""
    (evse,) = build_manifest(_profile(serial_number="drive-abc-123")).of_class("evse")

    assert evse.instance_id == "drive-abc-123"
    assert evse.metadata["serial-number"] == "drive-abc-123"
