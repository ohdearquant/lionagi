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

# ── Fixtures ──────────────────────────────────────────────────────────────────


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


# ── start_live_persist: happy path + invariants ───────────────────────────────


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


# ── start_live_persist: failure path closes the DB ────────────────────────────


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


# ── _register_branch_hook: lazy branch row + multi-message paths ──────────────


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


# ── stop_live_persist: invariants ─────────────────────────────────────────────


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


async def test_stop_with_none_context_is_noop(temp_db_path: Path):
    """If start failed, env._live_persist is None and stop is a no-op."""
    env = _minimal_env()
    # No start_live_persist call.
    assert env._live_persist is None
    await stop_live_persist(env, status="completed")  # MUST NOT raise


# ── End-to-end: no aiosqlite thread leak ──────────────────────────────────────


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


# ── lazy _ensure_branch_row retry after first-init failure ───────────────────


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


# ── finalize_orchestration() and stop_live_persist() DAG paths ───────────────


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


# ── Test 2.1 — finalize returns branch_ids and writes branch snapshots ─────────


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


# ── Test 2.2 — finalize stores dag extras for live persist teardown ────────────


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


# ── Test 2.3 — finalize emits resume hints for orchestrator and workers ────────


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


# ── Test 2.4 — snapshot write failure logs warning and continues ────────────────


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


# ── Test 2.5 — stop persists finalize extras without messages ─────────────────


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


# ── Test 2.6 — stop persists dag metadata and message bookmarks together ───────


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


# ── Test 2.7 — stop without finalize extras leaves node_metadata unchanged ────


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


# ── Test 2.8 — stop: get_progression failure logs and closes db ───────────────


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


# ── Test 2.9 — stop: close failure logs warning and clears context ────────────


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


# ── Test 2.10 — stop persists cancelled status with dag metadata ───────────────


async def test_stop_persists_cancelled_status_with_dag_metadata(
    temp_db_path: Path,
):
    env = _minimal_env()
    await start_live_persist(env)
    ctx = env._live_persist
    env._finalize_extras = dag_extras()

    await stop_live_persist(env, status="cancelled")

    async with StateDB() as db:
        s = await db.get_session(ctx["session_id"])

    assert s["status"] == "cancelled"
    assert_dag_and_identity(s["node_metadata"])
    assert s["ended_at"] is not None


# ── ADR-0064: artifact contract snapshot and verification ─────────────────────


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


# ── ADR-0064 + ADR-0057: session→invocation propagation on missing artifact ──
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


# ── bench545 regressions: plan-time per-leg wiring + escalation backstop ──────
#
# Both tests below reproduce the actual production gap (play fe9a23ac,
# artifacts under bench545): the play-level artifact_contract was NULL for
# the WHOLE run — nothing at plan time ever populated a per-leg contract, so
# the tests above (which hand-build a `contract` and pass it to
# start_live_persist directly) do not exercise the gap itself. These two
# start with NO contract, exactly like bench545, and drive the real
# _build_dag / _execute_dag phase functions to prove the wiring — not just a
# role-profile declaration nobody consults — is what closes it.


async def test_build_dag_wires_role_artifact_defaults_into_live_contract_and_fails_loud(
    temp_db_path: Path,
    tmp_path: Path,
):
    """A path: no whole-flow contract is declared (play-level NULL, as in
    bench545). The only source of a per-leg contract is the resolved
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
    assert ctx["artifact_contract"] is None  # bench545 starting state

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
        await _build_dag(env, "review this PR", plan_result, reactive_spec="off")

    # The live in-memory contract was extended during DAG build, before any
    # worker ran.
    merged = env._live_persist["artifact_contract"]
    assert merged is not None
    entry = next(e for e in merged["expected"] if e["id"] == "reviewer__review")
    assert entry["path"] == "reviewer/review.md"
    assert entry["required"] is True

    # ...and the session row itself carries it — the exact field bench545
    # showed stuck at NULL for the whole play.
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
