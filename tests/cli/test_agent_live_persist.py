# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Regression tests for setup_agent_persist / teardown_agent_persist: DB cleanup and no thread leaks."""

from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path
from unittest import mock

import pytest

from lionagi import Branch
from lionagi.cli._runs import setup_agent_persist as _setup_live_persist
from lionagi.cli._runs import teardown_agent_persist as _teardown_live_persist
from lionagi.state.db import StateDB

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def temp_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """File-backed per-test DB so aiosqlite WAL + non-daemon thread path is exercised."""
    db_path = tmp_path / "state.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", db_path)
    return db_path


def _aiosqlite_thread_count() -> int:
    """Live aiosqlite connection-worker threads — a leaked connection shows up here.

    Matched by the worker's run target ``_connection_worker_thread`` (stable across
    aiosqlite versions; current builds surface it in the auto thread name, and the
    ``_target`` qualname is the fallback) plus the legacy ``sqlite_<path>`` name prefix.
    """
    return sum(
        1
        for t in threading.enumerate()
        if "_connection_worker_thread" in t.name
        or t.name.startswith("sqlite")
        or getattr(getattr(t, "_target", None), "__name__", "") == "_connection_worker_thread"
    )


# ── _setup_live_persist: happy path + invariants ──────────────────────────────


async def test_setup_creates_session_branch_progression_rows(
    temp_db_path: Path,
):
    """Fresh setup creates session, branch, and progression rows and registers the message hook."""
    branch = Branch(name="b1")

    ctx = await _setup_live_persist(branch, agent_name="reviewer")

    assert ctx is not None
    assert ctx["db"] is not None
    assert ctx["session_id"]
    assert ctx["session_prog_id"]
    assert ctx["branch_prog_id"]
    assert ctx["existing_msg_ids"] == set()
    assert ctx["new_msg_ids"] == []
    # Persistence rides the hook bus (ADR-0047): the persist handler is on the
    # session bus, and the branch's emit hook drives MESSAGE_ADD.
    from lionagi.hooks.bus import HookPoint

    bus = branch._hooks
    assert bus is not None
    assert ctx["hook"] in bus.handlers_for(HookPoint.MESSAGE_ADD)
    assert sum(1 for h in bus.handlers_for(HookPoint.MESSAGE_ADD) if h is ctx["hook"]) == 1
    assert branch._persist_via_bus in branch.on_message_added

    # Spot-check the DB rows landed.
    async with StateDB() as db:
        s = await db.get_session(ctx["session_id"])
        b = await db.get_branch(str(branch.id))
    assert s is not None
    assert s["invocation_kind"] == "agent"
    assert s["agent_name"] == "reviewer"
    assert s["status"] == "running"
    assert b is not None
    assert b["session_id"] == ctx["session_id"]

    await _teardown_live_persist(ctx, status="completed")


async def test_setup_persists_system_message_when_branch_has_one(
    temp_db_path: Path,
):
    """Branch system message is inserted and branches.system_msg_id points at it."""
    branch = Branch(name="b1", system="you are a unit test")
    assert branch.system is not None
    sys_id = str(branch.system.id)

    ctx = await _setup_live_persist(branch)

    async with StateDB() as db:
        b = await db.get_branch(str(branch.id))
        m = await db.get_message(sys_id)
    assert b is not None
    assert b["system_msg_id"] == sys_id
    assert m is not None
    assert m["role"] == "system"

    await _teardown_live_persist(ctx, status="completed")


# ── _setup_live_persist: failure paths must close the DB ──────────────────────


async def test_setup_db_open_failure_disables_persist_no_thread_leak(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """db.open() raising → setup returns None and no aiosqlite worker thread leaks."""
    db_path = tmp_path / "state.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", db_path)

    async def boom(self):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(StateDB, "open", boom)

    before = _aiosqlite_thread_count()
    branch = Branch(name="b1")

    ctx = await _setup_live_persist(branch)

    assert ctx is None
    # The PERSISTENCE hook was NEVER registered when setup failed — only the
    # branch's baseline signal-emission hook (_schedule_emit) remains.
    assert branch.on_message_added == [branch._schedule_emit]
    # No leaked aiosqlite worker — the count is stable.
    assert _aiosqlite_thread_count() == before


