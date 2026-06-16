# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Thread-safety tests for synchronized Graph add_node, add_edge, and mixed add/remove."""

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from lionagi.protocols.types import Edge, Graph, Node


class TestGraphSynchronizedAddNode:
    """Concurrent add_node must not corrupt internal_nodes or node_edge_mapping."""

    def test_concurrent_add_node_no_corruption(self):
        """20 threads each add a unique node; final count must be 20."""
        graph = Graph()
        num_threads = 20
        nodes = [Node() for _ in range(num_threads)]

        with ThreadPoolExecutor(max_workers=num_threads) as pool:
            futures = [pool.submit(graph.add_node, n) for n in nodes]
            for f in as_completed(futures):
                f.result()  # propagate any exceptions

        assert len(graph.internal_nodes) == num_threads
        assert len(graph.node_edge_mapping) == num_threads

        for node in nodes:
            assert node.id in graph.node_edge_mapping
            assert graph.node_edge_mapping[node.id] == {"in": {}, "out": {}}

    def test_concurrent_add_node_large_batch(self):
        """100 threads adding nodes concurrently still yields correct count."""
        graph = Graph()
        num_threads = 100
        nodes = [Node() for _ in range(num_threads)]

        with ThreadPoolExecutor(max_workers=num_threads) as pool:
            futures = [pool.submit(graph.add_node, n) for n in nodes]
            for f in as_completed(futures):
                f.result()

        assert len(graph.internal_nodes) == num_threads
        assert len(graph.node_edge_mapping) == num_threads


class TestGraphSynchronizedAddEdge:
    """Concurrent add_edge must not corrupt internal_edges or node_edge_mapping."""

    def test_concurrent_add_edge_no_corruption(self):
        """20 threads each add a unique edge between pre-existing node pairs."""
        graph = Graph()
        num_edges = 20

        # Pre-create nodes: each edge needs a unique head and tail
        heads = [Node() for _ in range(num_edges)]
        tails = [Node() for _ in range(num_edges)]
        for n in heads + tails:
            graph.add_node(n)

        edges = [Edge(head=heads[i], tail=tails[i]) for i in range(num_edges)]

        with ThreadPoolExecutor(max_workers=num_edges) as pool:
            futures = [pool.submit(graph.add_edge, e) for e in edges]
            for f in as_completed(futures):
                f.result()

        assert len(graph.internal_edges) == num_edges

        for edge in edges:
            assert edge.id in graph.node_edge_mapping[edge.head]["out"]
            assert graph.node_edge_mapping[edge.head]["out"][edge.id] == edge.tail
            assert edge.id in graph.node_edge_mapping[edge.tail]["in"]
            assert graph.node_edge_mapping[edge.tail]["in"][edge.id] == edge.head

    def test_concurrent_add_edge_shared_nodes(self):
        """Multiple edges from the same head node added concurrently."""
        graph = Graph()
        head = Node()
        tails = [Node() for _ in range(20)]
        graph.add_node(head)
        for t in tails:
            graph.add_node(t)

        edges = [Edge(head=head, tail=t) for t in tails]

        with ThreadPoolExecutor(max_workers=20) as pool:
            futures = [pool.submit(graph.add_edge, e) for e in edges]
            for f in as_completed(futures):
                f.result()

        assert len(graph.internal_edges) == 20
        assert len(graph.node_edge_mapping[head.id]["out"]) == 20


