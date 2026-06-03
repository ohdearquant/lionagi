# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Engine base machinery — stateless config + per-run EngineRun. No LLM."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from lionagi.engines import Engine, EngineEvent


class Finding(EngineEvent):
    claim: str
    novelty: float = 0.5


def _run():
    return Engine().new_run()


@pytest.mark.asyncio
async def test_emit_records_and_queries():
    run = _run()
    await run.emit(Finding(claim="x", novelty=0.9))
    await run.emit(Finding(claim="y", novelty=0.2))
    assert len(run.by_type(Finding)) == 2
    # the emission store is queryable via pile[type] (Phase A)
    assert len(run.events[Finding]) == 2


@pytest.mark.asyncio
async def test_observe_reacts_to_type():
    run = _run()
    seen: list[str] = []

    @run.observe(Finding)
    def _on(f, _ctx):
        seen.append(f.claim)

    await run.emit(Finding(claim="hit"))
    assert seen == ["hit"]


@pytest.mark.asyncio
async def test_observe_with_field_filter():
    from lionagi.ln.types import Spec

    run = _run()
    high: list[Finding] = []

    @run.observe(Spec(float, name="novelty").q > 0.7)
    def _on(f, _ctx):
        high.append(f)

    await run.emit(Finding(claim="lo", novelty=0.1))
    await run.emit(Finding(claim="hi", novelty=0.9))
    assert [f.claim for f in high] == ["hi"]


@pytest.mark.asyncio
async def test_spawn_and_quiescence():
    run = _run()
    done: list[int] = []

    async def work(n: int) -> None:
        await asyncio.sleep(0.01)
        done.append(n)

    run.spawn(work(1))
    run.spawn(work(2))
    await run.wait_quiescence()
    assert sorted(done) == [1, 2]


@pytest.mark.asyncio
async def test_observer_spawns_depth_node():
    """The canonical engine loop: an emission triggers a spawned task."""
    run = Engine(max_depth=2).new_run()
    expanded: list[str] = []

    async def deeper(claim: str) -> None:
        await asyncio.sleep(0)
        expanded.append(claim)

    @run.observe(Finding)
    def _on(f, _ctx):
        if f.novelty > 0.7:
            run.spawn(deeper(f.claim))

    await run.emit(Finding(claim="deep", novelty=0.9))
    await run.emit(Finding(claim="shallow", novelty=0.3))
    await run.wait_quiescence()
    assert expanded == ["deep"]


@pytest.mark.asyncio
async def test_seen_dedup():
    run = _run()
    assert run.seen("Quantum Error Correction") is False  # first time → marked
    assert run.seen("quantum error correction") is True  # normalized dup


@pytest.mark.asyncio
async def test_two_runs_are_isolated():
    """A stateless engine: two runs do not share dedup/session state."""
    eng = Engine()
    a, b = eng.new_run(), eng.new_run()
    assert a.seen("topic") is False
    # b has its own _seen — the same key is still fresh
    assert b.seen("topic") is False
    assert a.session is not b.session


@pytest.mark.asyncio
async def test_run_team_sequences_and_carries_output():
    run = _run()
    calls: list[tuple[str, str]] = []

    def fake(name: str, reply: str):
        async def operate(*, instruction: str):
            calls.append((name, instruction))
            return reply

        return SimpleNamespace(name=name, operate=operate)

    team = [fake("a", "AOUT"), fake("b", "BOUT")]
    last = await run.run_team(team, "do the task")
    assert last == "BOUT"
    assert calls[0] == ("a", "do the task")
    assert "AOUT" in calls[1][1]  # b builds on a's output


@pytest.mark.asyncio
async def test_run_team_survives_agent_failure():
    run = _run()

    def boom(name: str):
        async def operate(*, instruction: str):
            raise RuntimeError("kaboom")

        return SimpleNamespace(name=name, operate=operate)

    def ok(name: str):
        async def operate(*, instruction: str):
            return "recovered"

        return SimpleNamespace(name=name, operate=operate)

    last = await run.run_team([boom("x"), ok("y")], "go")
    assert last == "recovered"  # team continued past the failure


@pytest.mark.asyncio
async def test_make_agent_builds_casts_branch_with_emissions():
    run = _run()
    b = await run.make_agent("researcher", name="r1", emits=(Finding,))
    assert b.name == "r1"
    assert b in run.session.branches
    assert b.capabilities is not None  # emissions granted
    assert b.system is not None  # casts role body composed


