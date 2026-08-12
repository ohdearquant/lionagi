# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for orchestration live-persist: start/stop_live_persist, lazy branch-row creation, and aiosqlite thread-leak guard."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from lionagi import Branch, Session
from lionagi.cli.orchestrate._orchestration import (
    OrchestrationEnv,
    start_live_persist,
    stop_live_persist,
)
from lionagi.cli.orchestrate._orchestration import (
    register_branch_hook as _register_branch_hook,
)
from lionagi.state.db import StateDB

# Fixtures


@pytest.fixture
def temp_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "state.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", db_path)
    return db_path


def _aiosqlite_thread_count() -> int:
    return sum(1 for t in threading.enumerate() if t.name.startswith("sqlite"))


def _minimal_env(orc_branch: Branch | None = None) -> OrchestrationEnv:
    """Stub OrchestrationEnv with only the fields live-persist touches (no provider setup required)."""
    if orc_branch is None:
        orc_branch = Branch(name="orchestrator")
    session = Session(default_branch=orc_branch)
    # We bypass setup_orchestration's full kwargs by directly constructing
    # OrchestrationEnv with only the fields live-persist reads.
    from unittest.mock import MagicMock

    return OrchestrationEnv(
        run=MagicMock(),
        session=session,
        orc_branch=orc_branch,
        builder=MagicMock(),
        orc_profile=None,
        orc_profile_name=None,
        default_model_spec="claude",
        bare=False,
        effort=None,
        theme=None,
        yolo=False,
        bypass=False,
        verbose=False,
        fast=False,
        cwd=None,
    )


# start_live_persist: happy path + invariants


async def test_start_creates_session_and_registers_hook_on_orc_branch(
    temp_db_path: Path,
    tmp_path: Path,
):
    """start_live_persist persists the session and registers a hook on every branch already in session.branches."""
    env = _minimal_env()
    artifacts = str(tmp_path / "artifacts")
    await start_live_persist(
        env,
        invocation_kind="flow",
        playbook_name="my-playbook",
        agent_name="orchestrator",
        artifacts_path=artifacts,
    )

    assert env._live_persist is not None
    ctx = env._live_persist
    assert ctx["db"] is not None
    assert ctx["session_id"] == str(env.session.id)
    assert ctx["session_prog_id"]
    # The orc branch already in session.branches got its hook.
    assert len(ctx["hooks"]) == 1
    assert ctx["hooks"][0][0] is env.orc_branch

    async with StateDB() as db:
        s = await db.get_session(ctx["session_id"])
    assert s is not None
    assert s["invocation_kind"] == "flow"
    assert s["playbook_name"] == "my-playbook"
    assert s["agent_name"] == "orchestrator"
    assert s["artifacts_path"] == artifacts
    assert s["status"] == "running"

    await stop_live_persist(env, status="completed")


