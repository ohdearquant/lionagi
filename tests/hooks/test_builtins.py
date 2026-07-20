# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for ADR-0047 built-in handlers and message persistence."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from lionagi.hooks.builtins import persist_message

# get_shared_db is the singleton accessor used by every hook in builtins;
# patching it controls which db instance the hooks operate on.
_GET_SHARED_DB = "lionagi.state.db.get_shared_db"


def _make_mock_db():
    return AsyncMock()


# ── persist_message: one logical persistence event ───────────────────────────


async def test_persist_message_appends_to_branch_progression():
    msg = {"id": "msg1", "role": "user", "content": "hi"}
    mock_db = _make_mock_db()

    with patch(_GET_SHARED_DB, return_value=mock_db):
        await persist_message(
            message=msg,
            session_id="sess1",
            branch_progression_id="bp1",
        )

    mock_db._persist_live_message.assert_awaited_once_with(
        msg,
        session_id="sess1",
        branch_progression_id="bp1",
        session_progression_id=None,
        system_branch_id=None,
        system_branch_update_before_activity=True,
    )


async def test_persist_message_appends_to_session_progression():
    msg = {"id": "msg2", "role": "user", "content": "hi"}
    mock_db = _make_mock_db()

    with patch(_GET_SHARED_DB, return_value=mock_db):
        await persist_message(
            message=msg,
            session_id="sess1",
            session_progression_id="sp1",
        )

    mock_db._persist_live_message.assert_awaited_once_with(
        msg,
        session_id="sess1",
        branch_progression_id=None,
        session_progression_id="sp1",
        system_branch_id=None,
        system_branch_update_before_activity=True,
    )


async def test_persist_message_appends_to_both_progressions():
    msg = {"id": "msg3", "role": "assistant", "content": "reply"}
    mock_db = _make_mock_db()

    with patch(_GET_SHARED_DB, return_value=mock_db):
        await persist_message(
            message=msg,
            session_id="sess1",
            branch_progression_id="bp1",
            session_progression_id="sp1",
        )

    mock_db._persist_live_message.assert_awaited_once_with(
        msg,
        session_id="sess1",
        branch_progression_id="bp1",
        session_progression_id="sp1",
        system_branch_id=None,
        system_branch_update_before_activity=True,
    )


async def test_persist_message_updates_system_msg_id_for_system_role():
    msg = {"id": "sys1", "role": "system", "content": "You are helpful."}
    mock_db = _make_mock_db()

    with patch(_GET_SHARED_DB, return_value=mock_db):
        await persist_message(
            message=msg,
            session_id="sess1",
            branch_id="branch1",
        )

    mock_db._persist_live_message.assert_awaited_once_with(
        msg,
        session_id="sess1",
        branch_progression_id=None,
        session_progression_id=None,
        system_branch_id="branch1",
        system_branch_update_before_activity=True,
    )


async def test_persist_message_no_system_msg_update_for_non_system_role():
    msg = {"id": "usr1", "role": "user", "content": "hello"}
    mock_db = _make_mock_db()

    with patch(_GET_SHARED_DB, return_value=mock_db):
        await persist_message(
            message=msg,
            session_id="sess1",
            branch_id="branch1",
        )

    mock_db._persist_live_message.assert_awaited_once_with(
        msg,
        session_id="sess1",
        branch_progression_id=None,
        session_progression_id=None,
        system_branch_id=None,
        system_branch_update_before_activity=True,
    )


async def test_persist_message_legacy_progression_id_still_works():
    """Backward compat: progression_id acts as branch_progression_id."""
    msg = {"id": "msg_legacy", "role": "user", "content": "hi"}
    mock_db = _make_mock_db()

    with patch(_GET_SHARED_DB, return_value=mock_db):
        await persist_message(
            message=msg,
            session_id="sess1",
            progression_id="legacy_prog",
        )

    mock_db._persist_live_message.assert_awaited_once_with(
        msg,
        session_id="sess1",
        branch_progression_id="legacy_prog",
        session_progression_id=None,
        system_branch_id=None,
        system_branch_update_before_activity=True,
    )


