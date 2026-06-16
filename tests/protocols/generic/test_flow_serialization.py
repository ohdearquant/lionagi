# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for Flow serialization round-trip support."""

from lionagi.protocols.generic.flow import Flow
from lionagi.protocols.generic.pile import Pile
from lionagi.protocols.generic.progression import Progression
from lionagi.protocols.graph.node import Node

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_nodes(n: int = 3) -> list[Node]:
    """Create n distinct Node instances."""
    return [Node(content=f"node-{i}") for i in range(n)]


def _flow_with_progression(n: int = 3, prog_name: str = "stage-1"):
    """Create a Flow with n items and one named progression over all items."""
    flow = Flow(name="test-flow")
    nodes = _make_nodes(n)
    for node in nodes:
        flow.add_item(node)
    prog = Progression(order=[nd.id for nd in nodes], name=prog_name)
    flow.add_progression(prog)
    return flow, nodes, prog


# ---------------------------------------------------------------------------
# to_dict
# ---------------------------------------------------------------------------


class TestFlowToDict:
    def test_to_dict_has_items_and_progressions(self):
        flow, nodes, prog = _flow_with_progression(3)
        d = flow.to_dict()
        assert "items" in d
        assert "progressions" in d

    def test_to_dict_items_is_dict(self):
        flow, _, _ = _flow_with_progression(2)
        d = flow.to_dict()
        assert isinstance(d["items"], dict)

    def test_to_dict_progressions_is_dict(self):
        flow, _, _ = _flow_with_progression(2)
        d = flow.to_dict()
        assert isinstance(d["progressions"], dict)

    def test_to_dict_items_has_collections(self):
        flow, _, _ = _flow_with_progression(2)
        d = flow.to_dict()
        assert "collections" in d["items"]
        assert len(d["items"]["collections"]) == 2

    def test_to_dict_progressions_has_collections(self):
        flow, _, _ = _flow_with_progression(2)
        d = flow.to_dict()
        assert "collections" in d["progressions"]
        assert len(d["progressions"]["collections"]) == 1

    def test_to_dict_items_has_lion_class(self):
        """Items Pile should have lion_class in metadata for polymorphic dispatch."""
        flow, _, _ = _flow_with_progression(1)
        d = flow.to_dict()
        items_meta = d["items"].get("metadata", {})
        assert "lion_class" in items_meta
        assert "Pile" in items_meta["lion_class"]

    def test_to_dict_preserves_flow_metadata(self):
        flow, _, _ = _flow_with_progression(1)
        d = flow.to_dict()
        assert "lion_class" in d.get("metadata", {})
        assert "Flow" in d["metadata"]["lion_class"]

    def test_to_dict_preserves_name(self):
        flow, _, _ = _flow_with_progression(1)
        d = flow.to_dict()
        assert d["name"] == "test-flow"

    def test_to_dict_preserves_id(self):
        flow, _, _ = _flow_with_progression(1)
        d = flow.to_dict()
        assert d["id"] == str(flow.id)


# ---------------------------------------------------------------------------
# Round-trip: to_dict -> from_dict
# ---------------------------------------------------------------------------


