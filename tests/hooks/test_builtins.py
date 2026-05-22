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
