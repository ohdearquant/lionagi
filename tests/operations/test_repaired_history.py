# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel

from lionagi import Session
from lionagi._errors import InputTokenCapError
from lionagi.cli._runs import _make_message_handler
from lionagi.hooks import route_message_persistence, unroute_message_persistence
from lionagi.operations.parse.parse import ExtractionError
from lionagi.protocols.messages.assistant_response import AssistantResponse
from lionagi.session.branch import Branch
from lionagi.state.compat import StateCompatibilityRefused
from lionagi.state.db import StateDB
from lionagi.testing import LionAGIMockFactory
from tests.state.test_compatibility_owner_ops import (
    OWNER,
    _execute,
    _managed_store,
    _profile,
    _rows,
)


class Answer(BaseModel):
    answer: str


@pytest.mark.asyncio
async def test_successful_repair_replaces_the_assistant_turn_in_later_api_requests():
    original = "unstructured original response"
    repaired = '{"answer":"repaired value"}'
    branch = LionAGIMockFactory.create_mocked_branch(
        responses=[original, repaired, '{"answer":"later value"}']
    )
    assert not branch.chat_model.is_cli

    first = await branch.operate(
        instruction="First question",
        response_format=Answer,
        invoke_actions=False,
    )
    assert first.answer == "repaired value"
    await branch.operate(instruction="Next question", invoke_actions=False, skip_validation=True)

    assert branch.chat_model.invoke.await_count == 3
    messages = branch.chat_model.invoke.await_args_list[2].kwargs["messages"]
    assistant_turns = [message["content"] for message in messages if message["role"] == "assistant"]
    assert assistant_turns == [repaired]
    assert original not in assistant_turns


@pytest.mark.asyncio
async def test_repaired_turn_updates_the_stored_row_without_repeating_add_observers():
    original = "original invalid answer"
    repaired = '{"answer":"fixed"}'
    branch = LionAGIMockFactory.create_mocked_branch(responses=[original, repaired])
    session = Session(default_branch=branch)
    session_id, branch_id = str(session.id), str(branch.id)
    db = StateDB(":memory:")
    await db.open()
    seen_rows = []
    new_ids = []
    try:
        await db.create_progression("branch-progression")
        await db.create_progression("session-progression")
        await db.create_session(
            {"id": session_id, "progression_id": "session-progression", "status": "running"}
        )
        await db.create_branch(
            {"id": branch_id, "session_id": session_id, "progression_id": "branch-progression"}
        )
        callback = _make_message_handler(
            db,
            branch_id,
            session_id,
            "branch-progression",
            "session-progression",
            dedup_set=set(),
            new_msg_ids_list=new_ids,
        )
        route = route_message_persistence(session, branch, callback)

        async def observe(message):
            if isinstance(message, AssistantResponse):
                seen_rows.append(await db.get_message(str(message.id)))

        branch.on_message_added.append(observe)
        result = await branch.operate(
            instruction="Question", response_format=Answer, invoke_actions=False
        )
        assert result.answer == "fixed"
        message = next(item for item in branch.messages if isinstance(item, AssistantResponse))
        stored = await db.get_message(str(message.id))
        assert len(seen_rows) == 1
        assert seen_rows[0]["content"]["assistant_response"] == original
        assert stored["content"]["assistant_response"] == repaired
        assert stored["node_metadata"]["original_text"] == original
        assert "original_model_response" in stored["node_metadata"]
        assert new_ids.count(str(message.id)) == 1
        assert (await db.get_progression("branch-progression")).count(str(message.id)) == 1
        assert (await db.get_progression("session-progression")).count(str(message.id)) == 1
        unroute_message_persistence(branch, route)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_react_api_next_round_receives_repaired_analysis():
    original = "unstructured analysis response"
    repaired = '{"analysis":"corrected analysis","extension_needed":false}'
    branch = LionAGIMockFactory.create_mocked_branch(
        responses=[original, repaired, '{"answer":"done"}']
    )
    assert not branch.chat_model.is_cli
    result = await branch.ReAct(instruction="Question", response_format=Answer, max_extensions=0)
    assert branch.chat_model.invoke.await_count == 3, result
    messages = branch.chat_model.invoke.await_args_list[2].kwargs["messages"]
    assistant_turns = [message["content"] for message in messages if message["role"] == "assistant"]
    assert assistant_turns == [repaired]
    assert original not in assistant_turns


