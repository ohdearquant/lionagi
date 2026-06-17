# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for Flow container (lionagi/protocols/generic/flow.py)."""

from uuid import uuid4

import pytest

from lionagi._errors import ItemExistsError, ItemNotFoundError
from lionagi.protocols.generic.flow import Flow
from lionagi.protocols.generic.progression import Progression
from lionagi.protocols.graph.node import Node

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_nodes(n: int = 3) -> list[Node]:
    """Create n distinct Node instances."""
    return [Node(content=f"node-{i}") for i in range(n)]


def _flow_with_items(n: int = 3):
    """Create a Flow pre-loaded with n items, return (flow, nodes)."""
    flow = Flow()
    nodes = _make_nodes(n)
    for node in nodes:
        flow.add_item(node)
    return flow, nodes


def _make_progression(nodes: list[Node], name: str | None = None) -> Progression:
    """Create a non-empty Progression referencing the given nodes."""
    return Progression(order=[n.id for n in nodes], name=name)


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestFlowCreation:
    def test_empty_flow(self):
        flow = Flow()
        assert len(flow) == 0
        assert len(flow.items) == 0
        assert len(flow.progressions) == 0
        assert flow.name is None

    def test_flow_with_name(self):
        flow = Flow(name="my-flow")
        assert flow.name == "my-flow"
        assert len(flow) == 0


# ---------------------------------------------------------------------------
# add_progression
# ---------------------------------------------------------------------------


class TestAddProgression:
    """Empty Progressions (__bool__==False) are silently dropped by Pile.include; all test progressions reference at least one item."""

    def test_add_basic_progression(self):
        flow, nodes = _flow_with_items(2)
        prog = _make_progression(nodes, name="stage-1")
        flow.add_progression(prog)
        assert len(flow.progressions) == 1
        assert flow.get_progression("stage-1") is prog

    def test_add_progression_with_name(self):
        flow, nodes = _flow_with_items(1)
        prog = _make_progression(nodes, name="alpha")
        flow.add_progression(prog)
        retrieved = flow.get_progression("alpha")
        assert retrieved.id == prog.id

    def test_add_progression_duplicate_name_raises(self):
        flow, nodes = _flow_with_items(2)
        flow.add_progression(_make_progression(nodes[:1], name="dup"))
        with pytest.raises(ItemExistsError, match="dup"):
            flow.add_progression(_make_progression(nodes[1:], name="dup"))

    def test_add_progression_with_missing_item_uuids_raises(self):
        """Progression references UUIDs not present in flow.items."""
        flow = Flow()
        orphan_id = uuid4()
        prog = Progression(order=[orphan_id], name="bad")
        with pytest.raises(ItemNotFoundError, match="missing items"):
            flow.add_progression(prog)

    def test_add_progression_with_valid_item_uuids(self):
        """Progression UUIDs that exist in items should succeed."""
        flow, nodes = _flow_with_items(2)
        prog = _make_progression(nodes, name="ok")
        flow.add_progression(prog)
        assert len(flow.progressions) == 1


# ---------------------------------------------------------------------------
# remove_progression
# ---------------------------------------------------------------------------


class TestRemoveProgression:
    def _setup(self):
        flow, nodes = _flow_with_items(2)
        prog = _make_progression(nodes, name="removeme")
        flow.add_progression(prog)
        return flow, nodes, prog

    def test_remove_by_uuid(self):
        flow, nodes, prog = self._setup()
        assert len(flow.progressions) == 1
        flow.remove_progression(prog.id)
        assert len(flow.progressions) == 0

    def test_remove_by_name(self):
        flow, _, _ = self._setup()
        flow.remove_progression("removeme")
        assert len(flow.progressions) == 0

    def test_remove_by_instance(self):
        flow, _, prog = self._setup()
        flow.remove_progression(prog)
        assert len(flow.progressions) == 0

    def test_remove_clears_name_index(self):
        """After removal the name should be available for reuse."""
        flow, nodes, _ = self._setup()
        flow.remove_progression("removeme")
        # Name is free again -- should not raise
        new_prog = _make_progression(nodes, name="removeme")
        flow.add_progression(new_prog)
        assert len(flow.progressions) == 1


# ---------------------------------------------------------------------------
# get_progression
# ---------------------------------------------------------------------------


class TestGetProgression:
    def _setup(self):
        flow, nodes = _flow_with_items(2)
        prog = _make_progression(nodes, name="named")
        flow.add_progression(prog)
        return flow, prog

    def test_get_by_uuid(self):
        flow, prog = self._setup()
        assert flow.get_progression(prog.id) is prog

    def test_get_by_name(self):
        flow, prog = self._setup()
        assert flow.get_progression("named") is prog

    def test_get_by_instance(self):
        flow, prog = self._setup()
        assert flow.get_progression(prog) is prog

    def test_get_missing_name_raises_item_not_found(self):
        flow = Flow()
        with pytest.raises(ItemNotFoundError, match="not found"):
            flow.get_progression("nonexistent")

    def test_get_missing_uuid_raises(self):
        flow = Flow()
        with pytest.raises(ItemNotFoundError):
            flow.get_progression(uuid4())


