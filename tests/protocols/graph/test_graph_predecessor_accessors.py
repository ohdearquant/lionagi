# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for Graph's id-only and cached predecessor/successor accessors.

Covers correctness (parity with get_predecessors/get_successors), cache-hit
identity, and invalidation across every mutator that can change adjacency:
add_edge, remove_edge, remove_node, replace_node, splice_after.
"""

import threading

import pytest

from lionagi._errors import RelationError
from lionagi.protocols.types import Edge, Graph, Node


@pytest.fixture
def complex_graph():
    """Fixture for complex graph with multiple nodes and edges"""
    graph = Graph()

    nodes = [Node() for _ in range(4)]

    for node in nodes:
        graph.add_node(node)

    edges = [
        Edge(head=nodes[0], tail=nodes[1]),  # 0 -> 1
        Edge(head=nodes[1], tail=nodes[2]),  # 1 -> 2
        Edge(head=nodes[2], tail=nodes[3]),  # 2 -> 3
        Edge(head=nodes[0], tail=nodes[3]),  # 0 -> 3
    ]

    for edge in edges:
        graph.add_edge(edge)

    return graph, nodes, edges


class TestIdOnlyAccessors:
    def test_predecessor_ids_matches_get_predecessors(self, complex_graph):
        graph, nodes, _ = complex_graph
        ids = graph.predecessor_ids(nodes[3])
        assert set(ids) == {n.id for n in graph.get_predecessors(nodes[3])}

    def test_successor_ids_matches_get_successors(self, complex_graph):
        graph, nodes, _ = complex_graph
        ids = graph.successor_ids(nodes[0])
        assert set(ids) == {n.id for n in graph.get_successors(nodes[0])}

    def test_predecessor_ids_empty_for_head_node(self, complex_graph):
        graph, nodes, _ = complex_graph
        assert graph.predecessor_ids(nodes[0]) == ()

    def test_successor_ids_empty_for_tail_node(self, complex_graph):
        graph, nodes, _ = complex_graph
        assert graph.successor_ids(nodes[3]) == ()

    def test_predecessor_ids_missing_node_raises(self):
        graph = Graph()
        with pytest.raises(RelationError):
            graph.predecessor_ids(Node())

    def test_successor_ids_missing_node_raises(self):
        graph = Graph()
        with pytest.raises(RelationError):
            graph.successor_ids(Node())


class TestCachedAccessorsCorrectness:
    def test_get_predecessors_cached_matches_get_predecessors(self, complex_graph):
        graph, nodes, _ = complex_graph
        cached = graph.get_predecessors_cached(nodes[3])
        assert isinstance(cached, tuple)
        assert {n.id for n in cached} == {n.id for n in graph.get_predecessors(nodes[3])}

    def test_get_successors_cached_matches_get_successors(self, complex_graph):
        graph, nodes, _ = complex_graph
        cached = graph.get_successors_cached(nodes[0])
        assert isinstance(cached, tuple)
        assert {n.id for n in cached} == {n.id for n in graph.get_successors(nodes[0])}

    def test_get_predecessors_cached_missing_node_raises(self):
        graph = Graph()
        with pytest.raises(RelationError):
            graph.get_predecessors_cached(Node())

    def test_get_successors_cached_missing_node_raises(self):
        graph = Graph()
        with pytest.raises(RelationError):
            graph.get_successors_cached(Node())


class TestCacheHitBehavior:
    def test_repeated_predecessor_call_returns_same_object(self, complex_graph):
        graph, nodes, _ = complex_graph
        first = graph.get_predecessors_cached(nodes[3])
        second = graph.get_predecessors_cached(nodes[3])
        assert first is second

    def test_repeated_successor_call_returns_same_object(self, complex_graph):
        graph, nodes, _ = complex_graph
        first = graph.get_successors_cached(nodes[0])
        second = graph.get_successors_cached(nodes[0])
        assert first is second


class TestCacheInvalidationOnMutators:
    """Every mutator that changes adjacency must invalidate stale cache entries."""

    def test_add_edge_invalidates_tail_predecessors_and_head_successors(self):
        graph = Graph()
        a, b, c = Node(), Node(), Node()
        graph.add_node(a)
        graph.add_node(b)
        graph.add_node(c)
        graph.add_edge(Edge(head=a, tail=b))

        stale_pred = graph.get_predecessors_cached(b)
        stale_succ = graph.get_successors_cached(a)
        assert {n.id for n in stale_pred} == {a.id}
        assert {n.id for n in stale_succ} == {b.id}

        graph.add_edge(Edge(head=c, tail=b))

        fresh_pred = graph.get_predecessors_cached(b)
        assert fresh_pred is not stale_pred
        assert {n.id for n in fresh_pred} == {a.id, c.id}

        graph.add_edge(Edge(head=a, tail=c))
        fresh_succ = graph.get_successors_cached(a)
        assert fresh_succ is not stale_succ
        assert {n.id for n in fresh_succ} == {b.id, c.id}

    def test_remove_edge_invalidates_tail_predecessors_and_head_successors(self):
        graph = Graph()
        a, b = Node(), Node()
        graph.add_node(a)
        graph.add_node(b)
        edge = Edge(head=a, tail=b)
        graph.add_edge(edge)

        stale_pred = graph.get_predecessors_cached(b)
        stale_succ = graph.get_successors_cached(a)
        assert {n.id for n in stale_pred} == {a.id}

        graph.remove_edge(edge)

        fresh_pred = graph.get_predecessors_cached(b)
        fresh_succ = graph.get_successors_cached(a)
        assert fresh_pred is not stale_pred
        assert fresh_succ is not stale_succ
        assert fresh_pred == ()
        assert fresh_succ == ()

    def test_remove_node_invalidates_neighbors(self):
        graph = Graph()
        a, b, c = Node(), Node(), Node()
        graph.add_node(a)
        graph.add_node(b)
        graph.add_node(c)
        graph.add_edge(Edge(head=a, tail=b))
        graph.add_edge(Edge(head=b, tail=c))

        stale_succ_a = graph.get_successors_cached(a)  # [b]
        stale_pred_c = graph.get_predecessors_cached(c)  # [b]
        assert {n.id for n in stale_succ_a} == {b.id}
        assert {n.id for n in stale_pred_c} == {b.id}

        graph.remove_node(b)

        fresh_succ_a = graph.get_successors_cached(a)
        fresh_pred_c = graph.get_predecessors_cached(c)
        assert fresh_succ_a is not stale_succ_a
        assert fresh_pred_c is not stale_pred_c
        assert fresh_succ_a == ()
        assert fresh_pred_c == ()

    def test_replace_node_invalidates_old_new_and_neighbors(self):
        graph = Graph()
        a, b, c = Node(), Node(), Node()
        graph.add_node(a)
        graph.add_node(b)
        graph.add_node(c)
        graph.add_edge(Edge(head=a, tail=b))
        graph.add_edge(Edge(head=b, tail=c))

        stale_succ_a = graph.get_successors_cached(a)  # [b]
        stale_pred_c = graph.get_predecessors_cached(c)  # [b]

        new_b = Node()
        graph.replace_node(b, new_b)

        fresh_succ_a = graph.get_successors_cached(a)
        fresh_pred_c = graph.get_predecessors_cached(c)
        assert fresh_succ_a is not stale_succ_a
        assert fresh_pred_c is not stale_pred_c
        assert {n.id for n in fresh_succ_a} == {new_b.id}
        assert {n.id for n in fresh_pred_c} == {new_b.id}
        # new_b's own cache reflects the rewired adjacency, not stale entries
        assert {n.id for n in graph.get_predecessors_cached(new_b)} == {a.id}
        assert {n.id for n in graph.get_successors_cached(new_b)} == {c.id}

    def test_splice_after_invalidates_anchor_and_former_successors(self):
        graph = Graph()
        anchor, s1, s2 = Node(), Node(), Node()
        graph.add_node(anchor)
        graph.add_node(s1)
        graph.add_node(s2)
        graph.add_edge(Edge(head=anchor, tail=s1))
        graph.add_edge(Edge(head=anchor, tail=s2))

        stale_succ_anchor = graph.get_successors_cached(anchor)  # [s1, s2]
        stale_pred_s1 = graph.get_predecessors_cached(s1)  # [anchor]

        new_node = Node()
        graph.splice_after(anchor, new_node)

        fresh_succ_anchor = graph.get_successors_cached(anchor)
        fresh_pred_s1 = graph.get_predecessors_cached(s1)
        assert fresh_succ_anchor is not stale_succ_anchor
        assert fresh_pred_s1 is not stale_pred_s1
        assert {n.id for n in fresh_succ_anchor} == {new_node.id}
        assert {n.id for n in fresh_pred_s1} == {new_node.id}
        # new_node inherits anchor's former successors and anchor as predecessor
        assert {n.id for n in graph.get_successors_cached(new_node)} == {s1.id, s2.id}
        assert {n.id for n in graph.get_predecessors_cached(new_node)} == {anchor.id}

    def test_add_node_does_not_disturb_existing_cache_entries(self):
        """A brand-new, edge-less node cannot invalidate anyone else's adjacency."""
        graph = Graph()
        a, b = Node(), Node()
        graph.add_node(a)
        graph.add_node(b)
        graph.add_edge(Edge(head=a, tail=b))

        cached = graph.get_predecessors_cached(b)
        graph.add_node(Node())
        assert graph.get_predecessors_cached(b) is cached