async def test_setup_create_session_failure_closes_db(
    temp_db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """create_session failure mid-setup closes the DB so the aiosqlite worker thread is reclaimed."""
    original_create = StateDB.create_session

    async def fail(self, session: dict):
        # Touch the DB once so the engine is actually open,
        # then fail. Mirrors a real partial-failure scenario.
        await self.execute("SELECT 1")
        raise RuntimeError("simulated mid-setup failure")

    monkeypatch.setattr(StateDB, "create_session", fail)

    before = _aiosqlite_thread_count()
    branch = Branch(name="b1")

    ctx = await _setup_live_persist(branch)

    assert ctx is None
    # Only the baseline signal-emission hook remains; no persistence hook.
    assert branch.on_message_added == [branch._schedule_emit]
    # Give aiosqlite a moment to join its thread after close().
    for _ in range(20):
        if _aiosqlite_thread_count() == before:
            break
        await asyncio.sleep(0.05)
    assert _aiosqlite_thread_count() == before, (
        "DB was not closed on setup failure — aiosqlite worker leaked"
    )

    # Restore so other tests run clean.
    monkeypatch.setattr(StateDB, "create_session", original_create)


# ── Shared-db reuse + teardown cleanup (lifecycle-hook leak guard) ────────────


async def test_lifecycle_hooks_reuse_owned_db_no_shared_leak(temp_db_path: Path):
    """SESSION_START/BRANCH_CREATE hooks reuse the owned connection, and teardown
    leaves no shared StateDB whose non-daemon aiosqlite worker would block exit."""
    from lionagi.state.db import _SHARED, get_shared_db

    before = _aiosqlite_thread_count()
    branch = Branch(name="b1")

    ctx = await _setup_live_persist(branch, agent_name="reviewer")
    assert ctx is not None

    # The lifecycle hooks reach the db via get_shared_db(); setup registered the
    # owned connection, so they reuse it instead of opening a second one.
    assert await get_shared_db() is ctx["db"]
    assert _SHARED.get(ctx["db"].url) is ctx["db"]

    await _teardown_live_persist(ctx, status="completed")

    # Registry swept — nothing left to leak its aiosqlite worker thread.
    assert ctx["db"].url not in _SHARED
    for _ in range(20):
        if _aiosqlite_thread_count() == before:
            break
        await asyncio.sleep(0.05)
    assert _aiosqlite_thread_count() == before, (
        "shared StateDB survived teardown — aiosqlite worker thread leaked"
    )


async def test_register_shared_db_closes_displaced_instance(temp_db_path: Path):
    """Re-registering a path must close the prior instance, not orphan its worker thread."""
    from lionagi.state.db import (
        _SHARED,
        close_shared_db,
        register_shared_db,
        unregister_shared_db,
    )

    before = _aiosqlite_thread_count()
    db1 = StateDB(temp_db_path)
    await db1.open()
    await register_shared_db(db1)

    db2 = StateDB(temp_db_path)
    await db2.open()
    await register_shared_db(db2)  # displaces db1 → must close it

    assert _SHARED.get(db2.url) is db2
    assert db1._engine is None, "displaced StateDB was orphaned instead of closed"
    for _ in range(20):
        if _aiosqlite_thread_count() == before + 1:
            break
        await asyncio.sleep(0.05)
    assert _aiosqlite_thread_count() == before + 1, (
        "displaced connection's aiosqlite worker thread leaked"
    )

    # unregister is identity-guarded: dropping a stale handle must not evict the live one.
    unregister_shared_db(db1)
    assert _SHARED.get(db2.url) is db2

    await close_shared_db()
    await db2.close()
    for _ in range(20):
        if _aiosqlite_thread_count() == before:
            break
        await asyncio.sleep(0.05)
    assert _aiosqlite_thread_count() == before


async def test_close_shared_db_sweeps_inflight_open(
    temp_db_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """close_shared_db() must serialize with an in-flight get_shared_db() open so the
    just-opened connection is swept, not orphaned past teardown."""
    from lionagi.state.db import _SHARED, close_shared_db, get_shared_db

    before = _aiosqlite_thread_count()
    opened = asyncio.Event()
    release = asyncio.Event()
    real_open = StateDB.open

    async def slow_open(self):
        await real_open(self)  # real aiosqlite worker spawned
        opened.set()  # get_shared_db holds the open lock; db open, not yet stored
        await release.wait()

    monkeypatch.setattr(StateDB, "open", slow_open)

    opener = asyncio.create_task(get_shared_db(temp_db_path))
    await asyncio.wait_for(opened.wait(), timeout=5)

    closer = asyncio.create_task(close_shared_db())
    await asyncio.sleep(0.1)
    assert not closer.done(), "close_shared_db() did not wait for the in-flight open"

    release.set()
    opened_db = await asyncio.wait_for(opener, timeout=5)
    await asyncio.wait_for(closer, timeout=5)

    assert opened_db.url not in _SHARED
    assert opened_db._engine is None, "in-flight-opened StateDB survived close_shared_db()"
    for _ in range(20):
        if _aiosqlite_thread_count() == before:
            break
        await asyncio.sleep(0.05)
    assert _aiosqlite_thread_count() == before, (
        "in-flight-opened aiosqlite worker leaked past close_shared_db()"
    )


async def test_close_shared_db_rejects_stale_waiter(temp_db_path: Path):
    """A get_shared_db() that waited on a lock a concurrent close swept must refuse to
    resurrect the singleton (raise), not open a fresh worker that survives teardown."""
    from lionagi.state.db import _SHARED, close_shared_db, get_shared_db

    before = _aiosqlite_thread_count()
    await get_shared_db(temp_db_path)  # give the sweep something to slow-close

    entered = asyncio.Event()
    release = asyncio.Event()
    real_close = StateDB.close

    async def slow_close(self):
        entered.set()  # close now holds the open lock, mid-sweep
        await release.wait()
        await real_close(self)

    with mock.patch.object(StateDB, "close", slow_close):
        closer = asyncio.create_task(close_shared_db())
        await asyncio.wait_for(entered.wait(), timeout=5)

        getter = asyncio.create_task(get_shared_db(temp_db_path))
        await asyncio.sleep(0.1)
        assert not getter.done(), "getter should block on the lock the close holds"

        release.set()
        await asyncio.wait_for(closer, timeout=5)
        with pytest.raises(RuntimeError):
            await asyncio.wait_for(getter, timeout=5)

    assert temp_db_path not in _SHARED
    for _ in range(20):
        if _aiosqlite_thread_count() == before:
            break
        await asyncio.sleep(0.05)
    assert _aiosqlite_thread_count() == before, "stale-waiter open leaked an aiosqlite worker"


# ── Resume path ───────────────────────────────────────────────────────────────


async def test_setup_resume_loads_existing_session_and_progression(
    temp_db_path: Path,
):
    """Resume case: existing session/progression are reused and existing_msg_ids seeded for dedup."""
    branch = Branch(name="b1")
    ctx1 = await _setup_live_persist(branch)
    session_id_1 = ctx1["session_id"]
    branch_prog_id_1 = ctx1["branch_prog_id"]

    # Fire the hook for one message so the progression has content.
    # Build the message directly so we don't trip the sync add_message
    # async-hook guard.
    from lionagi.protocols.messages.manager import MessageManager

    msg = MessageManager.create_instruction(
        instruction="hello",
        sender="user",
        recipient=str(branch.id),
    )
    await ctx1["hook"](msg)
    await _teardown_live_persist(ctx1, status="completed")

    # Resume: same branch instance (id is preserved across teardown).
    ctx2 = await _setup_live_persist(branch)
    assert ctx2["session_id"] == session_id_1
    assert ctx2["branch_prog_id"] == branch_prog_id_1
    assert str(msg.id) in ctx2["existing_msg_ids"]
    await _teardown_live_persist(ctx2, status="completed")


# ── Hook contract: best-effort + system_msg_id update ─────────────────────────


async def test_hook_dedupes_existing_messages_on_resume(
    temp_db_path: Path,
):
    """On resume the hook must not re-append already-persisted message IDs to the progression."""
    from lionagi.protocols.messages.manager import MessageManager

    branch = Branch(name="b1")
    ctx1 = await _setup_live_persist(branch)
    msg = MessageManager.create_instruction(
        instruction="hello",
        sender="user",
        recipient=str(branch.id),
    )
    await ctx1["hook"](msg)
    await _teardown_live_persist(ctx1, status="completed")

    ctx2 = await _setup_live_persist(branch)
    # Re-fire the same message — simulates an idempotent replay.
    await ctx2["hook"](msg)

    async with StateDB() as db:
        ids = await db.get_progression(ctx2["branch_prog_id"])
    # Exactly one entry — the dedupe set kept the re-fire out.
    assert ids.count(str(msg.id)) == 1
    await _teardown_live_persist(ctx2, status="completed")


async def test_hook_persists_one_assistant_message_in_one_transaction(
    temp_db_path: Path,
):
    """The live hook commits one assistant message and its bookkeeping together."""
    from lionagi.protocols.messages.manager import MessageManager

    branch = Branch(name="b1")
    ctx = await _setup_live_persist(branch)
    msg = MessageManager.create_assistant_response(
        assistant_response="one transaction",
        recipient=str(branch.id),
    )
    statements: list[str] = []
    db = ctx["db"]
    async with db._read() as conn:
        raw_conn = await conn.get_raw_connection()
        await raw_conn.driver_connection.set_trace_callback(statements.append)
    try:
        await ctx["hook"](msg)
    finally:
        async with db._read() as conn:
            raw_conn = await conn.get_raw_connection()
            await raw_conn.driver_connection.set_trace_callback(None)

    tx_statements = [statement.strip().upper() for statement in statements]
    assert tx_statements.count("BEGIN IMMEDIATE") == 1
    assert tx_statements.count("COMMIT") == 1

    async with StateDB() as check_db:
        saved = await check_db.get_message(str(msg.id))
        branch_progression = await check_db.get_progression(ctx["branch_prog_id"])
        session_progression = await check_db.get_progression(ctx["session_prog_id"])
    assert saved is not None
    assert branch_progression == [str(msg.id)]
    assert session_progression == [str(msg.id)]

    await _teardown_live_persist(ctx, status="completed")


async def test_hook_swallows_db_write_failure(
    temp_db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    """DB write failure must not abort the turn; hook logs a WARNING and continues."""
    import logging

    from lionagi.protocols.messages.manager import MessageManager

    branch = Branch(name="b1")
    ctx = await _setup_live_persist(branch)

    # Monkeypatch the live-persist bundle to raise.
    async def boom(self, msg, **kwargs):
        raise RuntimeError("simulated busy timeout")

    monkeypatch.setattr(StateDB, "_persist_live_message", boom)

    msg = MessageManager.create_instruction(
        instruction="hi",
        sender="user",
        recipient=str(branch.id),
    )

    with caplog.at_level(logging.WARNING, logger="lionagi.cli"):
        # MUST NOT raise.
        await ctx["hook"](msg)

    assert any("live persist write failed" in rec.message for rec in caplog.records), (
        "expected WARNING log on hook DB-write failure"
    )

    await _teardown_live_persist(ctx, status="completed")


async def test_hook_retries_middle_transaction_failure_before_next_message(
    temp_db_path: Path,
):
    """A rolled-back message remains pending and commits before the next event."""
    from sqlalchemy import event

    branch = Branch(name="b1")
    ctx = await _setup_live_persist(branch)
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
        lost = await branch.msgs.a_add_message(instruction="lost")
    finally:
        event.remove(db._engine.sync_engine, "before_cursor_execute", fail_second_progression)

    assert await db.get_message(str(lost.id)) is None
    assert await db.get_progression(ctx["branch_prog_id"]) == []
    assert await db.get_progression(ctx["session_prog_id"]) == []
    assert ctx["message_retry_queues"][0].pending_count == 1
    assert ctx["new_msg_ids"] == []

    next_message = await branch.msgs.a_add_message(instruction="next")

    assert await db.get_progression(ctx["branch_prog_id"]) == [
        str(lost.id),
        str(next_message.id),
    ]
    assert await db.get_progression(ctx["session_prog_id"]) == [
        str(lost.id),
        str(next_message.id),
    ]
    assert await db.get_message(str(lost.id)) is not None
    assert await db.get_message(str(next_message.id)) is not None
    assert ctx["message_retry_queues"][0].pending_count == 0
    assert ctx["new_msg_ids"] == [str(lost.id), str(next_message.id)]

    await _teardown_live_persist(ctx, status="completed")


async def test_hook_preserves_order_when_first_queued_retry_fails_again(
    temp_db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A later event cannot bypass an earlier queued event that still fails."""
    from lionagi.protocols.messages.manager import MessageManager

    branch = Branch(name="b1")
    ctx = await _setup_live_persist(branch)
    original_persist = StateDB._persist_live_message
    attempted_ids: list[str] = []

    async def fail_persist(self, message, **kwargs):
        attempted_ids.append(message["id"])
        raise RuntimeError("simulated transient failure")

    monkeypatch.setattr(StateDB, "_persist_live_message", fail_persist)

    first = MessageManager.create_instruction(
        instruction="first",
        sender="user",
        recipient=str(branch.id),
    )
    await ctx["hook"](first)
    attempted_ids.clear()

    second = MessageManager.create_instruction(
        instruction="second",
        sender="user",
        recipient=str(branch.id),
    )
    await ctx["hook"](second)

    assert attempted_ids == [str(first.id)]
    assert ctx["message_retry_queues"][0].pending_count == 2
    async with StateDB() as db:
        assert await db.get_message(str(second.id)) is None
        assert await db.get_progression(ctx["branch_prog_id"]) == []

    monkeypatch.setattr(StateDB, "_persist_live_message", original_persist)
    await _teardown_live_persist(ctx, status="completed")


async def test_hook_updates_system_msg_id_when_system_replaced(
    temp_db_path: Path,
):
    """Hook must update branches.system_msg_id when the system message is replaced mid-run."""
    branch = Branch(name="b1", system="initial")
    ctx = await _setup_live_persist(branch)
    original_sys_id = str(branch.system.id)

    # Replace the system message — runtime path uses set_system /
    # add_message(system=...). Simulate by constructing a new System
    # and firing the hook with it.
    from lionagi.protocols.messages import System

    new_sys = System(content={"system_message": "replaced"}, sender="system")
    await ctx["hook"](new_sys)

    async with StateDB() as db:
        b = await db.get_branch(str(branch.id))
    assert b["system_msg_id"] == str(new_sys.id)
    assert b["system_msg_id"] != original_sys_id

    await _teardown_live_persist(ctx, status="completed")


# ── _teardown_live_persist: invariants ────────────────────────────────────────


async def test_teardown_updates_session_bookmarks_and_status(
    temp_db_path: Path,
):
    from lionagi.protocols.messages.manager import MessageManager

    branch = Branch(name="b1")
    ctx = await _setup_live_persist(branch)

    msg_a = MessageManager.create_instruction(
        instruction="a",
        sender="u",
        recipient=str(branch.id),
    )
    await ctx["hook"](msg_a)
    msg_b = MessageManager.create_instruction(
        instruction="b",
        sender="u",
        recipient=str(branch.id),
    )
    await ctx["hook"](msg_b)

    await _teardown_live_persist(ctx, status="completed")

    async with StateDB() as db:
        s = await db.get_session(ctx["session_id"])
    assert s["status"] == "completed"
    assert s["first_msg_id"] == str(msg_a.id)
    assert s["last_msg_id"] == str(msg_b.id)
    assert s["ended_at"] is not None


async def test_teardown_finalizes_branch_status_and_ended_at(
    temp_db_path: Path,
):
    """The single-branch agent path never gets branches.status written
    anywhere else (create_branch() doesn't set it and there was no
    terminal-status hook) — teardown's BRANCH_END emission is its only
    finalize."""
    branch = Branch(name="b1")
    ctx = await _setup_live_persist(branch)

    async with StateDB() as db:
        before = await db.get_branch(str(branch.id))
    assert before["status"] is None
    assert before["ended_at"] is None

    await _teardown_live_persist(ctx, status="completed")

    async with StateDB() as db:
        after = await db.get_branch(str(branch.id))
    assert after["status"] == "completed"
    assert after["ended_at"] is not None


async def test_teardown_finalizes_branch_status_failed_on_exception(
    temp_db_path: Path,
):
    """A branch whose operation raised must not be left with a NULL/'running'
    status forever — teardown finalizes it to the run's actual outcome."""
    branch = Branch(name="b1")
    ctx = await _setup_live_persist(branch)

    await _teardown_live_persist(ctx, status="failed", exception=RuntimeError("boom"))

    async with StateDB() as db:
        b = await db.get_branch(str(branch.id))
    assert b["status"] == "failed"
    assert b["ended_at"] is not None


async def test_teardown_branch_end_skipped_when_defer_terminal(
    temp_db_path: Path,
):
    """defer_terminal=True skips ALL DB mutation, including BRANCH_END — the
    resumed leg's own (non-deferred) teardown owns the real finalize."""
    branch = Branch(name="b1")
    ctx = await _setup_live_persist(branch)

    await _teardown_live_persist(ctx, status="timed_out", defer_terminal=True)

    async with StateDB() as db:
        b = await db.get_branch(str(branch.id))
    assert b["status"] is None
    assert b["ended_at"] is None


async def test_teardown_branch_end_guard_skips_already_terminal_branch(
    temp_db_path: Path,
):
    """finalize_branch()'s guard: a branch row already in a terminal status
    (however it got there) is never overwritten by a later teardown's
    coarser run-level status."""
    branch = Branch(name="b1")
    ctx = await _setup_live_persist(branch)

    async with StateDB() as db:
        await db.update_branch(str(branch.id), status="completed", ended_at=111.0)

    await _teardown_live_persist(ctx, status="failed", exception=RuntimeError("boom"))

    async with StateDB() as db:
        b = await db.get_branch(str(branch.id))
    assert b["status"] == "completed"
    assert b["ended_at"] == 111.0


async def test_teardown_detaches_persistence_from_bus(
    temp_db_path: Path,
):
    """Teardown removes the persistence handler from the bus so a closed-DB handler cannot fire later."""
    from lionagi.hooks.bus import HookPoint

    branch = Branch(name="b1")
    ctx = await _setup_live_persist(branch)
    bus = branch._hooks
    assert ctx["hook"] in bus.handlers_for(HookPoint.MESSAGE_ADD)
    assert branch._persist_via_bus in branch.on_message_added

    await _teardown_live_persist(ctx, status="completed")

    assert ctx["hook"] not in bus.handlers_for(HookPoint.MESSAGE_ADD)
    assert branch._persist_via_bus not in branch.on_message_added


async def test_teardown_closes_db_even_if_bookmark_update_fails(
    temp_db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """db.close() must run in finally even when update_session raises, so the aiosqlite worker is reclaimed."""
    branch = Branch(name="b1")
    ctx = await _setup_live_persist(branch)
    db = ctx["db"]

    async def boom(self, session_id, **kw):
        raise RuntimeError("simulated bookmark failure")

    monkeypatch.setattr(StateDB, "update_session", boom)

    before = _aiosqlite_thread_count()
    # MUST NOT raise — teardown logs and continues.
    await _teardown_live_persist(ctx, status="completed")

    # The connection was closed even though update_session failed.
    assert db._engine is None
    for _ in range(20):
        if _aiosqlite_thread_count() <= before:
            break
        await asyncio.sleep(0.05)
    # Worker count should be back to (or below) baseline.
    assert _aiosqlite_thread_count() <= before

    # Ownership release also ran despite the failure: the long-lived branch
    # is back to a clean standalone state and can be re-wrapped on resume.
    assert branch._owning_session_id is None
    assert branch._observer is None
    assert branch._hooks is None
    assert branch.user is None


async def test_rejected_second_setup_leaves_first_context_intact(
    temp_db_path: Path,
):
    """A second setup on a still-owned branch must fail WITHOUT closing the
    first context's shared DB handle or blocking its teardown release."""
    branch = Branch(name="b1")
    ctx1 = await _setup_live_persist(branch)

    ctx2 = await _setup_live_persist(branch)
    assert ctx2 is None  # rejected: branch still owned by ctx1's session

    # ctx1's DB handle survived the rejected setup...
    assert ctx1["db"]._engine is not None
    # ...and teardown still releases ownership cleanly.
    await _teardown_live_persist(ctx1, status="completed")
    assert branch._owning_session_id is None

    ctx3 = await _setup_live_persist(branch)
    assert ctx3 is not None
    await _teardown_live_persist(ctx3, status="completed")


async def test_failed_setup_releases_branch_claim(
    temp_db_path: Path,
):
    """Setup failing AFTER the wrapper session claims the branch must release
    the claim, so a retry (or a later run) can wrap the branch again."""
    import lionagi.cli._runs as _runs_mod

    async def boom():
        raise RuntimeError("simulated db-open failure")

    branch = Branch(name="b1")

    # Scoped to its own MonkeyPatch context (not the test's shared `monkeypatch`
    # fixture) so undoing it on exit only reverts `_open_shared_db` — reusing
    # the shared fixture + `monkeypatch.undo()` here would also roll back
    # `temp_db_path`'s DEFAULT_DB_PATH patch and point the retry below at the
    # real `~/.lionagi/state.db`.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_runs_mod, "_open_shared_db", boom)
        ctx = await _setup_live_persist(branch)
        assert ctx is None
        assert branch._owning_session_id is None

    ctx2 = await _setup_live_persist(branch)
    assert ctx2 is not None
    await _teardown_live_persist(ctx2, status="completed")


async def test_teardown_with_none_context_is_noop(temp_db_path: Path):
    """If setup returned None (failed), teardown(None) must be safe."""
    await _teardown_live_persist(None, status="completed")  # MUST NOT raise


# ── defer_terminal: auto-resume must not stamp a premature terminal status ────


async def test_teardown_defer_terminal_leaves_session_running(
    temp_db_path: Path,
):
    """defer_terminal=True (an auto-resume leg is about to fire on this same
    session) must skip the status write entirely, leaving the session at
    'running' for the resumed leg to own the real terminal write."""
    branch = Branch(name="b1")
    ctx = await _setup_live_persist(branch)

    result = await _teardown_live_persist(ctx, status="timed_out", defer_terminal=True)

    assert result == "timed_out"
    async with StateDB() as db:
        s = await db.get_session(ctx["session_id"])
    assert s["status"] == "running"
    assert s["ended_at"] is None


async def test_teardown_defer_terminal_still_detaches_hooks(
    temp_db_path: Path,
):
    """Even when the status write is deferred, non-status bookkeeping (hook
    unroute) still runs so a closed-DB handler cannot fire later."""
    from lionagi.hooks.bus import HookPoint

    branch = Branch(name="b1")
    ctx = await _setup_live_persist(branch)
    bus = branch._hooks

    await _teardown_live_persist(ctx, status="timed_out", defer_terminal=True)

    assert ctx["hook"] not in bus.handlers_for(HookPoint.MESSAGE_ADD)
    assert branch._persist_via_bus not in branch.on_message_added


async def test_teardown_non_resume_timeout_stamps_terminal_unchanged(
    temp_db_path: Path,
):
    """defer_terminal defaults to False: a genuine (non-auto-resume) timeout
    still stamps a terminal 'timed_out' status exactly as before this fix."""
    branch = Branch(name="b1")
    ctx = await _setup_live_persist(branch)

    result = await _teardown_live_persist(ctx, status="timed_out")

    assert result == "timed_out"
    async with StateDB() as db:
        s = await db.get_session(ctx["session_id"])
    assert s["status"] == "timed_out"
    assert s["ended_at"] is not None


# ── ADR-0035 terminal-race: a second teardown must not crash past callers ─────


async def test_teardown_already_terminal_session_reports_attempted_status(
    temp_db_path: Path,
    caplog: pytest.LogCaptureFixture,
):
    """A teardown that reattaches to a session an EARLIER, unrelated run
    already finalized (setup_agent_persist reuses an existing branch's
    session without checking terminality) must not let the stale persisted
    status masquerade as this invocation's outcome: a later leg's genuine
    'failed' must not be silently reported back as the old 'completed'. The
    rejection must still protect the DB row (ADR-0035 unchanged) — only the
    caller-visible return value differs, and it's logged at warning level."""
    branch = Branch(name="b1")
    ctx1 = await _setup_live_persist(branch)
    first = await _teardown_live_persist(ctx1, status="completed")
    assert first == "completed"

    # A later leg attaches to the SAME session/branch row, which is already
    # terminal by the time this teardown even starts (mirrors a resume/
    # follow-up reusing a branch whose session an earlier run finalized).
    ctx2 = await _setup_live_persist(branch)
    assert ctx2 is not None
    assert ctx2["session_id"] == ctx1["session_id"]

    with caplog.at_level(logging.WARNING):
        second = await _teardown_live_persist(ctx2, status="failed", exception=RuntimeError("boom"))

    assert second == "failed"  # this invocation's honest outcome, not "completed"
    assert any(
        rec.levelno >= logging.WARNING and "completed" in rec.message and "failed" in rec.message
        for rec in caplog.records
    )

    # The DB record itself is untouched — ADR-0035 still protects it.
    async with StateDB() as db:
        s = await db.get_session(ctx1["session_id"])
    assert s["status"] == "completed"


async def test_teardown_concurrent_race_non_terminal_entry_returns_winner_status(
    temp_db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    """A true concurrent race: this teardown observes the session non-
    terminal at entry, but a separate writer finalizes the row between that
    read and this teardown's own update_status() call. Unlike the already-
    terminal-on-entry case above, this is the benign race the catch's
    read-back path exists for: return the winner's persisted status at
    debug level, not a warning."""
    from lionagi.state.db import StateDB as _StateDB
    from lionagi.state.reasons import RunReasons

    branch = Branch(name="b1")
    ctx = await _setup_live_persist(branch)

    real_update_status = _StateDB.update_status
    calls = {"n": 0}

    async def racing_update_status(self, *a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            # A separate writer finalizes the row first, simulating a
            # concurrent teardown of this same in-flight session.
            async with _StateDB() as other:
                await other.update_status(
                    "session",
                    ctx["session_id"],
                    new_status="completed",
                    reason_code=RunReasons.COMPLETED_OK,
                    source="executor",
                )
        return await real_update_status(self, *a, **kw)

    monkeypatch.setattr(_StateDB, "update_status", racing_update_status)

    with caplog.at_level(logging.WARNING):
        result = await _teardown_live_persist(ctx, status="failed", exception=RuntimeError("boom"))

    assert result == "completed"  # the race winner's persisted status
    assert not any(rec.levelno >= logging.WARNING for rec in caplog.records)

    async with StateDB() as db:
        s = await db.get_session(ctx["session_id"])
    assert s["status"] == "completed"


# ── End-to-end: no aiosqlite thread leak across setup+teardown ────────────────


async def test_setup_teardown_does_not_leak_aiosqlite_thread(
    temp_db_path: Path,
):
    """Run setup+teardown several times and verify the aiosqlite worker
    count returns to baseline each time. This is the root-cause check
    for the original "li agent claude 'hi' hangs after exit" bug.
    """
    from lionagi.protocols.messages.manager import MessageManager

    branch = Branch(name="b1")

    baseline = _aiosqlite_thread_count()

    for _ in range(3):
        ctx = await _setup_live_persist(branch)
        assert ctx is not None
        msg = MessageManager.create_instruction(
            instruction="hi",
            sender="u",
            recipient=str(branch.id),
        )
        await ctx["hook"](msg)
        await _teardown_live_persist(ctx, status="completed")

        # aiosqlite uses a per-connection thread that joins on close.
        # Allow a brief window for the join.
        for _ in range(20):
            if _aiosqlite_thread_count() <= baseline:
                break
            await asyncio.sleep(0.05)
        assert _aiosqlite_thread_count() <= baseline, (
            "aiosqlite worker thread leaked across setup/teardown cycle"
        )


# ── R5-A HIGH-2: NULL progression_id resume repair ────────────────────────────


async def _legacy_db_with_nullable_progression(
    db_path: Path,
) -> StateDB:
    """Open a DB and rebuild branches+sessions tables with NULLABLE
    progression_id, mimicking the legacy schema pre-PR. The current
    schema declares those columns NOT NULL, so we can only reach the
    repair path by relaxing the constraint in test setup.
    """
    state = StateDB(db_path)
    await state.open()
    # Drop FK enforcement for the rebuild (we'll re-enable after).
    await state.execute("PRAGMA foreign_keys = OFF")
    # Recreate branches with NULLABLE progression_id.
    await state.execute("DROP TABLE IF EXISTS branches")
    await state.execute(
        """
        CREATE TABLE branches (
          id TEXT PRIMARY KEY,
          created_at REAL NOT NULL,
          node_metadata JSON,
          user TEXT,
          name TEXT,
          session_id TEXT NOT NULL,
          progression_id TEXT,
          system_msg_id TEXT
        )
        """
    )
    await state.execute("DROP TABLE IF EXISTS sessions")
    await state.execute(
        """
        CREATE TABLE sessions (
          id TEXT PRIMARY KEY,
          created_at REAL NOT NULL,
          node_metadata JSON,
          name TEXT,
          user TEXT,
          progression_id TEXT,
          first_msg_id TEXT,
          last_msg_id TEXT,
          updated_at REAL NOT NULL,
          playbook_name TEXT,
          agent_name TEXT,
          invocation_kind TEXT,
          show_topic TEXT,
          show_play_name TEXT,
          artifacts_path TEXT,
          source_kind TEXT,
          status TEXT,
          started_at REAL,
          ended_at REAL
        )
        """
    )
    await state.execute("PRAGMA foreign_keys = ON")
    return state


async def test_setup_resume_repairs_null_branch_progression_id(
    temp_db_path: Path,
):
    """A legacy branch row with progression_id NULL must be repaired on
    resume: setup creates a fresh progression, points the row at it,
    and seeds existing_msg_ids from the (now non-empty) progression
    so future hook calls dedupe correctly.

    Without repair, append_to_progression(None, msg_id) is a no-op and
    branch history is silently lost (R5-A HIGH-2).
    """
    import uuid

    from lionagi.protocols.messages.manager import MessageManager

    branch = Branch(name="b1")
    branch_id = str(branch.id)
    session_id = str(uuid.uuid4())
    sess_prog = str(uuid.uuid4())

    # Build a legacy-shaped DB and insert the NULL-progression branch.
    legacy_db = await _legacy_db_with_nullable_progression(temp_db_path)
    try:
        await legacy_db.create_progression(sess_prog)
        await legacy_db.execute(
            "INSERT INTO sessions (id, created_at, progression_id, "
            "updated_at, status) VALUES (?, ?, ?, ?, ?)",
            (session_id, 0.0, sess_prog, 0.0, "completed"),
        )
        await legacy_db.execute(
            "INSERT INTO branches (id, created_at, session_id, "
            "progression_id) VALUES (?, ?, ?, NULL)",
            (branch_id, 0.0, session_id),
        )
    finally:
        await legacy_db.close()

    ctx = await _setup_live_persist(branch)
    assert ctx is not None
    # Resume detected the existing branch and produced a NON-NULL
    # progression id (the repair created a fresh one).
    assert ctx["branch_prog_id"] is not None
    assert ctx["session_id"] == session_id

    # Fire a message — branch progression now actually receives it.
    msg = MessageManager.create_instruction(
        instruction="post-repair",
        sender="u",
        recipient=str(branch.id),
    )
    await ctx["hook"](msg)

    async with StateDB() as db:
        b = await db.get_branch(branch_id)
        bprog = await db.get_progression(b["progression_id"])
    assert b["progression_id"] is not None
    assert str(msg.id) in bprog

    await _teardown_live_persist(ctx, status="completed")


async def test_repair_branch_progression_returns_existing_id_under_race(
    temp_db_path: Path,
):
    """R6 HIGH-2: when two callers race to repair the same NULL
    progression_id, the conditional UPDATE only lands one id. The
    LOSER's repair call must return the WINNING id so the loser does
    not keep using its locally-generated (orphan) progression.

    Without this, the loser writes to an orphan progression while
    branches.progression_id points elsewhere — same silent data loss
    as the original HIGH-2.
    """
    import uuid

    branch_id = str(uuid.uuid4())
    legacy_db = await _legacy_db_with_nullable_progression(temp_db_path)
    try:
        session_id = str(uuid.uuid4())
        sess_prog = str(uuid.uuid4())
        await legacy_db.create_progression(sess_prog)
        await legacy_db.execute(
            "INSERT INTO sessions (id, created_at, progression_id, updated_at) VALUES (?, ?, ?, ?)",
            (session_id, 0.0, sess_prog, 0.0),
        )
        await legacy_db.execute(
            "INSERT INTO branches (id, created_at, session_id, "
            "progression_id) VALUES (?, ?, ?, NULL)",
            (branch_id, 0.0, session_id),
        )
    finally:
        await legacy_db.close()

    # Simulate the race: caller A wins, caller B loses.
    async with StateDB() as db:
        winner_id = str(uuid.uuid4())
        loser_id = str(uuid.uuid4())
        await db.create_progression(winner_id)
        await db.create_progression(loser_id)

        # Caller A's repair lands first.
        effective_a = await db.repair_branch_progression(branch_id, winner_id)
        assert effective_a == winner_id

        # Caller B repairs second — UPDATE no-ops (column already set),
        # but the return value MUST be the winner's id, not B's local id.
        effective_b = await db.repair_branch_progression(branch_id, loser_id)
        assert effective_b == winner_id, f"loser must adopt winner's id, got {effective_b!r}"

        # Spot-check the row really stored the winner's id.
        b = await db.get_branch(branch_id)
        assert b["progression_id"] == winner_id


async def test_repair_session_progression_returns_existing_id_under_race(
    temp_db_path: Path,
):
    """Same R6 invariant as above, for the session-level repair."""
    import uuid

    session_id = str(uuid.uuid4())
    legacy_db = await _legacy_db_with_nullable_progression(temp_db_path)
    try:
        await legacy_db.execute(
            "INSERT INTO sessions (id, created_at, progression_id, "
            "updated_at) VALUES (?, ?, NULL, ?)",
            (session_id, 0.0, 0.0),
        )
    finally:
        await legacy_db.close()

    async with StateDB() as db:
        winner_id = str(uuid.uuid4())
        loser_id = str(uuid.uuid4())
        await db.create_progression(winner_id)
        await db.create_progression(loser_id)

        effective_a = await db.repair_session_progression(
            session_id,
            winner_id,
        )
        assert effective_a == winner_id

        effective_b = await db.repair_session_progression(
            session_id,
            loser_id,
        )
        assert effective_b == winner_id


async def test_setup_resume_repairs_null_session_progression_id(
    temp_db_path: Path,
):
    """Same as above but for the session-level progression — a legacy
    session row with NULL progression_id must be repaired so the
    session-wide message timeline isn't lost.
    """
    import uuid

    from lionagi.protocols.messages.manager import MessageManager

    branch = Branch(name="b1")
    branch_id = str(branch.id)
    session_id = str(uuid.uuid4())
    branch_prog = str(uuid.uuid4())

    legacy_db = await _legacy_db_with_nullable_progression(temp_db_path)
    try:
        await legacy_db.create_progression(branch_prog)
        # Session row with NULL progression_id.
        await legacy_db.execute(
            "INSERT INTO sessions (id, created_at, progression_id, "
            "updated_at) VALUES (?, ?, NULL, ?)",
            (session_id, 0.0, 0.0),
        )
        # Branch row with a valid branch progression.
        await legacy_db.execute(
            "INSERT INTO branches (id, created_at, session_id, progression_id) VALUES (?, ?, ?, ?)",
            (branch_id, 0.0, session_id, branch_prog),
        )
    finally:
        await legacy_db.close()

    ctx = await _setup_live_persist(branch)
    assert ctx["session_prog_id"] is not None

    msg = MessageManager.create_instruction(
        instruction="hi",
        sender="u",
        recipient=str(branch.id),
    )
    await ctx["hook"](msg)

    async with StateDB() as db:
        s = await db.get_session(session_id)
        sprog = await db.get_progression(s["progression_id"])
    assert s["progression_id"] is not None
    assert str(msg.id) in sprog

    await _teardown_live_persist(ctx, status="completed")


# ── ADR-0064: artifact contract snapshot and verification ─────────────────────


async def test_setup_persists_artifact_contract(temp_db_path: Path):
    """artifact_contract passed to setup is stored in the session row."""
    branch = Branch(name="b1")
    contract = {"expected": [{"id": "report", "path": "report.md"}]}

    ctx = await _setup_live_persist(
        branch,
        agent_name="researcher",
        artifact_contract=contract,
    )
    assert ctx is not None

    async with StateDB() as db:
        s = await db.get_session(ctx["session_id"])
    assert s is not None
    stored = s["artifact_contract_json"]
    assert isinstance(stored, dict), f"expected dict, got {type(stored)}"
    assert stored["expected"][0]["id"] == "report"

    await _teardown_live_persist(ctx, status="completed")


async def test_teardown_verification_passes_when_artifact_present(
    temp_db_path: Path, tmp_path: Path
):
    """Clean completion with required artifact present → status passed, session completed."""
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    (artifacts_dir / "report.md").write_text("report content")

    branch = Branch(name="b1")
    contract = {"expected": [{"id": "report", "path": "report.md"}]}

    ctx = await _setup_live_persist(
        branch,
        agent_name="researcher",
        artifacts_path=str(artifacts_dir),
        artifact_contract=contract,
    )
    assert ctx is not None
    await _teardown_live_persist(ctx, status="completed")

    async with StateDB() as db:
        s = await db.get_session(ctx["session_id"])
    assert s is not None
    assert s["status"] == "completed"
    v = s["artifact_verification_json"]
    assert isinstance(v, dict)
    assert v["status"] == "passed"


async def test_teardown_verification_fails_flips_status(temp_db_path: Path, tmp_path: Path):
    """Clean completion with missing required artifact → status flipped to failed."""
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    # deliberately NOT creating report.md

    branch = Branch(name="b1")
    contract = {"expected": [{"id": "report", "path": "report.md"}]}

    ctx = await _setup_live_persist(
        branch,
        agent_name="researcher",
        artifacts_path=str(artifacts_dir),
        artifact_contract=contract,
    )
    assert ctx is not None
    await _teardown_live_persist(ctx, status="completed")

    async with StateDB() as db:
        s = await db.get_session(ctx["session_id"])
    assert s is not None
    assert s["status"] == "failed"
    assert s["status_reason_code"] == "run.failed.missing_artifact"
    # Evidence refs include the missing artifact entry (stored as JSON string).
    import json as _json

    evidence_raw = s["status_evidence_refs"]
    evidence = _json.loads(evidence_raw) if isinstance(evidence_raw, str) else evidence_raw
    assert isinstance(evidence, list)
    assert any(e.get("id") == "report" for e in evidence)
    v = s["artifact_verification_json"]
    assert isinstance(v, dict)
    assert v["status"] == "failed"


async def test_teardown_verification_preserves_non_completed_reason(
    temp_db_path: Path, tmp_path: Path
):
    """Missing artifact on a failed (non-completed) run keeps original reason."""
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    # deliberately NOT creating report.md

    branch = Branch(name="b1")
    contract = {"expected": [{"id": "report", "path": "report.md"}]}

    ctx = await _setup_live_persist(
        branch,
        agent_name="researcher",
        artifacts_path=str(artifacts_dir),
        artifact_contract=contract,
    )
    assert ctx is not None
    exc = RuntimeError("simulated failure")
    await _teardown_live_persist(ctx, status="failed", exception=exc)

    async with StateDB() as db:
        s = await db.get_session(ctx["session_id"])
    assert s is not None
    assert s["status"] == "failed"
    # Original exception reason preserved — NOT overridden by artifact code.
    assert s["status_reason_code"] == "run.failed.exception"
    # Verification still ran and is stored.
    v = s["artifact_verification_json"]
    assert isinstance(v, dict)
    assert v["status"] == "failed"


# ── Phantom 'failed' suppression: linked engine session is alive/completed ───


async def test_teardown_suppresses_failed_when_linked_engine_session_running(
    temp_db_path: Path,
):
    """A stream/provider error must not read as 'failed' while the real engine session is still running."""
    from lionagi.providers._provider_errors import ProviderError
    from lionagi.state.claude_mirror import mirror_session, session_db_id

    engine_uid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    async with StateDB() as db:
        await mirror_session(
            db,
            session_uid=engine_uid,
            events=[
                {
                    "type": "assistant",
                    "uuid": "e1",
                    "timestamp": "2026-07-05T00:00:00.000Z",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "working on it"}],
                    },
                }
            ],
            tool_names={},
            status="running",
        )

    branch = Branch(name="b1")
    ctx = await _setup_live_persist(branch, agent_name="implementer")
    assert ctx is not None

    exc = ProviderError("abandoned stream reader")
    final_status = await _teardown_live_persist(
        ctx, status="failed", exception=exc, engine_session_uid=engine_uid
    )

    assert final_status == "running"
    async with StateDB() as db:
        s = await db.get_session(ctx["session_id"])
    assert s is not None
    assert s["status"] == "running"
    assert s["node_metadata"]["linked_engine_session_id"] == session_db_id(engine_uid)


async def test_teardown_suppresses_failed_when_linked_engine_session_completed(
    temp_db_path: Path,
):
    """A stream/provider error must not read as 'failed' once the real engine session has completed."""
    from lionagi.providers._provider_errors import ProviderError
    from lionagi.state.claude_mirror import mirror_session, session_db_id

    engine_uid = "aaaaaaaa-bbbb-cccc-dddd-ffffffffffff"
    async with StateDB() as db:
        await mirror_session(
            db,
            session_uid=engine_uid,
            events=[
                {
                    "type": "assistant",
                    "uuid": "e1",
                    "timestamp": "2026-07-05T00:00:00.000Z",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "done"}],
                    },
                }
            ],
            tool_names={},
            status="completed",
        )

    branch = Branch(name="b1")
    ctx = await _setup_live_persist(branch, agent_name="reviewer")
    assert ctx is not None

    exc = ProviderError("abandoned stream reader")
    final_status = await _teardown_live_persist(
        ctx, status="failed", exception=exc, engine_session_uid=engine_uid
    )

    assert final_status == "completed"
    async with StateDB() as db:
        s = await db.get_session(ctx["session_id"])
    assert s is not None
    assert s["status"] == "completed"
    assert s["node_metadata"]["linked_engine_session_id"] == session_db_id(engine_uid)


async def test_teardown_keeps_failed_without_linked_engine_session(temp_db_path: Path):
    """No linked engine session (or no engine_session_uid at all) → a real failure stays failed."""
    from lionagi.providers._provider_errors import ProviderError

    branch = Branch(name="b1")
    ctx = await _setup_live_persist(branch, agent_name="implementer")
    assert ctx is not None

    exc = ProviderError("genuine failure")
    final_status = await _teardown_live_persist(
        ctx, status="failed", exception=exc, engine_session_uid="no-such-session-uid"
    )

    assert final_status == "failed"
    async with StateDB() as db:
        s = await db.get_session(ctx["session_id"])
    assert s is not None
    assert s["status"] == "failed"


async def test_teardown_keeps_failed_for_real_wrapper_bug_even_when_linked_running(
    temp_db_path: Path,
):
    """A genuine profile-layer exception (not a ProviderError) must stay 'failed' even
    when a linked engine session is running/completed — suppression is narrowly scoped
    to the CLI provider's own reported stream errors, not arbitrary wrapper bugs."""
    from lionagi.state.claude_mirror import mirror_session

    engine_uid = "aaaaaaaa-bbbb-cccc-dddd-111111111111"
    async with StateDB() as db:
        await mirror_session(
            db,
            session_uid=engine_uid,
            events=[
                {
                    "type": "assistant",
                    "uuid": "e1",
                    "timestamp": "2026-07-05T00:00:00.000Z",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "working on it"}],
                    },
                }
            ],
            tool_names={},
            status="running",
        )

    branch = Branch(name="b1")
    ctx = await _setup_live_persist(branch, agent_name="implementer")
    assert ctx is not None

    exc = RuntimeError("bug in artifact verification")
    final_status = await _teardown_live_persist(
        ctx, status="failed", exception=exc, engine_session_uid=engine_uid
    )

    assert final_status == "failed"
    async with StateDB() as db:
        s = await db.get_session(ctx["session_id"])
    assert s is not None
    assert s["status"] == "failed"