async def test_start_live_persist_stashes_resolved_project_not_raw_arg(
    temp_db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Round-2 regression: env._project must carry the RESOLVED project (what
    the session row actually gets — explicit arg or detect_project()
    fallback), not the caller's raw argument. The common case is a run whose
    project came from detection rather than an explicit --project flag;
    before this fix, anything reading env._project (e.g. the
    escalation-mirror-link hook) would see the unresolved raw value —- None
    here — instead of what the session row actually recorded."""
    monkeypatch.setattr(
        "lionagi.cli._project.detect_project",
        lambda cwd=None: ("acme/widget", "git_remote"),
    )

    env = _minimal_env()
    await start_live_persist(env, invocation_kind="flow", project=None)
    try:
        ctx = env._live_persist
        assert ctx is not None
        async with StateDB() as db:
            s = await db.get_session(ctx["session_id"])
        assert s["project"] == "acme/widget"
        assert env._project == "acme/widget"
    finally:
        await stop_live_persist(env, status="completed")


async def test_start_live_persist_stashes_explicit_project_verbatim(
    temp_db_path: Path,
):
    """The explicit-argument path: env._project must match what was passed,
    same as the session row — the resolved and raw values coincide here, but
    the read must still go through the resolved value, not bypass it."""
    env = _minimal_env()
    await start_live_persist(env, invocation_kind="flow", project="explicit/proj")
    try:
        ctx = env._live_persist
        assert ctx is not None
        assert env._project == "explicit/proj"
    finally:
        await stop_live_persist(env, status="completed")


async def test_orchestration_manifest_tracks_start_and_terminal_status(
    temp_db_path: Path, tmp_path: Path
):
    from lionagi.cli._runs import RunDir

    env = _minimal_env()
    env.run = RunDir(
        run_id="orchestration-run",
        state_root=tmp_path / "state",
        artifact_root=tmp_path / "artifacts",
    )
    env.run.ensure_state_dirs()
    env.run.ensure_artifact_root()

    await start_live_persist(
        env,
        invocation_kind="flow",
        agent_name="orchestrator",
        model="codex/model",
        provider="codex",
    )

    started = env.run.read_manifest()
    assert started["branch_id"] == str(env.orc_branch.id)
    assert started["agent_name"] == "orchestrator"
    assert started["provider"] == "codex"
    assert started["status"] == "running"
    assert started["ended_at"] is None

    await stop_live_persist(env, status="completed")

    completed = env.run.read_manifest()
    assert completed["status"] == "completed"
    assert completed["ended_at"] >= completed["started_at"]


# start_live_persist: failure path closes the DB


async def test_start_create_session_failure_closes_db(
    temp_db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """If create_session fails, the DB is closed and env._live_persist is set to None (prevents interpreter-shutdown hang)."""

    async def fail(self, session: dict):
        await self.execute("SELECT 1")
        raise RuntimeError("simulated DB failure")

    monkeypatch.setattr(StateDB, "create_session", fail)

    env = _minimal_env()
    before = _aiosqlite_thread_count()

    # Must NOT raise — start swallows the failure and logs it.
    await start_live_persist(env)

    assert env._live_persist is None
    # No persistence is wired after the DB failure. Persistence rides the hook
    # bus (ADR-0047) and its emit hook (_persist_via_bus) is registered only by
    # route_message_persistence — never reached here — so on_message_added holds
    # just the branch's baseline signal-emission hook (_schedule_emit).
    assert env.orc_branch.on_message_added == [env.orc_branch._schedule_emit]

    for _ in range(20):
        if _aiosqlite_thread_count() <= before:
            break
        await asyncio.sleep(0.05)
    assert _aiosqlite_thread_count() <= before, (
        "DB was not closed on start failure — aiosqlite worker leaked"
    )


# _register_branch_hook: lazy branch row + multi-message paths


async def test_register_branch_hook_creates_row_on_first_message(
    temp_db_path: Path,
):
    """Branch row + progression are created lazily on the FIRST message, not eagerly when the hook is registered."""
    from lionagi.protocols.messages.manager import MessageManager

    env = _minimal_env()
    await start_live_persist(env)

    # Add a worker branch AFTER start_live_persist (mirrors
    # build_worker_branch). Hook must be registered but branch row
    # NOT yet created.
    worker = Branch(name="worker-1")
    env.session.include_branches(worker)
    _register_branch_hook(env._live_persist, worker)

    async with StateDB() as db:
        b_before = await db.get_branch(str(worker.id))
    assert b_before is None, "branch row should NOT exist before first message"

    # Fire one message via the registered hook.
    msg = MessageManager.create_instruction(
        instruction="hi",
        sender="u",
        recipient=str(worker.id),
    )
    hook = env._live_persist["hooks"][-1][1]
    await hook(msg)

    async with StateDB() as db:
        b_after = await db.get_branch(str(worker.id))
        prog = await db.get_progression(env._live_persist["branch_prog_ids"][str(worker.id)])
        session_prog = await db.get_progression(env._live_persist["session_prog_id"])
    assert b_after is not None
    assert b_after["session_id"] == str(env.session.id)
    assert str(msg.id) in prog
    # The session-level progression also got the message.
    assert str(msg.id) in session_prog

    await stop_live_persist(env, status="completed")


async def test_reactive_spawn_persists_spawned_branch(
    temp_db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A spawned branch is wired before its first message reaches persistence."""
    from lionagi.casts.emission import SpawnRequest, TaskAssignment
    from lionagi.cli.orchestrate.flow import _DagState, _execute_dag, _PlanResult
    from lionagi.operations.builder import OperationGraphBuilder

    env = _minimal_env()
    worker = Branch(name="worker")
    env.session.include_branches(worker)
    env.builder = OperationGraphBuilder()
    node_id = env.builder.add_operation(
        "operate",
        node_id="initial",
        branch=worker,
        instruction="spawn follow-up work",
    )
    await start_live_persist(env, invocation_kind="flow")
    ctx = env._live_persist
    assert ctx is not None

    spawned_branch_ids: list[str] = []

    async def operate(self, instruction=None, **kwargs):
        if self.id == worker.id:
            return SpawnRequest(
                instruction="persist the spawned result",
                assignee="worker",
                independent=True,
            )
        spawned_branch_ids.append(str(self.id))
        await self.msgs.a_add_message(assistant_response="durable spawned result")
        return "durable spawned result"

    monkeypatch.setattr(Branch, "operate", operate)
    plan_result = _PlanResult(
        assignments=[TaskAssignment(task="spawn follow-up work", assignee="worker")],
        agent_ids=["worker"],
        dep_indices=[[]],
        pool=[],
        budget_preambles={},
    )
    dag_state = _DagState(
        node_ids=[node_id],
        known_nodes={node_id},
        deps_by_node={node_id: []},
        reactive=True,
        spawn_roles=None,
        role_base={"worker": worker},
        worker_models=["test/model"],
    )

    try:
        exec_result = await _execute_dag(
            env,
            plan_result,
            dag_state,
            max_concurrent=1,
            max_ops=0,
        )

        assert exec_result.n_spawned == 1
        assert len(spawned_branch_ids) == 1
        spawned_branch_id = spawned_branch_ids[0]
        assert spawned_branch_id in ctx["branch_prog_ids"]
        spawned_row = await ctx["db"].get_branch(spawned_branch_id)
        assert spawned_row is not None
        assert spawned_row["session_id"] == ctx["session_id"]
        progression = await ctx["db"].get_progression(ctx["branch_prog_ids"][spawned_branch_id])
        assert len(progression) == 1
    finally:
        await stop_live_persist(env, status="completed")


async def test_register_branch_hook_ensure_branch_row_idempotent(
    temp_db_path: Path,
):
    """Multiple messages on the same branch must NOT re-create the row or re-insert the system message (initialized flag gate)."""
    from lionagi.protocols.messages.manager import MessageManager

    env = _minimal_env()
    await start_live_persist(env)

    worker = Branch(name="worker-1", system="you are a worker")
    env.session.include_branches(worker)
    _register_branch_hook(env._live_persist, worker)
    hook = env._live_persist["hooks"][-1][1]

    msg1 = MessageManager.create_instruction(
        instruction="a",
        sender="u",
        recipient=str(worker.id),
    )
    msg2 = MessageManager.create_instruction(
        instruction="b",
        sender="u",
        recipient=str(worker.id),
    )
    await hook(msg1)
    await hook(msg2)

    async with StateDB() as db:
        # Branch row exists once.
        row = await db.fetch_one(
            "SELECT COUNT(*) AS n FROM branches WHERE id = ?", (str(worker.id),)
        )
        n_rows = row["n"]
        # System message exists once.
        row = await db.fetch_one(
            "SELECT COUNT(*) AS n FROM messages WHERE id = ?",
            (str(worker.system.id),),
        )
        n_sys = row["n"]
        # Branch progression has both user messages.
        prog = await db.get_progression(env._live_persist["branch_prog_ids"][str(worker.id)])

    assert n_rows == 1
    assert n_sys == 1
    assert str(msg1.id) in prog
    assert str(msg2.id) in prog

    await stop_live_persist(env, status="completed")


async def test_multiple_branches_share_session_progression(
    temp_db_path: Path,
):
    """Each worker has its own branch_prog, but ALL messages land in the shared session_prog (Studio ordered timeline)."""
    from lionagi.protocols.messages.manager import MessageManager

    env = _minimal_env()
    await start_live_persist(env)

    w1 = Branch(name="worker-1")
    w2 = Branch(name="worker-2")
    env.session.include_branches(w1)
    env.session.include_branches(w2)
    _register_branch_hook(env._live_persist, w1)
    _register_branch_hook(env._live_persist, w2)

    # Find each branch's hook
    hooks = {str(br.id): hk for br, hk in env._live_persist["hooks"]}

    m1 = MessageManager.create_instruction(
        instruction="from-w1",
        sender="u",
        recipient=str(w1.id),
    )
    m2 = MessageManager.create_instruction(
        instruction="from-w2",
        sender="u",
        recipient=str(w2.id),
    )

    await hooks[str(w1.id)](m1)
    await hooks[str(w2.id)](m2)

    async with StateDB() as db:
        session_prog = await db.get_progression(env._live_persist["session_prog_id"])
        w1_prog = await db.get_progression(env._live_persist["branch_prog_ids"][str(w1.id)])
        w2_prog = await db.get_progression(env._live_persist["branch_prog_ids"][str(w2.id)])

    assert set(session_prog) == {str(m1.id), str(m2.id)}
    assert w1_prog == [str(m1.id)]
    assert w2_prog == [str(m2.id)]

    await stop_live_persist(env, status="completed")


async def test_hook_swallows_db_write_failure(
    temp_db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    """A failed DB write inside the hook must NOT abort the orchestration — logs at WARNING, message still flows."""
    import logging

    from lionagi.protocols.messages.manager import MessageManager

    env = _minimal_env()
    await start_live_persist(env)

    worker = Branch(name="worker-1")
    env.session.include_branches(worker)
    _register_branch_hook(env._live_persist, worker)
    hook = env._live_persist["hooks"][-1][1]

    async def boom(self, msg, **kwargs):
        raise RuntimeError("simulated busy timeout")

    monkeypatch.setattr(StateDB, "_persist_live_message", boom)

    msg = MessageManager.create_instruction(
        instruction="hi",
        sender="u",
        recipient=str(worker.id),
    )
    with caplog.at_level(logging.WARNING, logger="lionagi.cli"):
        await hook(msg)  # MUST NOT raise

    assert any("live persist write failed" in rec.message for rec in caplog.records)

    await stop_live_persist(env, status="completed")


async def test_hook_updates_system_msg_id_when_system_replaced(
    temp_db_path: Path,
):
    """If a worker's system message is replaced mid-run, the hook updates branches.system_msg_id to the new system."""
    from lionagi.protocols.messages import System
    from lionagi.protocols.messages.manager import MessageManager

    env = _minimal_env()
    await start_live_persist(env)

    worker = Branch(name="worker-1", system="initial")
    env.session.include_branches(worker)
    _register_branch_hook(env._live_persist, worker)
    hook = env._live_persist["hooks"][-1][1]

    # First message ensures branch row exists with original system_msg_id.
    init_msg = MessageManager.create_instruction(
        instruction="warm up",
        sender="u",
        recipient=str(worker.id),
    )
    await hook(init_msg)

    new_sys = System(content={"system_message": "replaced"}, sender="system")
    await hook(new_sys)

    async with StateDB() as db:
        b = await db.get_branch(str(worker.id))
    assert b["system_msg_id"] == str(new_sys.id)

    await stop_live_persist(env, status="completed")


# stop_live_persist: invariants


async def test_stop_updates_session_bookmarks_and_status(
    temp_db_path: Path,
):
    from lionagi.protocols.messages.manager import MessageManager

    env = _minimal_env()
    await start_live_persist(env)
    worker = Branch(name="worker-1")
    env.session.include_branches(worker)
    _register_branch_hook(env._live_persist, worker)
    hook = env._live_persist["hooks"][-1][1]

    m1 = MessageManager.create_instruction(
        instruction="a",
        sender="u",
        recipient=str(worker.id),
    )
    m2 = MessageManager.create_instruction(
        instruction="b",
        sender="u",
        recipient=str(worker.id),
    )
    await hook(m1)
    await hook(m2)

    ctx = env._live_persist
    await stop_live_persist(env, status="completed")

    async with StateDB() as db:
        s = await db.get_session(ctx["session_id"])
    assert s["status"] == "completed"
    assert s["first_msg_id"] == str(m1.id)
    assert s["last_msg_id"] == str(m2.id)
    assert s["ended_at"] is not None
    # env._live_persist is cleared after stop.
    assert env._live_persist is None


def _usage_message(input_tokens: int, output_tokens: int, cost: float, turns: int):
    from lionagi.protocols.messages.assistant_response import (
        AssistantResponse,
        AssistantResponseContent,
    )

    return AssistantResponse(
        content=AssistantResponseContent(assistant_response="ok"),
        metadata={
            "model_response": {
                "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
                "total_cost_usd": cost,
                "num_turns": turns,
            }
        },
    )


async def test_stop_aggregates_usage_across_all_dag_leg_branches(
    temp_db_path: Path,
):
    """Regression test for the orchestrator/play/flow usage-tracking gap:
    setup_orchestration_persist() never sets a singular ctx["branch"] (every
    leg is tracked via ctx["hooks"] instead), so the session row's usage
    columns must be the SUM across every branch registered there — the
    orchestrator branch plus every worker — not left at zero/NULL.
    """
    orc_branch = Branch(name="orchestrator", messages=[_usage_message(10, 5, 0.001, 1)])
    env = _minimal_env(orc_branch=orc_branch)
    await start_live_persist(env)

    worker_a = Branch(name="worker-a", messages=[_usage_message(100, 50, 0.02, 3)])
    worker_b = Branch(name="worker-b", messages=[_usage_message(200, 75, 0.03, 2)])
    env.session.include_branches(worker_a)
    env.session.include_branches(worker_b)
    _register_branch_hook(env._live_persist, worker_a)
    _register_branch_hook(env._live_persist, worker_b)

    ctx = env._live_persist
    # Confirm the fixture matches the real production shape before asserting
    # on the fix: orchestration sessions never populate a singular ctx["branch"].
    assert ctx.get("branch") is None
    assert len(ctx["hooks"]) == 3  # orchestrator + 2 workers

    await stop_live_persist(env, status="completed")

    async with StateDB() as db:
        s = await db.get_session(ctx["session_id"])

    assert s["num_turns"] == 1 + 3 + 2
    assert s["input_tokens"] == 10 + 100 + 200
    assert s["output_tokens"] == 5 + 50 + 75
    assert s["total_cost_usd"] == pytest.approx(0.001 + 0.02 + 0.03)
    # Must be a real sum across every leg, not just one branch's value and not zero.
    assert s["num_turns"] not in (0, 1, 3, 2)
    assert s["input_tokens"] not in (0, 10, 100, 200)


async def test_stop_finalizes_branch_status_for_all_dag_legs(
    temp_db_path: Path,
):
    """BRANCH_END: every leg tracked via ctx["hooks"] (including the
    orchestrator branch itself, which never gets a per-op NodeCompleted/
    NodeFailed status write from cli/orchestrate/flow.py) gets its terminal
    status/ended_at written at teardown."""
    from lionagi.protocols.messages.manager import MessageManager

    orc_branch = Branch(name="orchestrator")
    env = _minimal_env(orc_branch=orc_branch)
    await start_live_persist(env)
    orc_hook = env._live_persist["hooks"][0][1]
    orc_msg = MessageManager.create_instruction(
        instruction="plan", sender="u", recipient=str(orc_branch.id)
    )
    await orc_hook(orc_msg)  # first message -> lazily creates the orc branch row

    worker = Branch(name="worker-1")
    env.session.include_branches(worker)
    _register_branch_hook(env._live_persist, worker)
    worker_hook = env._live_persist["hooks"][-1][1]
    worker_msg = MessageManager.create_instruction(
        instruction="do", sender="u", recipient=str(worker.id)
    )
    await worker_hook(worker_msg)

    await stop_live_persist(env, status="failed")

    async with StateDB() as db:
        orc_row = await db.get_branch(str(orc_branch.id))
        worker_row = await db.get_branch(str(worker.id))

    assert orc_row is not None
    assert orc_row["status"] == "failed"
    assert orc_row["ended_at"] is not None
    assert worker_row is not None
    assert worker_row["status"] == "failed"
    assert worker_row["ended_at"] is not None


async def test_stop_does_not_clobber_worker_status_flow_already_finalized(
    temp_db_path: Path,
):
    """A worker branch flow.py's own NodeCompleted handler already marked
    'completed' must survive teardown's coarser run-level BRANCH_END even
    when the overall session ends 'failed' because a different leg failed."""
    from lionagi.protocols.messages.manager import MessageManager

    env = _minimal_env()
    await start_live_persist(env)

    worker = Branch(name="worker-done")
    env.session.include_branches(worker)
    _register_branch_hook(env._live_persist, worker)
    hook = env._live_persist["hooks"][-1][1]
    msg = MessageManager.create_instruction(instruction="a", sender="u", recipient=str(worker.id))
    await hook(msg)

    ctx = env._live_persist
    # Simulate flow.py's NodeCompleted per-op write finalizing this leg early.
    await ctx["db"].update_branch(str(worker.id), status="completed", ended_at=111.0)

    await stop_live_persist(env, status="failed")

    async with StateDB() as db:
        worker_row = await db.get_branch(str(worker.id))
    assert worker_row["status"] == "completed"
    assert worker_row["ended_at"] == 111.0


async def test_stop_removes_persistence_handler_from_bus(
    temp_db_path: Path,
):
    """stop_live_persist detaches each branch's persistence handler from the session hook bus so it cannot fire after teardown."""
    from lionagi.hooks.bus import HookPoint

    env = _minimal_env()
    await start_live_persist(env)
    worker = Branch(name="worker-1")
    env.session.include_branches(worker)
    _register_branch_hook(env._live_persist, worker)
    handler = env._live_persist["hooks"][-1][1]
    bus = env.session.hooks
    assert handler in bus.handlers_for(HookPoint.MESSAGE_ADD)

    await stop_live_persist(env, status="completed")

    assert handler not in bus.handlers_for(HookPoint.MESSAGE_ADD)


async def test_stop_closes_db_even_if_bookmark_update_fails(
    temp_db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """If update_session raises during stop, the DB still closes via its own finally block (hang-fix invariant)."""
    env = _minimal_env()
    await start_live_persist(env)
    db = env._live_persist["db"]

    async def boom(self, session_id, **kw):
        raise RuntimeError("simulated bookmark failure")

    monkeypatch.setattr(StateDB, "update_session", boom)

    before = _aiosqlite_thread_count()
    await stop_live_persist(env, status="completed")  # MUST NOT raise

    # Connection was closed.
    assert db._engine is None
    for _ in range(20):
        if _aiosqlite_thread_count() <= before:
            break
        await asyncio.sleep(0.05)
    assert _aiosqlite_thread_count() <= before


async def test_stop_does_not_claim_a_terminal_status_the_db_never_recorded(
    temp_db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """If a later write inside teardown raises after an earlier one already
    committed, the returned status must reflect what is durably persisted
    (still "running"), never the terminal status the caller asked for."""
    env = _minimal_env()
    await start_live_persist(env)
    session_id = env._live_persist["session_id"]

    async def boom(self, session_id, verification):
        raise RuntimeError("simulated artifact verification write failure")

    monkeypatch.setattr(StateDB, "update_artifact_verification", boom)

    final_status = await stop_live_persist(env, status="completed")

    assert final_status != "completed"

    checker = StateDB(temp_db_path)
    await checker.open()
    try:
        row = await checker.get_session(session_id)
        assert row["status"] == "running"
        assert final_status == row["status"]
    finally:
        await checker.close()


async def test_stop_with_none_context_is_noop(temp_db_path: Path):
    """If start failed, env._live_persist is None and stop is a no-op."""
    env = _minimal_env()
    # No start_live_persist call.
    assert env._live_persist is None
    await stop_live_persist(env, status="completed")  # MUST NOT raise


# End-to-end: no aiosqlite thread leak


async def test_start_stop_does_not_leak_aiosqlite_thread(temp_db_path: Path):
    """aiosqlite worker count returns to baseline after each start+multi-branch+stop cycle (orchestration hang guard)."""
    from lionagi.protocols.messages.manager import MessageManager

    baseline = _aiosqlite_thread_count()

    for _ in range(3):
        env = _minimal_env()
        await start_live_persist(env)
        w = Branch(name="w")
        env.session.include_branches(w)
        _register_branch_hook(env._live_persist, w)
        hook = env._live_persist["hooks"][-1][1]
        msg = MessageManager.create_instruction(
            instruction="hi",
            sender="u",
            recipient=str(w.id),
        )
        await hook(msg)
        await stop_live_persist(env, status="completed")

        for _ in range(20):
            if _aiosqlite_thread_count() <= baseline:
                break
            await asyncio.sleep(0.05)
        assert _aiosqlite_thread_count() <= baseline, (
            "aiosqlite worker thread leaked across orchestration start/stop"
        )


# lazy _ensure_branch_row retry after first-init failure


async def test_ensure_branch_row_retries_after_transient_failure(
    temp_db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """_ensure_branch_row retries after a transient failure: the initialized flag must be set ONLY after writes commit."""
    from lionagi.protocols.messages.manager import MessageManager

    env = _minimal_env()
    await start_live_persist(env)
    worker = Branch(name="worker-1")
    env.session.include_branches(worker)
    _register_branch_hook(env._live_persist, worker)
    hook = env._live_persist["hooks"][-1][1]

    # Make the FIRST create_branch fail, then succeed.
    real_create = StateDB.create_branch
    state = {"calls": 0}

    async def flaky_create(self, branch):
        state["calls"] += 1
        if state["calls"] == 1:
            raise RuntimeError("simulated transient DB failure")
        await real_create(self, branch)

    monkeypatch.setattr(StateDB, "create_branch", flaky_create)

    m1 = MessageManager.create_instruction(
        instruction="a",
        sender="u",
        recipient=str(worker.id),
    )
    m2 = MessageManager.create_instruction(
        instruction="b",
        sender="u",
        recipient=str(worker.id),
    )
    # First fire: row creation fails; hook swallows the error.
    await hook(m1)
    # Branch row does NOT exist.
    async with StateDB() as db:
        assert (await db.get_branch(str(worker.id))) is None

    # Second fire: retry happens, row creation succeeds, m2 lands.
    await hook(m2)
    async with StateDB() as db:
        b = await db.get_branch(str(worker.id))
        prog = await db.get_progression(env._live_persist["branch_prog_ids"][str(worker.id)])
    assert b is not None
    # Only m2 made it into the progression — m1's append was after a
    # failed _ensure_branch_row, so its progression write also failed
    # and was swallowed. The critical regression is that the row
    # actually got created on the retry.
    assert str(m2.id) in prog
    assert state["calls"] == 2

    await stop_live_persist(env, status="completed")


# finalize_orchestration() and stop_live_persist() DAG paths


def dag_extras() -> dict:
    return {
        "agents": [
            {"id": "analyst", "name": "Analyst", "model": "openai/gpt-5.4"},
            {"id": "critic", "name": "Critic", "model": "anthropic/claude-sonnet-4-6"},
        ],
        "operations": [
            {"id": "collect", "agent_id": "analyst", "depends_on": []},
            {"id": "validate", "agent_id": "critic", "depends_on": ["collect"]},
        ],
    }


def assert_dag_and_identity(node_metadata: dict) -> None:
    """node_metadata must carry DAG extras AND pid/pid_create_time kill-identity markers (CWE-362)."""
    for k, v in dag_extras().items():
        assert node_metadata[k] == v
    assert node_metadata.get("pid")
    assert node_metadata.get("pid_create_time")


def configure_run_for_finalize(env, tmp_path: Path) -> None:
    from unittest.mock import MagicMock

    env.run.run_id = "run-finalize"
    env.run.ensure_state_dirs = MagicMock()
    env.run.branch_path.side_effect = lambda bid: tmp_path / f"{bid}.json"


def _mock_chat_model(branch: Branch) -> None:
    """Inject a MagicMock as chat_model without going through iModel type-check."""
    from unittest.mock import MagicMock

    mock = MagicMock()
    mock.endpoint.config.provider = "openai"
    branch._imodel_manager.registry["chat"] = mock


# Test 2.1 — finalize returns branch_ids and writes branch snapshots


def test_finalize_returns_branch_ids_and_writes_branch_snapshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import json as _json

    import lionagi.cli.orchestrate._orchestration as orch_mod
    from lionagi.cli.orchestrate._orchestration import finalize_orchestration

    saved: list = []
    hints: list = []
    monkeypatch.setattr(
        orch_mod,
        "save_last_branch_pointer",
        lambda run_id, bid: saved.append((run_id, bid)),
    )
    monkeypatch.setattr(orch_mod, "hint", lambda msg: hints.append(msg))

    env = _minimal_env()
    worker = Branch(name="worker-1")
    env.session.include_branches(worker)
    _mock_chat_model(env.orc_branch)
    _mock_chat_model(worker)
    configure_run_for_finalize(env, tmp_path)

    branch_ids, orc_branch_id = finalize_orchestration(
        env, kind="flow", prompt="do work", extras=None, emit_hints=False
    )

    assert orc_branch_id == str(env.orc_branch.id)
    ids_set = {bid for _, bid, _ in branch_ids}
    assert ids_set == {str(env.orc_branch.id), str(worker.id)}

    for _, bid, _ in branch_ids:
        snap = tmp_path / f"{bid}.json"
        assert snap.exists(), f"snapshot missing for branch {bid}"
        data = _json.loads(snap.read_text())
        assert bid in snap.read_text()
        assert isinstance(data, dict)

    env.run.ensure_state_dirs.assert_called_once_with()
    assert saved == [("run-finalize", str(env.orc_branch.id))]
    assert hints == []


# Test 2.2 — finalize stores dag extras for live persist teardown


def test_finalize_stores_dag_extras_for_live_persist_teardown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import lionagi.cli.orchestrate._orchestration as orch_mod
    from lionagi.cli.orchestrate._orchestration import finalize_orchestration

    monkeypatch.setattr(orch_mod, "save_last_branch_pointer", lambda *_: None)
    monkeypatch.setattr(orch_mod, "hint", lambda *_: None)

    env = _minimal_env()
    _mock_chat_model(env.orc_branch)
    configure_run_for_finalize(env, tmp_path)
    extras = dag_extras()

    branch_ids, orc_branch_id = finalize_orchestration(
        env, kind="fanout", prompt="analyze", extras=extras, emit_hints=False
    )

    assert getattr(env, "_finalize_extras", None) == extras
    assert orc_branch_id == str(env.orc_branch.id)
    assert (tmp_path / f"{orc_branch_id}.json").exists()


def test_finalize_merges_summed_khive_injection_stats(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import lionagi.cli.orchestrate._orchestration as orch_mod
    from lionagi.cli.orchestrate._orchestration import finalize_orchestration

    monkeypatch.setattr(orch_mod, "save_last_branch_pointer", lambda *_: None)
    monkeypatch.setattr(orch_mod, "hint", lambda *_: None)

    env = _minimal_env()
    worker = Branch(name="worker")
    env.session.include_branches(worker)
    for branch in (env.orc_branch, worker):
        _mock_chat_model(branch)
        branch.providers.register(object())
    env.orc_branch.providers.stats.update({"recall_turns": 2, "blocks_injected": 3, "failed": 1})
    worker.providers.stats.update(
        {"recall_turns": 4, "writeback_records": 5, "writeback_failed": 2}
    )
    configure_run_for_finalize(env, tmp_path)

    finalize_orchestration(
        env,
        kind="fanout",
        prompt="analyze",
        extras={"existing": "value"},
        emit_hints=False,
    )

    assert env._finalize_extras == {
        "existing": "value",
        "khive_injection": {
            "recall_turns": 6,
            "blocks_injected": 3,
            "failed": 1,
            "writeback_records": 5,
            "writeback_failed": 2,
        },
    }


def test_finalize_omits_khive_injection_stats_without_registered_providers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import lionagi.cli.orchestrate._orchestration as orch_mod
    from lionagi.cli.orchestrate._orchestration import finalize_orchestration

    monkeypatch.setattr(orch_mod, "save_last_branch_pointer", lambda *_: None)
    monkeypatch.setattr(orch_mod, "hint", lambda *_: None)

    env = _minimal_env()
    _mock_chat_model(env.orc_branch)
    configure_run_for_finalize(env, tmp_path)

    finalize_orchestration(
        env,
        kind="flow",
        prompt="analyze",
        extras={"existing": "value"},
        emit_hints=False,
    )

    assert env._finalize_extras == {"existing": "value"}
    assert "khive_injection" not in env._finalize_extras


# Test 2.3 — finalize emits resume hints for orchestrator and workers


def test_finalize_emits_resume_hints_for_orchestrator_and_workers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import lionagi.cli.orchestrate._orchestration as orch_mod
    from lionagi.cli.orchestrate._orchestration import finalize_orchestration

    hints: list[str] = []
    monkeypatch.setattr(orch_mod, "save_last_branch_pointer", lambda *_: None)
    monkeypatch.setattr(orch_mod, "hint", lambda msg: hints.append(msg))

    env = _minimal_env()
    analyst = Branch(name="analyst")
    critic = Branch(name="critic")
    env.session.include_branches(analyst)
    env.session.include_branches(critic)
    _mock_chat_model(env.orc_branch)
    _mock_chat_model(analyst)
    _mock_chat_model(critic)
    configure_run_for_finalize(env, tmp_path)

    finalize_orchestration(env, kind="flow", prompt="x", extras=None, emit_hints=True)

    assert len(hints) == 3
    orc_hint = next((h for h in hints if "[orchestrator]" in h), None)
    assert orc_hint is not None and str(env.orc_branch.id) in orc_hint

    analyst_hint = next((h for h in hints if "[analyst]" in h), None)
    assert analyst_hint is not None and str(analyst.id) in analyst_hint

    critic_hint = next((h for h in hints if "[critic]" in h), None)
    assert critic_hint is not None and str(critic.id) in critic_hint


# Test 2.4 — snapshot write failure logs warning and continues


def test_finalize_snapshot_write_failure_logs_warning_and_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    import logging
    from unittest.mock import MagicMock

    import lionagi.cli.orchestrate._orchestration as orch_mod
    from lionagi.cli.orchestrate._orchestration import finalize_orchestration

    saved: list = []
    monkeypatch.setattr(
        orch_mod,
        "save_last_branch_pointer",
        lambda run_id, bid: saved.append((run_id, bid)),
    )
    monkeypatch.setattr(orch_mod, "hint", lambda *_: None)

    env = _minimal_env()
    worker = Branch(name="worker-1")
    env.session.include_branches(worker)
    _mock_chat_model(env.orc_branch)
    _mock_chat_model(worker)

    env.run.run_id = "run-finalize-failure"
    env.run.ensure_state_dirs = MagicMock()

    orc_id = str(env.orc_branch.id)
    worker_id = str(worker.id)
    valid_path = tmp_path / f"{orc_id}.json"

    bad_path = MagicMock()
    bad_path.write_text.side_effect = OSError("disk full")

    env.run.branch_path.side_effect = lambda bid: valid_path if bid == orc_id else bad_path

    with caplog.at_level(logging.WARNING, logger="lionagi.cli"):
        branch_ids, orc_branch_id = finalize_orchestration(
            env, kind="flow", prompt="x", extras=dag_extras(), emit_hints=False
        )

    assert orc_branch_id == orc_id
    assert {bid for _, bid, _ in branch_ids} == {orc_id, worker_id}
    assert getattr(env, "_finalize_extras", None) == dag_extras()
    assert saved == [("run-finalize-failure", orc_id)]
    assert any("finalize: branch snapshot write failed" in rec.message for rec in caplog.records)


# Test 2.5 — stop persists finalize extras without messages


async def test_stop_persists_finalize_extras_as_session_node_metadata_without_messages(
    temp_db_path: Path,
):
    env = _minimal_env()
    await start_live_persist(env, invocation_kind="flow")
    ctx = env._live_persist
    env._finalize_extras = dag_extras()

    await stop_live_persist(env, status="completed")

    assert env._live_persist is None
    async with StateDB() as db:
        s = await db.get_session(ctx["session_id"])

    assert s is not None
    assert s["status"] == "completed"
    assert_dag_and_identity(s["node_metadata"])
    assert s["first_msg_id"] is None
    assert s["last_msg_id"] is None
    assert s["ended_at"] is not None


# Test 2.6 — stop persists dag metadata and message bookmarks together


async def test_stop_persists_dag_metadata_and_message_bookmarks_together(
    temp_db_path: Path,
):
    from lionagi.protocols.messages.manager import MessageManager

    env = _minimal_env()
    await start_live_persist(env)
    worker = Branch(name="worker-1")
    env.session.include_branches(worker)
    _register_branch_hook(env._live_persist, worker)
    hook = env._live_persist["hooks"][-1][1]

    m1 = MessageManager.create_instruction(
        instruction="a",
        sender="u",
        recipient=str(worker.id),
    )
    m2 = MessageManager.create_instruction(
        instruction="b",
        sender="u",
        recipient=str(worker.id),
    )
    await hook(m1)
    await hook(m2)

    env._finalize_extras = dag_extras()
    ctx = env._live_persist

    await stop_live_persist(env, status="completed")

    async with StateDB() as db:
        s = await db.get_session(ctx["session_id"])

    assert_dag_and_identity(s["node_metadata"])
    assert s["first_msg_id"] == str(m1.id)
    assert s["last_msg_id"] == str(m2.id)
    assert all(h is not hook for h in worker.on_message_added)


# Test 2.7 — stop without finalize extras leaves node_metadata unchanged


async def test_stop_without_finalize_extras_leaves_existing_node_metadata_unchanged(
    temp_db_path: Path,
):
    env = _minimal_env()
    await start_live_persist(env)
    ctx = env._live_persist

    async with StateDB() as db:
        before = await db.get_session(ctx["session_id"])

    if hasattr(env, "_finalize_extras"):
        delattr(env, "_finalize_extras")

    await stop_live_persist(env, status="completed")

    async with StateDB() as db:
        after = await db.get_session(ctx["session_id"])

    assert after["node_metadata"] == before["node_metadata"]
    assert after["status"] == "completed"


# Test 2.8 — stop: get_progression failure logs and closes db


async def test_stop_get_progression_failure_logs_and_closes_db(
    temp_db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    import logging

    env = _minimal_env()
    await start_live_persist(env)
    db = env._live_persist["db"]

    async def boom(self, progression_id):
        raise RuntimeError("progression unavailable")

    monkeypatch.setattr(StateDB, "get_progression", boom)

    with caplog.at_level(logging.WARNING, logger="lionagi.cli"):
        await stop_live_persist(env, status="completed")

    assert db._engine is None
    assert env._live_persist is None
    assert any("live persist teardown failed" in rec.message for rec in caplog.records)
    assert any("progression unavailable" in rec.message for rec in caplog.records)


# Test 2.9 — stop: close failure logs warning and clears context


async def test_stop_close_failure_logs_warning_and_clears_context(
    temp_db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    import logging

    env = _minimal_env()
    await start_live_persist(env)
    real_close = env._live_persist["db"].close

    async def close_boom():
        await real_close()
        raise RuntimeError("close failed")

    monkeypatch.setattr(env._live_persist["db"], "close", close_boom)

    with caplog.at_level(logging.WARNING, logger="lionagi.cli"):
        await stop_live_persist(env, status="completed")

    assert env._live_persist is None
    assert any("live persist db.close failed" in rec.message for rec in caplog.records)
    assert any("close failed" in rec.message for rec in caplog.records)


# Test 2.10 — stop persists cancelled status with dag metadata


async def test_stop_persists_cancelled_status_with_dag_metadata(
    temp_db_path: Path,
):
    """'cancelled' must land on both the session row AND every DAG leg's
    branch row -- not just 'completed'/'failed', which finalize_branch()'s
    guard used to special-case."""
    from lionagi.protocols.messages.manager import MessageManager

    orc_branch = Branch(name="orchestrator")
    env = _minimal_env(orc_branch=orc_branch)
    await start_live_persist(env)
    orc_hook = env._live_persist["hooks"][0][1]
    await orc_hook(
        MessageManager.create_instruction(
            instruction="plan", sender="u", recipient=str(orc_branch.id)
        )
    )

    worker = Branch(name="worker-1")
    env.session.include_branches(worker)
    _register_branch_hook(env._live_persist, worker)
    worker_hook = env._live_persist["hooks"][-1][1]
    await worker_hook(
        MessageManager.create_instruction(instruction="do", sender="u", recipient=str(worker.id))
    )

    ctx = env._live_persist
    env._finalize_extras = dag_extras()

    await stop_live_persist(env, status="cancelled")

    async with StateDB() as db:
        s = await db.get_session(ctx["session_id"])
        orc_row = await db.get_branch(str(orc_branch.id))
        worker_row = await db.get_branch(str(worker.id))

    assert s["status"] == "cancelled"
    assert_dag_and_identity(s["node_metadata"])
    assert s["ended_at"] is not None

    assert orc_row["status"] == "cancelled"
    assert orc_row["ended_at"] is not None
    assert worker_row["status"] == "cancelled"
    assert worker_row["ended_at"] is not None


# ADR-0064: artifact contract snapshot and verification


async def test_start_persists_artifact_contract(
    temp_db_path: Path,
    tmp_path: Path,
):
    """artifact_contract passed to start_live_persist is stored in session."""
    env = _minimal_env()
    contract = {"expected": [{"id": "brief", "path": "brief.md"}]}
    await start_live_persist(
        env,
        invocation_kind="flow",
        artifacts_path=str(tmp_path / "artifacts"),
        artifact_contract=contract,
    )
    assert env._live_persist is not None
    ctx = env._live_persist

    async with StateDB() as db:
        s = await db.get_session(ctx["session_id"])
    assert s is not None
    stored = s["artifact_contract_json"]
    assert isinstance(stored, dict), f"expected dict, got {type(stored)}"
    assert stored["expected"][0]["id"] == "brief"

    await stop_live_persist(env, status="completed")


async def test_stop_uses_update_status_writes_reason(
    temp_db_path: Path,
):
    """stop_live_persist writes status through update_status(), so status_reason_code is set after clean completion."""
    env = _minimal_env()
    await start_live_persist(env, invocation_kind="flow")
    ctx = env._live_persist
    assert ctx is not None

    await stop_live_persist(env, status="completed")

    async with StateDB() as db:
        s = await db.get_session(ctx["session_id"])
    assert s is not None
    assert s["status"] == "completed"
    assert s["status_reason_code"] == "run.completed.ok"


async def test_stop_verification_fails_flips_status(
    temp_db_path: Path,
    tmp_path: Path,
):
    """Clean completion with missing required artifact → status flipped to failed."""
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    # deliberately NOT creating brief.md

    env = _minimal_env()
    contract = {"expected": [{"id": "brief", "path": "brief.md"}]}
    await start_live_persist(
        env,
        invocation_kind="flow",
        artifacts_path=str(artifacts_dir),
        artifact_contract=contract,
    )
    ctx = env._live_persist
    assert ctx is not None

    await stop_live_persist(env, status="completed")

    async with StateDB() as db:
        s = await db.get_session(ctx["session_id"])
    assert s is not None
    assert s["status"] == "failed"
    assert s["status_reason_code"] == "run.failed.missing_artifact"
    v = s["artifact_verification_json"]
    assert isinstance(v, dict)
    assert v["status"] == "failed"


async def test_stop_verification_preserves_non_completed_reason(
    temp_db_path: Path,
    tmp_path: Path,
):
    """Missing artifact on a failed run keeps the original exception reason."""
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    # deliberately NOT creating brief.md

    env = _minimal_env()
    contract = {"expected": [{"id": "brief", "path": "brief.md"}]}
    await start_live_persist(
        env,
        invocation_kind="flow",
        artifacts_path=str(artifacts_dir),
        artifact_contract=contract,
    )
    ctx = env._live_persist
    assert ctx is not None

    exc = RuntimeError("something broke")
    await stop_live_persist(env, status="failed", exception=exc)

    async with StateDB() as db:
        s = await db.get_session(ctx["session_id"])
    assert s is not None
    assert s["status"] == "failed"
    # Original exception reason preserved — NOT overridden by artifact code.
    assert s["status_reason_code"] == "run.failed.exception"
    # Verification still ran.
    v = s["artifact_verification_json"]
    assert isinstance(v, dict)
    assert v["status"] == "failed"


# A post-completion finalize error must not flip a successful DAG.


async def test_stop_finalize_error_stays_completed_with_distinct_reason(
    temp_db_path: Path,
):
    """A DAG that completed cleanly, but whose post-completion finalize step
    (team-teardown/persistence) raised, must keep status="completed" (exit 0)
    — the finalize error surfaces via its own reason code instead of flipping
    the run's terminal status, and is distinguishable from both a clean
    completion and a real DAG failure."""
    env = _minimal_env()
    await start_live_persist(env, invocation_kind="flow")
    ctx = env._live_persist
    assert ctx is not None

    # Mirrors what _finalize_flow (lionagi/cli/orchestrate/flow.py) stashes on
    # the env when a post-DAG finalize step (e.g. _post_results_to_team's file
    # lock) raises after the DAG already produced its result.
    env._finalize_error = {"error_class": "TimeoutError", "error": "team lock timed out"}

    final_status = await stop_live_persist(env, status="completed")
    assert final_status == "completed"

    async with StateDB() as db:
        s = await db.get_session(ctx["session_id"])
    assert s is not None
    assert s["status"] == "completed"
    assert s["status_reason_code"] == "run.completed.finalize_error"
    assert s["status_reason_code"] not in ("run.completed.ok", "run.failed.exception")
    assert "TimeoutError" in s["status_reason_summary"]


async def test_stop_finalize_error_does_not_override_real_dag_failure_reason(
    temp_db_path: Path,
):
    """When the DAG itself failed, a finalize error must not mask the real
    failure reason — it's recorded, but FAILED_EXCEPTION (or whatever the DAG
    raised) stays the reason code, not COMPLETED_FINALIZE_ERROR."""
    env = _minimal_env()
    await start_live_persist(env, invocation_kind="flow")
    ctx = env._live_persist
    assert ctx is not None

    env._finalize_error = {"error_class": "OSError", "error": "disk full"}
    dag_exc = RuntimeError("planner blew up")

    final_status = await stop_live_persist(env, status="failed", exception=dag_exc)
    assert final_status == "failed"

    async with StateDB() as db:
        s = await db.get_session(ctx["session_id"])
    assert s is not None
    assert s["status"] == "failed"
    assert s["status_reason_code"] == "run.failed.exception"


# output-write failure is a real failure, not a finalize hiccup


async def test_stop_artifact_write_error_flips_completed_to_failed_with_distinct_reason(
    temp_db_path: Path,
):
    """Unlike a finalize hiccup (team post, snapshots), the synthesis artifact
    IS the run's output. A DAG that completed but whose output write raised
    must be reported as failed (exit 1), not completed — exit 0 with no
    artifact is a success claim for a run that produced nothing. This must
    fail against the pre-split code, where the write shared the same
    `env._finalize_error` field as best-effort side effects and never
    flipped `final_status`."""
    env = _minimal_env()
    await start_live_persist(env, invocation_kind="flow")
    ctx = env._live_persist
    assert ctx is not None

    # Mirrors what _finalize_flow (lionagi/cli/orchestrate/flow.py) stashes on
    # the env when writing the synthesis artifact itself raises.
    env._artifact_write_error = {"error_class": "OSError", "error": "disk full"}

    final_status = await stop_live_persist(env, status="completed")
    assert final_status == "failed"

    async with StateDB() as db:
        s = await db.get_session(ctx["session_id"])
    assert s is not None
    assert s["status"] == "failed"
    assert s["status_reason_code"] == "run.failed.artifact_write"
    assert s["status_reason_code"] not in ("run.completed.ok", "run.completed.finalize_error")
    assert "OSError" in s["status_reason_summary"]


async def test_stop_artifact_write_error_does_not_override_real_dag_failure_reason(
    temp_db_path: Path,
):
    """When the DAG itself failed for an unrelated reason, an artifact-write
    error found alongside it must not mask the real failure reason — the
    original exception's reason code stays, with the write error recorded
    only in metadata."""
    env = _minimal_env()
    await start_live_persist(env, invocation_kind="flow")
    ctx = env._live_persist
    assert ctx is not None

    env._artifact_write_error = {"error_class": "OSError", "error": "disk full"}
    dag_exc = RuntimeError("planner blew up")

    final_status = await stop_live_persist(env, status="failed", exception=dag_exc)
    assert final_status == "failed"

    async with StateDB() as db:
        s = await db.get_session(ctx["session_id"])
    assert s is not None
    assert s["status"] == "failed"
    assert s["status_reason_code"] == "run.failed.exception"


# ADR-0064 + ADR-0057: session→invocation propagation on missing artifact
#
# The tests above prove the *session* row flips to failed/FAILED_MISSING_ARTIFACT.
# A multi-leg `li play`/`li o flow` run is read by callers (Studio, `li status`,
# `li play check`) at the *invocation* level, not the raw session level, via
# _resolve_invocation_terminal_flow(). That function had no direct test —
# these confirm the flip a reviewer/critic gate leg produces actually reaches
# the record a status-reader queries, and pin down exactly what does and does
# not survive the trip.


def _git(path: Path, *args: str) -> None:
    import subprocess

    subprocess.run(["git", *args], cwd=str(path), capture_output=True, check=True)


def _init_git_repo(path: Path) -> None:
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "test@test.com")
    _git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("initial\n")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "init")
    _git(path, "checkout", "-b", "feature")


async def test_stop_no_artifact_no_commits_flips_to_completed_empty(
    temp_db_path: Path,
    tmp_path: Path,
):
    """Completion-trust gate: a leg that declares no artifact contract and
    leaves the worktree exactly where base found it — no commits ahead, no
    dirty tree — must not read as a trustworthy `completed`."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    env = _minimal_env()
    env.cwd = str(repo)
    await start_live_persist(env, invocation_kind="flow")
    ctx = env._live_persist
    assert ctx is not None

    await stop_live_persist(env, status="completed")

    async with StateDB() as db:
        s = await db.get_session(ctx["session_id"])
    assert s is not None
    assert s["status"] == "completed_empty"
    assert s["status_reason_code"] == "run.completed_empty.no_evidence"
    v = s["artifact_verification_json"]
    # No artifact contract was declared, so verification itself is a no-op —
    # the git evidence check is what actually gated this.
    assert v is None


async def test_stop_failed_operation_evidence_wins_over_completed_empty_demotion(
    temp_db_path: Path,
    tmp_path: Path,
):
    """A run whose nodes all failed typically produces no commits, no dirty
    tree, and no artifacts either -- the same shape as a legitimate no-op
    leg. The node-failure backstop must see the failure evidence before the
    completion-trust gate gets a chance to demote the run to
    'completed_empty' and bury it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    env = _minimal_env()
    env.cwd = str(repo)
    await start_live_persist(env, invocation_kind="flow")
    ctx = env._live_persist
    assert ctx is not None

    env._failed_operation_evidence = [
        {"kind": "failed_operation", "id": "last", "label": "reviewer"}
    ]

    await stop_live_persist(env, status="completed")

    async with StateDB() as db:
        s = await db.get_session(ctx["session_id"])
    assert s is not None
    assert s["status"] == "failed", "failure evidence must outrank the no-evidence demotion"
    assert s["status_reason_code"] == "run.failed.exception"
    assert "last" in (s["status_reason_summary"] or "")


async def test_stop_escalated_evidence_wins_over_completed_empty_demotion(
    temp_db_path: Path,
    tmp_path: Path,
):
    """Same masking hazard as the node-failure backstop above, but for a leg
    that gave up via EscalationRequest: no commits, no dirty tree, no
    artifacts -- the escalation backstop must see the evidence before the
    completion-trust gate demotes the run to 'completed_empty'."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    env = _minimal_env()
    env.cwd = str(repo)
    await start_live_persist(env, invocation_kind="flow")
    ctx = env._live_persist
    assert ctx is not None

    env._escalated_evidence = [
        {"kind": "escalated_operation", "id": "worker", "label": "cannot proceed"}
    ]

    await stop_live_persist(env, status="completed")

    async with StateDB() as db:
        s = await db.get_session(ctx["session_id"])
    assert s is not None
    assert s["status"] == "failed", "escalation evidence must outrank the no-evidence demotion"
    assert s["status_reason_code"] == "run.failed.escalated"


async def test_stop_gate_rejected_evidence_wins_over_completed_empty_demotion(
    temp_db_path: Path,
    tmp_path: Path,
):
    """Same masking hazard as the node-failure and escalation backstops above,
    but for a gate that rejected mid-DAG: the rejected subtree is
    short-circuited and typically produces no commits, no dirty tree, no
    artifacts. Unlike those two backstops, a gate rejection is a deliberate,
    correct stop -- final status must stay 'completed' -- but its evidence
    must survive; the completion-trust gate must not overwrite it with a
    plain no-evidence 'completed_empty' verdict."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    env = _minimal_env()
    env.cwd = str(repo)
    await start_live_persist(env, invocation_kind="flow")
    ctx = env._live_persist
    assert ctx is not None

    env._gate_rejected_evidence = [
        {"kind": "gate_rejected", "id": "reviewer", "label": "reviewer-gate"}
    ]

    await stop_live_persist(env, status="completed")

    async with StateDB() as db:
        s = await db.get_session(ctx["session_id"])
    assert s is not None
    assert s["status"] == "completed", "gate rejection is a deliberate stop, not a failure"
    assert s["status_reason_code"] == "run.completed.gate_rejected"
    assert "reviewer-gate" in (s["status_reason_summary"] or "")
    refs = s["status_evidence_refs"] or []
    assert any(r.get("id") == "reviewer" for r in refs), (
        "gate-rejection evidence must survive the no-evidence demotion"
    )


async def test_stop_commits_ahead_of_base_stays_completed(
    temp_db_path: Path,
    tmp_path: Path,
):
    """Control case: commits ahead of base are real evidence — stays completed."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    (repo / "fix.py").write_text("print('fixed')\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "the fix")

    env = _minimal_env()
    env.cwd = str(repo)
    await start_live_persist(env, invocation_kind="flow")
    ctx = env._live_persist
    assert ctx is not None

    await stop_live_persist(env, status="completed")

    async with StateDB() as db:
        s = await db.get_session(ctx["session_id"])
    assert s is not None
    assert s["status"] == "completed"


async def test_stop_dirty_working_tree_stays_completed(
    temp_db_path: Path,
    tmp_path: Path,
):
    """Control case: reproduces the reported incident shape — a substantive
    fix sitting uncommitted in the working tree counts as evidence too."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    (repo / "fix.py").write_text("print('uncommitted fix')\n")

    env = _minimal_env()
    env.cwd = str(repo)
    await start_live_persist(env, invocation_kind="flow")
    ctx = env._live_persist
    assert ctx is not None

    await stop_live_persist(env, status="completed")

    async with StateDB() as db:
        s = await db.get_session(ctx["session_id"])
    assert s is not None
    assert s["status"] == "completed"


async def test_stop_assistant_output_only_stays_completed(
    temp_db_path: Path,
    tmp_path: Path,
):
    """A research/read-only leg whose deliverable is its response text — no
    commit, no dirty tree, no artifact — is legitimate work. A durable
    assistant message must count as completion evidence in its own right,
    or schedule chaining breaks for every read-only agent."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    env = _minimal_env()
    env.cwd = str(repo)
    await start_live_persist(env, invocation_kind="flow")
    ctx = env._live_persist
    assert ctx is not None

    async with StateDB() as db:
        msg_id = "msg-answer-1"
        await db.insert_message(
            {
                "id": msg_id,
                "created_at": 1.0,
                "content": {"assistant_response": "The answer to your question is 42."},
                "role": "assistant",
            }
        )
        await db.append_to_progression(ctx["session_prog_id"], msg_id)

    await stop_live_persist(env, status="completed")

    async with StateDB() as db:
        s = await db.get_session(ctx["session_id"])
    assert s is not None
    assert s["status"] == "completed"


async def test_stop_flushes_pending_only_message_before_completion_evidence(
    temp_db_path: Path,
    tmp_path: Path,
):
    """Teardown retries the only text event before deciding the run is empty."""
    from sqlalchemy import event

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    env = _minimal_env()
    env.cwd = str(repo)
    await start_live_persist(env, invocation_kind="flow")
    ctx = env._live_persist
    assert ctx is not None
    db = ctx["db"]
    progression_updates = 0

    def fail_second_progression(conn, cursor, statement, parameters, context, executemany):
        nonlocal progression_updates
        if statement.lstrip().startswith("UPDATE progressions"):
            progression_updates += 1
            if progression_updates == 2:
                raise RuntimeError("injected middle progression failure")

    event.listen(db._engine.sync_engine, "before_cursor_execute", fail_second_progression)
    try:
        only_message = await env.orc_branch.msgs.a_add_message(assistant_response="durable answer")
    finally:
        event.remove(db._engine.sync_engine, "before_cursor_execute", fail_second_progression)

    assert await db.get_progression(ctx["session_prog_id"]) == []
    assert ctx["message_retry_queues"][0].pending_count == 1

    final_status = await stop_live_persist(env, status="completed")

    async with StateDB() as check_db:
        session_progression = await check_db.get_progression(ctx["session_prog_id"])
        session = await check_db.get_session(ctx["session_id"])
    assert session_progression == [str(only_message.id)]
    assert final_status == "completed"
    assert session["status"] == "completed"
    assert session["status_reason_code"] != "run.completed_empty.no_evidence"


async def test_stop_whitespace_only_assistant_message_still_gates(
    temp_db_path: Path,
    tmp_path: Path,
):
    """A blank/whitespace-only assistant message is not a real deliverable —
    it must not be able to game the gate into staying `completed`."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    env = _minimal_env()
    env.cwd = str(repo)
    await start_live_persist(env, invocation_kind="flow")
    ctx = env._live_persist
    assert ctx is not None

    async with StateDB() as db:
        msg_id = "msg-blank-1"
        await db.insert_message(
            {
                "id": msg_id,
                "created_at": 1.0,
                "content": {"assistant_response": "   "},
                "role": "assistant",
            }
        )
        await db.append_to_progression(ctx["session_prog_id"], msg_id)

    await stop_live_persist(env, status="completed")

    async with StateDB() as db:
        s = await db.get_session(ctx["session_id"])
    assert s is not None
    assert s["status"] == "completed_empty"


async def test_stop_no_cwd_never_gates_on_git_evidence(
    temp_db_path: Path,
):
    """No cwd (e.g. a bare `li agent` with no --cwd) means the check has no
    opinion — must not downgrade a completion it can't evaluate."""
    env = _minimal_env()
    assert env.cwd is None
    await start_live_persist(env, invocation_kind="flow")
    ctx = env._live_persist
    assert ctx is not None

    await stop_live_persist(env, status="completed")

    async with StateDB() as db:
        s = await db.get_session(ctx["session_id"])
    assert s is not None
    assert s["status"] == "completed"


async def test_teardown_skips_already_terminal_session_without_rejection_audit(
    temp_db_path: Path,
):
    """A session already terminal (e.g. finalized by an earlier, concurrent
    teardown of the same session) must not attempt a redundant terminal
    overwrite — that trips the ADR-0035 floor and records a
    status_transition_rejected admin event for a write that was never a real
    integrity violation."""
    env = _minimal_env()
    await start_live_persist(env, invocation_kind="flow")
    ctx = env._live_persist
    assert ctx is not None

    async with StateDB() as db:
        await db.update_status(
            "session",
            ctx["session_id"],
            new_status="failed",
            reason_code="run.failed.exception",
            source="executor",
            actor=ctx["session_id"],
        )

    await stop_live_persist(env, status="completed")

    async with StateDB() as db:
        s = await db.get_session(ctx["session_id"])
        rejections = await db.list_admin_events(
            action="status_transition_rejected", target_id=ctx["session_id"]
        )
    assert s is not None
    # This invocation's own outcome was not persisted -- the earlier terminal
    # record (from the "other" writer) is what must survive.
    assert s["status"] == "failed"
    assert rejections == []


async def test_reconciled_linked_engine_completed_with_output_stays_completed(
    temp_db_path: Path,
    tmp_path: Path,
):
    """A profile session reconciled to a linked engine session's terminal
    'completed' status must not then be demoted to 'completed_empty' by the
    completion-trust gate just because the *profile* session's own
    progression carries no assistant output — the linked engine session's own
    progression (real answer text) is legitimate completion evidence too."""
    from lionagi.cli._runs import teardown_persist
    from lionagi.providers._provider_errors import ProviderError
    from lionagi.state.claude_mirror import mirror_session, session_db_id

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    env = _minimal_env()
    env.cwd = str(repo)
    await start_live_persist(env, invocation_kind="flow")
    ctx = env._live_persist
    assert ctx is not None

    engine_uid = "12121212-3434-5656-7878-909090909090"
    async with StateDB() as db:
        await mirror_session(
            db,
            session_uid=engine_uid,
            events=[
                {
                    "type": "user",
                    "uuid": "e-u1",
                    "timestamp": "2026-06-20T00:00:00.000Z",
                    "sessionId": engine_uid,
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": "what is the answer?"}],
                    },
                },
                {
                    "type": "assistant",
                    "uuid": "e-a1",
                    "timestamp": "2026-06-20T00:00:01.000Z",
                    "sessionId": engine_uid,
                    "message": {
                        "role": "assistant",
                        "model": "claude-opus-4-8",
                        "content": [{"type": "text", "text": "The real answer is 42."}],
                    },
                },
            ],
            tool_names={},
            status="completed",
        )

    final_status = await teardown_persist(
        ctx,
        status="failed",
        exception=ProviderError("stream error"),
        cwd=str(repo),
        engine_session_uid=engine_uid,
    )

    async with StateDB() as db:
        s = await db.get_session(ctx["session_id"])
        linked = await db.get_session(session_db_id(engine_uid))
    assert linked["status"] == "completed"
    assert final_status == "completed"
    assert s is not None
    assert s["status"] == "completed"
    assert s["status_reason_code"] != "run.completed_empty.no_evidence"


async def test_missing_artifact_session_failure_propagates_to_invocation_status(
    temp_db_path: Path,
    tmp_path: Path,
):
    """A required artifact missing at teardown flips the session to failed, and
    _resolve_invocation_terminal_flow (flow.py) — the function the real `finally`
    block in _run_flow uses to finalize the invocation record — reflects that
    into the invocation's terminal status. This is the propagation hop a
    status-reader (Studio, `li status`) actually observes.
    """
    from lionagi.cli.orchestrate.flow import _resolve_invocation_terminal_flow
    from lionagi.state.reasons import RunReasons

    invocation_id = "inv-missing-artifact"
    artifacts_dir = tmp_path / "artifacts" / "reviewer"
    artifacts_dir.mkdir(parents=True)
    # review.md deliberately not written — reproduces the incident shape: a
    # reviewer gate leg that completes without producing its review.

    async with StateDB() as db:
        await db.create_invocation(
            {"id": invocation_id, "skill": "codex-pr-review", "started_at": 0.0}
        )

    env = _minimal_env()
    contract = {"expected": [{"id": "review", "path": "review.md", "required": True}]}
    await start_live_persist(
        env,
        invocation_kind="flow",
        artifacts_path=str(artifacts_dir),
        artifact_contract=contract,
        invocation_id=invocation_id,
    )
    ctx = env._live_persist
    assert ctx is not None

    await stop_live_persist(env, status="completed")

    async with StateDB() as db:
        s = await db.get_session(ctx["session_id"])
    assert s is not None
    assert s["status"] == "failed"
    assert s["status_reason_code"] == "run.failed.missing_artifact"

    # The hop this test exists for: does the invocation-level resolver see it?
    (
        inv_status,
        inv_reason_code,
        inv_summary,
        inv_evidence,
        inv_metadata,
    ) = await _resolve_invocation_terminal_flow(invocation_id, fallback_status="completed")
    assert inv_status == "failed"
    # NOTE: the invocation layer generalizes to "a child session failed" — it
    # does NOT carry the session's specific FAILED_MISSING_ARTIFACT reason code
    # or the missing-artifact evidence forward verbatim. A status-reader at the
    # invocation level sees loud failure but must drill into the child session
    # (evidence below references it) to learn *why* it failed.
    assert inv_reason_code == RunReasons.FAILED_EXCEPTION
    assert "child session failed" in inv_summary
    assert any(e.get("id") == ctx["session_id"] for e in inv_evidence)
    assert inv_metadata["child_statuses"] == ["failed"]

    # Persist it exactly as _run_flow's finally block does, then read back the
    # invocation row itself — "the record a status-reader sees."
    async with StateDB() as db:
        await db.update_status(
            "invocation",
            invocation_id,
            new_status=inv_status,
            reason_code=inv_reason_code,
            reason_summary=inv_summary,
            evidence_refs=inv_evidence,
            source="executor",
            actor=invocation_id,
            metadata=inv_metadata,
        )
        inv_row = await db.get_invocation(invocation_id)
    assert inv_row is not None
    assert inv_row["status"] == "failed"


async def test_gate_rejection_reason_survives_child_to_invocation_resolution(
    temp_db_path: Path,
):
    """A child session that completed with a gate reject (status stays
    "completed", status_reason_code=COMPLETED_GATE_REJECTED) must carry that
    reason code through _resolve_invocation_terminal_flow's "all children
    completed" branch. That branch previously only special-cased
    COMPLETED_FINALIZE_ERROR and flattened every other completed reason to a
    plain COMPLETED_OK, silently erasing the gate-reject distinction at the
    invocation level -- the layer a status-reader (Studio, `li status`)
    actually queries.
    """
    from lionagi.cli.orchestrate.flow import _resolve_invocation_terminal_flow
    from lionagi.state.reasons import RunReasons

    invocation_id = "inv-gate-reject"
    async with StateDB() as db:
        await db.create_invocation({"id": invocation_id, "skill": "flow", "started_at": 0.0})

    env = _minimal_env()
    await start_live_persist(env, invocation_kind="flow", invocation_id=invocation_id)
    ctx = env._live_persist
    assert ctx is not None
    env._gate_rejected_evidence = [
        {"kind": "gate_rejected_operation", "id": "reviewer", "label": "reviewer"}
    ]

    assert await stop_live_persist(env, status="completed") == "completed"

    async with StateDB() as db:
        session = await db.get_session(ctx["session_id"])
    assert session["status_reason_code"] == RunReasons.COMPLETED_GATE_REJECTED

    _status, reason_code, _summary, evidence, _metadata = await _resolve_invocation_terminal_flow(
        invocation_id, fallback_status="completed"
    )
    assert reason_code == RunReasons.COMPLETED_GATE_REJECTED
    assert any(entry.get("id") == ctx["session_id"] for entry in evidence)


async def test_spawn_refusal_is_degraded_in_session_and_invocation_status(
    temp_db_path: Path,
):
    """A refused reactive spawn stays a completed run but cannot read clean.

    The session is Studio's run-detail status source and the invocation is the
    terminal-notify source for an MCP flow. Both layers must retain the same
    degraded reason instead of flattening it to ``run.completed.ok``.
    """
    from lionagi.cli.orchestrate.flow import _resolve_invocation_terminal_flow
    from lionagi.state.reasons import RunReasons

    invocation_id = "inv-spawn-refused"
    async with StateDB() as db:
        await db.create_invocation({"id": invocation_id, "skill": "flow", "started_at": 0.0})

    env = _minimal_env()
    await start_live_persist(env, invocation_kind="flow", invocation_id=invocation_id)
    ctx = env._live_persist
    assert ctx is not None
    env._spawn_refusal_evidence = [
        {
            "kind": "refused_spawn",
            "id": "parent-op",
            "label": "reviewer (max_spawn_exceeded)",
        }
    ]

    assert await stop_live_persist(env, status="completed") == "completed"

    async with StateDB() as db:
        session = await db.get_session(ctx["session_id"])
    assert session["status"] == "completed"
    assert session["status_reason_code"] == RunReasons.COMPLETED_SPAWN_REFUSED
    assert "1 reactive spawn" in (session["status_reason_summary"] or "")
    assert session["status_evidence_refs"] == env._spawn_refusal_evidence

    status, reason_code, _summary, evidence, _metadata = await _resolve_invocation_terminal_flow(
        invocation_id, fallback_status="completed"
    )
    assert status == "completed"
    assert reason_code == RunReasons.COMPLETED_SPAWN_REFUSED
    assert reason_code != RunReasons.COMPLETED_OK
    assert evidence == [{"kind": "session", "id": ctx["session_id"]}]


async def test_all_legs_completed_resolves_invocation_completed(
    temp_db_path: Path,
    tmp_path: Path,
):
    """Control case: when every child session completes cleanly (artifact
    present), the invocation resolves to completed — the failure path above
    is a real signal, not an always-failed resolver.
    """
    from lionagi.cli.orchestrate.flow import _resolve_invocation_terminal_flow

    invocation_id = "inv-all-completed"
    artifacts_dir = tmp_path / "artifacts" / "reviewer"
    artifacts_dir.mkdir(parents=True)
    (artifacts_dir / "review.md").write_text("looks good")

    async with StateDB() as db:
        await db.create_invocation(
            {"id": invocation_id, "skill": "codex-pr-review", "started_at": 0.0}
        )

    env = _minimal_env()
    contract = {"expected": [{"id": "review", "path": "review.md", "required": True}]}
    await start_live_persist(
        env,
        invocation_kind="flow",
        artifacts_path=str(artifacts_dir),
        artifact_contract=contract,
        invocation_id=invocation_id,
    )
    await stop_live_persist(env, status="completed")

    (
        inv_status,
        inv_reason_code,
        _summary,
        _evidence,
        _metadata,
    ) = await _resolve_invocation_terminal_flow(invocation_id, fallback_status="completed")
    assert inv_status == "completed"


async def test_child_finalize_error_surfaces_at_invocation_not_flattened_to_ok(
    temp_db_path: Path,
):
    """A child session can be "completed" (its own DAG produced its result)
    while carrying a COMPLETED_FINALIZE_ERROR reason (a guarded best-effort
    teardown step -- team post, snapshot, resume pointer, graph -- failed).
    _resolve_invocation_terminal_flow must not flatten that into plain
    COMPLETED_OK: a reader of the invocation record needs to see the same
    degraded-but-not-failed distinction the child record already carries,
    not a false "all clean" signal.

    A resolver that only inspects child_statuses (all "completed") and
    returns RunReasons.COMPLETED_OK regardless of status_reason_code would
    fail this.
    """
    from lionagi.cli.orchestrate.flow import _resolve_invocation_terminal_flow
    from lionagi.state.reasons import RunReasons

    invocation_id = "inv-child-finalize-error"

    async with StateDB() as db:
        await db.create_invocation({"id": invocation_id, "skill": "flow", "started_at": 0.0})

    env = _minimal_env()
    await start_live_persist(env, invocation_kind="flow", invocation_id=invocation_id)
    ctx = env._live_persist
    assert ctx is not None

    # Mirrors what _finalize_flow stashes on the env when a post-DAG,
    # non-output finalize step (e.g. the team-inbox post) raises after the
    # DAG already produced its result.
    env._finalize_error = {"error_class": "TimeoutError", "error": "team lock timed out"}

    final_status = await stop_live_persist(env, status="completed")
    assert final_status == "completed"

    async with StateDB() as db:
        s = await db.get_session(ctx["session_id"])
    assert s is not None
    assert s["status"] == "completed"
    assert s["status_reason_code"] == RunReasons.COMPLETED_FINALIZE_ERROR

    (
        inv_status,
        inv_reason_code,
        inv_summary,
        inv_evidence,
        _inv_metadata,
    ) = await _resolve_invocation_terminal_flow(invocation_id, fallback_status="completed")

    # The DAG's own work succeeded -- do not fail the run for a best-effort
    # teardown step.
    assert inv_status == "completed"
    # But the degraded reason must survive the child->invocation hop, not
    # collapse into indistinguishable clean success.
    assert inv_reason_code == RunReasons.COMPLETED_FINALIZE_ERROR
    assert inv_reason_code != RunReasons.COMPLETED_OK
    assert any(e.get("id") == ctx["session_id"] for e in inv_evidence)

    # Persist and read back exactly as _run_flow's finally block does --
    # "the record a status-reader sees" for the invocation itself.
    async with StateDB() as db:
        await db.update_status(
            "invocation",
            invocation_id,
            new_status=inv_status,
            reason_code=inv_reason_code,
            reason_summary=inv_summary,
            evidence_refs=inv_evidence,
            source="executor",
            actor=invocation_id,
            metadata=_inv_metadata,
        )
        inv_row = await db.get_invocation(invocation_id)
    assert inv_row is not None
    assert inv_row["status"] == "completed"
    assert inv_row["status_reason_code"] == RunReasons.COMPLETED_FINALIZE_ERROR


async def test_finalize_side_effects_guarded_independently(
    temp_db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The best-effort finalize side effects (team post, snapshot/resume-
    pointer, graph image) must each be guarded on their own -- a raising
    team post must not skip the snapshot/resume pointer step that runs
    after it.

    Code that shares one try/except across all three would fail this: the
    first raise (team post) would skip `finalize_orchestration` entirely,
    so no session snapshot/resume pointer would ever be written.
    """
    from types import SimpleNamespace
    from unittest.mock import patch

    import lionagi.cli.orchestrate._orchestration as orch_mod
    from lionagi.cli.orchestrate.flow import (
        _DagState,
        _ExecResult,
        _finalize_flow,
        _PlanResult,
    )

    monkeypatch.setattr(orch_mod, "save_last_branch_pointer", lambda run_id, bid: None)

    env = _minimal_env()
    env.team_data = {"id": "team-x", "name": "team-x"}
    configure_run_for_finalize(env, tmp_path)
    await start_live_persist(env, invocation_kind="flow")
    ctx = env._live_persist
    assert ctx is not None

    plan_result = _PlanResult(
        assignments=[SimpleNamespace(assignee="worker")],
        agent_ids=["op1"],
        dep_indices=[[]],
        pool=[],
        budget_preambles={},
    )
    dag_state = _DagState(
        node_ids=["op1"],
        known_nodes={"op1"},
        deps_by_node={"op1": []},
        reactive=False,
        spawn_roles=None,
        role_base={},
        worker_models=["claude"],
    )
    exec_result = _ExecResult(
        agent_results=[
            {
                "id": "op1",
                "agent_id": "op1",
                "name": "worker",
                "assignee": "worker",
                "response": "done",
                "model": "claude",
                "spawned": False,
                "time_ms": 100.0,
            }
        ],
        n_spawned=0,
        t_exec_elapsed=0.1,
    )

    with patch(
        "lionagi.cli.orchestrate.flow._post_results_to_team",
        side_effect=RuntimeError("team lock timed out"),
    ):
        _finalize_flow(
            env,
            "do the thing",
            plan_result,
            dag_state,
            exec_result,
            None,
            output_format="text",
            show_graph=False,
        )

    assert env._finalize_error is not None
    assert env._finalize_error["error_class"] == "RuntimeError"

    final_status = await stop_live_persist(env, status="completed")
    assert final_status == "completed"

    async with StateDB() as db:
        session = await db.get_session(ctx["session_id"])
    assert session is not None
    assert session["status_reason_code"] == "run.completed.finalize_error"
    node_metadata = session["node_metadata"]
    assert isinstance(node_metadata, dict)
    # The snapshot/resume-pointer step (finalize_orchestration) ran despite
    # the earlier team-post failure -- proven by its extras landing in
    # node_metadata.
    assert node_metadata.get("agents") is not None
    assert node_metadata["agents"][0]["id"] == "op1"


# Plan-time per-leg wiring + escalation backstop.
#
# Both tests below reproduce an observed production gap: the play-level
# artifact_contract was NULL for the whole run — nothing at plan time ever
# populated a per-leg contract, so the tests above (which hand-build a
# `contract` and pass it to start_live_persist directly) do not exercise the
# gap itself. These two start with no contract, exactly like that run, and
# drive the real _build_dag / _execute_dag phase functions to prove the
# wiring — not just a role-profile declaration nobody consults — is what
# closes it.


async def test_build_dag_wires_role_artifact_defaults_into_live_contract_and_fails_loud(
    temp_db_path: Path,
    tmp_path: Path,
):
    """A path: no whole-flow contract is declared (play-level NULL, as
    observed in production). The only source of a per-leg contract is the resolved
    worker's own casts Role (no committed AgentProfile file exists in this
    repo, so w_profile is always None in practice — the role fallback IS the
    real path). _build_dag must itself populate the live contract from the
    reviewer role's artifact_defaults, persist it to the session row, and
    the run must still fail loud at teardown when the declared artifact is
    never written.
    """
    from unittest.mock import patch

    from lionagi.casts.emission import TaskAssignment
    from lionagi.cli.orchestrate.flow import _build_dag, _PlanResult

    env = _minimal_env()
    artifacts_dir = tmp_path / "artifacts"
    await start_live_persist(
        env,
        invocation_kind="flow",
        artifacts_path=str(artifacts_dir),
    )
    ctx = env._live_persist
    assert ctx is not None
    assert ctx["artifact_contract"] is None  # matches the observed production starting state

    assignments = [TaskAssignment(task="review the PR", assignee="reviewer")]
    plan_result = _PlanResult(
        assignments=assignments,
        agent_ids=["reviewer"],
        dep_indices=[[]],
        pool=[],
        budget_preambles={},
    )

    with patch(
        "lionagi.cli.orchestrate.flow.build_worker_branch",
        return_value=(Branch(name="reviewer"), "codex/gpt-5.5", None, False),
    ):
        await _build_dag(env, "review this PR", plan_result, reactive_spec="off", max_spawn=20)

    # The live in-memory contract was extended during DAG build, before any
    # worker ran.
    merged = env._live_persist["artifact_contract"]
    assert merged is not None
    entry = next(e for e in merged["expected"] if e["id"] == "reviewer__review")
    assert entry["path"] == "reviewer/review.md"
    assert entry["required"] is True

    # ...and the session row itself carries it — the exact field the
    # production run showed stuck at NULL for the whole play.
    async with StateDB() as db:
        s = await db.get_session(ctx["session_id"])
    assert s is not None
    stored = s["artifact_contract_json"]
    assert isinstance(stored, dict)
    assert "reviewer__review" in {e["id"] for e in stored["expected"]}

    # The reviewer leg never wrote reviewer/review.md.
    await stop_live_persist(env, status="completed")

    async with StateDB() as db:
        s = await db.get_session(ctx["session_id"])
    assert s is not None
    assert s["status"] == "failed"
    assert s["status_reason_code"] == "run.failed.missing_artifact"


async def test_execute_dag_escalation_without_artifact_declaration_fails_loud(
    temp_db_path: Path,
    tmp_path: Path,
):
    """B path: an ordinary (non-gate) role with no artifact_defaults at all —
    the undeclared case _build_dag's merge cannot catch by construction — that
    gives up mid-run via EscalationRequest instead of completing cleanly. The
    ReactiveExecutor already tracks this (NodeEscalated / _escalated_ids) but
    before this fix nothing surfaced it past _execute_dag, so a completed run
    with an escalated leg and no result read as an ordinary clean completion.
    This is the backstop: it must still fail loud even though no contract was
    ever declared for the leg.
    """
    from unittest.mock import MagicMock, patch

    from lionagi.casts.emission import TaskAssignment
    from lionagi.cli.orchestrate.flow import _DagState, _execute_dag, _PlanResult

    env = _minimal_env()
    artifacts_dir = tmp_path / "artifacts"
    await start_live_persist(env, invocation_kind="flow", artifacts_path=str(artifacts_dir))
    ctx = env._live_persist
    assert ctx is not None

    assignments = [TaskAssignment(task="do the risky thing", assignee="worker")]
    plan_result = _PlanResult(
        assignments=assignments,
        agent_ids=["worker"],
        dep_indices=[[]],
        pool=[],
        budget_preambles={},
    )
    dag_state = _DagState(
        node_ids=["node-0"],
        known_nodes={"node-0"},
        deps_by_node={"node-0": []},
        reactive=False,
        spawn_roles=None,
        role_base={},
        worker_models=["codex/gpt-5.5"],
    )

    from lionagi.engines import PlanningEngine

    async def _run_dag_result():
        return {
            "operation_results": {},
            "spawned_operations": 0,
            "escalated_operations": ["node-0"],
        }

    fake_engine_run = MagicMock()
    fake_engine_run.run_dag = MagicMock(return_value=_run_dag_result())

    with patch.object(PlanningEngine, "new_run", return_value=fake_engine_run):
        exec_result = await _execute_dag(env, plan_result, dag_state, max_concurrent=1, max_ops=0)

    assert exec_result.escalated_agent_ids == ["worker"]
    assert env._escalated_evidence == [
        {"kind": "escalated_operation", "id": "worker", "label": "worker"}
    ]
    # Confirms this really is the undeclared case, not the A-path in disguise.
    assert env._live_persist["artifact_contract"] is None

    await stop_live_persist(env, status="completed")

    async with StateDB() as db:
        s = await db.get_session(ctx["session_id"])
    assert s is not None
    assert s["status"] == "failed"
    assert s["status_reason_code"] == "run.failed.escalated"

    import json as _json

    evidence = s["status_evidence_refs"]
    evidence = _json.loads(evidence) if isinstance(evidence, str) else evidence
    assert any(e.get("id") == "worker" for e in evidence)


async def test_execute_dag_gate_rejected_evidence_survives_uuid_node_ids(
    temp_db_path: Path,
    tmp_path: Path,
):
    """dag_state.node_ids holds real Operation UUIDs (not the plain strings
    other _execute_dag tests stub in), and dag_result["gate_rejected_operations"]
    holds their str() form -- exactly what the real executor/builder produce.
    Comparing node_ids[i] to that string set directly is always False for a
    UUID, so a planned gate's evidence entry was silently reclassified as a
    "spawned" node and lost its agent label. This must not regress."""
    from unittest.mock import MagicMock, patch
    from uuid import uuid4

    from lionagi.casts.emission import TaskAssignment
    from lionagi.cli.orchestrate.flow import _DagState, _execute_dag, _PlanResult

    env = _minimal_env()
    artifacts_dir = tmp_path / "artifacts"
    await start_live_persist(env, invocation_kind="flow", artifacts_path=str(artifacts_dir))
    ctx = env._live_persist
    assert ctx is not None

    gate_node_id = uuid4()
    assignments = [TaskAssignment(task="review the design", assignee="reviewer")]
    plan_result = _PlanResult(
        assignments=assignments,
        agent_ids=["reviewer"],
        dep_indices=[[]],
        pool=[],
        budget_preambles={},
    )
    dag_state = _DagState(
        node_ids=[gate_node_id],
        known_nodes={gate_node_id},
        deps_by_node={gate_node_id: []},
        reactive=False,
        spawn_roles=None,
        role_base={},
        worker_models=["codex/gpt-5.5"],
    )

    from lionagi.engines import PlanningEngine

    async def _run_dag_result():
        return {
            "operation_results": {gate_node_id: {"gate_verdict": "reject"}},
            "spawned_operations": 0,
            "gate_rejected_operations": [str(gate_node_id)],
        }

    fake_engine_run = MagicMock()
    fake_engine_run.run_dag = MagicMock(return_value=_run_dag_result())

    with patch.object(PlanningEngine, "new_run", return_value=fake_engine_run):
        await _execute_dag(env, plan_result, dag_state, max_concurrent=1, max_ops=0)

    # The planned "reviewer" gate, not a synthesized "spawn-N" placeholder.
    assert env._gate_rejected_evidence == [
        {"kind": "gate_rejected_operation", "id": "reviewer", "label": "reviewer"}
    ]

    await stop_live_persist(env, status="completed")

    async with StateDB() as db:
        s = await db.get_session(ctx["session_id"])
    assert s is not None
    assert s["status"] == "completed"
    assert s["status_reason_code"] == "run.completed.gate_rejected"


async def test_execute_dag_escalation_evidence_survives_uuid_node_ids(
    temp_db_path: Path,
    tmp_path: Path,
):
    """Same shape as test_execute_dag_gate_rejected_evidence_survives_uuid_node_ids,
    one screen up: dag_state.node_ids holds real Operation UUIDs, and
    dag_result["escalated_operations"] holds their str() form. Comparing
    node_ids[i] to that string set directly is always False for a UUID, so a
    planned escalated worker's evidence entry was silently reclassified as a
    "spawned" node and lost its agent label. This must not regress."""
    from unittest.mock import MagicMock, patch
    from uuid import uuid4

    from lionagi.casts.emission import TaskAssignment
    from lionagi.cli.orchestrate.flow import _DagState, _execute_dag, _PlanResult

    env = _minimal_env()
    artifacts_dir = tmp_path / "artifacts"
    await start_live_persist(env, invocation_kind="flow", artifacts_path=str(artifacts_dir))
    ctx = env._live_persist
    assert ctx is not None

    worker_node_id = uuid4()
    assignments = [TaskAssignment(task="do the risky thing", assignee="worker")]
    plan_result = _PlanResult(
        assignments=assignments,
        agent_ids=["worker"],
        dep_indices=[[]],
        pool=[],
        budget_preambles={},
    )
    dag_state = _DagState(
        node_ids=[worker_node_id],
        known_nodes={worker_node_id},
        deps_by_node={worker_node_id: []},
        reactive=False,
        spawn_roles=None,
        role_base={},
        worker_models=["codex/gpt-5.5"],
    )

    from lionagi.engines import PlanningEngine

    async def _run_dag_result():
        return {
            "operation_results": {},
            "spawned_operations": 0,
            "escalated_operations": [str(worker_node_id)],
        }

    fake_engine_run = MagicMock()
    fake_engine_run.run_dag = MagicMock(return_value=_run_dag_result())

    with patch.object(PlanningEngine, "new_run", return_value=fake_engine_run):
        exec_result = await _execute_dag(env, plan_result, dag_state, max_concurrent=1, max_ops=0)

    # The planned "worker", not a synthesized UUID-as-label placeholder.
    assert exec_result.escalated_agent_ids == ["worker"]
    assert env._escalated_evidence == [
        {"kind": "escalated_operation", "id": "worker", "label": "worker"}
    ]

    await stop_live_persist(env, status="completed")

    async with StateDB() as db:
        s = await db.get_session(ctx["session_id"])
    assert s is not None
    assert s["status"] == "failed"
    assert s["status_reason_code"] == "run.failed.escalated"

    import json as _json

    evidence = s["status_evidence_refs"]
    evidence = _json.loads(evidence) if isinstance(evidence, str) else evidence
    assert any(e.get("id") == "worker" for e in evidence)


async def test_execute_dag_drains_inflight_branch_status_before_teardown(
    temp_db_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from types import SimpleNamespace
    from unittest.mock import patch

    from lionagi.casts.emission import TaskAssignment
    from lionagi.cli.orchestrate.flow import _DagState, _execute_dag, _PlanResult
    from lionagi.engines import PlanningEngine
    from lionagi.session.signal import NodeCompleted

    env = _minimal_env()
    worker = Branch(name="worker")
    env.session.include_branches(worker)
    await start_live_persist(env, invocation_kind="flow")
    ctx = env._live_persist
    assert ctx is not None
    write_started = asyncio.Event()
    release_write = asyncio.Event()
    events: list[str] = []
    real_update_branch = ctx["db"].update_branch

    async def delayed_update_branch(branch_id, **kwargs):
        events.append("write-started")
        write_started.set()
        await release_write.wait()
        await real_update_branch(branch_id, **kwargs)
        events.append("write-finished")

    monkeypatch.setattr(ctx["db"], "update_branch", delayed_update_branch)
    plan_result = _PlanResult(
        assignments=[TaskAssignment(task="work", assignee="worker")],
        agent_ids=["worker"],
        dep_indices=[[]],
        pool=[],
        budget_preambles={},
    )
    dag_state = _DagState(
        node_ids=["node-0"],
        known_nodes={"node-0"},
        deps_by_node={"node-0": []},
        reactive=False,
        spawn_roles=None,
        role_base={"worker": worker},
        worker_models=["codex/model"],
    )

    async def run_dag(graph, **kwargs):
        await env.session.emit(NodeCompleted(op_id="node-0", name="worker"))
        return {
            "operation_results": {"node-0": "done"},
            "spawned_operations": 0,
            "escalated_operations": [],
        }

    engine_run = SimpleNamespace(run_dag=run_dag)
    with patch.object(PlanningEngine, "new_run", return_value=engine_run):

        async def execute_then_teardown():
            await _execute_dag(env, plan_result, dag_state, max_concurrent=1, max_ops=0)
            events.append("teardown-started")
            await stop_live_persist(env, status="completed")

        execute_task = asyncio.create_task(execute_then_teardown())
        await write_started.wait()
        await asyncio.sleep(0)
        assert not execute_task.done()
        events.append("teardown-waiting")
        release_write.set()
        await execute_task

    assert events.index("write-finished") < events.index("teardown-started")
    assert env._live_persist is None


async def test_execute_dag_drains_escalation_link_retries_before_teardown(
    temp_db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Round-2 regression: the escalation-mirror-link retry loop used to fire
    as an untracked task (bare ``_asyncio.ensure_future``, nothing awaited it)
    — a retry still sleeping when ``_execute_dag`` returned could go on to
    write into a db that ``stop_live_persist`` had already closed. It is now
    tracked in ``_escalation_link_tasks`` and drained in the same shielded
    finally block as ``_branch_status_tasks``/``_checkpoint_tasks``, so every
    retry must finish before ``_execute_dag`` returns — never after."""
    from types import SimpleNamespace
    from unittest.mock import patch

    from lionagi.casts.emission import TaskAssignment
    from lionagi.cli.orchestrate import flow as flow_module
    from lionagi.cli.orchestrate.flow import _DagState, _execute_dag, _PlanResult
    from lionagi.engines import PlanningEngine
    from lionagi.operations.node import create_operation

    monkeypatch.setattr(flow_module, "_ESCALATION_LINK_RETRIES", 3)
    monkeypatch.setattr(flow_module, "_ESCALATION_LINK_RETRY_INTERVAL", 0.02)

    env = _minimal_env()
    await start_live_persist(env, invocation_kind="flow")
    ctx = env._live_persist
    assert ctx is not None

    node = create_operation("operate", parameters={})
    node.metadata["escalated_from"] = "parent-op-1"
    node.metadata["escalated_from_name"] = "worker"
    node._branch = SimpleNamespace(
        chat_model=SimpleNamespace(provider_session_id="cli-session-xyz")
    )

    events: list[str] = []

    async def _spy_link(*a, **k):
        # The mirror row never appears — every retry must actually run.
        events.append("link-attempt")
        return False

    plan_result = _PlanResult(
        assignments=[TaskAssignment(task="do it", assignee="worker")],
        agent_ids=["worker"],
        dep_indices=[[]],
        pool=[],
        budget_preambles={},
    )
    dag_state = _DagState(
        node_ids=["node-0"],
        known_nodes={"node-0"},
        deps_by_node={"node-0": []},
        reactive=False,
        spawn_roles=None,
        role_base={},
        worker_models=["test/model"],
    )

    async def run_dag(graph, **kwargs):
        on_op_complete = kwargs.get("on_op_complete")
        if on_op_complete is not None:
            on_op_complete(node)
        return {"operation_results": {}, "spawned_operations": 0, "escalated_operations": []}

    engine_run = SimpleNamespace(run_dag=run_dag)
    with (
        patch("lionagi.state.claude_mirror.link_escalation_session", _spy_link),
        patch.object(PlanningEngine, "new_run", return_value=engine_run),
    ):
        await _execute_dag(env, plan_result, dag_state, max_concurrent=1, max_ops=0)
    events.append("execute-dag-returned")
    await stop_live_persist(env, status="completed")
    events.append("teardown-finished")

    # All three retries ran to exhaustion, entirely before _execute_dag
    # returned — none leaked past it into (or after) teardown.
    assert events == [
        "link-attempt",
        "link-attempt",
        "link-attempt",
        "execute-dag-returned",
        "teardown-finished",
    ]


async def test_execute_dag_bounds_escalation_link_drain_on_cancellation(
    temp_db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Escalation-link drain must be bounded, not just guarded — see
    docs/internals/cli.md's `_orchestration.py` section. A shielded finally
    that gathers ``_escalation_link_tasks`` with no cancel and no timeout
    lets a link write that never returns (hung DB call, stuck await) block
    teardown forever. This drives cancellation into ``_execute_dag`` itself
    and confirms an in-flight link gets a short grace period, then is
    cancelled and actually unwinds before returning."""
    import time
    from types import SimpleNamespace
    from unittest.mock import patch

    from lionagi.casts.emission import TaskAssignment
    from lionagi.cli.orchestrate.flow import _DagState, _execute_dag, _PlanResult
    from lionagi.engines import PlanningEngine
    from lionagi.operations.node import create_operation

    env = _minimal_env()
    await start_live_persist(env, invocation_kind="flow")
    ctx = env._live_persist
    assert ctx is not None

    node = create_operation("operate", parameters={})
    node.metadata["escalated_from"] = "parent-op-1"
    node.metadata["escalated_from_name"] = "worker"
    node._branch = SimpleNamespace(
        chat_model=SimpleNamespace(provider_session_id="cli-session-xyz")
    )

    link_started = asyncio.Event()
    link_cancelled = asyncio.Event()

    async def _hanging_link(*a, **k):
        link_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            link_cancelled.set()
            raise

    plan_result = _PlanResult(
        assignments=[TaskAssignment(task="do it", assignee="worker")],
        agent_ids=["worker"],
        dep_indices=[[]],
        pool=[],
        budget_preambles={},
    )
    dag_state = _DagState(
        node_ids=["node-0"],
        known_nodes={"node-0"},
        deps_by_node={"node-0": []},
        reactive=False,
        spawn_roles=None,
        role_base={},
        worker_models=["test/model"],
    )

    async def run_dag(graph, **kwargs):
        on_op_complete = kwargs.get("on_op_complete")
        if on_op_complete is not None:
            on_op_complete(node)
        # Hangs until _execute_dag's own task is cancelled from outside.
        await asyncio.Event().wait()

    engine_run = SimpleNamespace(run_dag=run_dag)
    with (
        patch("lionagi.state.claude_mirror.link_escalation_session", _hanging_link),
        patch.object(PlanningEngine, "new_run", return_value=engine_run),
    ):
        execute_task = asyncio.create_task(
            _execute_dag(env, plan_result, dag_state, max_concurrent=1, max_ops=0)
        )
        await link_started.wait()
        execute_task.cancel()

        start = time.monotonic()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(execute_task, timeout=5)
        elapsed = time.monotonic() - start

    try:
        assert elapsed < 5, (
            f"teardown took {elapsed}s — the escalation-link drain is unbounded again"
        )
        assert link_cancelled.is_set()
    finally:
        await stop_live_persist(env, status="completed")


async def test_execute_dag_bounds_escalation_link_drain_on_late_cancellation(
    temp_db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Covers the OTHER route into the drain (see docs/internals/cli.md's
    `_orchestration.py` section): cancellation landing after ``run_dag()``
    already returned, inside the finally's own drain gather, where the
    except that sets ``_dag_cancelled`` never runs — that flag stays False,
    so this branch must bound the drain on its own, independent of the
    mid-``run_dag()`` case.

    A link task that responds to a single cancellation would not exercise
    this: ``asyncio.gather()``'s own cancellation only needs one child to
    unwind once, so a bare, unguarded gather already handles that case. This
    link task instead swallows the first cancellation it receives and only
    unwinds on a second one — confirmed by temporarily reverting the
    shield+drain handling below to a bare-gather shape and re-running this
    exact test: it timed out at the 8s deadline instead of finishing at
    ~2s, because ``asyncio.gather()`` doesn't settle its own cancellation
    until every child finishes, and a raw ``Task.cancel()`` only delivers
    once."""
    import time
    from types import SimpleNamespace
    from unittest.mock import patch

    from lionagi.casts.emission import TaskAssignment
    from lionagi.cli.orchestrate.flow import _DagState, _execute_dag, _PlanResult
    from lionagi.engines import PlanningEngine
    from lionagi.operations.node import create_operation

    env = _minimal_env()
    await start_live_persist(env, invocation_kind="flow")
    ctx = env._live_persist
    assert ctx is not None

    node = create_operation("operate", parameters={})
    node.metadata["escalated_from"] = "parent-op-1"
    node.metadata["escalated_from_name"] = "worker"
    node._branch = SimpleNamespace(
        chat_model=SimpleNamespace(provider_session_id="cli-session-xyz")
    )

    link_started = asyncio.Event()
    link_cancelled = asyncio.Event()

    async def _swallow_once_link(*a, **k):
        link_started.set()
        swallowed = False
        while True:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                if not swallowed:
                    swallowed = True
                    continue
                link_cancelled.set()
                raise

    plan_result = _PlanResult(
        assignments=[TaskAssignment(task="do it", assignee="worker")],
        agent_ids=["worker"],
        dep_indices=[[]],
        pool=[],
        budget_preambles={},
    )
    dag_state = _DagState(
        node_ids=["node-0"],
        known_nodes={"node-0"},
        deps_by_node={"node-0": []},
        reactive=False,
        spawn_roles=None,
        role_base={},
        worker_models=["test/model"],
    )

    async def run_dag(graph, **kwargs):
        on_op_complete = kwargs.get("on_op_complete")
        if on_op_complete is not None:
            on_op_complete(node)
        # Returns normally — unlike the mid-run_dag() cancellation case,
        # cancellation has not landed yet, so the except that sets
        # _dag_cancelled never fires.
        return {"operation_results": {}, "spawned_operations": 0, "escalated_operations": []}

    engine_run = SimpleNamespace(run_dag=run_dag)
    with (
        patch("lionagi.state.claude_mirror.link_escalation_session", _swallow_once_link),
        patch.object(PlanningEngine, "new_run", return_value=engine_run),
    ):
        execute_task = asyncio.create_task(
            _execute_dag(env, plan_result, dag_state, max_concurrent=1, max_ops=0)
        )
        # run_dag() has already returned by the time the link task starts
        # (it's scheduled from the on_op_complete callback inside run_dag),
        # so cancelling here lands inside the finally's own drain gather.
        await link_started.wait()
        execute_task.cancel()

        start = time.monotonic()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(execute_task, timeout=8)
        elapsed = time.monotonic() - start

    try:
        assert elapsed < 6, (
            f"teardown took {elapsed}s — a late-arriving cancellation still hangs the drain"
        )
        assert link_cancelled.is_set()
    finally:
        await stop_live_persist(env, status="completed")


async def test_execute_dag_bounds_escalation_link_drain_survivor_await(
    temp_db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """The survivor-await step of ``_drain_escalation_links_bounded`` (see
    docs/internals/cli.md's `_orchestration.py` section) needs its own bound:
    after the first grace period, whatever survived gets cancelled and
    awaited, and that second await must not itself be unbounded.

    This exercises the full drain sequence end to end — grace-period gather,
    explicit cancel of survivors, second bounded gather — with a link task
    that needs two cancellation deliveries to unwind, and confirms the whole
    thing stays inside the hard deadline below. It does NOT isolate whether
    the second window's bound specifically is load-bearing for this shape:
    anyio's cancel scope keeps re-delivering cancellation as long as a task
    keeps re-suspending on a fresh awaitable, so a finite swallow count tends
    to resolve inside the first grace window rather than surviving into the
    second, and a link task that never responds to cancellation at all
    defeats both windows identically (confirmed empirically — neither
    `move_on_after` returns control while such a task stays alive). There is
    no task shape that reliably survives window one but is bounded by window
    two, so this test cannot isolate that distinction; it verifies the drain
    sequence doesn't regress a link task needing more than one cancellation,
    and that the abandonment logging doesn't itself hang."""
    import time
    from types import SimpleNamespace
    from unittest.mock import patch

    from lionagi.casts.emission import TaskAssignment
    from lionagi.cli.orchestrate.flow import _DagState, _execute_dag, _PlanResult
    from lionagi.engines import PlanningEngine
    from lionagi.operations.node import create_operation

    env = _minimal_env()
    await start_live_persist(env, invocation_kind="flow")
    ctx = env._live_persist
    assert ctx is not None

    node = create_operation("operate", parameters={})
    node.metadata["escalated_from"] = "parent-op-1"
    node.metadata["escalated_from_name"] = "worker"
    node._branch = SimpleNamespace(
        chat_model=SimpleNamespace(provider_session_id="cli-session-xyz")
    )

    link_started = asyncio.Event()
    link_cancelled = asyncio.Event()

    async def _swallow_once_link(*a, **k):
        link_started.set()
        swallowed = False
        while True:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                if not swallowed:
                    swallowed = True
                    continue
                link_cancelled.set()
                raise

    plan_result = _PlanResult(
        assignments=[TaskAssignment(task="do it", assignee="worker")],
        agent_ids=["worker"],
        dep_indices=[[]],
        pool=[],
        budget_preambles={},
    )
    dag_state = _DagState(
        node_ids=["node-0"],
        known_nodes={"node-0"},
        deps_by_node={"node-0": []},
        reactive=False,
        spawn_roles=None,
        role_base={},
        worker_models=["test/model"],
    )

    async def run_dag(graph, **kwargs):
        on_op_complete = kwargs.get("on_op_complete")
        if on_op_complete is not None:
            on_op_complete(node)
        # Hangs until _execute_dag's own task is cancelled from outside —
        # cancellation lands *during* run_dag(), so _dag_cancelled is True
        # and the drain goes straight through _drain_escalation_links_bounded.
        await asyncio.Event().wait()

    engine_run = SimpleNamespace(run_dag=run_dag)
    with (
        patch("lionagi.state.claude_mirror.link_escalation_session", _swallow_once_link),
        patch.object(PlanningEngine, "new_run", return_value=engine_run),
    ):
        execute_task = asyncio.create_task(
            _execute_dag(env, plan_result, dag_state, max_concurrent=1, max_ops=0)
        )
        await link_started.wait()
        execute_task.cancel()

        start = time.monotonic()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(execute_task, timeout=8)
        elapsed = time.monotonic() - start

    try:
        assert elapsed < 6, (
            f"teardown took {elapsed}s — the escalation-link survivor await is unbounded again"
        )
        assert link_cancelled.is_set()
    finally:
        await stop_live_persist(env, status="completed")


async def test_execute_dag_late_cancellation_does_not_clobber_dag_exception(
    temp_db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """The finally's escalation-link drain re-raises a ``CancelledError``
    that lands during the drain itself. When ``run_dag()`` raised a real
    exception (not a cancellation) and *that* exception is what's
    propagating through the finally, a cancellation landing during the
    drain must not silently replace it — the caller was waiting on the real
    failure, not a teardown-time cancellation that has nothing to do with
    why the run actually failed."""
    from types import SimpleNamespace
    from unittest.mock import patch

    from lionagi.casts.emission import TaskAssignment
    from lionagi.cli.orchestrate.flow import _DagState, _execute_dag, _PlanResult
    from lionagi.engines import PlanningEngine
    from lionagi.operations.node import create_operation

    class _DagBoomError(RuntimeError):
        pass

    env = _minimal_env()
    await start_live_persist(env, invocation_kind="flow")
    ctx = env._live_persist
    assert ctx is not None

    node = create_operation("operate", parameters={})
    node.metadata["escalated_from"] = "parent-op-1"
    node.metadata["escalated_from_name"] = "worker"
    node._branch = SimpleNamespace(
        chat_model=SimpleNamespace(provider_session_id="cli-session-xyz")
    )

    link_started = asyncio.Event()

    async def _hanging_link(*a, **k):
        link_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise

    plan_result = _PlanResult(
        assignments=[TaskAssignment(task="do it", assignee="worker")],
        agent_ids=["worker"],
        dep_indices=[[]],
        pool=[],
        budget_preambles={},
    )
    dag_state = _DagState(
        node_ids=["node-0"],
        known_nodes={"node-0"},
        deps_by_node={"node-0": []},
        reactive=False,
        spawn_roles=None,
        role_base={},
        worker_models=["test/model"],
    )

    async def run_dag(graph, **kwargs):
        on_op_complete = kwargs.get("on_op_complete")
        if on_op_complete is not None:
            on_op_complete(node)
        # A real dag failure, not a cancellation — _dag_cancelled stays
        # False, so the finally's escalation-link drain takes the same
        # "else" branch the late-cancellation tests above exercise.
        raise _DagBoomError("worker failed for real")

    engine_run = SimpleNamespace(run_dag=run_dag)
    with (
        patch("lionagi.state.claude_mirror.link_escalation_session", _hanging_link),
        patch.object(PlanningEngine, "new_run", return_value=engine_run),
    ):
        execute_task = asyncio.create_task(
            _execute_dag(env, plan_result, dag_state, max_concurrent=1, max_ops=0)
        )
        # _DagBoomError is already propagating through the finally by the
        # time the link task gets scheduled to run (it's created from
        # on_op_complete before run_dag raises); cancelling here lands
        # while that exception is in flight through the drain.
        await link_started.wait()
        execute_task.cancel()

        with pytest.raises(_DagBoomError):
            await asyncio.wait_for(execute_task, timeout=8)

    await stop_live_persist(env, status="completed")


async def test_flow_timeout_shields_completed_branch_status_until_commit(
    temp_db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from lionagi._errors import TimeoutError as LionTimeoutError
    from lionagi.casts.emission import TaskAssignment
    from lionagi.cli.orchestrate import flow as flow_module
    from lionagi.cli.orchestrate._orchestration import register_branch_hook
    from lionagi.engines import PlanningEngine
    from lionagi.operations.builder import OperationGraphBuilder
    from lionagi.session.signal import NodeCompleted

    env = _minimal_env()
    env.run.run_id = "timeout-status-drain"
    env.builder = OperationGraphBuilder()

    release_write = asyncio.Event()
    execution_cancelled = asyncio.Event()
    status_write_cancelled = asyncio.Event()
    write_committed = asyncio.Event()
    worker_id = ""

    async def plan_once(*args, **kwargs):
        return [TaskAssignment(task="finish one operation", assignee="worker")]

    async def build_worker(*args, **kwargs):
        nonlocal worker_id
        worker = Branch(name=kwargs["agent_id"])
        worker_id = str(worker.id)
        env.session.include_branches(worker)
        ctx = env._live_persist
        assert ctx is not None
        register_branch_hook(ctx, worker)
        await worker.msgs.a_add_message(assistant_response="durable result")

        real_update_branch = ctx["db"].update_branch

        async def delayed_update_branch(branch_id, **fields):
            if fields.get("status") != "completed":
                return await real_update_branch(branch_id, **fields)
            try:
                await release_write.wait()
            except asyncio.CancelledError:
                status_write_cancelled.set()
                raise
            await real_update_branch(branch_id, **fields)
            write_committed.set()

        real_close = ctx["db"].close

        async def tracked_close():
            assert write_committed.is_set()
            await real_close()

        monkeypatch.setattr(ctx["db"], "update_branch", delayed_update_branch)
        monkeypatch.setattr(ctx["db"], "close", tracked_close)
        return worker, "test/model", None, False

    class BlockingEngineRun:
        async def run_dag(self, graph, **kwargs):
            node_id = str(next(iter(graph.internal_nodes)))
            await env.session.emit(NodeCompleted(op_id=node_id, name="worker"))
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                execution_cancelled.set()
                raise

    async def release_after_timeout():
        await execution_cancelled.wait()
        await asyncio.sleep(0.05)
        release_write.set()

    async def setup_env(**kwargs):
        return env

    monkeypatch.setattr(flow_module, "setup_orchestration", setup_env)
    monkeypatch.setattr(flow_module, "available_roles", lambda: ["worker"])
    monkeypatch.setattr(flow_module, "plan", plan_once)
    monkeypatch.setattr(flow_module, "build_worker_branch", build_worker)
    monkeypatch.setattr(PlanningEngine, "new_run", lambda self, *, session: BlockingEngineRun())

    release_task = asyncio.create_task(release_after_timeout())
    try:
        with pytest.raises(LionTimeoutError):
            await flow_module._run_flow("test/model", "run once", timeout=2)
    finally:
        release_write.set()
        release_task.cancel()
        await asyncio.gather(release_task, return_exceptions=True)

    assert execution_cancelled.is_set()
    assert not status_write_cancelled.is_set()
    assert write_committed.is_set()
    async with StateDB() as db:
        branch = await db.get_branch(worker_id)
    assert branch is not None
    assert branch["status"] == "completed"
    assert env._live_persist is None


async def test_execute_dag_escalation_backstop_catches_reactively_spawned_node(
    temp_db_path: Path,
    tmp_path: Path,
):
    """The escalation backstop above only walks `range(len(assignments))` /
    `node_ids` — the fixed-size arrays built once at plan time — so it can
    never match an escalated id belonging to a node spawned mid-run via
    SpawnRequest (reactive mode). ReactiveExecutor's own escalation tracking
    is plan-agnostic: it adds ANY emitting node's id to `_escalated_ids`,
    spawned or not. Reproduce the exact gap — an escalated id present in
    `escalated_operations` but absent from `node_ids`/`known_nodes` — and
    confirm it still surfaces past the backstop.
    """
    from types import SimpleNamespace
    from unittest.mock import MagicMock, patch

    from lionagi.casts.emission import TaskAssignment
    from lionagi.cli.orchestrate.flow import _DagState, _execute_dag, _PlanResult

    env = _minimal_env()
    artifacts_dir = tmp_path / "artifacts"
    await start_live_persist(env, invocation_kind="flow", artifacts_path=str(artifacts_dir))
    ctx = env._live_persist
    assert ctx is not None

    assignments = [TaskAssignment(task="do the risky thing", assignee="worker")]
    plan_result = _PlanResult(
        assignments=assignments,
        agent_ids=["worker"],
        dep_indices=[[]],
        pool=[],
        budget_preambles={},
    )
    dag_state = _DagState(
        node_ids=["node-0"],
        known_nodes={"node-0"},
        deps_by_node={"node-0": []},
        reactive=True,
        spawn_roles=None,
        role_base={},
        worker_models=["codex/gpt-5.5"],
    )

    # This node was injected directly (e.g. an EscalationRequest child, or a
    # raw Session.flow inject()) rather than through role_node_builder, so it
    # carries no stamped spawn_id in the graph — the evidence must fall back
    # to its raw node id, same as the artifact-recovery loop's own unstamped
    # fallback. env.builder defaults to a bare MagicMock() (_minimal_env),
    # whose auto-attributes would otherwise look like a "found" node.
    env.builder.get_graph = lambda: SimpleNamespace(nodes=[], internal_nodes={})

    from lionagi.engines import PlanningEngine

    async def _run_dag_result():
        return {
            # The plan-time leg completed cleanly; a reactively spawned node
            # ("node-spawned-1", never appended to node_ids/agent_ids) is the
            # one that escalated.
            "operation_results": {"node-0": "ok", "node-spawned-1": "(gave up)"},
            "spawned_operations": 1,
            "escalated_operations": ["node-spawned-1"],
        }

    fake_engine_run = MagicMock()
    fake_engine_run.run_dag = MagicMock(return_value=_run_dag_result())

    with patch.object(PlanningEngine, "new_run", return_value=fake_engine_run):
        exec_result = await _execute_dag(env, plan_result, dag_state, max_concurrent=1, max_ops=0)

    assert exec_result.escalated_agent_ids == ["node-spawned-1"]
    assert env._escalated_evidence == [
        {"kind": "escalated_operation", "id": "node-spawned-1", "label": "node-spawned-1"}
    ]

    await stop_live_persist(env, status="completed")

    async with StateDB() as db:
        s = await db.get_session(ctx["session_id"])
    assert s is not None
    assert s["status"] == "failed"
    assert s["status_reason_code"] == "run.failed.escalated"


# Node-failure backstop
#
# A flow whose final node died could still report succeeded: an operation's
# invoke() raised, DependencyAwareExecutor caught it and set that node's own
# status to FAILED, but the DAG-level result folded it into
# completed_operations right alongside genuine completions and never rolled
# the per-node failure up into the run's own status. These tests pin the
# fix at both the _execute_dag plumbing layer and the teardown status flip.


async def test_execute_dag_records_failed_operation_evidence(
    temp_db_path: Path,
    tmp_path: Path,
):
    """A node's own invoke() failure (DependencyAwareExecutor's
    failed_operations, distinct from completed_operations) must be named in
    env._failed_operation_evidence so stop_live_persist can fail the run
    loud instead of it reading as an ordinary clean completion."""
    from unittest.mock import MagicMock, patch

    from lionagi.casts.emission import TaskAssignment
    from lionagi.cli.orchestrate.flow import _DagState, _execute_dag, _PlanResult

    env = _minimal_env()
    artifacts_dir = tmp_path / "artifacts"
    await start_live_persist(env, invocation_kind="flow", artifacts_path=str(artifacts_dir))
    ctx = env._live_persist
    assert ctx is not None

    assignments = [
        TaskAssignment(task="do first", assignee="worker"),
        TaskAssignment(task="do last (dies)", assignee="reviewer"),
    ]
    plan_result = _PlanResult(
        assignments=assignments,
        agent_ids=["first", "last"],
        dep_indices=[[], [0]],
        pool=[],
        budget_preambles={},
    )
    dag_state = _DagState(
        node_ids=["node-0", "node-1"],
        known_nodes={"node-0", "node-1"},
        deps_by_node={"node-0": [], "node-1": ["node-0"]},
        reactive=False,
        spawn_roles=None,
        role_base={},
        worker_models=["codex/gpt-5.5", "codex/gpt-5.5"],
    )

    from lionagi.engines import PlanningEngine

    async def _run_dag_result():
        return {
            "operation_results": {
                "node-0": "first ok",
                "node-1": {
                    "error": (
                        "Failed to stream API call: CLI subprocess exited "
                        "with code 1 and wrote nothing to stderr"
                    )
                },
            },
            "spawned_operations": 0,
            "escalated_operations": [],
            "failed_operations": ["node-1"],
        }

    fake_engine_run = MagicMock()
    fake_engine_run.run_dag = MagicMock(return_value=_run_dag_result())

    with patch.object(PlanningEngine, "new_run", return_value=fake_engine_run):
        await _execute_dag(env, plan_result, dag_state, max_concurrent=1, max_ops=0)

    assert env._failed_operation_evidence == [
        {"kind": "failed_operation", "id": "last", "label": "reviewer"}
    ]
    # An escalation-free, ordinary FAILED node must not also read as an
    # escalation -- these are different failure modes with different reasons.
    assert getattr(env, "_escalated_evidence", None) is None

    await stop_live_persist(env, status="completed")

    async with StateDB() as db:
        s = await db.get_session(ctx["session_id"])
    assert s is not None
    assert s["status"] == "failed"
    assert s["status_reason_code"] == "run.failed.exception"

    import json as _json

    evidence = s["status_evidence_refs"]
    evidence = _json.loads(evidence) if isinstance(evidence, str) else evidence
    assert any(e.get("id") == "last" for e in evidence)


async def test_dead_terminal_node_flips_completed_to_failed_end_to_end(
    temp_db_path: Path,
    tmp_path: Path,
):
    """True end-to-end repro of a flow whose terminal node dies: a two-node
    DAG (first -> last) run through the REAL DependencyAwareExecutor, where
    the terminal node raises exactly the kind of error a dead CLI subprocess
    produces. Before the fix, this session's own status ended 'completed'
    even though its terminal node never produced a result -- indistinguishable
    from a run that actually passed its gate."""
    from unittest.mock import MagicMock, patch

    from lionagi.casts.emission import TaskAssignment
    from lionagi.cli.orchestrate.flow import _DagState, _execute_dag, _PlanResult
    from lionagi.operations.builder import OperationGraphBuilder
    from lionagi.operations.flow import flow as _real_flow

    env = _minimal_env()
    artifacts_dir = tmp_path / "artifacts"
    await start_live_persist(env, invocation_kind="flow", artifacts_path=str(artifacts_dir))
    ctx = env._live_persist
    assert ctx is not None

    async def first(**kw):
        return "first ok"

    async def last(**kw):
        raise RuntimeError(
            "Failed to stream API call: CLI subprocess exited with code 1 "
            "and wrote nothing to stderr"
        )

    env.session.register_operation("first", first)
    env.session.register_operation("last", last)

    builder = OperationGraphBuilder()
    first_id = builder.add_operation("first", depends_on=[])
    last_id = builder.add_operation("last", depends_on=[first_id])
    graph = builder.get_graph()

    assignments = [
        TaskAssignment(task="do first", assignee="worker"),
        TaskAssignment(task="do last (dies)", assignee="reviewer"),
    ]
    plan_result = _PlanResult(
        assignments=assignments,
        agent_ids=["first", "last"],
        dep_indices=[[], [0]],
        pool=[],
        budget_preambles={},
    )
    dag_state = _DagState(
        node_ids=[first_id, last_id],
        known_nodes={first_id, last_id},
        deps_by_node={first_id: [], last_id: ["first"]},
        reactive=False,
        spawn_roles=None,
        role_base={},
        worker_models=["codex/gpt-5.5", "codex/gpt-5.5"],
    )

    from lionagi.engines import PlanningEngine

    async def _run_dag_result():
        # The real executor, not a hand-built dict -- proves the rollup
        # itself end-to-end, not just the _execute_dag plumbing around it.
        return await _real_flow(env.session, graph, parallel=False, verbose=False)

    fake_engine_run = MagicMock()
    fake_engine_run.run_dag = MagicMock(return_value=_run_dag_result())

    with patch.object(PlanningEngine, "new_run", return_value=fake_engine_run):
        await _execute_dag(env, plan_result, dag_state, max_concurrent=1, max_ops=0)

    assert env._failed_operation_evidence == [
        {"kind": "failed_operation", "id": "last", "label": "reviewer"}
    ]

    await stop_live_persist(env, status="completed")

    async with StateDB() as db:
        s = await db.get_session(ctx["session_id"])
    assert s is not None
    assert s["status"] == "failed", "a dead terminal node must not read as a clean completion"
    assert s["status_reason_code"] == "run.failed.exception"


# A planned node that never produced a result at all -- absent from
# operation_results, not in failed_operations (no invoke() ever raised) and
# not in skipped_operations (no edge condition short-circuited it) -- read as
# an ordinary "(no response)" entry with the run's own status untouched.
# These tests pin the reconciliation between the fixed initial plan
# (dag_state.node_ids) and what the executor actually observed.


async def test_execute_dag_records_lost_operation_evidence_for_missing_result(
    temp_db_path: Path,
    tmp_path: Path,
):
    """A two-node plan where only node-0 shows up in operation_results (the
    executor never produced a result -- completed, failed, or skipped -- for
    node-1) must be named in env._failed_operation_evidence so the run fails
    loud instead of reading as run.completed.ok."""
    from unittest.mock import MagicMock, patch

    from lionagi.casts.emission import TaskAssignment
    from lionagi.cli.orchestrate.flow import _DagState, _execute_dag, _PlanResult

    env = _minimal_env()
    artifacts_dir = tmp_path / "artifacts"
    await start_live_persist(env, invocation_kind="flow", artifacts_path=str(artifacts_dir))
    ctx = env._live_persist
    assert ctx is not None

    assignments = [
        TaskAssignment(task="do first", assignee="worker"),
        TaskAssignment(task="do last (never runs)", assignee="reviewer"),
    ]
    plan_result = _PlanResult(
        assignments=assignments,
        agent_ids=["first", "last"],
        dep_indices=[[], [0]],
        pool=[],
        budget_preambles={},
    )
    dag_state = _DagState(
        node_ids=["node-0", "node-1"],
        known_nodes={"node-0", "node-1"},
        deps_by_node={"node-0": [], "node-1": ["node-0"]},
        reactive=False,
        spawn_roles=None,
        role_base={},
        worker_models=["codex/gpt-5.5", "codex/gpt-5.5"],
    )

    from lionagi.engines import PlanningEngine

    async def _run_dag_result():
        return {
            "operation_results": {"node-0": "first ok"},
            "spawned_operations": 0,
            "escalated_operations": [],
            "failed_operations": [],
            "skipped_operations": [],
        }

    fake_engine_run = MagicMock()
    fake_engine_run.run_dag = MagicMock(return_value=_run_dag_result())

    with patch.object(PlanningEngine, "new_run", return_value=fake_engine_run):
        await _execute_dag(env, plan_result, dag_state, max_concurrent=1, max_ops=0)

    assert env._failed_operation_evidence == [
        {"kind": "lost_operation", "id": "last", "label": "reviewer"}
    ]
    assert getattr(env, "_escalated_evidence", None) is None

    await stop_live_persist(env, status="completed")

    async with StateDB() as db:
        s = await db.get_session(ctx["session_id"])
    assert s is not None
    assert s["status"] == "failed", "a missing planned node must not read as a clean completion"
    assert s["status_reason_code"] == "run.failed.exception"


async def test_execute_dag_skipped_node_does_not_trip_lost_operation_evidence(
    temp_db_path: Path,
    tmp_path: Path,
):
    """node-1 absent from operation_results is NOT a lost node when the
    executor named it in skipped_operations -- an ordinary edge-condition
    skip is a legitimate, non-failing outcome and must not fail the run."""
    from unittest.mock import MagicMock, patch

    from lionagi.casts.emission import TaskAssignment
    from lionagi.cli.orchestrate.flow import _DagState, _execute_dag, _PlanResult

    env = _minimal_env()
    artifacts_dir = tmp_path / "artifacts"
    await start_live_persist(env, invocation_kind="flow", artifacts_path=str(artifacts_dir))

    assignments = [
        TaskAssignment(task="do first", assignee="worker"),
        TaskAssignment(task="skip me", assignee="reviewer"),
    ]
    plan_result = _PlanResult(
        assignments=assignments,
        agent_ids=["first", "skipped"],
        dep_indices=[[], [0]],
        pool=[],
        budget_preambles={},
    )
    dag_state = _DagState(
        node_ids=["node-0", "node-1"],
        known_nodes={"node-0", "node-1"},
        deps_by_node={"node-0": [], "node-1": ["node-0"]},
        reactive=False,
        spawn_roles=None,
        role_base={},
        worker_models=["codex/gpt-5.5", "codex/gpt-5.5"],
    )

    from lionagi.engines import PlanningEngine

    async def _run_dag_result():
        return {
            "operation_results": {"node-0": "first ok"},
            "spawned_operations": 0,
            "escalated_operations": [],
            "failed_operations": [],
            "skipped_operations": ["node-1"],
        }

    fake_engine_run = MagicMock()
    fake_engine_run.run_dag = MagicMock(return_value=_run_dag_result())

    with patch.object(PlanningEngine, "new_run", return_value=fake_engine_run):
        await _execute_dag(env, plan_result, dag_state, max_concurrent=1, max_ops=0)

    assert not getattr(env, "_failed_operation_evidence", None)

    await stop_live_persist(env, status="completed")


async def test_execute_dag_dropped_spawn_does_not_trip_lost_operation_evidence(
    temp_db_path: Path,
    tmp_path: Path,
):
    """A reactively spawned node that got dropped (budget/cycle refusal) was
    never a planned node -- it must not surface as a lost planned node just
    because its id shows up somewhere in the DAG result. Reconciliation is
    scoped to dag_state.node_ids only."""
    from unittest.mock import MagicMock, patch

    from lionagi.casts.emission import TaskAssignment
    from lionagi.cli.orchestrate.flow import _DagState, _execute_dag, _PlanResult

    env = _minimal_env()
    artifacts_dir = tmp_path / "artifacts"
    await start_live_persist(env, invocation_kind="flow", artifacts_path=str(artifacts_dir))

    assignments = [
        TaskAssignment(task="do first", assignee="worker"),
        TaskAssignment(task="do last", assignee="reviewer"),
    ]
    plan_result = _PlanResult(
        assignments=assignments,
        agent_ids=["first", "last"],
        dep_indices=[[], [0]],
        pool=[],
        budget_preambles={},
    )
    dag_state = _DagState(
        node_ids=["node-0", "node-1"],
        known_nodes={"node-0", "node-1"},
        deps_by_node={"node-0": [], "node-1": ["node-0"]},
        reactive=True,
        spawn_roles=None,
        role_base={},
        worker_models=["codex/gpt-5.5", "codex/gpt-5.5"],
    )

    from lionagi.engines import PlanningEngine

    async def _run_dag_result():
        return {
            "operation_results": {"node-0": "first ok", "node-1": "last ok"},
            "spawned_operations": 0,
            "escalated_operations": [],
            "failed_operations": [],
            "skipped_operations": [],
            "dropped_spawns": [
                {"reason": "max_spawn_exceeded", "assignee": "reviewer", "op_id": "spawn-99"}
            ],
        }

    fake_engine_run = MagicMock()
    fake_engine_run.run_dag = MagicMock(return_value=_run_dag_result())

    with patch.object(PlanningEngine, "new_run", return_value=fake_engine_run):
        await _execute_dag(env, plan_result, dag_state, max_concurrent=1, max_ops=0)

    assert not getattr(env, "_failed_operation_evidence", None)

    await stop_live_persist(env, status="completed")


async def test_unknown_spawn_assignee_reaches_terminal_failure(
    temp_db_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    import json as _json

    from lionagi.casts.emission import SpawnRequest, TaskAssignment
    from lionagi.cli.orchestrate import flow as flow_mod
    from lionagi.cli.orchestrate.flow import _DagState, _execute_dag, _PlanResult
    from lionagi.operations.builder import OperationGraphBuilder

    branch = Branch(name="architect")
    env = _minimal_env(branch)
    env.run.agent_artifact_dir.side_effect = lambda agent_id: tmp_path / agent_id
    env.builder = OperationGraphBuilder()
    node_id = env.builder.add_operation("spawner", depends_on=[])

    async def spawner(**_kwargs):
        return SpawnRequest(instruction="missing work", assignee="ghost", independent=True)

    env.session.register_operation("spawner", spawner)
    await start_live_persist(
        env,
        invocation_kind="flow",
        artifacts_path=str(tmp_path / "artifacts"),
    )
    ctx = env._live_persist
    assert ctx is not None

    plan_result = _PlanResult(
        assignments=[TaskAssignment(task="spawn missing work", assignee="architect")],
        agent_ids=["architect"],
        dep_indices=[[]],
        pool=[],
        budget_preambles={},
    )
    dag_state = _DagState(
        node_ids=[node_id],
        known_nodes={node_id},
        deps_by_node={node_id: []},
        reactive=True,
        spawn_roles=None,
        role_base={"architect": branch},
        worker_models=["test/model"],
    )
    emitted: list[str] = []
    monkeypatch.setattr(flow_mod, "progress", emitted.append)
    expected_error = "SpawnRequest assignee 'ghost' is not a recognized role (known: ['architect'])"
    expected_evidence = [{"kind": "unroutable_spawn", "id": "ghost", "label": expected_error}]

    try:
        result = await _execute_dag(env, plan_result, dag_state, max_concurrent=1, max_ops=2)
        assert result.n_spawned == 0
        assert env._failed_operation_evidence == expected_evidence
        assert any(
            "SPAWN REJECTED" in line and "ghost" in line and expected_error in line
            for line in emitted
        )
    finally:
        final_status = await stop_live_persist(env, status="completed")

    assert final_status == "failed"
    async with StateDB() as db:
        session_row = await db.get_session(ctx["session_id"])
    assert session_row is not None
    assert session_row["status"] == "failed"
    assert session_row["status_reason_code"] == "run.failed.exception"
    evidence_refs = session_row["status_evidence_refs"]
    if isinstance(evidence_refs, str):
        evidence_refs = _json.loads(evidence_refs)
    assert evidence_refs == expected_evidence


async def test_execute_dag_cancelled_spawn_records_lost_operation_evidence(
    temp_db_path: Path,
    tmp_path: Path,
):
    """A reactively accepted spawn whose EventStatus reaches CANCELLED
    before producing a result is absent from operation_results,
    failed_operations, AND skipped_operations -- it was genuinely accepted
    into the graph (unlike a dropped_spawns entry), so spawned_operations
    (a count) reports it but none of the outcome sets do. Reconciling the
    executor's spawned_ids roster -- the accept-time roster of every node
    _accept_node actually let in, real ReactiveExecutor state rather than a
    re-derived counter -- against operation_results/failed/skipped/escalated
    must still name it as lost, closing the same false-success class as a
    missing planned node."""
    from unittest.mock import MagicMock, patch

    from lionagi.casts.emission import TaskAssignment
    from lionagi.cli.orchestrate.flow import _DagState, _execute_dag, _PlanResult
    from lionagi.operations.builder import OperationGraphBuilder
    from lionagi.operations.node import create_operation
    from lionagi.protocols.generic.event import EventStatus

    env = _minimal_env()
    artifacts_dir = tmp_path / "artifacts"
    await start_live_persist(env, invocation_kind="flow", artifacts_path=str(artifacts_dir))
    ctx = env._live_persist
    assert ctx is not None

    # A real Graph carrying the accepted-then-cancelled spawn, exactly what
    # env.builder.get_graph() would hold after a genuine ReactiveExecutor
    # run accepted this node via _accept_node before it hit a terminal
    # CANCELLED status with no response.
    builder = OperationGraphBuilder()
    cancelled_child = create_operation("follow_up", parameters={})
    cancelled_child.execution.status = EventStatus.CANCELLED
    cancelled_child.metadata["spawn_id"] = "spawn-1"
    cancelled_child.metadata["assignee"] = "reviewer"
    builder.graph.add_node(cancelled_child)
    env.builder = builder
    cancelled_id = str(cancelled_child.id)

    assignments = [TaskAssignment(task="do first", assignee="worker")]
    plan_result = _PlanResult(
        assignments=assignments,
        agent_ids=["first"],
        dep_indices=[[]],
        pool=[],
        budget_preambles={},
    )
    dag_state = _DagState(
        node_ids=["node-0"],
        known_nodes={"node-0"},
        deps_by_node={"node-0": []},
        reactive=True,
        spawn_roles=None,
        role_base={},
        worker_models=["codex/gpt-5.5"],
    )

    from lionagi.engines import PlanningEngine

    async def _run_dag_result():
        return {
            "operation_results": {"node-0": "first ok"},
            "spawned_operations": 1,
            "spawned_ids": [cancelled_id],
            "escalated_operations": [],
            "failed_operations": [],
            "skipped_operations": [],
            "dropped_spawns": [],
        }

    fake_engine_run = MagicMock()
    fake_engine_run.run_dag = MagicMock(return_value=_run_dag_result())

    with patch.object(PlanningEngine, "new_run", return_value=fake_engine_run):
        await _execute_dag(env, plan_result, dag_state, max_concurrent=1, max_ops=0)

    assert env._failed_operation_evidence == [
        {"kind": "lost_operation", "id": "spawn-1", "label": "spawn-1"}
    ]

    await stop_live_persist(env, status="completed")

    async with StateDB() as db:
        s = await db.get_session(ctx["session_id"])
    assert s is not None
    assert s["status"] == "failed", (
        "a cancelled-but-accepted spawn must not read as a clean completion"
    )
    assert s["status_reason_code"] == "run.failed.exception"


async def test_execute_dag_reinjected_initial_node_yields_one_lost_operation_entry(
    temp_db_path: Path,
    tmp_path: Path,
):
    """One level up from the executor-level regression: an on_op_complete
    hook that re-injects the SAME already-cancelled initial node through the
    public inject() API used to pollute spawned_ids with that node's own id
    (_accept_node bumped the roster/counter even though the node was already
    in the graph, not newly added). With that pollution, the lost node was
    named twice -- once by the fixed-size planned-node check
    (dag_state.node_ids, by plan index) and once by the spawned_ids check (by
    id) -- since neither check excludes ids the other has already claimed.
    Fixed at the source (_accept_node no longer counts a re-injected existing
    node as a spawn), so only the planned-node check fires."""
    from unittest.mock import MagicMock, patch

    from lionagi.casts.emission import TaskAssignment
    from lionagi.cli.orchestrate.flow import _DagState, _execute_dag, _PlanResult
    from lionagi.operations.builder import OperationGraphBuilder
    from lionagi.operations.flow import flow as _real_flow
    from lionagi.protocols.generic.event import EventStatus

    env = _minimal_env()
    artifacts_dir = tmp_path / "artifacts"
    await start_live_persist(env, invocation_kind="flow", artifacts_path=str(artifacts_dir))
    ctx = env._live_persist
    assert ctx is not None

    async def spawner(**kw):
        return "should not run"

    env.session.register_operation("spawner", spawner)

    builder = OperationGraphBuilder()
    spawner_id = builder.add_operation("spawner", depends_on=[])
    graph = builder.get_graph()
    env.builder = builder
    initial_op = next(iter(graph.internal_nodes.values()))
    initial_op.execution.status = EventStatus.CANCELLED

    assignments = [TaskAssignment(task="do it", assignee="worker")]
    plan_result = _PlanResult(
        assignments=assignments,
        agent_ids=["first"],
        dep_indices=[[]],
        pool=[],
        budget_preambles={},
    )
    dag_state = _DagState(
        node_ids=[spawner_id],
        known_nodes={spawner_id},
        deps_by_node={spawner_id: []},
        reactive=True,
        spawn_roles=None,
        role_base={},
        worker_models=["codex/gpt-5.5"],
    )

    from lionagi.engines import PlanningEngine

    executor_ref: dict = {}
    injected_once = {"done": False}

    def on_op_complete(node):
        if injected_once["done"]:
            return
        injected_once["done"] = True
        # Re-inject the SAME already-in-graph, already-cancelled initial
        # node through the public inject() API.
        executor_ref["executor"].inject(node, independent=True)

    async def _run_dag_result():
        # The real executor, not a hand-built dict -- proves the rollup
        # against genuine ReactiveExecutor state, not just the reconciliation
        # plumbing around it.
        return await _real_flow(
            env.session,
            graph,
            reactive=True,
            executor_ref=executor_ref,
            on_op_complete=on_op_complete,
        )

    fake_engine_run = MagicMock()
    fake_engine_run.run_dag = MagicMock(return_value=_run_dag_result())

    with patch.object(PlanningEngine, "new_run", return_value=fake_engine_run):
        await _execute_dag(env, plan_result, dag_state, max_concurrent=1, max_ops=0)

    lost_entries = [
        e for e in (env._failed_operation_evidence or []) if e["kind"] == "lost_operation"
    ]
    assert len(lost_entries) == 1

    await stop_live_persist(env, status="completed")


async def test_execute_dag_run_level_cancellation_skips_reconciliation_entirely(
    temp_db_path: Path,
    tmp_path: Path,
):
    """A user-killed run (run_dag itself raises CancelledError, not a
    per-node terminal status) must not additionally accrue lost-node/
    lost-spawn evidence -- the whole evidence block, including both
    reconciliation checks, sits after the awaited run_dag call and is never
    reached when that call raises instead of returning."""
    from unittest.mock import MagicMock, patch

    from lionagi.casts.emission import TaskAssignment
    from lionagi.cli.orchestrate.flow import _DagState, _execute_dag, _PlanResult

    env = _minimal_env()
    artifacts_dir = tmp_path / "artifacts"
    await start_live_persist(env, invocation_kind="flow", artifacts_path=str(artifacts_dir))

    assignments = [TaskAssignment(task="do first", assignee="worker")]
    plan_result = _PlanResult(
        assignments=assignments,
        agent_ids=["first"],
        dep_indices=[[]],
        pool=[],
        budget_preambles={},
    )
    dag_state = _DagState(
        node_ids=["node-0"],
        known_nodes={"node-0"},
        deps_by_node={"node-0": []},
        reactive=True,
        spawn_roles=None,
        role_base={},
        worker_models=["codex/gpt-5.5"],
    )

    from lionagi.engines import PlanningEngine

    async def _run_dag_raises():
        raise asyncio.CancelledError()

    fake_engine_run = MagicMock()
    fake_engine_run.run_dag = MagicMock(return_value=_run_dag_raises())

    with patch.object(PlanningEngine, "new_run", return_value=fake_engine_run):
        with pytest.raises(asyncio.CancelledError):
            await _execute_dag(env, plan_result, dag_state, max_concurrent=1, max_ops=0)

    assert not getattr(env, "_failed_operation_evidence", None)

    await stop_live_persist(env, status="cancelled")


async def test_execute_dag_records_control_log_write_failure_when_update_session_raises(
    temp_db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    """A control-log metadata write (``update_session()``) that raises used to
    be swallowed by a bare ``contextlib.suppress(Exception)`` inside the
    fire-and-forget task -- the applied control was recorded in memory but the
    write that was supposed to persist it silently vanished. It must now be
    logged instead of dropped."""
    from types import SimpleNamespace
    from unittest.mock import patch

    from lionagi.casts.emission import TaskAssignment
    from lionagi.cli.orchestrate import flow as flow_module
    from lionagi.cli.orchestrate.flow import _DagState, _execute_dag, _PlanResult
    from lionagi.engines import PlanningEngine

    monkeypatch.setattr(flow_module, "_CONTROL_POLL_INTERVAL", 0.01)

    env = _minimal_env()
    await start_live_persist(env, invocation_kind="flow")
    ctx = env._live_persist
    assert ctx is not None

    calls = {"n": 0}

    async def _list_pending(session_id):
        calls["n"] += 1
        if calls["n"] == 1:
            return [{"id": "control-1", "verb": "pause"}]
        return []

    real_update_session = ctx["db"].update_session

    async def _raising_update_session(session_id, **kwargs):
        if "node_metadata" in kwargs:
            raise RuntimeError("db unavailable")
        return await real_update_session(session_id, **kwargs)

    # The control-log write goes through the atomic merge, not a whole-column
    # update_session(). Both are faulted so the test pins "a failed write is
    # logged" rather than pinning which call carries the write.
    async def _raising_merge(session_id, patch):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(ctx["db"], "list_pending_session_controls", _list_pending)
    monkeypatch.setattr(ctx["db"], "update_session", _raising_update_session)
    monkeypatch.setattr(ctx["db"], "merge_session_node_metadata", _raising_merge)

    async def _stub_apply_session_control(db, executor, row):
        return "applied"

    monkeypatch.setattr(flow_module, "_apply_session_control", _stub_apply_session_control)

    plan_result = _PlanResult(
        assignments=[TaskAssignment(task="do it", assignee="worker")],
        agent_ids=["worker"],
        dep_indices=[[]],
        pool=[],
        budget_preambles={},
    )
    dag_state = _DagState(
        node_ids=["node-0"],
        known_nodes={"node-0"},
        deps_by_node={"node-0": []},
        reactive=False,
        spawn_roles=None,
        role_base={},
        worker_models=["test/model"],
    )

    async def run_dag(graph, **kwargs):
        kwargs["executor_ref"]["executor"] = object()
        # Long enough for the (fast-forwarded) control poller to tick at
        # least once before this returns.
        await asyncio.sleep(0.1)
        return {"operation_results": {}, "spawned_operations": 0, "escalated_operations": []}

    engine_run = SimpleNamespace(run_dag=run_dag)
    with (
        patch.object(PlanningEngine, "new_run", return_value=engine_run),
        caplog.at_level("WARNING", logger="lionagi.cli.orchestrate.flow"),
    ):
        await _execute_dag(env, plan_result, dag_state, max_concurrent=1, max_ops=0)

    await stop_live_persist(env, status="completed")

    assert any(
        "control-log metadata write failed" in record.message for record in caplog.records
    ), "a raising control-log write must be logged, not silently dropped"


async def test_execute_dag_bounds_control_log_drain_on_hanging_update_session(
    temp_db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """The same drain, but ``update_session()`` hangs instead of raising.
    Before the fix, the finally block's plain ``gather()`` over the
    control-log tasks had no bound: a hung write meant ``_execute_dag`` never
    returned, teardown never ran, and the live database stayed open forever.
    The drain must now give the write a grace period, then cancel and
    abandon it -- completing in bounded time and recording that it did."""
    from types import SimpleNamespace
    from unittest.mock import patch

    from lionagi.casts.emission import TaskAssignment
    from lionagi.cli.orchestrate import flow as flow_module
    from lionagi.cli.orchestrate.flow import _DagState, _execute_dag, _PlanResult
    from lionagi.engines import PlanningEngine

    monkeypatch.setattr(flow_module, "_CONTROL_POLL_INTERVAL", 0.01)

    env = _minimal_env()
    await start_live_persist(env, invocation_kind="flow")
    ctx = env._live_persist
    assert ctx is not None

    calls = {"n": 0}

    async def _list_pending(session_id):
        calls["n"] += 1
        if calls["n"] == 1:
            return [{"id": "control-1", "verb": "pause"}]
        return []

    real_update_session = ctx["db"].update_session
    write_started = asyncio.Event()
    write_cancelled = asyncio.Event()

    async def _hanging_update_session(session_id, **kwargs):
        if "node_metadata" not in kwargs:
            return await real_update_session(session_id, **kwargs)
        write_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            write_cancelled.set()
            raise

    # Same reason as the raising variant: the control-log write is an atomic
    # merge, so that is the call that has to hang for the drain to be exercised.
    real_merge = ctx["db"].merge_session_node_metadata

    async def _hanging_merge(session_id, patch):
        write_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            write_cancelled.set()
            raise

    monkeypatch.setattr(ctx["db"], "list_pending_session_controls", _list_pending)
    monkeypatch.setattr(ctx["db"], "update_session", _hanging_update_session)
    monkeypatch.setattr(ctx["db"], "merge_session_node_metadata", _hanging_merge)

    async def _stub_apply_session_control(db, executor, row):
        return "applied"

    monkeypatch.setattr(flow_module, "_apply_session_control", _stub_apply_session_control)

    warnings: list[str] = []
    monkeypatch.setattr(flow_module, "_warn", warnings.append)

    plan_result = _PlanResult(
        assignments=[TaskAssignment(task="do it", assignee="worker")],
        agent_ids=["worker"],
        dep_indices=[[]],
        pool=[],
        budget_preambles={},
    )
    dag_state = _DagState(
        node_ids=["node-0"],
        known_nodes={"node-0"},
        deps_by_node={"node-0": []},
        reactive=False,
        spawn_roles=None,
        role_base={},
        worker_models=["test/model"],
    )

    async def run_dag(graph, **kwargs):
        kwargs["executor_ref"]["executor"] = object()
        # Hangs until _execute_dag's own task is cancelled from outside --
        # by that point the control-log write is already in flight.
        await write_started.wait()
        await asyncio.Event().wait()

    engine_run = SimpleNamespace(run_dag=run_dag)
    with patch.object(PlanningEngine, "new_run", return_value=engine_run):
        execute_task = asyncio.create_task(
            _execute_dag(env, plan_result, dag_state, max_concurrent=1, max_ops=0)
        )
        await write_started.wait()
        execute_task.cancel()

        start = asyncio.get_event_loop().time()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(execute_task, timeout=8)
        elapsed = asyncio.get_event_loop().time() - start

    try:
        assert elapsed < 6, f"teardown took {elapsed}s -- the control-log drain is unbounded again"
        assert write_cancelled.is_set(), (
            "the hung control-log write must be cancelled during teardown, not left running"
        )
        assert any("cancelled" in w and "control-log-metadata" in w for w in warnings), (
            "a cancelled-after-timeout metadata write must be recorded through the "
            f"caller-visible warning sink, not dropped silently; got {warnings!r}"
        )
    finally:
        # _teardown_common's final metadata write also reaches the db whenever
        # extras is non-empty, which it is here (the control log): once through
        # update_session() and once through merge_session_node_metadata(). Neither
        # hanging double may still be wired for those calls or this cleanup step
        # hangs too. This must run even if an assertion above fails, or a hanging
        # double stays wired and later cleanup hangs.
        monkeypatch.setattr(ctx["db"], "update_session", real_update_session)
        monkeypatch.setattr(ctx["db"], "merge_session_node_metadata", real_merge)
        await stop_live_persist(env, status="completed")