@pytest.mark.asyncio
async def test_run_dag_emits_node_lifecycle_signals():
    """run_dag executes a prebuilt DAG and tees NodeStarted/NodeCompleted onto
    the bus — the seam persistence/Studio observe instead of an on_progress
    callback. Exercised with a registered coroutine op (no LLM)."""
    from lionagi.operations.builder import OperationGraphBuilder
    from lionagi.session.branch import Branch
    from lionagi.session.session import Session
    from lionagi.session.signal import NodeCompleted, NodeStarted

    async def work(**kw):
        return "ok"

    session = Session()
    branch = Branch(name="root")
    session.include_branches(branch)
    session.default_branch = branch
    session.register_operation("work", work)

    started: list[str] = []
    completed: list[str] = []
    session.observe(NodeStarted, handler=lambda s, _c: started.append(s.name))
    session.observe(NodeCompleted, handler=lambda s, _c: completed.append(s.op_id))

    builder = OperationGraphBuilder()
    builder.add_operation("work")
    graph = builder.get_graph()

    run = Engine().new_run(session=session)
    result = await run.run_dag(graph)

    assert len(result["completed_operations"]) == 1
    assert started == ["root"]  # NodeStarted reached the observer with the branch name
    assert len(completed) == 1  # NodeCompleted carried the op id


@pytest.mark.asyncio
async def test_run_dag_emits_node_queued_before_started():
    """run_dag emits NodeQueued before NodeStarted for each op (MAJ-3 live emission)."""
    from lionagi.operations.builder import OperationGraphBuilder
    from lionagi.session.branch import Branch
    from lionagi.session.session import Session
    from lionagi.session.signal import NodeQueued, NodeStarted

    async def work(**kw):
        return "ok"

    session = Session()
    branch = Branch(name="root")
    session.include_branches(branch)
    session.default_branch = branch
    session.register_operation("work", work)

    signal_log: list[str] = []  # record "queued:<op_id>" and "started:<op_id>"
    session.observe(NodeQueued, handler=lambda s, _: signal_log.append(f"queued:{s.op_id}"))
    session.observe(NodeStarted, handler=lambda s, _: signal_log.append(f"started:{s.op_id}"))

    builder = OperationGraphBuilder()
    builder.add_operation("work")
    graph = builder.get_graph()

    run = Engine().new_run(session=session)
    result = await run.run_dag(graph)

    assert len(result["completed_operations"]) == 1
    # Must have at least one queued and one started for the same op.
    queued_ops = [e.split(":")[1] for e in signal_log if e.startswith("queued:")]
    started_ops = [e.split(":")[1] for e in signal_log if e.startswith("started:")]
    assert len(queued_ops) >= 1
    assert len(started_ops) >= 1
    assert queued_ops[0] == started_ops[0], "NodeQueued and NodeStarted must share same op_id"
    # queued must arrive before started in the log.
    qi = next(i for i, e in enumerate(signal_log) if e.startswith("queued:"))
    si = next(i for i, e in enumerate(signal_log) if e.startswith("started:"))
    assert qi < si, "NodeQueued must precede NodeStarted in the signal log"


@pytest.mark.asyncio
async def test_run_dag_queued_for_reactive_injected_child():
    """Reactively injected child nodes also receive NodeQueued before NodeStarted."""
    from lionagi.casts.emission import SpawnRequest
    from lionagi.operations.builder import OperationGraphBuilder
    from lionagi.operations.node import create_operation
    from lionagi.session.branch import Branch
    from lionagi.session.session import Session
    from lionagi.session.signal import NodeQueued, NodeStarted

    async def spawner(**kw):
        return SpawnRequest(instruction="follow-up", independent=True)

    async def follow_up(**kw):
        return "child done"

    session = Session()
    branch = Branch(name="root")
    session.include_branches(branch)
    session.default_branch = branch
    session.register_operation("spawner", spawner)
    session.register_operation("follow_up", follow_up)

    queued_ids: list[str] = []
    started_ids: list[str] = []
    session.observe(NodeQueued, handler=lambda s, _: queued_ids.append(s.op_id))
    session.observe(NodeStarted, handler=lambda s, _: started_ids.append(s.op_id))

    def node_builder(req, emitter):
        return create_operation("follow_up", parameters={})

    builder = OperationGraphBuilder()
    builder.add_operation("spawner")
    graph = builder.get_graph()

    run = Engine().new_run(session=session)
    result = await run.run_dag(graph, reactive=True, node_builder=node_builder, max_spawn=1)

    assert result["spawned_operations"] == 1
    assert len(result["completed_operations"]) == 2
    # Both parent and child must have queued signals.
    assert len(queued_ids) == 2
    # Every started op must have been queued first.
    for op_id in started_ids:
        assert op_id in queued_ids, f"op {op_id} started without NodeQueued"