class TestGraphSynchronizedMixedOperations:
    """Mixed add/remove operations must not deadlock or corrupt state."""

    def test_add_then_remove_no_deadlock(self):
        """Add nodes and remove them concurrently; uses a barrier to maximize contention."""
        graph = Graph()
        num_threads = 20
        nodes = [Node() for _ in range(num_threads)]

        # Phase 1: add all nodes concurrently
        with ThreadPoolExecutor(max_workers=num_threads) as pool:
            futures = [pool.submit(graph.add_node, n) for n in nodes]
            for f in as_completed(futures):
                f.result()

        assert len(graph.internal_nodes) == num_threads

        # Phase 2: remove all nodes concurrently
        with ThreadPoolExecutor(max_workers=num_threads) as pool:
            futures = [pool.submit(graph.remove_node, n) for n in nodes]
            for f in as_completed(futures):
                f.result()

        assert len(graph.internal_nodes) == 0
        assert len(graph.node_edge_mapping) == 0

    def test_interleaved_add_remove_edges(self):
        """Add edges then remove them with overlapping thread pools."""
        graph = Graph()
        num_pairs = 10

        heads = [Node() for _ in range(num_pairs)]
        tails = [Node() for _ in range(num_pairs)]
        for n in heads + tails:
            graph.add_node(n)

        edges = [Edge(head=heads[i], tail=tails[i]) for i in range(num_pairs)]

        for e in edges:
            graph.add_edge(e)

        assert len(graph.internal_edges) == num_pairs

        with ThreadPoolExecutor(max_workers=num_pairs) as pool:
            futures = [pool.submit(graph.remove_edge, e) for e in edges]
            for f in as_completed(futures):
                f.result()

        assert len(graph.internal_edges) == 0
        # Node mapping should still exist but with empty in/out
        for n in heads + tails:
            assert graph.node_edge_mapping[n.id] == {"in": {}, "out": {}}

    def test_mixed_add_node_and_add_edge_no_deadlock(self):
        """Concurrent add_node and add_edge on non-overlapping data; RLock ensures no deadlock."""
        graph = Graph()

        # Pre-seed nodes for edge operations
        seed_heads = [Node() for _ in range(10)]
        seed_tails = [Node() for _ in range(10)]
        for n in seed_heads + seed_tails:
            graph.add_node(n)

        new_nodes = [Node() for _ in range(10)]
        new_edges = [Edge(head=seed_heads[i], tail=seed_tails[i]) for i in range(10)]

        errors = []

        def add_node_task(node):
            try:
                graph.add_node(node)
            except Exception as exc:
                errors.append(exc)

        def add_edge_task(edge):
            try:
                graph.add_edge(edge)
            except Exception as exc:
                errors.append(exc)

        with ThreadPoolExecutor(max_workers=20) as pool:
            for n in new_nodes:
                pool.submit(add_node_task, n)
            for e in new_edges:
                pool.submit(add_edge_task, e)

        assert errors == [], f"Unexpected errors: {errors}"
        assert len(graph.internal_nodes) == 30  # 20 seeds + 10 new
        assert len(graph.internal_edges) == 10

    def test_remove_node_cascades_edges_under_contention(self):
        """Removing head nodes concurrently cascades edge removal."""
        graph = Graph()
        num = 10

        heads = [Node() for _ in range(num)]
        tails = [Node() for _ in range(num)]
        for n in heads + tails:
            graph.add_node(n)

        edges = [Edge(head=heads[i], tail=tails[i]) for i in range(num)]
        for e in edges:
            graph.add_edge(e)

        assert len(graph.internal_edges) == num

        with ThreadPoolExecutor(max_workers=num) as pool:
            futures = [pool.submit(graph.remove_node, h) for h in heads]
            for f in as_completed(futures):
                f.result()

        assert len(graph.internal_edges) == 0
        assert len(graph.internal_nodes) == num  # only tails remain


class TestGraphLockAttribute:
    """Verify the _lock PrivateAttr exists and is an RLock."""

    def test_lock_is_rlock(self):
        graph = Graph()
        assert hasattr(graph, "_lock")
        assert isinstance(graph._lock, type(threading.RLock()))

    def test_lock_is_per_instance(self):
        g1 = Graph()
        g2 = Graph()
        assert g1._lock is not g2._lock

    def test_lock_not_serialized(self):
        """The _lock field must not appear in serialized output."""
        graph = Graph()
        d = graph.to_dict()
        assert "_lock" not in d
