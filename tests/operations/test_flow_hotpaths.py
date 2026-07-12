# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Regression coverage for flow executor hot-path lookups."""

from __future__ import annotations

import pytest

from lionagi.casts.emission import SpawnRequest
from lionagi.operations.flow import DependencyAwareExecutor, ReactiveExecutor
from lionagi.operations.node import Operation
from lionagi.protocols.graph.edge import Edge, EdgeCondition
from lionagi.protocols.graph.graph import Graph
from lionagi.session.branch import Branch
from lionagi.session.session import Session


class _AlwaysFalse(EdgeCondition):
    async def apply(self, context: dict) -> bool:
        return False


class _AlwaysTrue(EdgeCondition):
    async def apply(self, context: dict) -> bool:
        return True


def _session() -> Session:
    session = Session()
    branch = Branch()
    session.include_branches(branch)
    session.default_branch = branch
    return session


@pytest.mark.asyncio
async def test_edge_condition_uses_same_incoming_edges_as_full_scan_for_fan_in():
    """Incoming adjacency must retain the full-scan edge set for a fan-in target."""
    left = Operation(operation="chat", parameters={})
    right = Operation(operation="chat", parameters={})
    join = Operation(operation="chat", parameters={})
    other = Operation(operation="chat", parameters={})
    graph = Graph()
    for node in (left, right, join, other):
        graph.add_node(node)

    left_join = Edge(head=left.id, tail=join.id, condition=_AlwaysFalse())
    right_join = Edge(head=right.id, tail=join.id, condition=_AlwaysTrue())
    graph.add_edge(left_join)
    graph.add_edge(Edge(head=left.id, tail=other.id))
    graph.add_edge(right_join)
    graph.add_edge(Edge(head=right.id, tail=other.id))

    expected = [edge.id for edge in graph.internal_edges.values() if edge.tail == join.id]
    actual = list(graph.node_edge_mapping[join.id]["in"])
    assert actual == expected

    executor = DependencyAwareExecutor(session=_session(), graph=graph)
    executor.completion_events[left.id].set()
    executor.completion_events[right.id].set()
    assert await executor._check_edge_conditions(join)


def test_predecessor_cache_refreshes_after_reactive_expansion_rewires_a_node():
    """A topology change cannot reuse a predecessor list cached before expansion."""
    root = Operation(operation="chat", parameters={})
    target = Operation(operation="chat", parameters={})
    graph = Graph()
    graph.add_node(root)
    graph.add_node(target)
    graph.add_edge(Edge(head=root.id, tail=target.id))

    executor = ReactiveExecutor(session=_session(), graph=graph)
    cached = executor._get_predecessors(target)
    assert [node.id for node in cached] == [root.id]
    assert executor._get_predecessors(target) is cached

    class _TaskGroupStub:
        def start_soon(self, *args) -> None:
            pass

    request = SpawnRequest(instruction="follow-up", independent=False)
    spawned = Operation(operation="chat", parameters={})
    executor.node_builder = lambda req, emitter: spawned
    executor._tg = _TaskGroupStub()
    assert executor._inject_request(request, emitter=root)
    graph.add_edge(Edge(head=spawned.id, tail=target.id))

    refreshed = executor._get_predecessors(target)
    assert refreshed is not cached
    assert [node.id for node in refreshed] == [root.id, spawned.id]
