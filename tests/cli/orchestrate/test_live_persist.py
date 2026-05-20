# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""
Regression tests for ``lionagi.cli.orchestrate._orchestration``
live-persist functions: ``start_live_persist``, ``stop_live_persist``,
and the lazy ``_register_branch_hook`` path used by
``build_worker_branch``.

The orchestration shape differs from ``cli/agent.py`` in three ways:

1. The session row is created up-front, but each branch row is created
   LAZILY on the first message via ``_ensure_branch_row``. The flow /
   fanout patterns add worker branches AFTER ``start_live_persist``,
   so the lazy path is the common case.
2. Multiple branches share the same session-level progression.
3. The lazy ``_ensure_branch_row`` must fire exactly once per branch.

These tests use a temp file DB (not ``:memory:``) so aiosqlite's WAL
mode and non-daemon worker thread match production. No real API calls
are made.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from lionagi import Branch, Session
from lionagi.cli.orchestrate._orchestration import (
    OrchestrationEnv,
    _register_branch_hook,
    start_live_persist,
    stop_live_persist,
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
    """Build a stub OrchestrationEnv with just the fields these tests use.

    The full setup_orchestration path requires a model spec + provider
    setup; live-persist functions only touch ``env.session`` and
    ``env._live_persist``, so a stripped env keeps the test surface
    minimal and free of provider imports.
    """
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
    """start_live_persist must persist the session and register a hook
    on every branch already in session.branches (the orchestrator).
    """
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
    """If create_session fails during start, the DB is closed, the
    env._live_persist is set to None, and the aiosqlite worker is
    reclaimed.

    The orchestration hang would otherwise mirror agent.py's: a failed
    start leaves the connection open, the non-daemon worker prevents
    interpreter shutdown.
    """
    async def fail(self, session: dict):
        await self.db.execute("SELECT 1")
        raise RuntimeError("simulated DB failure")

    monkeypatch.setattr(StateDB, "create_session", fail)

    env = _minimal_env()
    before = _aiosqlite_thread_count()

    # Must NOT raise — start swallows the failure and logs it.
    await start_live_persist(env)

    assert env._live_persist is None
    # The orchestrator branch should not have a hook registered.
    assert env.orc_branch.on_message_added == []

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
    """The branch row + progression are created lazily on the FIRST
    message — not eagerly when the hook is registered. This matches the
    build_worker_branch path which runs from a sync context.
    """
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
        instruction="hi", sender="u", recipient=str(worker.id),
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
    """Multiple messages on the same branch must NOT re-create the row
    or re-insert the system message — ``_ensure_branch_row`` is gated
    by the ``initialized`` flag.
    """
    from lionagi.protocols.messages.manager import MessageManager

    env = _minimal_env()
    await start_live_persist(env)

    worker = Branch(name="worker-1", system="you are a worker")
    env.session.include_branches(worker)
    _register_branch_hook(env._live_persist, worker)
    hook = env._live_persist["hooks"][-1][1]

    msg1 = MessageManager.create_instruction(
        instruction="a", sender="u", recipient=str(worker.id),
    )
    msg2 = MessageManager.create_instruction(
        instruction="b", sender="u", recipient=str(worker.id),
    )
    await hook(msg1)
    await hook(msg2)

    async with StateDB() as db:
        # Branch row exists once.
        cur = await db.db.execute(
            "SELECT COUNT(*) AS n FROM branches WHERE id = ?", (str(worker.id),)
        )
        n_rows = (await cur.fetchone())["n"]
        # System message exists once.
        cur = await db.db.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE id = ?",
            (str(worker.system.id),),
        )
        n_sys = (await cur.fetchone())["n"]
        # Branch progression has both user messages.
        prog = await db.get_progression(
            env._live_persist["branch_prog_ids"][str(worker.id)]
        )

    assert n_rows == 1
    assert n_sys == 1
    assert str(msg1.id) in prog
    assert str(msg2.id) in prog

    await stop_live_persist(env, status="completed")


async def test_multiple_branches_share_session_progression(
    temp_db_path: Path,
):
    """Each worker has its OWN branch_prog, but ALL messages from ALL
    workers land in the SAME session_prog. This is how Studio renders
    a multi-branch flow as a single ordered timeline.
    """
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
        instruction="from-w1", sender="u", recipient=str(w1.id),
    )
    m2 = MessageManager.create_instruction(
        instruction="from-w2", sender="u", recipient=str(w2.id),
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
    """A failed DB write inside the hook must NOT abort the
    orchestration — it logs at WARNING and the in-memory message
    continues to flow through the user-facing turn.
    """
    import logging

    from lionagi.protocols.messages.manager import MessageManager

    env = _minimal_env()
    await start_live_persist(env)

    worker = Branch(name="worker-1")
    env.session.include_branches(worker)
    _register_branch_hook(env._live_persist, worker)
    hook = env._live_persist["hooks"][-1][1]

    async def boom(self, msg):
        raise RuntimeError("simulated busy timeout")

    monkeypatch.setattr(StateDB, "insert_message", boom)

    msg = MessageManager.create_instruction(
        instruction="hi", sender="u", recipient=str(worker.id),
    )
    with caplog.at_level(logging.WARNING, logger="lionagi.cli"):
        await hook(msg)  # MUST NOT raise

    assert any(
        "live persist write failed" in rec.message for rec in caplog.records
    )

    await stop_live_persist(env, status="completed")


async def test_hook_updates_system_msg_id_when_system_replaced(
    temp_db_path: Path,
):
    """If a worker's system message is replaced mid-run, the hook
    updates ``branches.system_msg_id`` to point at the new system.
    """
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
        instruction="warm up", sender="u", recipient=str(worker.id),
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
        instruction="a", sender="u", recipient=str(worker.id),
    )
    m2 = MessageManager.create_instruction(
        instruction="b", sender="u", recipient=str(worker.id),
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


async def test_stop_removes_all_duplicate_hook_registrations(
    temp_db_path: Path,
):
    """Duplicate registrations of the same hook (test/dev) must ALL be
    removed by stop_live_persist — otherwise a stale closed-DB hook
    would survive teardown.
    """
    env = _minimal_env()
    await start_live_persist(env)
    worker = Branch(name="worker-1")
    env.session.include_branches(worker)
    _register_branch_hook(env._live_persist, worker)
    hook = env._live_persist["hooks"][-1][1]
    # Append the same hook a second time on purpose.
    worker.on_message_added.append(hook)
    assert sum(1 for h in worker.on_message_added if h is hook) == 2

    await stop_live_persist(env, status="completed")

    assert sum(1 for h in worker.on_message_added if h is hook) == 0


async def test_stop_closes_db_even_if_bookmark_update_fails(
    temp_db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """If update_session raises during stop, the DB STILL closes
    (the close lives in its own ``finally``). Hang-fix invariant.
    """
    env = _minimal_env()
    await start_live_persist(env)
    db = env._live_persist["db"]

    async def boom(self, session_id, **kw):
        raise RuntimeError("simulated bookmark failure")

    monkeypatch.setattr(StateDB, "update_session", boom)

    before = _aiosqlite_thread_count()
    await stop_live_persist(env, status="completed")  # MUST NOT raise

    # Connection was closed.
    assert db._db is None
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
    """Run start + multi-branch + stop several times; aiosqlite worker
    count returns to baseline each cycle. Root-cause guard for the
    orchestration variant of the hang bug.
    """
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
            instruction="hi", sender="u", recipient=str(w.id),
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
