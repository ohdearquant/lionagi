# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Lifecycle telemetry corrections: one stable identity across queued/started/
terminal signals, and exactly one terminal on_progress emission after every
"started" across success, failure, cancellation, and abandonment.

Regression guards for the root cause traced in signal_root_cause.md: a node
built without ``reference_id`` was announced under the UUID prefix at queued
time and under the branch name at started time, and a cancelled or otherwise
abandoned operation never reached a terminal on_progress call at all — the
graph rendered it as running forever.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from lionagi.ln.concurrency import CapacityLimiter
from lionagi.operations.flow import DependencyAwareExecutor
from lionagi.operations.node import Operation
from lionagi.protocols.graph.graph import Graph
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


class _ProgressLog:
    """Captures on_progress(op_id, name, status, elapsed) calls in order."""

    def __init__(self):
        self.calls: list[tuple[str, str, str, float]] = []

    def __call__(self, op_id: str, name: str, status: str, elapsed: float) -> None:
        self.calls.append((op_id, name, status, elapsed))

    def statuses_for(self, op_id: str) -> list[str]:
        return [c[2] for c in self.calls if c[0] == op_id]

    def names_for(self, op_id: str) -> list[str]:
        return [c[1] for c in self.calls if c[0] == op_id]


# ---------------------------------------------------------------------------
# queued/started identity agreement (reference_id fix)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_queued_and_started_share_the_same_name_when_reference_id_set():
    """A node built with a reference_id (the CLI-flow fix: ``node_id=agent_ids[i]``
    threaded through ``_build_worker_operate_node`` -> ``add_operation``) must be
    announced under the SAME name at queued and started time on the actual
    Studio-facing signal bus (``Engine.run_dag`` -> ``flow_progress_signals``).

    The identity agreement happens in ``flow_signals._on_progress``, which
    prefers the authored ``reference_id`` snapshot over whatever name the raw
    executor callback passed — this is what makes queued and started resolve
    to one name regardless of the executor's own (still divergent) internal
    fallback chain, exercised directly by the next test below."""
    from lionagi.engines import Engine
    from lionagi.operations.builder import OperationGraphBuilder
    from lionagi.session.signal import NodeQueued, NodeStarted

    async def work(**kw):
        return "ok"

    session = _session_with_ops(work=work)

    signal_log: list[tuple[str, str, str]] = []
    session.observe(NodeQueued, handler=lambda s, _: signal_log.append(("queued", s.op_id, s.name)))
    session.observe(
        NodeStarted, handler=lambda s, _: signal_log.append(("started", s.op_id, s.name))
    )

    builder = OperationGraphBuilder()
    builder.add_operation("work", node_id="analyst")
    graph = builder.get_graph()

    run = Engine().new_run(session=session)
    result = await run.run_dag(graph)
    assert len(result["completed_operations"]) == 1

    op_id = str(result["completed_operations"][0])
    names = {kind: name for kind, oid, name in signal_log if oid == op_id}
    assert names == {"queued": "analyst", "started": "analyst"}, (
        f"queued/started must share one identity, got {signal_log}"
    )