class TestCachedAccessorsReturnImmutableTuples:
    """get_predecessors_cached/get_successors_cached hand back the exact
    memoized object on every cache hit. If that object were a mutable list,
    a caller doing e.g. ``graph.get_predecessors_cached(node).append(x)``
    would silently corrupt the graph-level cache for every other reader
    until an unrelated mutator happened to evict the entry. Returning a
    tuple closes that hazard structurally: there is no in-place mutation
    API to call in the first place, so a corrupted-cache scenario is
    impossible to construct, not just discouraged by convention.
    """

    def test_predecessor_cache_entry_rejects_append(self, complex_graph):
        graph, nodes, _ = complex_graph
        cached = graph.get_predecessors_cached(nodes[3])
        with pytest.raises(AttributeError):
            cached.append(Node())

    def test_predecessor_cache_entry_rejects_item_assignment(self, complex_graph):
        graph, nodes, _ = complex_graph
        cached = graph.get_predecessors_cached(nodes[3])
        with pytest.raises(TypeError):
            cached[0] = Node()

    def test_successor_cache_entry_rejects_clear(self, complex_graph):
        graph, nodes, _ = complex_graph
        cached = graph.get_successors_cached(nodes[0])
        assert not hasattr(cached, "clear")

    def test_attempted_mutation_cannot_corrupt_subsequent_reads(self, complex_graph):
        """Even after a failed mutation attempt, later cache hits must still
        return the same, correct data — proving there is no partial-mutation
        window a tuple's immutability could otherwise leave open.
        """
        graph, nodes, _ = complex_graph
        expected_ids = {n.id for n in graph.get_predecessors(nodes[3])}

        cached = graph.get_predecessors_cached(nodes[3])
        with pytest.raises(AttributeError):
            cached.append(Node())
        with pytest.raises(TypeError):
            cached[0] = Node()

        again = graph.get_predecessors_cached(nodes[3])
        assert again is cached
        assert {n.id for n in again} == expected_ids


