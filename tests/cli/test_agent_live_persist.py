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
from lionagi.cli._persist import setup_agent_persist as _setup_live_persist
from lionagi.cli._persist import teardown_agent_persist as _teardown_live_persist
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
    # Persistence rides the hook bus (ADR-0023b): the persist handler is on the
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
    # The PERSISTENCE hook was NEVER registered when setup failed — only the
    # branch's baseline signal-emission hook (_schedule_emit) remains.
    assert branch.on_message_added == [branch._schedule_emit]
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
    """On resume, the hook must NOT re-append already-persisted message
    IDs to the progression — that would silently double-count history.
    """
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


async def test_teardown_detaches_persistence_from_bus(
    temp_db_path: Path,
):
    """Teardown detaches the persistence handler from the session hook bus and
    the emit hook (_persist_via_bus) from the branch (ADR-0023b), so a closed-DB
    handler cannot survive teardown and fire on later messages.
    """
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
    await state.db.execute("PRAGMA foreign_keys = OFF")
    # Recreate branches with NULLABLE progression_id.
    await state.db.executescript(
        """
        DROP TABLE IF EXISTS branches;
        CREATE TABLE branches (
          id TEXT PRIMARY KEY,
          created_at REAL NOT NULL,
          node_metadata JSON,
          user TEXT,
          name TEXT,
          session_id TEXT NOT NULL,
          progression_id TEXT,
          system_msg_id TEXT
        );
        DROP TABLE IF EXISTS sessions;
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
        );
        """
    )
    await state.db.execute("PRAGMA foreign_keys = ON")
    await state.db.commit()
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
        await legacy_db.db.execute(
            "INSERT INTO sessions (id, created_at, progression_id, "
            "updated_at, status) VALUES (?, ?, ?, ?, ?)",
            (session_id, 0.0, sess_prog, 0.0, "completed"),
        )
        await legacy_db.db.execute(
            "INSERT INTO branches (id, created_at, session_id, "
            "progression_id) VALUES (?, ?, ?, NULL)",
            (branch_id, 0.0, session_id),
        )
        await legacy_db.db.commit()
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
        await legacy_db.db.execute(
            "INSERT INTO sessions (id, created_at, progression_id, updated_at) VALUES (?, ?, ?, ?)",
            (session_id, 0.0, sess_prog, 0.0),
        )
        await legacy_db.db.execute(
            "INSERT INTO branches (id, created_at, session_id, "
            "progression_id) VALUES (?, ?, ?, NULL)",
            (branch_id, 0.0, session_id),
        )
        await legacy_db.db.commit()
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
        await legacy_db.db.execute(
            "INSERT INTO sessions (id, created_at, progression_id, "
            "updated_at) VALUES (?, ?, NULL, ?)",
            (session_id, 0.0, 0.0),
        )
        await legacy_db.db.commit()
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
        await legacy_db.db.execute(
            "INSERT INTO sessions (id, created_at, progression_id, "
            "updated_at) VALUES (?, ?, NULL, ?)",
            (session_id, 0.0, 0.0),
        )
        # Branch row with a valid branch progression.
        await legacy_db.db.execute(
            "INSERT INTO branches (id, created_at, session_id, progression_id) VALUES (?, ?, ?, ?)",
            (branch_id, 0.0, session_id, branch_prog),
        )
        await legacy_db.db.commit()
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


# ── ADR-0029: artifact contract snapshot and verification ─────────────────────


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
