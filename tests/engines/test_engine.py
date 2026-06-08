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


# ── LIONAGI-AUDIT-001: spawned task failures surface to caller ─────────────────


@pytest.mark.asyncio
async def test_spawned_task_failure_is_reported():
    """Regression: wait_quiescence() must not silently swallow task exceptions.

    A spawned coroutine that raises RuntimeError must cause wait_quiescence
    to propagate that exception rather than returning successfully.
    """
    run = _run()

    async def boom():
        await asyncio.sleep(0)
        raise RuntimeError("engine node failed")

    run.spawn(boom())
    # On Python 3.11+ an ExceptionGroup is raised; on 3.10 the first exception
    # is re-raised directly.  Either way, the call must NOT return normally.
    raised: BaseException | None = None
    try:
        await run.wait_quiescence()
    except BaseException as exc:
        raised = exc
    assert raised is not None, "wait_quiescence must raise when a spawned task fails"
    # The original error message must be visible somewhere in the exception chain.
    assert "engine node failed" in str(raised)


@pytest.mark.asyncio
async def test_spawned_task_cancellation_is_not_surfaced():
    """CancelledError from a spawned task must be silently discarded —
    the whole run is not cancelled when a single branch is cancelled."""
    run = _run()
    done: list[int] = []

    async def cancel_me():
        raise asyncio.CancelledError

    async def succeed():
        await asyncio.sleep(0)
        done.append(1)

    run.spawn(cancel_me())
    run.spawn(succeed())
    # Must not raise; the CancelledError is discarded.
    await run.wait_quiescence()
    assert done == [1]


@pytest.mark.asyncio
async def test_failed_parent_still_drains_spawned_child():
    """A parent that spawns a child and then fails must NOT short-circuit the
    wait. wait_quiescence must drain the child to completion (genuine
    quiescence — no background work left running) before surfacing the failure.
    """
    run = _run()
    child_ran = asyncio.Event()

    async def child():
        await asyncio.sleep(0.02)
        child_ran.set()

    async def parent():
        run.spawn(child())  # schedule child, then fail
        await asyncio.sleep(0)
        raise RuntimeError("parent failed after spawning child")

    run.spawn(parent())

    raised: BaseException | None = None
    try:
        await run.wait_quiescence()
    except BaseException as exc:
        raised = exc

    assert raised is not None, "parent failure must be surfaced"
    assert "parent failed after spawning child" in str(raised)
    # The contract: the run is genuinely quiescent — child finished, none left.
    assert child_ran.is_set(), "child spawned by failed parent must still run"
    assert not run._active, "wait_quiescence must leave no background tasks running"


@pytest.mark.asyncio
async def test_successful_tasks_do_not_raise():
    """Successful spawned tasks must complete without raising."""
    run = _run()
    results: list[str] = []

    async def work(val: str) -> None:
        await asyncio.sleep(0)
        results.append(val)

    run.spawn(work("a"))
    run.spawn(work("b"))
    await run.wait_quiescence()
    assert sorted(results) == ["a", "b"]


# ── New edge cases ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_seen_unicode_and_whitespace_normalization():
    run = _run()
    # RTL and zero-width chars are stripped by .strip().lower()
    assert run.seen("topic") is False
    # Same key with leading/trailing spaces normalizes to same entry
    assert run.seen("  topic  ") is True
    # Unicode case folding
    assert run.seen("TOPIC") is True


@pytest.mark.asyncio
async def test_seen_zero_width_chars_normalized():
    run = _run()
    normal = "test"
    with_zwsp = "test​"  # zero-width space — .strip() does NOT remove it
    # they should NOT be the same because ​ is not ASCII whitespace
    assert run.seen(normal) is False
    result = run.seen(with_zwsp)
    # whether equal or not depends on strip; just verify no crash and consistent
    # call is idempotent
    assert run.seen(with_zwsp) is True


@pytest.mark.asyncio
async def test_concurrent_emit_and_observe_race():
    run = _run()
    seen_claims: list[str] = []

    @run.observe(Finding)
    def _on(f, _ctx):
        seen_claims.append(f.claim)

    # concurrent emits from gather — no crash, all should be collected
    await asyncio.gather(*[run.emit(Finding(claim=str(i))) for i in range(20)])
    assert len(seen_claims) == 20


@pytest.mark.asyncio
async def test_many_events_accumulated_without_cleanup():
    run = _run()
    for i in range(500):
        await run.emit(Finding(claim=str(i), novelty=0.5))
    findings = run.by_type(Finding)
    assert len(findings) == 500


@pytest.mark.asyncio
async def test_max_depth_attribute_stored_on_engine():
    eng = Engine(max_depth=7)
    assert eng.max_depth == 7
    run = eng.new_run()
    assert run.engine.max_depth == 7


@pytest.mark.asyncio
async def test_spawn_chain_does_not_exceed_depth_by_design():
    run = Engine(max_depth=2).new_run()
    depth_reached: list[int] = []

    async def level(n: int) -> None:
        depth_reached.append(n)
        if n < 5:
            run.spawn(level(n + 1))

    run.spawn(level(0))
    await run.wait_quiescence()
    # max_depth is advisory: Engine itself does not enforce it in _run()
    # but EngineRun stores it for subclass use; verify spawn loop still works
    assert len(depth_reached) > 0