async def test_explicit_message_hook_retries_middle_transaction_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    """HookBus keeps a rolled-back transcript event for the next emission."""
    from sqlalchemy import event

    import lionagi.state.db as state_db_module
    from lionagi.hooks import build_session_bus
    from lionagi.hooks.bus import HookPoint
    from lionagi.protocols.messages.manager import MessageManager
    from lionagi.state.engine import normalize_state_db_url

    db_url = _redirect_shared_db(monkeypatch, tmp_path)
    null_url = normalize_state_db_url(None)
    db = await _shared(db_url)
    session_id = "hook-retry-session"
    branch_id = "hook-retry-branch"
    branch_prog_id = "hook-retry-branch-progression"
    session_prog_id = "hook-retry-session-progression"
    await _seed_session(db, session_id, session_prog_id)
    await _seed_branch(db, branch_id, session_id, branch_prog_id)
    bus = build_session_bus({"message.add": ["persist_message"]})
    progression_updates = 0

    def fail_second_progression(conn, cursor, statement, parameters, context, executemany):
        nonlocal progression_updates
        if statement.lstrip().startswith("UPDATE progressions"):
            progression_updates += 1
            if progression_updates == 2:
                raise RuntimeError("injected middle progression failure")

    lost = MessageManager.create_instruction(instruction="lost").to_dict(mode="db")
    event.listen(db._engine.sync_engine, "before_cursor_execute", fail_second_progression)
    try:
        await bus.emit(
            HookPoint.MESSAGE_ADD,
            message=lost,
            session_id=session_id,
            branch_id=branch_id,
            branch_progression_id=branch_prog_id,
            session_progression_id=session_prog_id,
        )
    finally:
        event.remove(db._engine.sync_engine, "before_cursor_execute", fail_second_progression)

    retry_queue = next(iter(bus._message_retry_queues.values()))
    assert retry_queue.pending_count == 1
    assert await db.get_message(lost["id"]) is None
    assert await db.get_progression(branch_prog_id) == []
    assert await db.get_progression(session_prog_id) == []

    next_message = MessageManager.create_instruction(instruction="next").to_dict(mode="db")
    await bus.emit(
        HookPoint.MESSAGE_ADD,
        message=next_message,
        session_id=session_id,
        branch_id=branch_id,
        branch_progression_id=branch_prog_id,
        session_progression_id=session_prog_id,
    )

    assert await db.get_progression(branch_prog_id) == [lost["id"], next_message["id"]]
    assert await db.get_progression(session_prog_id) == [lost["id"], next_message["id"]]
    assert await db.get_message(lost["id"]) is not None
    assert await db.get_message(next_message["id"]) is not None
    assert retry_queue.pending_count == 0

    await db.close()
    state_db_module._SHARED.pop(db_url, None)
    state_db_module._SHARED.pop(null_url, None)


# ── persist_session_start: running status must carry a reason_code ─────────────


class TestPersistSessionStartReasonCode:
    """persist_session_start must write a reason_code or the bus silently drops every provenance field."""

    async def test_persist_session_start_persists_provenance_with_reason(
        self, monkeypatch, tmp_path
    ):
        import lionagi.state.db as _db_module
        from lionagi.state.db import StateDB, get_shared_db, register_shared_db
        from lionagi.state.engine import normalize_state_db_url

        db_path = tmp_path / "state.db"
        db_url = normalize_state_db_url(db_path)
        null_url = normalize_state_db_url(None)
        monkeypatch.setattr(_db_module, "DEFAULT_DB_PATH", db_path)
        _db_module._SHARED.pop(db_url, None)
        _db_module._SHARED.pop(null_url, None)

        from lionagi.hooks.builtins import persist_session_start
        from lionagi.state.reasons import RunReasons

        sid = "sess-start-1"
        # Keep the db open so get_shared_db() (no-arg) finds it in _SHARED.
        db = StateDB(db_path)
        await db.open()
        # Register under both the test URL and the null-arg key.
        await register_shared_db(db)
        _db_module._SHARED[null_url] = db
        try:
            await db.create_progression("prog-start-1")
            await db.create_session(
                {
                    "id": sid,
                    "progression_id": "prog-start-1",
                    "status": "running",
                }
            )

            # Must NOT raise (the bug raised ValueError, swallowed by the bus).
            await persist_session_start(
                session_id=sid,
                model="gpt-5.4-mini",
                provider="openai",
                effort="high",
                agent_name="reviewer",
            )

            row = await db.get_session(sid)
        finally:
            await db.close()
            _db_module._SHARED.pop(db_url, None)
            _db_module._SHARED.pop(null_url, None)

        assert row is not None
        assert row["status"] == "running"
        # Provenance survived (it would have been dropped if the status write
        # raised before the legacy field UPDATE).
        assert row["model"] == "gpt-5.4-mini"
        assert row["provider"] == "openai"
        # A canonical "started" reason was recorded, not the deprecation default.
        assert row["status_reason_code"] == RunReasons.STARTED_OK


# ── Singleton reuse: same instance returned across multiple firings ───────────


async def test_shared_db_returns_same_instance(tmp_path, monkeypatch):
    """get_shared_db() for the same path must return the identical StateDB instance."""
    import lionagi.state.db as _db_module
    from lionagi.state.engine import normalize_state_db_url

    db_path = tmp_path / "state.db"
    db_url = normalize_state_db_url(db_path)
    monkeypatch.setattr(_db_module, "DEFAULT_DB_PATH", db_path)
    _db_module._SHARED.pop(db_url, None)
    _db_module._SHARED_OPEN_LOCK = None

    from lionagi.state.db import get_shared_db

    db1 = await get_shared_db()
    db2 = await get_shared_db()
    assert db1 is db2, "get_shared_db() must return the same instance on repeated calls"

    # Cleanup
    await db1.close()
    _db_module._SHARED.pop(db_url, None)