@pytest.mark.asyncio
async def test_first_valid_reply_stays_in_history_without_repair():
    original = '{"answer":"already valid"}'
    branch = LionAGIMockFactory.create_mocked_branch(responses=[original])
    result = await branch.operate(
        instruction="Question", response_format=Answer, invoke_actions=False
    )
    assert result.answer == "already valid"
    assert branch.chat_model.invoke.await_count == 1
    message = next(item for item in branch.messages if isinstance(item, AssistantResponse))
    assert message.response == original
    assert "original_text" not in message.metadata


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["raise", "return_none", "return_value"])
async def test_failed_repair_keeps_original_history(mode):
    original = "not structured"
    branch = LionAGIMockFactory.create_mocked_branch(
        responses=[original] + ["still not structured"] * 4
    )
    kwargs = dict(
        instruction="Question",
        response_format=Answer,
        invoke_actions=False,
        handle_validation=mode,
    )
    if mode == "raise":
        with pytest.raises(ExtractionError):
            await branch.operate(**kwargs)
    else:
        result = await branch.operate(**kwargs)
        assert (result is None) == (mode == "return_none")
    assert branch.chat_model.invoke.await_count == 5
    message = next(item for item in branch.messages if isinstance(item, AssistantResponse))
    assert message.response == original
    assert "original_text" not in message.metadata


@pytest.mark.asyncio
async def test_empty_reply_does_not_trigger_repair():
    branch = LionAGIMockFactory.create_mocked_branch(responses=[""])
    result = await branch.operate(
        instruction="Question",
        response_format=Answer,
        invoke_actions=False,
        handle_validation="return_none",
    )
    assert result is None
    assert branch.chat_model.invoke.await_count == 1
    message = next(item for item in branch.messages if isinstance(item, AssistantResponse))
    assert message.response == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "refusal",
    [
        InputTokenCapError("refused", details={"estimate": 9, "cap": 1}),
        StateCompatibilityRefused("persist_live_message", "messages@1", "refused"),
    ],
)
async def test_refused_repair_keeps_original_history(monkeypatch, refusal):
    original = "unstructured reply"
    branch = LionAGIMockFactory.create_mocked_branch(responses=[original])

    async def refuse(*args, **kwargs):
        raise refusal

    monkeypatch.setattr(Branch, "chat", refuse)
    with pytest.raises(type(refusal)):
        await branch.operate(
            instruction="Question",
            response_format=Answer,
            invoke_actions=False,
            handle_validation="return_value",
        )
    assert branch.chat_model.invoke.await_count == 1
    message = next(item for item in branch.messages if isinstance(item, AssistantResponse))
    assert message.response == original
    assert "original_text" not in message.metadata


@pytest.mark.asyncio
async def test_later_successful_retry_is_the_history_text():
    original = "unstructured"
    repaired = '{"answer":"finally"}'
    branch = LionAGIMockFactory.create_mocked_branch(
        responses=[original, "still unstructured", repaired, '{"answer":"next"}']
    )
    result = await branch.operate(
        instruction="Question", response_format=Answer, invoke_actions=False
    )
    assert result.answer == "finally"
    await branch.operate(instruction="Next", invoke_actions=False, skip_validation=True)
    messages = branch.chat_model.invoke.await_args_list[3].kwargs["messages"]
    assert [m["content"] for m in messages if m["role"] == "assistant"] == [repaired]
    message = next(item for item in branch.messages if isinstance(item, AssistantResponse))
    assert message.metadata["original_text"] == original


async def _routed_store(branch, db, *, queues=None, new_ids=None):
    session = Session(default_branch=branch)
    session_id, branch_id = str(session.id), str(branch.id)
    await db.create_progression("bp")
    await db.create_progression("sp")
    await db.create_session({"id": session_id, "progression_id": "sp", "status": "running"})
    await db.create_branch({"id": branch_id, "session_id": session_id, "progression_id": "bp"})
    callback = _make_message_handler(
        db,
        branch_id,
        session_id,
        "bp",
        "sp",
        dedup_set=set(),
        new_msg_ids_list=new_ids,
        message_retry_queues=queues,
    )
    handler = route_message_persistence(session, branch, callback)
    return session_id, handler


