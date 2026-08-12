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


# queued/started identity agreement (reference_id fix)


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
async def test_reactive_spawn_shares_one_name_when_spawn_branch_setup_names_the_clone():
    """Same reactive-spawn setup as the test above, but the caller's
    ``spawn_branch_setup`` hook (the public callback ``_assign_injected_branch``
    invokes right after cloning) assigns the cloned branch a display name.
    ``started``/``completed`` used to resolve their on_progress ``name`` from
    ``getattr(branch, "name", None) or ref_id`` (flow.py), which then preferred
    the hook-assigned branch name over the ``reference_id`` the ``queued``
    signal already used -- splitting one spawned child across two identities
    (queued=spawn-1, started/completed=<branch name>). A branch-naming hook
    must not change which identity a reactive child's lifecycle signals
    correlate under."""
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

    def _name_the_clone(_op, clone) -> None:
        clone.name = "explicit-child-name"

    run = Engine().new_run(session=session)
    result = await run.run_dag(
        graph,
        reactive=True,
        node_builder=role_node_builder({"follower": follower}),
        spawn_branch_setup=_name_the_clone,
    )

    assert result["spawned_operations"] == 1
    spawned_op_id = next(oid for oid in result["completed_operations"] if str(oid) != str(root.id))
    names = {kind: name for kind, oid, name in signal_log if oid == str(spawned_op_id)}
    assert "started" in names, f"no started signal recorded for spawned child, got {signal_log}"
    terminal_name = names.get("completed") or names.get("failed")
    assert names["queued"] == names["started"] == terminal_name == "spawn-1", (
        "a branch-naming hook must not split a reactive child's lifecycle "
        f"identity, got {signal_log}"
    )


@pytest.mark.asyncio
async def test_branch_name_colliding_with_op_id_prefix_stays_pinned_through_later_rename():
    """A branch's real ``.name`` is an unrestricted str, assignable via the
    branch setup seam (spawn_branch_setup / on_branch_created) -- it can
    coincidentally equal the op_id's own 8-char prefix. flow_signals used to
    infer "this is the queued-time fallback placeholder" from string equality
    with ``op_id[:8]``, so a genuine branch name that happened to collide
    with the prefix was misclassified as a fallback and left unpinned; a
    later rename (e.g. the cancellation/abandoned-terminal safety net
    re-reading branch.name after started already fired) then split the
    operation across two names on the Studio-facing signal bus. Structural
    provenance (name_is_fallback, computed by the producer) replaces the
    string comparison."""
    from lionagi.engines.flow_signals import flow_progress_signals
    from lionagi.operations.flow import DependencyAwareExecutor
    from lionagi.session.branch import Branch
    from lionagi.session.session import Session
    from lionagi.session.signal import NodeCompleted, NodeFailed, NodeQueued, NodeStarted

    op = Operation(operation="work", parameters={})
    collision_name = str(op.id)[:8]  # no reference_id -> queued's own fallback too

    session = Session()
    branch = Branch(name=collision_name)
    session.include_branches(branch)
    session.default_branch = branch

    graph = Graph()
    graph.add_node(op)

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

    async with flow_progress_signals(session, graph) as on_progress:
        executor = DependencyAwareExecutor(session=session, graph=graph, max_concurrent=10)
        executor.on_progress = on_progress

        # queued: no reference_id -> falls back to op_id[:8], which happens
        # to equal the branch's genuine name (pure coincidence).
        name, is_fallback = executor._display_name(op)
        executor._emit_progress(str(op.id), name, "queued", 0.0, is_fallback)

        # started: resolves the branch's OWN name -- genuine, not a
        # fallback, even though it coincides with op_id[:8].
        executor._started_ops.add(op.id)
        name, is_fallback = executor._branch_display_name(op, branch)
        executor._emit_progress(str(op.id), name, "started", 0.0, is_fallback)
        assert name == collision_name
        assert is_fallback is False

        # A later rename (a workspace-retargeting hook, or the cancellation
        # safety net re-reading branch.name after started already fired)
        # must not split the correlation.
        branch.name = "renamed-later"
        executor._emit_abandoned_terminal(op)

    op_id = str(op.id)
    names = {kind: name for kind, oid, name in signal_log if oid == op_id}
    assert names["queued"] == names["started"] == collision_name
    terminal_name = names.get("completed") or names.get("failed")
    assert terminal_name == collision_name, (
        "a branch name colliding with op_id[:8] must stay pinned through a "
        f"later rename, got {signal_log}"
    )