@pytest.mark.parametrize(
    "error_name,message",
    [("ProviderQuotaError", "usage limit reached"), ("ProviderAuthError", "not logged in")],
)
async def test_teardown_keeps_failed_for_genuine_provider_error_even_when_linked_running(
    temp_db_path: Path, error_name, message
):
    """ProviderQuotaError/ProviderAuthError are genuine, well-classified provider
    failures -- unlike the generic unclassified stream/transport ProviderError, they
    must never be suppressed into 'running'/'completed' just because a linked engine
    session happens to still be alive."""
    import lionagi.providers._provider_errors as provider_errors
    from lionagi.state.claude_mirror import mirror_session

    error_cls = getattr(provider_errors, error_name)
    engine_uid = "aaaaaaaa-bbbb-cccc-dddd-222222222221"
    async with StateDB() as db:
        await mirror_session(
            db,
            session_uid=engine_uid,
            events=[
                {
                    "type": "assistant",
                    "uuid": "e1",
                    "timestamp": "2026-07-05T00:00:00.000Z",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "working on it"}],
                    },
                }
            ],
            tool_names={},
            status="running",
        )

    branch = Branch(name="b1")
    ctx = await _setup_live_persist(branch, agent_name="implementer")
    assert ctx is not None

    exc = error_cls(message)
    final_status = await _teardown_live_persist(
        ctx, status="failed", exception=exc, engine_session_uid=engine_uid
    )

    assert final_status == "failed"
    async with StateDB() as db:
        s = await db.get_session(ctx["session_id"])
    assert s is not None
    assert s["status"] == "failed"