async def test_shared_db_connection_open_once(tmp_path, monkeypatch):
    """StateDB.open() is called exactly once even when get_shared_db() is called N times."""
    import lionagi.state.db as _db_module
    from lionagi.state.engine import normalize_state_db_url

    db_path = tmp_path / "state.db"
    db_url = normalize_state_db_url(db_path)
    # get_shared_db() with no arg uses normalize_state_db_url(None) = null_url.
    null_url = normalize_state_db_url(None)
    monkeypatch.setattr(_db_module, "DEFAULT_DB_PATH", db_path)
    _db_module._SHARED.pop(db_url, None)
    _db_module._SHARED.pop(null_url, None)
    _db_module._SHARED_OPEN_LOCK = None

    open_count = 0
    original_open = _db_module.StateDB.open

    async def counting_open(self):
        nonlocal open_count
        open_count += 1
        return await original_open(self)

    monkeypatch.setattr(_db_module.StateDB, "open", counting_open)

    from lionagi.state.db import get_shared_db

    # Call 5 times with the test URL so open() is invoked exactly once.
    for _ in range(5):
        await get_shared_db(db_path)

    assert open_count == 1, f"StateDB.open() called {open_count} times; expected 1"

    # Cleanup
    db = _db_module._SHARED.pop(db_url, None)
    if db is not None:
        await db.close()


async def test_concurrent_hook_firings_use_same_instance(tmp_path, monkeypatch):
    """Concurrent get_shared_db() calls must all resolve to the same instance without error."""
    import lionagi.state.db as _db_module
    from lionagi.state.engine import normalize_state_db_url

    db_path = tmp_path / "state.db"
    db_url = normalize_state_db_url(db_path)
    monkeypatch.setattr(_db_module, "DEFAULT_DB_PATH", db_path)
    _db_module._SHARED.pop(db_url, None)
    _db_module._SHARED_OPEN_LOCK = None

    from lionagi.state.db import get_shared_db

    # Fire 20 concurrent calls.
    results = await asyncio.gather(*[get_shared_db() for _ in range(20)])
    first = results[0]
    assert all(r is first for r in results), "All concurrent calls must return the same instance"

    # Cleanup
    await first.close()
    _db_module._SHARED.pop(db_url, None)


# ── Lifecycle hook emission: SESSION_START / SESSION_END / BRANCH_CREATE ──────


def _redirect_shared_db(monkeypatch, tmp_path):
    """Redirect the singleton to tmp_path and return the URL key used by _SHARED."""
    import lionagi.state.db as _db_module
    from lionagi.state.engine import normalize_state_db_url

    db_path = tmp_path / "lifecycle.db"
    db_url = normalize_state_db_url(db_path)
    null_url = normalize_state_db_url(None)
    monkeypatch.setattr(_db_module, "DEFAULT_DB_PATH", db_path)
    _db_module._SHARED.pop(db_url, None)
    _db_module._SHARED.pop(null_url, None)
    _db_module._SHARED_OPEN_LOCK = None
    return db_url


async def _shared(db_url):
    from lionagi.state.db import _SHARED, get_shared_db
    from lionagi.state.engine import normalize_state_db_url

    db = await get_shared_db(db_url)
    # Also register under the no-arg key so that _db() in builtins (which
    # calls get_shared_db() without a path) resolves to the same test instance
    # rather than opening the real LIONAGI_HOME/state.db.
    null_key = normalize_state_db_url(None)
    _SHARED[null_key] = db
    return db


async def _seed_session(db, sid, prog_id, status="running"):
    await db.create_progression(prog_id)
    await db.create_session({"id": sid, "progression_id": prog_id, "status": status})


async def _seed_branch(db, bid, sid, prog_id):
    await db.create_progression(prog_id)
    await db.create_branch({"id": bid, "session_id": sid, "progression_id": prog_id})


async def _transition_count(db, entity_id, reason_code):
    row = await db.fetch_one(
        "SELECT COUNT(*) AS n FROM status_transitions WHERE entity_id = ? AND reason_code = ?",
        (entity_id, reason_code),
    )
    return row["n"]


