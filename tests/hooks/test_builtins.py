# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for ADR-0023 built-in handlers (FIX 4: persist_message dual progressions)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from lionagi.hooks.builtins import persist_message

# StateDB is imported lazily inside persist_message, so we patch the module
# it lives in — not lionagi.hooks.builtins.
_STATEDB_PATH = "lionagi.state.db.StateDB"


def _make_mock_db_ctx():
    mock_db = AsyncMock()
    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_db)
    mock_ctx.__aexit__ = AsyncMock(return_value=None)
    return mock_db, mock_ctx


# ── persist_message: dual progressions ───────────────────────────────────────


async def test_persist_message_appends_to_branch_progression():
    msg = {"id": "msg1", "role": "user", "content": "hi"}
    mock_db, mock_ctx = _make_mock_db_ctx()

    with patch(_STATEDB_PATH, return_value=mock_ctx):
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
    mock_db, mock_ctx = _make_mock_db_ctx()

    with patch(_STATEDB_PATH, return_value=mock_ctx):
        await persist_message(
            message=msg,
            session_id="sess1",
            session_progression_id="sp1",
        )

    mock_db.append_to_progression.assert_any_await("sp1", "msg2")


async def test_persist_message_appends_to_both_progressions():
    msg = {"id": "msg3", "role": "assistant", "content": "reply"}
    mock_db, mock_ctx = _make_mock_db_ctx()

    with patch(_STATEDB_PATH, return_value=mock_ctx):
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
    mock_db, mock_ctx = _make_mock_db_ctx()

    with patch(_STATEDB_PATH, return_value=mock_ctx):
        await persist_message(
            message=msg,
            session_id="sess1",
            branch_id="branch1",
        )

    mock_db.update_branch.assert_awaited_once_with("branch1", system_msg_id="sys1")


async def test_persist_message_no_system_msg_update_for_non_system_role():
    msg = {"id": "usr1", "role": "user", "content": "hello"}
    mock_db, mock_ctx = _make_mock_db_ctx()

    with patch(_STATEDB_PATH, return_value=mock_ctx):
        await persist_message(
            message=msg,
            session_id="sess1",
            branch_id="branch1",
        )

    mock_db.update_branch.assert_not_awaited()


async def test_persist_message_legacy_progression_id_still_works():
    """Backward compat: progression_id acts as branch_progression_id."""
    msg = {"id": "msg_legacy", "role": "user", "content": "hi"}
    mock_db, mock_ctx = _make_mock_db_ctx()

    with patch(_STATEDB_PATH, return_value=mock_ctx):
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
        monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", tmp_path / "state.db")
        from lionagi.hooks.builtins import persist_session_start
        from lionagi.state.db import StateDB
        from lionagi.state.reasons import RunReasons

        sid = "sess-start-1"
        async with StateDB() as db:
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

        async with StateDB() as db:
            row = await db.get_session(sid)

        assert row is not None
        assert row["status"] == "running"
        # Provenance survived (it would have been dropped if the status write
        # raised before the legacy field UPDATE).
        assert row["model"] == "gpt-5.4-mini"
        assert row["provider"] == "openai"
        # A canonical "started" reason was recorded, not the deprecation default.
        assert row["status_reason_code"] == RunReasons.STARTED_OK