@pytest.mark.asyncio
async def test_reactive_spawn_shares_one_name_across_queued_started_terminal():
    """The static case above threads node_id through the CLI builder; a
    REACTIVE spawn takes a different path — role_node_builder stamps
    spawn_id/reference_id on the child node (orchestration/patterns.py), but
    flow_signals._on_spawned's NodeSpawned handler used to overwrite that
    child's node_edge_meta entry with only parent_id/depends_on, dropping the
    name. queued (which reads reference_id straight off the node) and
    started (which falls back to the cloned branch's own name) then resolved
    to two different names for the same op_id — buildNodeStatusesByName
    (operationGraph.ts) split the same reactive child into a phantom
    queued-forever node plus a separately-named started node."""
    import json

    from lionagi.operations.node import create_operation
    from lionagi.orchestration.patterns import grant_spawn, role_node_builder
    from lionagi.protocols.graph.graph import Graph
    from lionagi.session.signal import NodeCompleted, NodeFailed, NodeQueued, NodeStarted
    from lionagi.testing import TestBranch

    def _capability_chunk(**spawn_fields) -> dict:
        payload = json.dumps({"spawn_request": spawn_fields})
        return {"type": "stream", "chunks": [{"type": "text", "content": payload}]}

    spawner = TestBranch.from_responses(
        [_capability_chunk(instruction="do the follow-up", assignee="follower", independent=True)],
        name="spawner",
    )
    follower = TestBranch.from_text("follow-up complete", name="follower")

    session = _session_with_ops()
    session.include_branches(spawner)
    session.include_branches(follower)
    session.default_branch = spawner
    grant_spawn(spawner, prompt=False)

    signal_log: list[tuple[str, str, str]] = []
    for sig_cls, kind in (
        (NodeQueued, "queued"),
        (NodeStarted, "started"),
        (NodeCompleted, "completed"),
        (NodeFailed, "failed"),
    ):
        session.observe(
            sig_cls,
            handler=lambda s, _, kind=kind: signal_log.append((kind, s.op_id, s.name)),
        )

    graph = Graph()
    root = create_operation("operate", parameters={"instruction": "start"})
    root.branch_id = spawner.id
    graph.add_node(root)

    from lionagi.engines import Engine

    run = Engine().new_run(session=session)
    result = await run.run_dag(
        graph,
        reactive=True,
        node_builder=role_node_builder({"follower": follower}),
    )

    assert result["spawned_operations"] == 1
    spawned_op_id = next(oid for oid in result["completed_operations"] if str(oid) != str(root.id))
    names = {kind: name for kind, oid, name in signal_log if oid == str(spawned_op_id)}
    assert "started" in names, f"no started signal recorded for spawned child, got {signal_log}"
    terminal_name = names.get("completed") or names.get("failed")
    assert names["queued"] == names["started"] == terminal_name == "spawn-1", (
        f"reactive spawn must keep ONE name across queued/started/terminal, got {signal_log}"
    )


@pytest.mark.asyncio
async def test_executor_raw_callback_diverges_without_reference_id_pre_fix_symptom():
    """Pins the pre-fix symptom at its source, one layer below the signal bus:
    the raw ``DependencyAwareExecutor.on_progress`` callback itself falls back
    to the UUID prefix at queued time and the branch name at started time when
    no ``reference_id`` is set on the node. This is exactly why an unfixed CLI
    call site (no ``node_id=``) produced two different names — the bus-level
    override in ``flow_signals`` only works because the CLI fix populates
    ``reference_id`` in the first place."""

    async def work(**kw):
        return "ok"

    session = _session_with_ops(work=work)
    graph = Graph()
    op = Operation(operation="work", parameters={})
    graph.add_node(op)

    log = _ProgressLog()
    executor = DependencyAwareExecutor(session=session, graph=graph, max_concurrent=10)
    executor.on_progress = log
    await executor.execute()

    op_id = str(op.id)
    names = log.names_for(op_id)
    assert names[0] == op_id[:8], "queued falls back to the UUID prefix without reference_id"
    assert names[1] == "root", "started falls back to branch.name without reference_id"
    assert names[0] != names[1]


# ---------------------------------------------------------------------------
# exactly one terminal signal after every start
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_success_path_emits_exactly_one_terminal_signal():
    async def work(**kw):
        return "ok"

    session = _session_with_ops(work=work)
    graph = Graph()
    op = Operation(operation="work", parameters={})
    graph.add_node(op)

    log = _ProgressLog()
    executor = DependencyAwareExecutor(session=session, graph=graph, max_concurrent=10)
    executor.on_progress = log
    await executor.execute()

    op_id = str(op.id)
    terminal = [s for s in log.statuses_for(op_id) if s in ("completed", "failed")]
    assert terminal == ["completed"]


@pytest.mark.asyncio
async def test_failure_path_emits_exactly_one_terminal_signal():
    async def work(**kw):
        raise RuntimeError("operation-level failure")

    session = _session_with_ops(work=work)
    graph = Graph()
    op = Operation(operation="work", parameters={})
    graph.add_node(op)

    log = _ProgressLog()
    executor = DependencyAwareExecutor(session=session, graph=graph, max_concurrent=10)
    executor.on_progress = log
    await executor.execute()

    op_id = str(op.id)
    terminal = [s for s in log.statuses_for(op_id) if s in ("completed", "failed")]
    assert terminal == ["failed"]