class TestSessionStartEmission:
    """SESSION_START emit → persist handler fires and is idempotent."""

    async def test_handler_fires_once_on_emit(self, monkeypatch, tmp_path):
        from lionagi.hooks.builtins import persist_session_start
        from lionagi.hooks.bus import HookBus, HookPoint
        from lionagi.state.reasons import RunReasons

        db_path = _redirect_shared_db(monkeypatch, tmp_path)

        db = await _shared(db_path)
        try:
            sid, prog_id = "ss-emit-1", "prog-ss-1"
            await _seed_session(db, sid, prog_id)

            bus = HookBus()
            bus.on(HookPoint.SESSION_START, persist_session_start)
            await bus.emit(
                HookPoint.SESSION_START,
                session_id=sid,
                model="claude",
                provider="anthropic",
                effort="high",
            )

            row = await db.get_session(sid)
            assert row is not None
            assert row["status"] == "running"
            assert row["model"] == "claude"
            assert row["status_reason_code"] == RunReasons.STARTED_OK
        finally:
            await db.close()
            import lionagi.state.db as _m
            from lionagi.state.engine import normalize_state_db_url

            _m._SHARED.pop(db_path, None)
            _m._SHARED.pop(normalize_state_db_url(None), None)

    async def test_double_emit_is_idempotent(self, monkeypatch, tmp_path):
        from lionagi.hooks.builtins import persist_session_start
        from lionagi.hooks.bus import HookBus, HookPoint
        from lionagi.state.reasons import RunReasons

        db_path = _redirect_shared_db(monkeypatch, tmp_path)

        db = await _shared(db_path)
        try:
            sid, prog_id = "ss-idem-1", "prog-ss-2"
            await _seed_session(db, sid, prog_id)

            bus = HookBus()
            bus.on(HookPoint.SESSION_START, persist_session_start)

            await bus.emit(
                HookPoint.SESSION_START, session_id=sid, model="claude", provider="anthropic"
            )
            await bus.emit(
                HookPoint.SESSION_START, session_id=sid, model="claude", provider="anthropic"
            )

            n = await _transition_count(db, sid, RunReasons.STARTED_OK)
            assert n == 1, f"Expected 1 STARTED_OK transition, got {n}"
        finally:
            await db.close()
            import lionagi.state.db as _m
            from lionagi.state.engine import normalize_state_db_url

            _m._SHARED.pop(db_path, None)
            _m._SHARED.pop(normalize_state_db_url(None), None)

    async def test_double_emit_does_not_clobber_started_at_or_provenance(
        self, monkeypatch, tmp_path
    ):
        """A duplicate SESSION_START for an already-STARTED_OK session must be
        a true no-op: started_at must not drift forward, and provenance
        fields written by the first (real) emit must not be nulled out by a
        second emit that omits them."""
        from lionagi.hooks.builtins import persist_session_start
        from lionagi.hooks.bus import HookBus, HookPoint

        db_path = _redirect_shared_db(monkeypatch, tmp_path)

        db = await _shared(db_path)
        try:
            sid, prog_id = "ss-idem-2", "prog-ss-3"
            await _seed_session(db, sid, prog_id)

            bus = HookBus()
            bus.on(HookPoint.SESSION_START, persist_session_start)

            await bus.emit(
                HookPoint.SESSION_START,
                session_id=sid,
                model="claude",
                provider="anthropic",
                effort="high",
                agent_name="implementer",
                agent_hash="deadbeef",
            )
            row_after_first = await db.get_session(sid)
            started_at_first = row_after_first["started_at"]
            assert started_at_first is not None

            # Second emit omits every optional provenance field (as a
            # supervisor re-firing SESSION_START without the original
            # invocation context would).
            await bus.emit(HookPoint.SESSION_START, session_id=sid)

            row_after_second = await db.get_session(sid)
            assert row_after_second["started_at"] == started_at_first
            assert row_after_second["model"] == "claude"
            assert row_after_second["provider"] == "anthropic"
            assert row_after_second["effort"] == "high"
            assert row_after_second["agent_name"] == "implementer"
            assert row_after_second["agent_hash"] == "deadbeef"
        finally:
            await db.close()
            import lionagi.state.db as _m
            from lionagi.state.engine import normalize_state_db_url

            _m._SHARED.pop(db_path, None)
            _m._SHARED.pop(normalize_state_db_url(None), None)

    async def test_uses_shared_db_singleton(self, monkeypatch, tmp_path):
        from lionagi.hooks.builtins import persist_session_start
        from lionagi.hooks.bus import HookBus, HookPoint
        from lionagi.state.db import get_shared_db

        db_path = _redirect_shared_db(monkeypatch, tmp_path)

        db1 = await _shared(db_path)
        try:
            sid, prog_id = "ss-singleton-1", "prog-ss-3"
            await _seed_session(db1, sid, prog_id)

            bus = HookBus()
            bus.on(HookPoint.SESSION_START, persist_session_start)
            await bus.emit(HookPoint.SESSION_START, session_id=sid, model="m", provider="p")

            db2 = await get_shared_db(db_path)
            assert db1 is db2, "Handler must use the shared singleton, not a fresh connection"
        finally:
            await db1.close()
            import lionagi.state.db as _m

            _m._SHARED.pop(db_path, None)


