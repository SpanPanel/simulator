from span_panel_simulator.flat_emitter.manifest import DeviceInstance, DeviceManifest
from span_panel_simulator.flat_emitter.wire.graph_builder import build_graph
from span_panel_simulator.flat_emitter.wire.mapping_loader import load_mapping_table
from span_panel_simulator.flat_emitter.wire.profile_loader import load_profiles

# 2023-11-14 in epoch-ms. Any build of this simulator is later, and a seconds-epoch
# value is ~1000x smaller, so this separates the two representations.
_MILLISECONDS_EPOCH_FLOOR = 1_700_000_000_000


def _without_version(
    payloads: dict[str, dict[str, object]],
) -> dict[str, dict[str, object]]:
    return {
        device_id: {k: v for k, v in payload.items() if k != "version"}
        for device_id, payload in payloads.items()
    }


def _manifest_panel_with_one_circuit() -> DeviceManifest:
    return DeviceManifest(
        instances=(
            DeviceInstance(entity_class="panel", instance_id="p1", display_name="Span"),
            DeviceInstance(entity_class="circuit", instance_id="c1", display_name="Kitchen"),
        )
    )


def test_build_graph_for_panel_and_one_circuit() -> None:
    profiles = load_profiles()
    mapping = load_mapping_table()
    g = build_graph(_manifest_panel_with_one_circuit(), mapping, profiles)
    # Panel is the only Device under v1_flat node-on-parent layout.
    assert "p1" in g.devices
    assert "c1" not in g.devices
    # Circuit's properties are present, attached to the panel device under namespaced nodes.
    assert ("circuit", "c1", "circuit/active-power") in g.properties
    assert ("circuit", "c1", "circuit/relay") in g.properties


def test_build_graph_is_deterministic() -> None:
    profiles = load_profiles()
    mapping = load_mapping_table()
    g1 = build_graph(_manifest_panel_with_one_circuit(), mapping, profiles)
    g2 = build_graph(_manifest_panel_with_one_circuit(), mapping, profiles)
    assert sorted(g1.properties.keys()) == sorted(g2.properties.keys())
    # Every part of a description except ``version`` is a pure function of the
    # manifest. ``version`` is epoch-ms on purpose: Homie 5 uses it to tell a
    # consumer the description changed, and a live panel varies it per build for
    # exactly that reason. So it is checked for shape here, not for equality --
    # asserting two builds agree on it would be asserting the clock stood still.
    assert _without_version(g1.description_payloads) == _without_version(g2.description_payloads)
    versions = [p["version"] for p in g1.description_payloads.values() if "version" in p]
    assert versions, "root description should carry a version"
    for version in versions:
        assert isinstance(version, int)
        # Milliseconds, not seconds: a seconds-epoch value here would still look
        # like a plausible integer and would compare wrong against a panel's.
        assert version > _MILLISECONDS_EPOCH_FLOOR


def test_build_graph_includes_panel_settable_property() -> None:
    profiles = load_profiles()
    mapping = load_mapping_table()
    g = build_graph(_manifest_panel_with_one_circuit(), mapping, profiles)
    assert ("panel", "p1", "core/dominant-power-source") in g.properties


def test_description_declares_the_properties_each_node_publishes() -> None:
    """A node's ``$description`` entry carries its properties, as a panel's does.

    This published ``{"type": ...}`` and nothing else, so the tree a consumer
    discovers the Homie way was empty: 39 nodes declaring zero properties, where a
    panel on the flat data model declares 438. It stayed invisible because
    our own consumer reads values off the wire and takes only ``type`` from here,
    so no test and no integration ever asked the description what it contained.
    """
    profiles = load_profiles()
    mapping = load_mapping_table()
    g = build_graph(_manifest_panel_with_one_circuit(), mapping, profiles)
    root = g.description_payloads["p1"]

    nodes = root["nodes"]
    assert isinstance(nodes, dict)
    assert nodes, "root description declares no nodes"

    for node_id, body in nodes.items():
        assert set(body) == {"name", "type", "properties"}, node_id
        assert body["properties"], f"node {node_id} declares no properties"

    # Every property the graph will publish is declared, and nothing is declared
    # that will not be published -- the invariant that was broken, rather than the
    # weaker "some properties exist".
    #
    # Counted rather than matched name-by-name on purpose. Wire node ids are the
    # capability name for root entities and the instance id for node-on-parent
    # ones, so pairing a `g.properties` key to its node means restating
    # `_attach_profile`'s naming rule here -- and a test that restates the thing it
    # checks passes while mirroring its own copy. Both sides are built from the
    # same walk, so their cardinality is the honest comparison.
    declared_count = sum(len(body["properties"]) for body in nodes.values())
    assert declared_count == len(g.properties)

    # Shape a live panel also publishes, and this did not.
    assert root["children"] == []
    assert root["extensions"] == []
    assert "id" not in root