# ---------------------------------------------------------------------------
# add_item
# ---------------------------------------------------------------------------


class TestAddItem:
    def test_add_item_basic(self):
        flow = Flow()
        node = Node(content="hello")
        flow.add_item(node)
        assert len(flow) == 1
        assert node.id in flow.items

    def test_add_item_with_progression_by_name(self):
        flow, nodes = _flow_with_items(1)
        prog = _make_progression(nodes, name="stage")
        flow.add_progression(prog)
        new_node = Node(content="x")
        flow.add_item(new_node, progressions="stage")
        assert new_node.id in prog

    def test_add_item_with_progression_by_uuid(self):
        flow, nodes = _flow_with_items(1)
        prog = _make_progression(nodes, name="s2")
        flow.add_progression(prog)
        new_node = Node(content="y")
        flow.add_item(new_node, progressions=prog.id)
        assert new_node.id in prog

    def test_add_item_with_progression_by_instance(self):
        flow, nodes = _flow_with_items(1)
        prog = _make_progression(nodes, name="s3")
        flow.add_progression(prog)
        new_node = Node(content="z")
        flow.add_item(new_node, progressions=prog)
        assert new_node.id in prog

    def test_add_item_to_multiple_progressions(self):
        flow, nodes = _flow_with_items(2)
        p1 = _make_progression(nodes[:1], name="a")
        p2 = _make_progression(nodes[1:], name="b")
        flow.add_progression(p1)
        flow.add_progression(p2)
        new_node = Node(content="multi")
        flow.add_item(new_node, progressions=["a", "b"])
        assert new_node.id in p1
        assert new_node.id in p2

    def test_add_item_nonexistent_progression_raises(self):
        flow = Flow()
        node = Node(content="oops")
        with pytest.raises(ItemNotFoundError):
            flow.add_item(node, progressions="ghost")


# ---------------------------------------------------------------------------
# remove_item
# ---------------------------------------------------------------------------


class TestRemoveItem:
    """Test Flow.remove_item removes from items and all progressions."""

    def test_remove_item_basic(self):
        flow = Flow()
        node = Node(content="bye")
        flow.add_item(node)
        assert len(flow) == 1
        flow.remove_item(node.id)
        assert len(flow) == 0

    def test_remove_item_clears_from_progressions(self):
        flow, nodes = _flow_with_items(3)
        p1 = _make_progression(nodes[:2], name="p1")
        p2 = _make_progression(nodes[1:], name="p2")
        flow.add_progression(p1)
        flow.add_progression(p2)
        # nodes[1] is in both p1 and p2
        shared = nodes[1]
        assert shared.id in p1
        assert shared.id in p2
        flow.remove_item(shared)
        assert shared.id not in p1
        assert shared.id not in p2
        assert len(flow) == 2  # nodes[0] and nodes[2] remain

    def test_remove_item_by_element_instance(self):
        flow = Flow()
        node = Node(content="inst")
        flow.add_item(node)
        flow.remove_item(node)
        assert len(flow) == 0


# ---------------------------------------------------------------------------
# clear
# ---------------------------------------------------------------------------


class TestFlowClear:
    def test_clear(self):
        flow, nodes = _flow_with_items(3)
        prog = _make_progression(nodes, name="clearme")
        flow.add_progression(prog)
        assert len(flow) == 3
        assert len(flow.progressions) == 1

        flow.clear()
        assert len(flow) == 0
        assert len(flow.items) == 0
        assert len(flow.progressions) == 0


# ---------------------------------------------------------------------------
# __repr__ and __len__
# ---------------------------------------------------------------------------


class TestFlowReprLen:
    def test_len_empty(self):
        assert len(Flow()) == 0

    def test_len_with_items(self):
        flow, _ = _flow_with_items(5)
        assert len(flow) == 5

    def test_repr_empty(self):
        flow = Flow()
        r = repr(flow)
        assert "items=0" in r
        assert "progressions=0" in r

    def test_repr_with_name(self):
        flow = Flow(name="test-flow")
        r = repr(flow)
        assert "test-flow" in r

    def test_repr_with_items_and_progressions(self):
        flow, nodes = _flow_with_items(1)
        prog = _make_progression(nodes, name="s")
        flow.add_progression(prog)
        r = repr(flow)
        assert "items=1" in r
        assert "progressions=1" in r


# ---------------------------------------------------------------------------
# Referential integrity on init
# ---------------------------------------------------------------------------


class TestReferentialIntegrityOnInit:
    """Progression UUIDs must exist in items at construction time."""

    def test_valid_init_with_matching_ids(self):
        nodes = _make_nodes(2)
        prog = Progression(order=[n.id for n in nodes], name="ok")
        # Pile expects dict-style init for Flow fields
        flow = Flow(
            items={"collections": nodes},
            progressions={"collections": [prog]},
        )
        assert len(flow) == 2

    def test_invalid_init_missing_ids_raises(self):
        orphan_id = uuid4()
        prog = Progression(order=[orphan_id], name="bad")
        with pytest.raises(ItemNotFoundError, match="missing items"):
            Flow(
                items={"collections": []},
                progressions={"collections": [prog]},
            )