class TestSessionEndEmission:
    """SESSION_END emit → persist handler fires and is idempotent."""

    async def test_handler_fires_on_emit(self, monkeypatch, tmp_path):
        from lionagi.hooks.builtins import persist_session_end
        from lionagi.hooks.bus import HookBus, HookPoint

        db_path = _redirect_shared_db(monkeypatch, tmp_path)

        db = await _shared(db_path)
        try:
            sid, prog_id = "se-emit-1", "prog-se-1"
            await _seed_session(db, sid, prog_id, status="running")

            bus = HookBus()
            bus.on(HookPoint.SESSION_END, persist_session_end)
            await bus.emit(HookPoint.SESSION_END, session_id=sid, status="completed")

            row = await db.get_session(sid)
            assert row is not None
            assert row["status"] == "completed"
            assert row["ended_at"] is not None
        finally:
            await db.close()
            import lionagi.state.db as _m
            from lionagi.state.engine import normalize_state_db_url

            _m._SHARED.pop(db_path, None)
            _m._SHARED.pop(normalize_state_db_url(None), None)

    async def test_double_emit_is_idempotent(self, monkeypatch, tmp_path):
        from lionagi.hooks.builtins import persist_session_end
        from lionagi.hooks.bus import HookBus, HookPoint
        from lionagi.state.reasons import RunReasons

        db_path = _redirect_shared_db(monkeypatch, tmp_path)

        db = await _shared(db_path)
        try:
            sid, prog_id = "se-idem-1", "prog-se-2"
            await _seed_session(db, sid, prog_id, status="running")

            bus = HookBus()
            bus.on(HookPoint.SESSION_END, persist_session_end)

            await bus.emit(HookPoint.SESSION_END, session_id=sid, status="completed")
            await bus.emit(HookPoint.SESSION_END, session_id=sid, status="completed")

            n = await _transition_count(db, sid, RunReasons.COMPLETED_OK)
            assert n == 1, f"Expected 1 COMPLETED_OK transition, got {n}"

            row = await db.get_session(sid)
            assert row["status"] == "completed"
        finally:
            await db.close()
            import lionagi.state.db as _m
            from lionagi.state.engine import normalize_state_db_url

            _m._SHARED.pop(db_path, None)
            _m._SHARED.pop(normalize_state_db_url(None), None)

    async def test_already_terminal_skips_write(self, monkeypatch, tmp_path):
        from lionagi.hooks.builtins import persist_session_end
        from lionagi.hooks.bus import HookBus, HookPoint

        db_path = _redirect_shared_db(monkeypatch, tmp_path)

        db = await _shared(db_path)
        try:
            sid, prog_id = "se-terminal-1", "prog-se-3"
            await _seed_session(db, sid, prog_id, status="running")

            bus = HookBus()
            bus.on(HookPoint.SESSION_END, persist_session_end)

            await bus.emit(HookPoint.SESSION_END, session_id=sid, status="completed")
            await bus.emit(HookPoint.SESSION_END, session_id=sid, status="failed")

            row = await db.get_session(sid)
            assert row["status"] == "completed", "Second emit must not overwrite terminal status"
        finally:
            await db.close()
            import lionagi.state.db as _m
            from lionagi.state.engine import normalize_state_db_url

            _m._SHARED.pop(db_path, None)
            _m._SHARED.pop(normalize_state_db_url(None), None)

    async def test_already_terminal_with_error_preserves_node_metadata(self, monkeypatch, tmp_path):
        """already_terminal + error must not clobber node_metadata _teardown_common wrote.

        _teardown_common() writes node_metadata (extras + identity markers)
        and transitions the row to terminal status before SESSION_END fires.
        If SESSION_END later carries an error (the exception/timeout teardown
        path), the already_terminal branch must not overwrite that richer
        node_metadata with a bare {"error": ...} dict — update_session() does
        a plain column SET, not a merge, so any write there replaces the
        whole column instead of adding to it.
        """
        import json

        from lionagi.hooks.builtins import persist_session_end
        from lionagi.hooks.bus import HookBus, HookPoint

        db_path = _redirect_shared_db(monkeypatch, tmp_path)

        db = await _shared(db_path)
        try:
            sid, prog_id = "se-terminal-error-1", "prog-se-4"
            await _seed_session(db, sid, prog_id, status="running")

            # Written as a JSON string, matching _teardown_common's own
            # update_kwargs["node_metadata"] = json.dumps({**extras, **markers}).
            rich_metadata = {"pid": 4242, "identity": "worker-abc", "cwd": "/work"}
            await db.update_session(sid, node_metadata=json.dumps(rich_metadata))

            bus = HookBus()
            bus.on(HookPoint.SESSION_END, persist_session_end)

            # First emit mirrors _teardown_common's update_status() call:
            # transitions the row to terminal, no error on this leg.
            await bus.emit(HookPoint.SESSION_END, session_id=sid, status="failed")

            row = await db.get_session(sid)
            assert row["status"] == "failed"
            assert row["node_metadata"] == rich_metadata, (
                "sanity: metadata survives the transition emit"
            )

            # Second emit mirrors the exception/timeout teardown path: the
            # row is already terminal, error is set, and usage fields are
            # attached (as a real li agent invocation would pass them).
            await bus.emit(
                HookPoint.SESSION_END,
                session_id=sid,
                status="failed",
                error="boom",
                num_turns=5,
            )

            row = await db.get_session(sid)
            assert row["num_turns"] == 5, (
                "usage fields must still be written on the terminal+error path"
            )
            assert row["node_metadata"] == rich_metadata, (
                "node_metadata must survive an already-terminal SESSION_END with an "
                "error — _teardown_common owns it for terminal rows"
            )
        finally:
            await db.close()
            import lionagi.state.db as _m
            from lionagi.state.engine import normalize_state_db_url

            _m._SHARED.pop(db_path, None)
            _m._SHARED.pop(normalize_state_db_url(None), None)

    async def test_non_terminal_error_persists_usage_and_error(self, monkeypatch, tmp_path):
        """A raw {"error": ...} dict bind must not silently drop usage data,
        and must not clobber node_metadata the row already carries.

        Before the fix, the not-already-terminal branch assigned
        fields["node_metadata"] = {"error": error} as a bare dict.
        update_session()'s dynamic UPDATE builder binds fields as raw SQL
        parameters (no JSON bindparam), so sqlite3.InterfaceError aborted the
        whole statement — taking input_tokens/output_tokens/total_cost_usd/
        num_turns/status down with it — and HookBus.emit() swallows handler
        exceptions, so the failure was silent.

        Even once pre-serialized, update_session() does a plain column SET,
        not a merge, so the fix must read the row's existing node_metadata
        and merge {"error": error} into it rather than overwrite wholesale.
        """
        from lionagi.hooks.builtins import persist_session_end
        from lionagi.hooks.bus import HookBus, HookPoint

        db_path = _redirect_shared_db(monkeypatch, tmp_path)

        db = await _shared(db_path)
        try:
            sid, prog_id = "se-err-nonterm-1", "prog-se-5"
            await _seed_session(db, sid, prog_id, status="running")
            await db.set_session_provenance(
                sid, node_metadata={"identity": "marker-1", "kind": "cli"}
            )

            bus = HookBus()
            bus.on(HookPoint.SESSION_END, persist_session_end)
            await bus.emit(
                HookPoint.SESSION_END,
                session_id=sid,
                status="failed",
                error="ValueError: boom",
                input_tokens=42,
                output_tokens=7,
                total_cost_usd=0.01,
                num_turns=3,
            )

            row = await db.get_session(sid)
            assert row["status"] == "failed"
            assert row["input_tokens"] == 42
            assert row["output_tokens"] == 7
            assert row["total_cost_usd"] == 0.01
            assert row["num_turns"] == 3
            assert row["node_metadata"] == {
                "identity": "marker-1",
                "kind": "cli",
                "error": "ValueError: boom",
            }
        finally:
            await db.close()
            import lionagi.state.db as _m
            from lionagi.state.engine import normalize_state_db_url

            _m._SHARED.pop(db_path, None)
            _m._SHARED.pop(normalize_state_db_url(None), None)