async def test_teardown_reconciles_after_delayed_mirror_row_creation(
    temp_db_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The claude/codex mirror row for engine_session_uid may not exist YET at teardown
    time (mirror-lag race) — _linked_engine_session must bounded-retry and pick it up
    instead of immediately falling through to 'failed'."""
    from lionagi.providers._provider_errors import ProviderError
    from lionagi.state.claude_mirror import mirror_session, session_db_id

    engine_uid = "aaaaaaaa-bbbb-cccc-dddd-222222222222"
    branch = Branch(name="b1")
    ctx = await _setup_live_persist(branch, agent_name="implementer")
    assert ctx is not None

    real_get_session = StateDB.get_session
    calls: list[str] = []
    created = False

    async def delayed_get_session(self, session_id, *a, **kw):
        nonlocal created
        if session_id == session_db_id(engine_uid) and not created:
            calls.append(session_id)
            if len(calls) < 3:
                # Row doesn't exist yet the first couple of lookups —
                # simulate the mirror still catching up.
                return None
            created = True
            # Write the mirror row through the SAME connection teardown is
            # using (self) -- opening a second StateDB() to the same sqlite
            # file from inside this monkeypatch recurses into SQLAlchemy's
            # engine-connect event dispatch. mirror_session() itself calls
            # db.get_session(sid) internally, so `created` must already be
            # True before this await to avoid re-entering this branch.
            await mirror_session(
                self,
                session_uid=engine_uid,
                events=[
                    {
                        "type": "assistant",
                        "uuid": "e1",
                        "timestamp": "2026-07-05T00:00:00.000Z",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "done"}],
                        },
                    }
                ],
                tool_names={},
                status="completed",
            )
        return await real_get_session(self, session_id, *a, **kw)

    monkeypatch.setattr(StateDB, "get_session", delayed_get_session)

    exc = ProviderError("abandoned stream reader")
    final_status = await _teardown_live_persist(
        ctx,
        status="failed",
        exception=exc,
        engine_session_uid=engine_uid,
    )

    assert len(calls) >= 3
    assert final_status == "completed"