@pytest.mark.asyncio
async def test_cancelled_after_start_emits_exactly_one_terminal_signal():
    """CancelledError during invoke() must still close the started identity
    with a terminal "failed" (there is no separate cancelled signal kind) —
    the pre-fix behaviour swallowed the terminal signal in this path
    entirely, leaving the node rendered as running forever."""

    async def work(**kw):
        return "unused"

    session = _session_with_ops(work=work)
    graph = Graph()
    op = Operation(operation="work", parameters={})
    graph.add_node(op)

    log = _ProgressLog()
    executor = DependencyAwareExecutor(session=session, graph=graph, max_concurrent=10)
    executor.on_progress = log
    executor.operation_branches[op.id] = session.default_branch

    object.__setattr__(op, "invoke", AsyncMock(side_effect=asyncio.CancelledError()))
    limiter = CapacityLimiter(10)

    with pytest.raises(asyncio.CancelledError):
        await executor._execute_operation(op, limiter)

    op_id = str(op.id)
    assert log.statuses_for(op_id) == ["started", "failed"]
    assert executor.completion_events[op.id].is_set()


@pytest.mark.asyncio
async def test_abandonment_unexpected_flow_error_after_start_emits_one_terminal():
    """An unexpected flow-level error after "started" (not an operation-level
    failure — those are caught inside Event.invoke() and become the normal
    FAILED path) must still close the started identity with exactly one
    terminal "failed" signal, and must not propagate out of
    _execute_operation (matching the existing defensive-net contract)."""

    async def work(**kw):
        return "unused"

    session = _session_with_ops(work=work)
    graph = Graph()
    op = Operation(operation="work", parameters={})
    graph.add_node(op)

    log = _ProgressLog()
    executor = DependencyAwareExecutor(session=session, graph=graph, max_concurrent=10)
    executor.on_progress = log
    executor.operation_branches[op.id] = session.default_branch

    object.__setattr__(
        op, "invoke", AsyncMock(side_effect=RuntimeError("unexpected flow-level bug"))
    )
    limiter = CapacityLimiter(10)

    await executor._execute_operation(op, limiter)  # must not raise

    op_id = str(op.id)
    assert log.statuses_for(op_id) == ["started", "failed"]
    assert executor.completion_events[op.id].is_set()
    assert op.id in executor.results


@pytest.mark.asyncio
async def test_never_started_op_gets_no_terminal_from_safety_net():
    """The abandonment safety net must be a no-op for an operation that never
    reached "started" — e.g. a sibling still queued when a group cancels —
    so it never fabricates a terminal signal for work that never began."""

    async def work(**kw):
        return "ok"

    session = _session_with_ops(work=work)
    graph = Graph()
    op = Operation(operation="work", parameters={})
    graph.add_node(op)

    log = _ProgressLog()
    executor = DependencyAwareExecutor(session=session, graph=graph, max_concurrent=10)
    executor.on_progress = log

    assert op.id not in executor._started_ops
    executor._emit_abandoned_terminal(op)

    assert log.calls == []


@pytest.mark.asyncio
async def test_emit_terminal_once_is_idempotent_across_call_sites():
    """Two independent exit paths racing to close out the same operation
    (e.g. the normal FAILED branch and a safety net) must produce exactly
    one terminal on_progress call — the first one wins."""

    async def work(**kw):
        return "ok"

    session = _session_with_ops(work=work)
    graph = Graph()
    op = Operation(operation="work", parameters={})
    graph.add_node(op)

    log = _ProgressLog()
    executor = DependencyAwareExecutor(session=session, graph=graph, max_concurrent=10)
    executor.on_progress = log

    executor._emit_terminal_once(op, "analyst", "completed", 1.0)
    executor._emit_terminal_once(op, "analyst", "failed", 2.0)

    op_id = str(op.id)
    assert log.statuses_for(op_id) == ["completed"]