class TestBranchCreateEmission:
    """BRANCH_CREATE emit → persist handler fires and is idempotent."""

    async def test_handler_fires_on_emit(self, monkeypatch, tmp_path):
        from lionagi.hooks.builtins import persist_branch_provenance
        from lionagi.hooks.bus import HookBus, HookPoint

        db_path = _redirect_shared_db(monkeypatch, tmp_path)

        db = await _shared(db_path)
        try:
            sid, sprog = "bc-session-1", "prog-bc-s1"
            bid, bprog = "bc-branch-1", "prog-bc-b1"
            await _seed_session(db, sid, sprog)
            await _seed_branch(db, bid, sid, bprog)

            bus = HookBus()
            bus.on(HookPoint.BRANCH_CREATE, persist_branch_provenance)
            await bus.emit(
                HookPoint.BRANCH_CREATE,
                branch_id=bid,
                model="gpt-5.4-mini",
                provider="openai",
                agent_name="coder",
            )

            row = await db.get_branch(bid)
            assert row is not None
            assert row["model"] == "gpt-5.4-mini"
            assert row["provider"] == "openai"
            assert row["agent_name"] == "coder"
        finally:
            await db.close()
            import lionagi.state.db as _m
            from lionagi.state.engine import normalize_state_db_url

            _m._SHARED.pop(db_path, None)
            _m._SHARED.pop(normalize_state_db_url(None), None)

    async def test_double_emit_is_idempotent(self, monkeypatch, tmp_path):
        from lionagi.hooks.builtins import persist_branch_provenance
        from lionagi.hooks.bus import HookBus, HookPoint

        db_path = _redirect_shared_db(monkeypatch, tmp_path)

        db = await _shared(db_path)
        try:
            sid, sprog = "bc-session-2", "prog-bc-s2"
            bid, bprog = "bc-branch-2", "prog-bc-b2"
            await _seed_session(db, sid, sprog)
            await _seed_branch(db, bid, sid, bprog)

            bus = HookBus()
            bus.on(HookPoint.BRANCH_CREATE, persist_branch_provenance)

            await bus.emit(HookPoint.BRANCH_CREATE, branch_id=bid, model="m", provider="p")
            await bus.emit(HookPoint.BRANCH_CREATE, branch_id=bid, model="m", provider="p")

            row = await db.get_branch(bid)
            assert row["model"] == "m"
            assert row["provider"] == "p"
        finally:
            await db.close()
            import lionagi.state.db as _m
            from lionagi.state.engine import normalize_state_db_url

            _m._SHARED.pop(db_path, None)
            _m._SHARED.pop(normalize_state_db_url(None), None)