class TestCachedAccessorsSerializeWithMutators:
    """The cache lives on the Graph instance, so it is visible to every
    concurrent consumer (unlike the old per-executor cache it replaced). A
    cache-populating read must not interleave with a mutator: otherwise a
    miss can read pre-mutation adjacency, a mutator can invalidate and
    update the graph, and the read can then store the stale result — a
    wrong answer that persists until an unrelated future mutation happens
    to evict that node's entry. These tests prove get_predecessors_cached/
    get_successors_cached block while another thread holds the graph lock,
    the same guarantee every mutator already provides for itself.
    """

    def test_get_predecessors_cached_blocks_while_lock_held(self):
        graph = Graph()
        a, b = Node(), Node()
        graph.add_node(a)
        graph.add_node(b)
        graph.add_edge(Edge(head=a, tail=b))

        entered = threading.Event()
        release = threading.Event()

        def hold_lock():
            with graph._lock:
                entered.set()
                release.wait(timeout=5)

        holder = threading.Thread(target=hold_lock)
        holder.start()
        try:
            assert entered.wait(timeout=5)

            started = threading.Event()
            result = {}

            def read_cache():
                started.set()
                result["value"] = graph.get_predecessors_cached(b)

            reader = threading.Thread(target=read_cache)
            reader.start()
            try:
                assert started.wait(timeout=5)
                reader.join(timeout=0.2)
                assert reader.is_alive(), "get_predecessors_cached did not respect the graph lock"
            finally:
                release.set()
                reader.join(timeout=5)
            assert {n.id for n in result["value"]} == {a.id}
        finally:
            release.set()
            holder.join(timeout=5)

    def test_get_successors_cached_blocks_while_lock_held(self):
        graph = Graph()
        a, b = Node(), Node()
        graph.add_node(a)
        graph.add_node(b)
        graph.add_edge(Edge(head=a, tail=b))

        entered = threading.Event()
        release = threading.Event()

        def hold_lock():
            with graph._lock:
                entered.set()
                release.wait(timeout=5)

        holder = threading.Thread(target=hold_lock)
        holder.start()
        try:
            assert entered.wait(timeout=5)

            started = threading.Event()
            result = {}

            def read_cache():
                started.set()
                result["value"] = graph.get_successors_cached(a)

            reader = threading.Thread(target=read_cache)
            reader.start()
            try:
                assert started.wait(timeout=5)
                reader.join(timeout=0.2)
                assert reader.is_alive(), "get_successors_cached did not respect the graph lock"
            finally:
                release.set()
                reader.join(timeout=5)
            assert {n.id for n in result["value"]} == {b.id}
        finally:
            release.set()
            holder.join(timeout=5)