class TestFlowRoundTrip:
    def test_basic_round_trip(self):
        flow, nodes, prog = _flow_with_progression(3, "pipeline")
        d = flow.to_dict()
        flow2 = Flow.from_dict(d)
        assert isinstance(flow2, Flow)

    def test_round_trip_preserves_id(self):
        flow, _, _ = _flow_with_progression(2)
        d = flow.to_dict()
        flow2 = Flow.from_dict(d)
        assert flow2.id == flow.id

    def test_round_trip_preserves_name(self):
        flow, _, _ = _flow_with_progression(2)
        d = flow.to_dict()
        flow2 = Flow.from_dict(d)
        assert flow2.name == flow.name

    def test_round_trip_preserves_created_at(self):
        flow, _, _ = _flow_with_progression(1)
        d = flow.to_dict()
        flow2 = Flow.from_dict(d)
        assert flow2.created_at == flow.created_at

    def test_round_trip_items_are_pile(self):
        flow, _, _ = _flow_with_progression(3)
        d = flow.to_dict()
        flow2 = Flow.from_dict(d)
        assert isinstance(flow2.items, Pile)

    def test_round_trip_progressions_are_pile(self):
        flow, _, _ = _flow_with_progression(3)
        d = flow.to_dict()
        flow2 = Flow.from_dict(d)
        assert isinstance(flow2.progressions, Pile)

    def test_round_trip_item_count(self):
        flow, _, _ = _flow_with_progression(3)
        d = flow.to_dict()
        flow2 = Flow.from_dict(d)
        assert len(flow2.items) == 3

    def test_round_trip_progression_count(self):
        flow, _, _ = _flow_with_progression(3)
        d = flow.to_dict()
        flow2 = Flow.from_dict(d)
        assert len(flow2.progressions) == 1

    def test_round_trip_item_ids_match(self):
        flow, nodes, _ = _flow_with_progression(3)
        d = flow.to_dict()
        flow2 = Flow.from_dict(d)
        original_ids = {n.id for n in nodes}
        restored_ids = set(flow2.items.keys())
        assert original_ids == restored_ids

    def test_round_trip_item_content_preserved(self):
        flow, nodes, _ = _flow_with_progression(3)
        d = flow.to_dict()
        flow2 = Flow.from_dict(d)
        for node in nodes:
            restored = flow2.items[node.id]
            assert restored.content == node.content

    def test_round_trip_items_are_nodes(self):
        """Deserialized items should be Node instances, not raw Elements."""
        flow, _, _ = _flow_with_progression(2)
        d = flow.to_dict()
        flow2 = Flow.from_dict(d)
        for item in flow2.items:
            assert isinstance(item, Node)

    def test_round_trip_progressions_are_progressions(self):
        """Deserialized progressions should be Progression instances."""
        flow, _, _ = _flow_with_progression(2)
        d = flow.to_dict()
        flow2 = Flow.from_dict(d)
        for prog in flow2.progressions:
            assert isinstance(prog, Progression)

    def test_round_trip_referential_integrity(self):
        """All UUIDs in progressions must exist in items after round-trip."""
        flow, _, _ = _flow_with_progression(3, "pipeline")
        d = flow.to_dict()
        flow2 = Flow.from_dict(d)
        item_ids = set(flow2.items.keys())
        for prog in flow2.progressions:
            for uid in prog:
                assert uid in item_ids


# ---------------------------------------------------------------------------
# _progression_names index rebuild
# ---------------------------------------------------------------------------


class TestProgressionNamesRebuild:
    """Test that _progression_names is rebuilt after deserialization."""

    def test_progression_names_rebuilt(self):
        flow, _, _ = _flow_with_progression(2, "my-stage")
        d = flow.to_dict()
        flow2 = Flow.from_dict(d)
        assert "my-stage" in flow2._progression_names

    def test_get_progression_by_name_after_round_trip(self):
        flow, _, prog = _flow_with_progression(2, "lookup-me")
        d = flow.to_dict()
        flow2 = Flow.from_dict(d)
        restored_prog = flow2.get_progression("lookup-me")
        assert restored_prog.name == "lookup-me"
        assert restored_prog.id == prog.id

    def test_multiple_progressions_names_rebuilt(self):
        """Multiple named progressions should all be indexed."""
        flow = Flow(name="multi")
        nodes = _make_nodes(4)
        for n in nodes:
            flow.add_item(n)
        p1 = Progression(order=[nodes[0].id, nodes[1].id], name="alpha")
        p2 = Progression(order=[nodes[2].id, nodes[3].id], name="beta")
        flow.add_progression(p1)
        flow.add_progression(p2)

        d = flow.to_dict()
        flow2 = Flow.from_dict(d)

        assert "alpha" in flow2._progression_names
        assert "beta" in flow2._progression_names
        assert flow2.get_progression("alpha").id == p1.id
        assert flow2.get_progression("beta").id == p2.id

    def test_unnamed_progression_no_name_entry(self):
        """Progressions without names should not create _progression_names entries."""
        flow = Flow()
        nodes = _make_nodes(2)
        for n in nodes:
            flow.add_item(n)
        unnamed = Progression(order=[n.id for n in nodes])
        flow.add_progression(unnamed)

        d = flow.to_dict()
        flow2 = Flow.from_dict(d)
        assert len(flow2._progression_names) == 0


# ---------------------------------------------------------------------------
# Empty flow round-trip
# ---------------------------------------------------------------------------


class TestEmptyFlowRoundTrip:
    def test_empty_flow_to_dict(self):
        flow = Flow()
        d = flow.to_dict()
        assert isinstance(d["items"], dict)
        assert isinstance(d["progressions"], dict)

    def test_empty_flow_round_trip(self):
        flow = Flow()
        d = flow.to_dict()
        flow2 = Flow.from_dict(d)
        assert isinstance(flow2, Flow)
        assert len(flow2.items) == 0
        assert len(flow2.progressions) == 0

    def test_empty_flow_preserves_id(self):
        flow = Flow()
        d = flow.to_dict()
        flow2 = Flow.from_dict(d)
        assert flow2.id == flow.id

    def test_empty_flow_items_is_pile(self):
        flow = Flow()
        d = flow.to_dict()
        flow2 = Flow.from_dict(d)
        assert isinstance(flow2.items, Pile)

    def test_empty_flow_progressions_is_pile(self):
        flow = Flow()
        d = flow.to_dict()
        flow2 = Flow.from_dict(d)
        assert isinstance(flow2.progressions, Pile)

    def test_empty_named_flow_round_trip(self):
        flow = Flow(name="empty-but-named")
        d = flow.to_dict()
        flow2 = Flow.from_dict(d)
        assert flow2.name == "empty-but-named"