class TestBranchEndEmission:
    """BRANCH_END emit → persist handler fires and is guarded against clobbering
    a more specific terminal status a per-op writer already recorded."""

    async def test_handler_fires_on_emit(self, monkeypatch, tmp_path):
        from lionagi.hooks.builtins import persist_branch_end
        from lionagi.hooks.bus import HookBus, HookPoint

        db_path = _redirect_shared_db(monkeypatch, tmp_path)

        db = await _shared(db_path)
        try:
            sid, sprog = "be-session-1", "prog-be-s1"
            bid, bprog = "be-branch-1", "prog-be-b1"
            await _seed_session(db, sid, sprog)
            await _seed_branch(db, bid, sid, bprog)

            bus = HookBus()
            bus.on(HookPoint.BRANCH_END, persist_branch_end)
            await bus.emit(
                HookPoint.BRANCH_END,
                branch_id=bid,
                status="completed",
                ended_at=42.0,
            )

            row = await db.get_branch(bid)
            assert row is not None
            assert row["status"] == "completed"
            assert row["ended_at"] == 42.0
        finally:
            await db.close()
            import lionagi.state.db as _m
            from lionagi.state.engine import normalize_state_db_url

            _m._SHARED.pop(db_path, None)
            _m._SHARED.pop(normalize_state_db_url(None), None)

    async def test_guard_skips_already_terminal_status(self, monkeypatch, tmp_path):
        """A per-op writer's more specific outcome (e.g. cli/orchestrate/flow.py's
        NodeFailed branch-status update) must not be clobbered by a run-level
        BRANCH_END carrying a different, coarser status."""
        from lionagi.hooks.builtins import persist_branch_end
        from lionagi.hooks.bus import HookBus, HookPoint

        db_path = _redirect_shared_db(monkeypatch, tmp_path)

        db = await _shared(db_path)
        try:
            sid, sprog = "be-session-2", "prog-be-s2"
            bid, bprog = "be-branch-2", "prog-be-b2"
            await _seed_session(db, sid, sprog)
            await _seed_branch(db, bid, sid, bprog)
            await db.update_branch(bid, status="failed", ended_at=10.0)

            bus = HookBus()
            bus.on(HookPoint.BRANCH_END, persist_branch_end)
            await bus.emit(
                HookPoint.BRANCH_END,
                branch_id=bid,
                status="completed",
                ended_at=999.0,
            )

            row = await db.get_branch(bid)
            assert row["status"] == "failed"
            assert row["ended_at"] == 10.0
        finally:
            await db.close()
            import lionagi.state.db as _m
            from lionagi.state.engine import normalize_state_db_url

            _m._SHARED.pop(db_path, None)
            _m._SHARED.pop(normalize_state_db_url(None), None)

    async def test_default_status_is_completed(self, monkeypatch, tmp_path):
        from lionagi.hooks.builtins import persist_branch_end
        from lionagi.hooks.bus import HookBus, HookPoint

        db_path = _redirect_shared_db(monkeypatch, tmp_path)

        db = await _shared(db_path)
        try:
            sid, sprog = "be-session-3", "prog-be-s3"
            bid, bprog = "be-branch-3", "prog-be-b3"
            await _seed_session(db, sid, sprog)
            await _seed_branch(db, bid, sid, bprog)

            bus = HookBus()
            bus.on(HookPoint.BRANCH_END, persist_branch_end)
            await bus.emit(HookPoint.BRANCH_END, branch_id=bid)

            row = await db.get_branch(bid)
            assert row["status"] == "completed"
            assert row["ended_at"] is not None
        finally:
            await db.close()
            import lionagi.state.db as _m
            from lionagi.state.engine import normalize_state_db_url

            _m._SHARED.pop(db_path, None)
            _m._SHARED.pop(normalize_state_db_url(None), None)

    async def test_missing_branch_row_does_not_raise(self, monkeypatch, tmp_path):
        """A DAG leg branch that never got a first message (no create_branch()
        call, so no row) must not make BRANCH_END raise."""
        from lionagi.hooks.builtins import persist_branch_end
        from lionagi.hooks.bus import HookBus, HookPoint

        db_path = _redirect_shared_db(monkeypatch, tmp_path)

        db = await _shared(db_path)
        try:
            bus = HookBus()
            bus.on(HookPoint.BRANCH_END, persist_branch_end)
            # MUST NOT raise.
            await bus.emit(HookPoint.BRANCH_END, branch_id="no-such-branch", status="failed")
        finally:
            await db.close()
            import lionagi.state.db as _m
            from lionagi.state.engine import normalize_state_db_url

            _m._SHARED.pop(db_path, None)
            _m._SHARED.pop(normalize_state_db_url(None), None)