@pytest.mark.asyncio
async def test_on_progress_seam_requires_provenance_instead_of_defaulting():
    """``flow_signals._on_progress`` used to default ``name_is_fallback=True``,
    so an untagged caller supplying an authored name at queued and the UUID
    prefix at started rendered a split identity: a direct probe fed
    ``on_progress(op_id, "authored-name", "queued", 0.0)`` then
    ``on_progress(op_id, op_id[:8], "started", 0.0)`` and the bus showed
    ``queued=authored-name``, ``started=109b2103`` -- two names for one op.

    The seam's only callers are the four lifecycle producers in
    ``operations/flow.py``, and all four already pass the bit explicitly, so
    there is no real "unknown provenance" case to default for. This test
    pins the seam shut: reproducing the exact untagged call shape from the
    probe above must now fail loudly (TypeError) rather than silently
    guessing fallback=True and risking the split-identity render.
    """
    from lionagi.engines.flow_signals import flow_progress_signals
    from lionagi.session.branch import Branch

    op = Operation(operation="work", parameters={})
    session = Session()
    branch = Branch(name="root")
    session.include_branches(branch)
    session.default_branch = branch

    graph = Graph()
    graph.add_node(op)

    async with flow_progress_signals(session, graph) as on_progress:
        op_id = str(op.id)
        with pytest.raises(TypeError):
            on_progress(op_id, "authored-name", "queued", 0.0)
        with pytest.raises(TypeError):
            on_progress(op_id, op_id[:8], "started", 0.0)


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


# exactly one terminal signal after every start


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
async def test_cancelled_after_start_emits_exactly_one_cancelled_signal():
    """CancelledError during invoke() closes the started identity as
    cancelled, rather than borrowing failed or leaving the node running."""

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
    assert log.statuses_for(op_id) == ["started", "cancelled"]
    assert executor.completion_events[op.id].is_set()


@pytest.mark.asyncio
async def test_operation_returning_cancelled_status_emits_cancelled_signal():
    """An operation can settle as CANCELLED without raising out of invoke().

    That normal-return path still owes the graph a terminal cancellation.
    """

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

    async def cancel_without_raising():
        op.execution.status = EventStatus.CANCELLED

    object.__setattr__(op, "invoke", AsyncMock(side_effect=cancel_without_raising))
    await executor._execute_operation(op, CapacityLimiter(10))

    assert log.statuses_for(str(op.id)) == ["started", "cancelled"]


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

    executor._emit_terminal_once(op, "analyst", "completed", 1.0, False)
    executor._emit_terminal_once(op, "analyst", "failed", 2.0, False)

    op_id = str(op.id)
    assert log.statuses_for(op_id) == ["completed"]


# skipped is its own terminal outcome, not a failure


def _skip_graph():
    """A two-node graph whose second node is gated off by a false edge condition."""
    from lionagi.operations.node import Operation
    from lionagi.protocols.graph.edge import Edge, EdgeCondition

    class AlwaysFalseCondition(EdgeCondition):
        async def apply(self, context: dict) -> bool:
            return False

    ran = Operation(operation="first", parameters={})
    gated = Operation(operation="never", parameters={})
    graph = Graph()
    graph.add_node(ran)
    graph.add_node(gated)
    graph.add_edge(Edge(head=ran.id, tail=gated.id, condition=AlwaysFalseCondition()))
    return graph, ran, gated


