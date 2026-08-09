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
from lionagi.protocols.types import EventStatus
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
async def test_preterminal_failed_op_is_not_completed_and_blocks_dependents_in_both_executors():
    outcomes = {}

    for reactive in (False, True):
        executed = []

        async def should_not_run(**_kwargs):
            executed.append("ran")
            return "unexpected"

        session = _session_with_ops(failed_source=should_not_run, dependent=should_not_run)
        builder = OperationGraphBuilder()
        failed_id = builder.add_operation("failed_source", depends_on=[])
        dependent_id = builder.add_operation("dependent", depends_on=[failed_id])
        graph = builder.get_graph()
        failed_node = graph.internal_nodes[failed_id]
        failed_node.execution.status = EventStatus.FAILED
        failed_node.execution.response = {"error": "boom"}

        result = await flow(session, graph, reactive=reactive)
        outcomes[reactive] = {
            "failed_is_completed": failed_id in result["completed_operations"],
            "failed_is_failed": failed_id in result["failed_operations"],
            "dependent_is_completed": dependent_id in result["completed_operations"],
            "dependent_is_skipped": dependent_id in result["skipped_operations"],
            "executed": executed,
            "failed_result": result["operation_results"].get(failed_id),
        }

    expected = {
        "failed_is_completed": False,
        "failed_is_failed": True,
        "dependent_is_completed": False,
        "dependent_is_skipped": True,
        "executed": [],
        "failed_result": {"error": "boom"},
    }
    assert outcomes == {False: expected, True: expected}


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


@pytest.mark.asyncio
async def test_non_mapping_response_context_is_rolled_up_as_failed():
    """An operation that completes but returns a non-Mapping response
    ``context`` is flipped to EventStatus.FAILED by the post-invoke
    validation guard -- that guard must land the op in failed_operations
    the same as an op whose invoke() itself raised, not bypass the rollup
    it exists to feed."""
    executed: list[str] = []

    async def first(**kw):
        executed.append("first")
        return "first ok"

    async def bad_context(**kw):
        executed.append("last")
        return {"context": "not-a-mapping"}

    session = _session_with_ops(first=first, last=bad_context)
    builder = OperationGraphBuilder()
    ids = _build_chain_graph(builder)
    graph = builder.get_graph()

    result = await flow(session, graph, parallel=False, verbose=False)

    assert "first" in executed
    assert "last" in executed
    assert ids["last"] in result["failed_operations"]
    assert ids["first"] not in result["failed_operations"]
    assert ids["last"] in result["completed_operations"]


@pytest.mark.parametrize("reactive", [False, True])
@pytest.mark.asyncio
async def test_failed_predecessor_payload_does_not_reach_a_running_dependent(reactive):
    """A dependent with two predecessors, one healthy and one carrying a
    restored terminal failure, still runs -- the healthy edge gives it a
    valid path. Context preparation iterates ALL predecessors, so without
    the omission for a pre-terminal failure the dead predecessor's error
    payload is handed to the dependent as though it were an input.

    Shape::

        good_source ---\\
                        +--> dependent
        failed_source --/    (restored FAILED, response={"error": ...})
    """
    seen: dict[str, object] = {}

    async def good_source(**kw):
        return "good"

    async def failed_source(**kw):  # must never run
        return "unexpected"

    async def dependent(**kw):
        seen["context"] = kw.get("context")
        return "dependent ok"

    session = _session_with_ops(
        good_source=good_source, failed_source=failed_source, dependent=dependent
    )
    builder = OperationGraphBuilder()
    good_id = builder.add_operation("good_source", depends_on=[])
    failed_id = builder.add_operation("failed_source", depends_on=[])
    dep_id = builder.add_operation("dependent", depends_on=[good_id, failed_id])
    graph = builder.get_graph()
    restored = graph.internal_nodes[failed_id]
    restored.execution.status = EventStatus.FAILED
    restored.execution.response = {"error": "boom"}

    result = await flow(session, graph, reactive=reactive, verbose=False)

    # Preconditions: without these the assertion below proves nothing.
    assert dep_id in result["completed_operations"]
    assert "context" in seen

    blob = repr(seen["context"])
    assert "boom" not in blob, f"failed predecessor payload leaked into dependent: {blob}"
