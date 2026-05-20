# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""
Regression tests for ``lionagi.cli.agent._setup_live_persist`` and
``_teardown_live_persist``.

These tests target the CLI hang bug fixed in commit ``0d8027958``: the
aiosqlite worker is a non-daemon thread; leaking it prevents the Python
interpreter from shutting down. ``_setup_live_persist`` must close the
DB on any failure, and ``_teardown_live_persist`` must close it in a
dedicated ``finally`` so a bookmark-update failure cannot leak the
worker.

All tests use a temp file DB (NOT ``:memory:``) so the aiosqlite WAL
mode and background thread match production. No real API calls are
made — the ``Branch`` is constructed locally and the message hook is
fired by hand or via ``branch.msgs.add_message``.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from lionagi import Branch
from lionagi.cli.agent import _setup_live_persist, _teardown_live_persist
from lionagi.state.db import StateDB

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def temp_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point StateDB() at a per-test file DB.

    File-backed (not ``:memory:``) so aiosqlite's WAL + non-daemon
    worker thread path is exercised — the production hang scenario.
    """
    db_path = tmp_path / "state.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", db_path)
    return db_path


def _aiosqlite_thread_count() -> int:
    """Number of live threads whose name starts with 'sqlite_'.

    aiosqlite spawns a worker per connection with names like
    ``sqlite_/path/to/db``. A leaked connection shows up here.
    """
    return sum(1 for t in threading.enumerate() if t.name.startswith("sqlite"))


# ── _setup_live_persist: happy path + invariants ──────────────────────────────


async def test_setup_creates_session_branch_progression_rows(
    temp_db_path: Path,
):
    """A fresh setup creates session, branch, and two progression rows
    and registers the message hook on the branch.
    """
    branch = Branch(name="b1")

    ctx = await _setup_live_persist(branch, agent_name="reviewer")

    assert ctx is not None
    assert ctx["db"] is not None
    assert ctx["session_id"]
    assert ctx["session_prog_id"]
    assert ctx["branch_prog_id"]
    assert ctx["existing_msg_ids"] == set()
    assert ctx["new_msg_ids"] == []
    # The hook was registered (exactly once) on the branch.
    assert ctx["hook"] in branch.on_message_added
    assert sum(1 for h in branch.on_message_added if h is ctx["hook"]) == 1

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
    """If the Branch has a system message, setup must insert it and
    point ``branches.system_msg_id`` at it.
    """
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
    """If ``db.open()`` itself raises, setup returns ``None`` and no
    aiosqlite worker thread is left dangling.
    """
    # Point the default DB at a path inside a file (not a directory) so
    # ``mkdir(parents=True, exist_ok=True)`` succeeds on the parent but
    # downstream connect could still work — instead, patch ``open`` to
    # raise directly to simulate I/O failure.
    db_path = tmp_path / "state.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", db_path)

    async def boom(self):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(StateDB, "open", boom)

    before = _aiosqlite_thread_count()
    branch = Branch(name="b1")

    ctx = await _setup_live_persist(branch)

    assert ctx is None
    # The hook was NEVER registered when setup failed.
    assert branch.on_message_added == []
    # No leaked aiosqlite worker — the count is stable.
    assert _aiosqlite_thread_count() == before


async def test_setup_create_session_failure_closes_db(
    temp_db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """If ``create_session`` fails mid-setup, the DB is closed and the
    aiosqlite worker thread is reclaimed.

    This is the failure mode the hang fix targets: prior to the fix,
    a setup exception left the connection open and the non-daemon
    worker prevented interpreter shutdown.
    """
    original_create = StateDB.create_session

    async def fail(self, session: dict):
        # Touch the DB once so the worker thread is actually spawned,
        # then fail. Mirrors a real partial-failure scenario.
        await self.db.execute("SELECT 1")
        raise RuntimeError("simulated mid-setup failure")

    monkeypatch.setattr(StateDB, "create_session", fail)

    before = _aiosqlite_thread_count()
    branch = Branch(name="b1")

    ctx = await _setup_live_persist(branch)

    assert ctx is None
    assert branch.on_message_added == []
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


# ── Resume path ───────────────────────────────────────────────────────────────


async def test_setup_resume_loads_existing_session_and_progression(
    temp_db_path: Path,
):
    """If the branch already exists in the DB (resume case), setup
    reuses its session_id / progression_id and seeds existing_msg_ids
    from the prior progression so the hook can dedupe re-fires.
    """
    branch = Branch(name="b1")
    ctx1 = await _setup_live_persist(branch)
    session_id_1 = ctx1["session_id"]
    branch_prog_id_1 = ctx1["branch_prog_id"]

    # Fire the hook for one message so the progression has content.
    # Build the message directly so we don't trip the sync add_message
    # async-hook guard.
    from lionagi.protocols.messages.manager import MessageManager
    msg = MessageManager.create_instruction(
        instruction="hello", sender="user", recipient=str(branch.id),
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
    """On resume, the hook must NOT re-append already-persisted message
    IDs to the progression — that would silently double-count history.
    """
    from lionagi.protocols.messages.manager import MessageManager

    branch = Branch(name="b1")
    ctx1 = await _setup_live_persist(branch)
    msg = MessageManager.create_instruction(
        instruction="hello", sender="user", recipient=str(branch.id),
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


async def test_hook_swallows_db_write_failure(
    temp_db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    """A DB write blip MUST NOT abort the user-facing turn. The hook
    logs and continues — the in-memory message is still valid.
    """
    import logging

    from lionagi.protocols.messages.manager import MessageManager

    branch = Branch(name="b1")
    ctx = await _setup_live_persist(branch)

    # Monkeypatch insert_message to raise.
    async def boom(self, msg):
        raise RuntimeError("simulated busy timeout")

    monkeypatch.setattr(StateDB, "insert_message", boom)

    msg = MessageManager.create_instruction(
        instruction="hi", sender="user", recipient=str(branch.id),
    )

    with caplog.at_level(logging.WARNING, logger="lionagi.cli"):
        # MUST NOT raise.
        await ctx["hook"](msg)

    assert any(
        "live persist write failed" in rec.message for rec in caplog.records
    ), "expected WARNING log on hook DB-write failure"

    await _teardown_live_persist(ctx, status="completed")


async def test_hook_updates_system_msg_id_when_system_replaced(
    temp_db_path: Path,
):
    """If the runtime replaces the system message mid-run, the hook
    must update ``branches.system_msg_id`` so Studio's O(1) lookup
    returns the current system, not the stale one.
    """
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
        instruction="a", sender="u", recipient=str(branch.id),
    )
    await ctx["hook"](msg_a)
    msg_b = MessageManager.create_instruction(
        instruction="b", sender="u", recipient=str(branch.id),
    )
    await ctx["hook"](msg_b)

    await _teardown_live_persist(ctx, status="completed")

    async with StateDB() as db:
        s = await db.get_session(ctx["session_id"])
    assert s["status"] == "completed"
    assert s["first_msg_id"] == str(msg_a.id)
    assert s["last_msg_id"] == str(msg_b.id)
    assert s["ended_at"] is not None


async def test_teardown_removes_all_duplicate_hook_registrations(
    temp_db_path: Path,
):
    """If the same hook callable is appended twice (test/dev mistake or
    a re-entrant setup), teardown removes ALL copies — not just the
    first — so a closed-DB hook cannot survive teardown.
    """
    branch = Branch(name="b1")
    ctx = await _setup_live_persist(branch)
    # Register the same hook a second time on purpose.
    branch.on_message_added.append(ctx["hook"])
    assert sum(1 for h in branch.on_message_added if h is ctx["hook"]) == 2

    await _teardown_live_persist(ctx, status="completed")

    assert sum(1 for h in branch.on_message_added if h is ctx["hook"]) == 0


async def test_teardown_closes_db_even_if_bookmark_update_fails(
    temp_db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """The hang fix invariant: db.close() lives in its own ``finally``.
    If update_session raises, close STILL runs and the aiosqlite worker
    is reclaimed.
    """
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
    assert db._db is None
    for _ in range(20):
        if _aiosqlite_thread_count() <= before:
            break
        await asyncio.sleep(0.05)
    # Worker count should be back to (or below) baseline.
    assert _aiosqlite_thread_count() <= before


async def test_teardown_with_none_context_is_noop(temp_db_path: Path):
    """If setup returned None (failed), teardown(None) must be safe."""
    await _teardown_live_persist(None, status="completed")  # MUST NOT raise


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
            instruction="hi", sender="u", recipient=str(branch.id),
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