@pytest.mark.asyncio
async def test_gate_skipped_node_emits_skipped_not_failed_on_progress():
    """The raw executor callback must announce a gated-off node as "skipped".

    The rollup already told these apart (skipped_operations vs
    failed_operations), but the on_progress stream did not: the skip path had
    no "skipped" status to pass, so it passed "failed" and every consumer
    reading the callback saw a deliberate skip as an error.
    """
    from lionagi.operations.flow import flow

    async def first(**kw):
        return "first ok"

    async def never(**kw):  # pragma: no cover - the point is that it never runs
        raise AssertionError("gated node must not execute")

    session = _session_with_ops(first=first, never=never)
    graph, _ran, gated = _skip_graph()
    log = _ProgressLog()

    result = await flow(session, graph, parallel=False, verbose=False, on_progress=log)

    gated_id = str(gated.id)
    statuses = log.statuses_for(gated_id)
    assert "skipped" in statuses, f"gated node was never announced as skipped: {statuses}"
    assert "failed" not in statuses, (
        f"a node an edge condition passed over must not be announced as failed: {statuses}"
    )
    # The rollup and the callback must agree about the same node.
    assert gated.id in result["skipped_operations"]
    assert gated.id not in result["failed_operations"]


@pytest.mark.asyncio
async def test_gate_skipped_node_projects_to_the_skipped_lane_on_the_signal_bus():
    """End-to-end over the surface the execution graph actually renders.

    ``flow_progress_signals`` is the Studio-facing bridge, and ``lane_for`` is
    the projection the canvas reads. A skipped node has to arrive as
    NodeSkipped and project to the "skipped" lane -- asserting only on the
    flow result would pass just as happily while the canvas painted the node
    red, which is exactly how this defect survived.
    """
    from lionagi.engines.flow_signals import flow_progress_signals
    from lionagi.operations.flow import flow
    from lionagi.session.signal import NodeFailed, NodeSkipped, lane_for

    async def first(**kw):
        return "first ok"

    async def never(**kw):  # pragma: no cover - the point is that it never runs
        raise AssertionError("gated node must not execute")

    session = _session_with_ops(first=first, never=never)
    graph, _ran, gated = _skip_graph()

    seen: list[object] = []
    session.observe(NodeSkipped, handler=lambda s, _: seen.append(s))
    session.observe(NodeFailed, handler=lambda s, _: seen.append(s))

    async with flow_progress_signals(session, graph) as on_progress:
        await flow(session, graph, parallel=False, verbose=False, on_progress=on_progress)

    gated_id = str(gated.id)
    for_gated = [s for s in seen if getattr(s, "op_id", None) == gated_id]
    assert for_gated, "no terminal signal reached the bus for the gated node"
    assert all(isinstance(s, NodeSkipped) for s in for_gated), (
        f"gated node reached the signal bus as {[type(s).__name__ for s in for_gated]}"
    )
    assert lane_for(for_gated) == "skipped"


# an operation already terminal on arrival must still be answered


def _one_op_graph(status):
    """A single-node graph whose only operation is already terminal on arrival.

    This is the shape a resumed run replays: work an earlier attempt finished
    arrives carrying its outcome, so the executor short-circuits it.
    """
    op = Operation(operation="work", parameters={})
    op.execution.status = status
    graph = Graph()
    graph.add_node(op)
    return graph, op


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (EventStatus.COMPLETED, "completed"),
        (EventStatus.FAILED, "failed"),
        (EventStatus.SKIPPED, "skipped"),
    ],
)
@pytest.mark.asyncio
async def test_preterminal_op_is_announced_with_its_own_outcome(status, expected):
    """An operation that is already terminal when the flow reaches it gets a
    terminal announcement carrying the outcome it actually holds.

    Every node is announced "queued" up front, so short-circuiting without an
    announcement leaves the node announced and never answered — it reads as
    still-pending work forever. That is a different defect from announcing the
    wrong word, and it is not fixed by adding vocabulary, because the emitter
    on this path was never reached at all.
    """
    from lionagi.operations.flow import flow

    async def work(**kw):  # pragma: no cover - a terminal op must not re-execute
        raise AssertionError("an already-terminal operation must not run again")

    session = _session_with_ops(work=work)
    graph, op = _one_op_graph(status)
    log = _ProgressLog()

    await flow(session, graph, parallel=False, verbose=False, on_progress=log)

    statuses = log.statuses_for(str(op.id))
    assert statuses == ["queued", expected], (
        f"a pre-{expected} operation was announced {statuses}; it must be "
        f'answered with "{expected}" rather than left sitting at "queued"'
    )


