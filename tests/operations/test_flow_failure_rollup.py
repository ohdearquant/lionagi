# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""A DAG whose terminal node raises must be able to say so:
DependencyAwareExecutor/ReactiveExecutor caught the exception, set the
node's own status to FAILED, and recorded an error result for it -- but
never rolled that per-node failure up into a distinguishable place in the
returned dict, so a caller had no way to tell a dead node from a completed
one without inspecting every result value by hand."""

from __future__ import annotations

import pytest

from lionagi.operations import flow
from lionagi.operations.builder import OperationGraphBuilder
from lionagi.session.session import Session


def _session_with_ops(**ops):
    """A Session whose default branch resolves the given named operations."""
    from lionagi.session.branch import Branch

    session = Session()
    branch = Branch(name="root")
    session.include_branches(branch)
    session.default_branch = branch
    for name, fn in ops.items():
        session.register_operation(name, fn)
    return session


def _build_chain_graph(builder: OperationGraphBuilder) -> dict[str, str]:
    """first -> last, a minimal chain where the terminal node is the one
    that dies."""
    first_id = builder.add_operation("first", depends_on=[])
    last_id = builder.add_operation("last", depends_on=[first_id])
    return {"first": first_id, "last": last_id}


def _make_ops(executed: list[str]):
    async def first(**kw):
        executed.append("first")
        return "first ok"

    async def last(**kw):
        executed.append("last")
        raise RuntimeError("CLI subprocess exited with code 1 and wrote nothing to stderr")

    return {"first": first, "last": last}


@pytest.mark.asyncio
async def test_dead_terminal_node_is_rolled_up_as_failed():
    """The failing node must be visible via a dedicated failed_operations
    list; completed_operations keeps its existing (deliberate, tested)
    meaning of 'the executor produced a result for this op, whatever it
    was' -- see test_flow_operation_error_handling in test_flow.py."""
    executed: list[str] = []
    session = _session_with_ops(**_make_ops(executed))
    builder = OperationGraphBuilder()
    ids = _build_chain_graph(builder)
    graph = builder.get_graph()

    result = await flow(session, graph, parallel=False, verbose=False)

    assert "first" in executed
    assert "last" in executed
    assert ids["last"] in result["failed_operations"]
    assert ids["first"] not in result["failed_operations"]
    # Back-compat: completed_operations is unchanged (a FAILED op still
    # produced a (error) result, which is what that list has always meant).
    assert ids["last"] in result["completed_operations"]


@pytest.mark.asyncio
async def test_dead_terminal_node_is_rolled_up_as_failed_reactive():
    """Same rollup under the reactive (self-expanding) executor."""
    executed: list[str] = []
    session = _session_with_ops(**_make_ops(executed))
    builder = OperationGraphBuilder()
    ids = _build_chain_graph(builder)
    graph = builder.get_graph()

    result = await flow(session, graph, reactive=True, verbose=False)

    assert ids["last"] in result["failed_operations"]
    assert ids["first"] not in result["failed_operations"]
    assert ids["last"] in result["completed_operations"]


@pytest.mark.asyncio
async def test_skipped_operation_is_not_reported_as_failed():
    """A node skipped by an ordinary edge-condition short-circuit is a
    different outcome from a node that raised; failed_operations must stay
    orthogonal to skipped_operations."""
    from lionagi.protocols.graph.edge import Edge, EdgeCondition

    class AlwaysFalseCondition(EdgeCondition):
        async def apply(self, context: dict) -> bool:
            return False

    executed: list[str] = []

    async def first(**kw):
        executed.append("first")
        return "first ok"

    async def never(**kw):
        executed.append("never")
        return "should not run"

    session = _session_with_ops(first=first, never=never)
    builder = OperationGraphBuilder()
    first_id = builder.add_operation("first", depends_on=[])
    never_id = builder.add_operation("never", depends_on=[])
    graph = builder.get_graph()
    graph.add_edge(Edge(head=first_id, tail=never_id, condition=AlwaysFalseCondition()))

    result = await flow(session, graph, parallel=False, verbose=False)

    assert "never" not in executed
    assert never_id in result["skipped_operations"]
    assert never_id not in result["failed_operations"]