# ---------------------------------------------------------------------------
# Pile coercion (_coerce_pile classmethod)
# ---------------------------------------------------------------------------


class TestPileCoercion:
    def test_coerce_pile_passthrough(self):
        """Pile instances should pass through unchanged."""
        pile = Pile()
        result = Flow._coerce_pile(pile)
        assert result is pile

    def test_coerce_dict_to_pile(self):
        """Dict (from Pile.to_dict) should be coerced to Pile."""
        nodes = _make_nodes(2)
        items_pile = Pile(collections=nodes)
        items_dict = items_pile.to_dict()
        result = Flow._coerce_pile(items_dict)
        assert isinstance(result, Pile)
        assert len(result) == 2

    def test_coerce_list_to_pile(self):
        """List of elements should be coerced to Pile."""
        nodes = _make_nodes(2)
        result = Flow._coerce_pile(nodes)
        assert isinstance(result, Pile)
        assert len(result) == 2

    def test_coerce_none_returns_none(self):
        """None should pass through (from_dict handles the fallback)."""
        result = Flow._coerce_pile(None)
        assert result is None

    def test_from_dict_with_dict_items(self):
        """from_dict should coerce dict items field to Pile."""
        flow, _, _ = _flow_with_progression(2)
        d = flow.to_dict()
        assert isinstance(d["items"], dict)
        flow2 = Flow.from_dict(d)
        assert isinstance(flow2.items, Pile)
        assert len(flow2.items) == 2

    def test_from_dict_with_list_items(self):
        """from_dict should coerce list items field to Pile."""
        nodes = _make_nodes(2)
        d = {
            "items": [n.to_dict() for n in nodes],
            "progressions": [],
        }
        flow = Flow.from_dict(d)
        assert isinstance(flow.items, Pile)
        assert len(flow.items) == 2


# ---------------------------------------------------------------------------
# JSON round-trip
# ---------------------------------------------------------------------------


class TestFlowJsonRoundTrip:
    def test_json_round_trip(self):
        flow, nodes, _ = _flow_with_progression(3, "json-stage")
        json_str = flow.to_json()
        flow2 = Flow.from_json(json_str)
        assert isinstance(flow2, Flow)
        assert flow2.id == flow.id
        assert flow2.name == flow.name
        assert len(flow2.items) == 3
        assert len(flow2.progressions) == 1
        assert isinstance(flow2.items, Pile)
        assert isinstance(flow2.progressions, Pile)

    def test_json_round_trip_progression_names(self):
        flow, _, _ = _flow_with_progression(2, "json-lookup")
        json_str = flow.to_json()
        flow2 = Flow.from_json(json_str)
        assert "json-lookup" in flow2._progression_names
        assert flow2.get_progression("json-lookup") is not None


# ---------------------------------------------------------------------------
# Mutability after round-trip
# ---------------------------------------------------------------------------


class TestMutabilityAfterRoundTrip:
    """Ensure deserialized flows are fully functional."""

    def test_add_item_after_round_trip(self):
        flow, _, _ = _flow_with_progression(2, "mutable")
        d = flow.to_dict()
        flow2 = Flow.from_dict(d)
        new_node = Node(content="added-after-roundtrip")
        flow2.add_item(new_node, progressions="mutable")
        assert new_node.id in flow2.items
        restored_prog = flow2.get_progression("mutable")
        assert new_node.id in restored_prog

    def test_remove_item_after_round_trip(self):
        flow, nodes, _ = _flow_with_progression(3, "removable")
        d = flow.to_dict()
        flow2 = Flow.from_dict(d)
        flow2.remove_item(nodes[0].id)
        assert len(flow2.items) == 2

    def test_add_progression_after_round_trip(self):
        flow, nodes, _ = _flow_with_progression(3, "existing")
        d = flow.to_dict()
        flow2 = Flow.from_dict(d)
        new_prog = Progression(order=[nodes[0].id, nodes[1].id], name="new-stage")
        flow2.add_progression(new_prog)
        assert len(flow2.progressions) == 2
        assert flow2.get_progression("new-stage").id == new_prog.id