@pytest.mark.asyncio
async def test_preterminal_cancelled_is_announced_with_its_own_outcome():
    """A resumed CANCELLED node answers its earlier queued announcement with
    its real terminal outcome, without being misreported as failed/skipped."""
    from lionagi.operations.flow import flow

    async def work(**kw):  # pragma: no cover - a terminal op must not re-execute
        raise AssertionError("an already-terminal operation must not run again")

    session = _session_with_ops(work=work)
    graph, op = _one_op_graph(EventStatus.CANCELLED)
    log = _ProgressLog()

    await flow(session, graph, parallel=False, verbose=False, on_progress=log)

    statuses = log.statuses_for(str(op.id))
    assert statuses == ["queued", "cancelled"], (
        f"cancelled must leave the queued lane under its own word, got {statuses}"
    )


@pytest.mark.asyncio
async def test_preterminal_cancelled_reaches_signal_bus_cancelled_lane():
    """The Flow-to-Studio bridge persists NodeCancelled and lane_for projects
    it as cancelled, so the execution graph cannot keep showing queued."""
    from lionagi.engines.flow_signals import flow_progress_signals
    from lionagi.operations.flow import flow
    from lionagi.session.signal import NodeCancelled, lane_for

    async def work(**kw):  # pragma: no cover - terminal work never reruns
        raise AssertionError("an already-terminal operation must not run again")

    session = _session_with_ops(work=work)
    graph, op = _one_op_graph(EventStatus.CANCELLED)
    seen: list[object] = []
    session.observe(NodeCancelled, handler=lambda signal, _: seen.append(signal))

    async with flow_progress_signals(session, graph) as on_progress:
        await flow(session, graph, parallel=False, verbose=False, on_progress=on_progress)

    for_op = [signal for signal in seen if getattr(signal, "op_id", None) == str(op.id)]
    assert len(for_op) == 1
    assert lane_for(for_op) == "cancelled"


@pytest.mark.asyncio
async def test_preterminal_completed_reaches_the_signal_bus_and_leaves_the_queued_lane():
    """End-to-end over the surface the execution graph renders.

    Asserting only on the raw callback would pass while the canvas still showed
    the node waiting, so this follows the same path a viewer sees: through
    ``flow_progress_signals`` to ``lane_for``.
    """
    from lionagi.engines.flow_signals import flow_progress_signals
    from lionagi.operations.flow import flow
    from lionagi.session.signal import NodeCompleted, lane_for

    async def work(**kw):  # pragma: no cover - a terminal op must not re-execute
        raise AssertionError("an already-terminal operation must not run again")

    session = _session_with_ops(work=work)
    graph, op = _one_op_graph(EventStatus.COMPLETED)

    seen: list[object] = []
    session.observe(NodeCompleted, handler=lambda s, _: seen.append(s))

    async with flow_progress_signals(session, graph) as on_progress:
        await flow(session, graph, parallel=False, verbose=False, on_progress=on_progress)

    op_id = str(op.id)
    for_op = [s for s in seen if getattr(s, "op_id", None) == op_id]
    assert for_op, "a pre-completed node reached no terminal signal on the bus"
    assert lane_for(for_op) == "succeeded", (
        f"pre-completed node projected to {lane_for(for_op)!r}, not the succeeded lane"
    )