class TestDefaultHookBusEmissions:
    """build_session_bus wires all three handlers; each fires on the correct point."""

    async def test_session_start_handler_wired_in_default_bus(self, monkeypatch, tmp_path):
        from lionagi.hooks.bus import HookPoint
        from lionagi.hooks.loader import build_session_bus

        db_path = _redirect_shared_db(monkeypatch, tmp_path)

        db = await _shared(db_path)
        try:
            sid, prog_id = "dbus-ss-1", "prog-dbus-1"
            await _seed_session(db, sid, prog_id)

            bus = build_session_bus()
            handlers = bus.handlers_for(HookPoint.SESSION_START)
            assert len(handlers) == 1
            await bus.emit(HookPoint.SESSION_START, session_id=sid, model="x", provider="y")

            row = await db.get_session(sid)
            assert row["model"] == "x"
        finally:
            await db.close()
            import lionagi.state.db as _m
            from lionagi.state.engine import normalize_state_db_url

            _m._SHARED.pop(db_path, None)
            _m._SHARED.pop(normalize_state_db_url(None), None)

    async def test_session_end_handler_wired_in_default_bus(self, monkeypatch, tmp_path):
        from lionagi.hooks.bus import HookPoint
        from lionagi.hooks.loader import build_session_bus

        db_path = _redirect_shared_db(monkeypatch, tmp_path)

        db = await _shared(db_path)
        try:
            sid, prog_id = "dbus-se-1", "prog-dbus-2"
            await _seed_session(db, sid, prog_id, status="running")

            bus = build_session_bus()
            handlers = bus.handlers_for(HookPoint.SESSION_END)
            assert len(handlers) == 1
            await bus.emit(HookPoint.SESSION_END, session_id=sid, status="completed")

            row = await db.get_session(sid)
            assert row["status"] == "completed"
        finally:
            await db.close()
            import lionagi.state.db as _m
            from lionagi.state.engine import normalize_state_db_url

            _m._SHARED.pop(db_path, None)
            _m._SHARED.pop(normalize_state_db_url(None), None)

    async def test_branch_create_handler_wired_in_default_bus(self, monkeypatch, tmp_path):
        from lionagi.hooks.bus import HookPoint
        from lionagi.hooks.loader import build_session_bus

        db_path = _redirect_shared_db(monkeypatch, tmp_path)

        db = await _shared(db_path)
        try:
            sid, sprog = "dbus-bc-s1", "prog-dbus-bs1"
            bid, bprog = "dbus-bc-b1", "prog-dbus-bb1"
            await _seed_session(db, sid, sprog)
            await _seed_branch(db, bid, sid, bprog)

            bus = build_session_bus()
            handlers = bus.handlers_for(HookPoint.BRANCH_CREATE)
            assert len(handlers) == 1
            await bus.emit(HookPoint.BRANCH_CREATE, branch_id=bid, model="m2", provider="p2")

            row = await db.get_branch(bid)
            assert row["model"] == "m2"
        finally:
            await db.close()
            import lionagi.state.db as _m
            from lionagi.state.engine import normalize_state_db_url

            _m._SHARED.pop(db_path, None)
            _m._SHARED.pop(normalize_state_db_url(None), None)

    async def test_branch_end_handler_wired_in_default_bus(self, monkeypatch, tmp_path):
        from lionagi.hooks.bus import HookPoint
        from lionagi.hooks.loader import build_session_bus

        db_path = _redirect_shared_db(monkeypatch, tmp_path)

        db = await _shared(db_path)
        try:
            sid, sprog = "dbus-be-s1", "prog-dbus-bes1"
            bid, bprog = "dbus-be-b1", "prog-dbus-beb1"
            await _seed_session(db, sid, sprog)
            await _seed_branch(db, bid, sid, bprog)

            bus = build_session_bus()
            handlers = bus.handlers_for(HookPoint.BRANCH_END)
            assert len(handlers) == 1
            await bus.emit(HookPoint.BRANCH_END, branch_id=bid, status="failed")

            row = await db.get_branch(bid)
            assert row["status"] == "failed"
            assert row["ended_at"] is not None
        finally:
            await db.close()
            import lionagi.state.db as _m
            from lionagi.state.engine import normalize_state_db_url

            _m._SHARED.pop(db_path, None)
            _m._SHARED.pop(normalize_state_db_url(None), None)
