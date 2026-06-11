# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for Graph.replace_node() and Graph.splice_after().

Also exercises the Edge constructor's relaxation from ``EdgeCondition``
to any ``Condition`` subclass.
"""

from uuid import uuid4

import pytest

from lionagi._errors import RelationError
from lionagi.protocols._concepts import Condition
from lionagi.protocols.graph.edge import Edge, EdgeCondition
from lionagi.protocols.graph.graph import Graph
from lionagi.protocols.graph.node import Node


class _YesCondition(EdgeCondition):
    """Concrete EdgeCondition subclass usable in tests."""

    async def apply(self, *args, **kwargs) -> bool:
        return True


class _AsyncYesCondition(Condition):
    """Custom Condition subclass that is not an EdgeCondition."""

    def __init__(self, value: bool = True):
        self.value = value

    async def apply(self, *args, **kwargs) -> bool:
        return self.value


# ── replace_node ────────────────────────────────────────────────────────


def test_replace_node_preserves_incoming_and_outgoing_edges():
    g = Graph()
    a, b, c = Node(), Node(), Node()
    g.add_node(a)
    g.add_node(b)
    g.add_node(c)
    g.add_edge(Edge(head=a.id, tail=b.id))
    g.add_edge(Edge(head=b.id, tail=c.id))

    new_b = Node()
    removed = g.replace_node(b, new_b)

    assert removed.id == b.id
    assert new_b.id in g.internal_nodes
    assert b.id not in g.internal_nodes
    assert {n.id for n in g.get_successors(a)} == {new_b.id}
    assert {n.id for n in g.get_successors(new_b)} == {c.id}
    assert {n.id for n in g.get_predecessors(c)} == {new_b.id}


def test_replace_node_preserves_edge_conditions_and_labels():
    g = Graph()
    a, b, c = Node(), Node(), Node()
    g.add_node(a)
    g.add_node(b)
    g.add_node(c)
    cond = _YesCondition(source="token")
    g.add_edge(Edge(head=a.id, tail=b.id, condition=cond, label=["primary"]))
    g.add_edge(Edge(head=b.id, tail=c.id, label=["downstream"]))

    new_b = Node()
    g.replace_node(b, new_b)

    in_edges = [g.internal_edges[eid] for eid in g.node_edge_mapping[new_b.id]["in"]]
    out_edges = [g.internal_edges[eid] for eid in g.node_edge_mapping[new_b.id]["out"]]
    assert len(in_edges) == 1 and in_edges[0].condition is cond
    assert in_edges[0].label == ["primary"]
    assert len(out_edges) == 1 and out_edges[0].label == ["downstream"]


def test_replace_node_rejects_missing_old_node():
    g = Graph()
    new_node = Node()
    with pytest.raises(RelationError, match="not found in graph"):
        g.replace_node(uuid4(), new_node)


def test_replace_node_rejects_replacement_already_in_graph():
    g = Graph()
    a, b = Node(), Node()
    g.add_node(a)
    g.add_node(b)
    with pytest.raises(RelationError, match="already in graph"):
        g.replace_node(a, b)


# ── splice_after ────────────────────────────────────────────────────────


def test_splice_after_inserts_between_anchor_and_successors():
    g = Graph()
    a, s1, s2 = Node(), Node(), Node()
    g.add_node(a)
    g.add_node(s1)
    g.add_node(s2)
    g.add_edge(Edge(head=a.id, tail=s1.id))
    g.add_edge(Edge(head=a.id, tail=s2.id))

    new = Node()
    new_edges = g.splice_after(a, new)

    assert new.id in g.internal_nodes
    # New edges: link + 2 replacement edges
    assert len(new_edges) == 3
    assert {n.id for n in g.get_successors(a)} == {new.id}
    assert {n.id for n in g.get_successors(new)} == {s1.id, s2.id}


def test_splice_after_preserves_custom_edge_properties():
    g = Graph()
    a, s = Node(), Node()
    g.add_node(a)
    g.add_node(s)
    g.add_edge(Edge(head=a.id, tail=s.id, label=["hot"], weight=7, custom="keep"))

    new = Node()
    g.splice_after(a, new)

    # The new-to-successor edge should carry extra properties forward.
    successor_edges = [g.internal_edges[eid] for eid in g.node_edge_mapping[new.id]["out"]]
    assert len(successor_edges) == 1
    e = successor_edges[0]
    assert e.label == ["hot"]
    assert e.properties.get("weight") == 7
    assert e.properties.get("custom") == "keep"


def test_splice_after_with_no_successors_just_links():
    g = Graph()
    a = Node()
    g.add_node(a)

    new = Node()
    new_edges = g.splice_after(a, new)

    assert len(new_edges) == 1
    assert new_edges[0].head == a.id and new_edges[0].tail == new.id


def test_splice_after_rejects_missing_anchor():
    g = Graph()
    new = Node()
    with pytest.raises(RelationError, match="not found in graph"):
        g.splice_after(uuid4(), new)


def test_splice_after_rejects_new_already_in_graph():
    g = Graph()
    a, b = Node(), Node()
    g.add_node(a)
    g.add_node(b)
    with pytest.raises(RelationError, match="already in graph"):
        g.splice_after(a, b)


# ── Edge accepts arbitrary Condition subclass ──────────────────────────


def test_edge_accepts_custom_condition_subclass():
    a, b = Node(), Node()
    cond = _AsyncYesCondition()
    # Must construct without error — previously required EdgeCondition.
    e = Edge(head=a.id, tail=b.id, condition=cond)
    assert e.condition is cond


def test_edge_rejects_non_condition():
    a, b = Node(), Node()

    class _NotACondition:
        async def apply(self, *args, **kwargs):
            return True

    with pytest.raises(ValueError, match="Condition subclass"):
        Edge(head=a.id, tail=b.id, condition=_NotACondition())


def test_edge_setter_accepts_custom_condition_subclass():
    a, b = Node(), Node()
    e = Edge(head=a.id, tail=b.id)
    e.condition = _AsyncYesCondition()
    assert isinstance(e.condition, _AsyncYesCondition)


def test_edge_setter_rejects_non_condition():
    a, b = Node(), Node()
    e = Edge(head=a.id, tail=b.id)

    class _NotACondition:
        pass

    with pytest.raises(ValueError, match="Condition subclass"):
        e.condition = _NotACondition()


# ---------------------------------------------------------------------------
# D5 – replace_node rewires inbound and outbound edges
# ---------------------------------------------------------------------------


def test_graph_replace_node_rewires_inbound_and_outbound_edges():
    """replace_node transfers all edges from old node to new node."""
    g = Graph()
    a, b, c = Node(), Node(), Node()
    for n in (a, b, c):
        g.add_node(n)

    # a --> b --> c
    e_ab = Edge(head=a.id, tail=b.id)
    e_bc = Edge(head=b.id, tail=c.id)
    g.add_edge(e_ab)
    g.add_edge(e_bc)

    new_b = Node()
    g.replace_node(b, new_b)

    # old b is gone; new_b is in graph
    assert b.id not in g.internal_nodes
    assert new_b.id in g.internal_nodes

    # Edge that was a->b should now point to new_b
    assert g.internal_edges[e_ab.id].tail == new_b.id
    # Edge that was b->c should now originate from new_b
    assert g.internal_edges[e_bc.id].head == new_b.id

    # new_b has one inbound and one outbound edge in the adjacency mapping
    mapping = g.node_edge_mapping[new_b.id]
    assert e_ab.id in mapping["in"]
    assert e_bc.id in mapping["out"]