@pytest.mark.asyncio
async def test_repair_update_waits_behind_failed_original_write(monkeypatch):
    original, repaired = "unstructured", '{"answer":"fixed"}'
    branch = LionAGIMockFactory.create_mocked_branch(responses=[original, repaired])
    db = StateDB(":memory:")
    await db.open()
    queues, new_ids, writes = [], [], []
    original_persist = StateDB._persist_live_message
    attempts = 0

    async def interrupted(self, message, **kwargs):
        nonlocal attempts
        if message["role"] == "assistant":
            attempts += 1
            if attempts <= 2:
                raise RuntimeError("database is locked")
        await original_persist(self, message, **kwargs)
        if message["role"] == "assistant":
            writes.append((message, kwargs))

    monkeypatch.setattr(StateDB, "_persist_live_message", interrupted)
    try:
        _, handler = await _routed_store(branch, db, queues=queues, new_ids=new_ids)
        result = await branch.operate(
            instruction="Question", response_format=Answer, invoke_actions=False
        )
        assert result.answer == "fixed"
        message = next(item for item in branch.messages if isinstance(item, AssistantResponse))
        assert queues[0].pending_count == 2
        assert await db.get_message(str(message.id)) is None
        assert await queues[0].flush()
        assert [row[0]["content"]["assistant_response"] for row in writes] == [original, repaired]
        assert [row[0]["id"] for row in writes] == [str(message.id)] * 2
        assert [row[1]["branch_progression_id"] for row in writes] == ["bp", None]
        assert [row[1]["session_progression_id"] for row in writes] == ["sp", None]
        assert (await db.get_message(str(message.id)))["content"]["assistant_response"] == repaired
        assert new_ids.count(str(message.id)) == 1
        assert (await db.get_progression("bp")).count(str(message.id)) == 1
        assert (await db.get_progression("sp")).count(str(message.id)) == 1
        unroute_message_persistence(branch, handler)
    finally:
        await db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["deleted", "reowned"])
async def test_compat_store_refuses_repair_update_if_original_row_is_lost(tmp_path, change):
    path = await _managed_store(tmp_path / "managed.db")
    db = StateDB(path, compatibility=_profile(path), owner="principal:a")
    await db.open()
    branch = LionAGIMockFactory.create_mocked_branch(
        responses=["invalid original", '{"answer":"fixed"}']
    )
    try:
        _, handler = await _routed_store(branch, db)
        ids = []

        async def change_row(message):
            if isinstance(message, AssistantResponse):
                ids.append(str(message.id))
                if change == "deleted":
                    _execute(path, "DELETE FROM messages WHERE id = ?", (ids[0],))
                else:
                    _execute(
                        path, f"UPDATE messages SET {OWNER} = 'principal:b' WHERE id = ?", (ids[0],)
                    )

        branch.on_message_added.append(change_row)
        with pytest.raises(StateCompatibilityRefused, match="gone or no longer belongs"):
            await branch.operate(
                instruction="Question", response_format=Answer, invoke_actions=False
            )
        rows = _rows(path, f"SELECT {OWNER}, content FROM messages WHERE id = ?", (ids[0],))
        if change == "deleted":
            assert rows == []
        else:
            assert rows[0][0] == "principal:b"
            assert "invalid original" in rows[0][1]
        unroute_message_persistence(branch, handler)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_store_refusal_on_add_never_attempts_repair_update(tmp_path):
    path = await _managed_store(tmp_path / "managed.db")
    db = StateDB(path, compatibility=_profile(path), owner="principal:a")
    await db.open()
    branch = LionAGIMockFactory.create_mocked_branch(
        responses=["invalid original", '{"answer":"fixed"}']
    )
    try:
        session_id, handler = await _routed_store(branch, db)
        update = AsyncMock(wraps=branch._persist_message_update)
        branch._persist_message_update = update

        async def reown_session(message):
            if not isinstance(message, AssistantResponse):
                _execute(
                    path, f"UPDATE sessions SET {OWNER} = 'principal:b' WHERE id = ?", (session_id,)
                )

        branch.on_message_added.append(reown_session)
        with pytest.raises(StateCompatibilityRefused):
            await branch.operate(
                instruction="Question", response_format=Answer, invoke_actions=False
            )
        assert branch.chat_model.invoke.await_count == 1
        update.assert_not_awaited()
        unroute_message_persistence(branch, handler)
    finally:
        await db.close()
