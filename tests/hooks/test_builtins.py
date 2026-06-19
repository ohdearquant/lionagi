# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for ADR-0023 built-in handlers (FIX 4: persist_message dual progressions)."""

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


# ── persist_message: dual progressions ───────────────────────────────────────


async def test_persist_message_appends_to_branch_progression():
    msg = {"id": "msg1", "role": "user", "content": "hi"}
    mock_db = _make_mock_db()

    with patch(_GET_SHARED_DB, return_value=mock_db):
        await persist_message(
            message=msg,
            session_id="sess1",
            branch_progression_id="bp1",
        )

    mock_db.insert_message.assert_awaited_once_with(msg)
    mock_db.append_to_progression.assert_any_await("bp1", "msg1")
    mock_db.touch_session_activity.assert_awaited_once_with("sess1")


async def test_persist_message_appends_to_session_progression():
    msg = {"id": "msg2", "role": "user", "content": "hi"}
    mock_db = _make_mock_db()

    with patch(_GET_SHARED_DB, return_value=mock_db):
        await persist_message(
            message=msg,
            session_id="sess1",
            session_progression_id="sp1",
        )

    mock_db.append_to_progression.assert_any_await("sp1", "msg2")


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

    calls = mock_db.append_to_progression.await_args_list
    progression_ids = [c.args[0] for c in calls]
    assert "bp1" in progression_ids
    assert "sp1" in progression_ids


async def test_persist_message_updates_system_msg_id_for_system_role():
    msg = {"id": "sys1", "role": "system", "content": "You are helpful."}
    mock_db = _make_mock_db()

    with patch(_GET_SHARED_DB, return_value=mock_db):
        await persist_message(
            message=msg,
            session_id="sess1",
            branch_id="branch1",
        )

    mock_db.update_branch.assert_awaited_once_with("branch1", system_msg_id="sys1")


async def test_persist_message_no_system_msg_update_for_non_system_role():
    msg = {"id": "usr1", "role": "user", "content": "hello"}
    mock_db = _make_mock_db()

    with patch(_GET_SHARED_DB, return_value=mock_db):
        await persist_message(
            message=msg,
            session_id="sess1",
            branch_id="branch1",
        )

    mock_db.update_branch.assert_not_awaited()


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

    mock_db.append_to_progression.assert_any_await("legacy_prog", "msg_legacy")


# ── persist_session_start: running status must carry a reason_code ─────────────


class TestPersistSessionStartReasonCode:
    """persist_session_start must write a reason_code or the bus silently drops every provenance field."""

    async def test_persist_session_start_persists_provenance_with_reason(
        self, monkeypatch, tmp_path
    ):
        import lionagi.state.db as _db_module

        monkeypatch.setattr(_db_module, "DEFAULT_DB_PATH", tmp_path / "state.db")
        # Reset the singleton cache so this test gets its own isolated DB.
        monkeypatch.setitem(_db_module._SHARED, tmp_path / "state.db", None)
        _db_module._SHARED.pop(tmp_path / "state.db", None)

        from lionagi.hooks.builtins import persist_session_start
        from lionagi.state.db import StateDB
        from lionagi.state.reasons import RunReasons

        sid = "sess-start-1"
        async with StateDB(tmp_path / "state.db") as db:
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

        shared = _db_module._SHARED.get(tmp_path / "state.db")
        if shared is not None:
            row = await shared.get_session(sid)
        else:
            async with StateDB(tmp_path / "state.db") as db:
                row = await db.get_session(sid)

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

    monkeypatch.setattr(_db_module, "DEFAULT_DB_PATH", tmp_path / "state.db")
    _db_module._SHARED.pop(tmp_path / "state.db", None)
    _db_module._SHARED_OPEN_LOCK = None

    from lionagi.state.db import get_shared_db

    db1 = await get_shared_db()
    db2 = await get_shared_db()
    assert db1 is db2, "get_shared_db() must return the same instance on repeated calls"

    # Cleanup
    await db1.close()
    _db_module._SHARED.pop(tmp_path / "state.db", None)


async def test_shared_db_connection_open_once(tmp_path, monkeypatch):
    """StateDB.open() is called exactly once even when get_shared_db() is called N times."""
    import lionagi.state.db as _db_module

    monkeypatch.setattr(_db_module, "DEFAULT_DB_PATH", tmp_path / "state.db")
    _db_module._SHARED.pop(tmp_path / "state.db", None)
    _db_module._SHARED_OPEN_LOCK = None

    open_count = 0
    original_open = _db_module.StateDB.open

    async def counting_open(self):
        nonlocal open_count
        open_count += 1
        return await original_open(self)

    monkeypatch.setattr(_db_module.StateDB, "open", counting_open)

    from lionagi.state.db import get_shared_db

    # Call 5 times; open() must be called exactly once.
    for _ in range(5):
        await get_shared_db()

    assert open_count == 1, f"StateDB.open() called {open_count} times; expected 1"

    # Cleanup
    db = _db_module._SHARED.pop(tmp_path / "state.db", None)
    if db is not None:
        await db.close()


async def test_concurrent_hook_firings_use_same_instance(tmp_path, monkeypatch):
    """Concurrent get_shared_db() calls must all resolve to the same instance without error."""
    import lionagi.state.db as _db_module

    monkeypatch.setattr(_db_module, "DEFAULT_DB_PATH", tmp_path / "state.db")
    _db_module._SHARED.pop(tmp_path / "state.db", None)
    _db_module._SHARED_OPEN_LOCK = None

    from lionagi.state.db import get_shared_db

    # Fire 20 concurrent calls.
    results = await asyncio.gather(*[get_shared_db() for _ in range(20)])
    first = results[0]
    assert all(r is first for r in results), "All concurrent calls must return the same instance"

    # Cleanup
    await first.close()
    _db_module._SHARED.pop(tmp_path / "state.db", None)
