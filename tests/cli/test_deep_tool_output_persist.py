# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Live message persistence keeps tool results that exceed JSON depth limits."""

from __future__ import annotations

import asyncio
import logging
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text

from lionagi.cli._runs import _make_message_handler
from lionagi.protocols.messages.action_request import ActionRequest, ActionRequestContent
from lionagi.protocols.messages.action_response import ActionResponse, ActionResponseContent
from lionagi.state.compat import StateCompatibilityRefused
from lionagi.state.db import StateDB

pytestmark = pytest.mark.asyncio

_SECRET = "deep-output-private-leaf"
_OUTPUT_MARKER = {"_lionagi_unserializable": {"field": "output", "exception": "TypeError"}}


def _nested(depth: int):
    value = _SECRET
    for _ in range(depth):
        value = [value]
    return value


def _cycle():
    value = []
    value.append(value)
    return value


@pytest_asyncio.fixture
async def history():
    db = StateDB(":memory:")
    await db.open()
    session_id, branch_id = str(uuid.uuid4()), str(uuid.uuid4())
    await db.create_progression("branch-progression")
    await db.create_progression("session-progression")
    await db.create_session(
        {"id": session_id, "progression_id": "session-progression", "status": "running"}
    )
    await db.create_branch(
        {"id": branch_id, "session_id": session_id, "progression_id": "branch-progression"}
    )
    added = []
    handler = _make_message_handler(
        db,
        branch_id,
        session_id,
        "branch-progression",
        "session-progression",
        dedup_set=set(),
        new_msg_ids_list=added,
    )
    try:
        yield db, handler, added
    finally:
        await db.close()


def _linked_messages(output, metadata=None):
    request = ActionRequest(content=ActionRequestContent(function="tool", arguments={}))
    response = ActionResponse(
        content=ActionResponseContent(
            function="tool", arguments={}, output=output, action_request_id=str(request.id)
        ),
        metadata={} if metadata is None else metadata,
    )
    request.content.action_response_id = str(response.id)
    return request, response


async def _assert_progressions(db, expected):
    assert await db.get_progression("branch-progression") == expected
    assert await db.get_progression("session-progression") == expected


@pytest.mark.parametrize("shape", ["depth-300", "depth-50000", "cycle"])
async def test_unserializable_output_keeps_linked_history_without_mutating_memory(
    history, caplog, shape
):
    db, persist, added = history
    output = _cycle() if shape == "cycle" else _nested(int(shape.removeprefix("depth-")))
    request, response = _linked_messages(output, {"owner": "kept"})
    content_before = response.content
    output_before = response.content.output
    metadata_before = dict(response.metadata)
    expected_ids = [str(request.id), str(response.id)]

    with caplog.at_level(logging.WARNING, logger="lionagi.cli"):
        await persist(request)
        await persist(response)

    request_row = await db.get_message(str(request.id))
    response_row = await db.get_message(str(response.id))
    assert request_row is not None
    assert response_row is not None
    assert request_row["content"]["action_response_id"] == str(response.id)
    assert response_row["content"]["action_request_id"] == str(request.id)
    assert response_row["content"]["output"] == _OUTPUT_MARKER
    assert response_row["node_metadata"]["lionagi_unserializable"] == ["output"]
    assert response_row["node_metadata"]["owner"] == "kept"
    assert response.content is content_before
    assert response.content.output is output_before
    assert dict(response.metadata) == metadata_before
    assert added == expected_ids
    await _assert_progressions(db, expected_ids)
    assert _SECRET not in caplog.text

    # An update to the already queued response must not add duplicate positions.
    await persist._persist_update(response)
    assert added == expected_ids
    await _assert_progressions(db, expected_ids)


@pytest.mark.parametrize(
    ("output", "metadata"),
    [
        ({"answer": [1, 2]}, {"owner": "kept"}),
        (_OUTPUT_MARKER, {"owner": "kept"}),
        ({"answer": "ordinary"}, {"lionagi_unserializable": ["user-value"]}),
    ],
    ids=["ordinary", "genuine-marker", "reserved-key-on-normal-row"],
)
async def test_serializable_output_keeps_existing_stored_bytes(history, output, metadata):
    db, persist, added = history
    _, response = _linked_messages(output, metadata)
    before = response.to_dict(mode="db")
    control = {**before, "id": str(uuid.uuid4())}
    await db.insert_message(control)

    await persist(response)

    async with db._read() as connection:
        saved = (
            await connection.execute(
                text("SELECT content, node_metadata FROM messages WHERE id = :id"),
                {"id": str(response.id)},
            )
        ).first()
        original = (
            await connection.execute(
                text("SELECT content, node_metadata FROM messages WHERE id = :id"),
                {"id": control["id"]},
            )
        ).first()
    assert saved is not None and original is not None
    assert tuple(saved) == tuple(original)
    row = await db.get_message(str(response.id))
    assert row["content"]["output"] == output
    assert row["node_metadata"] == before["node_metadata"]
    if metadata.get("lionagi_unserializable") is None:
        assert "lionagi_unserializable" not in row["node_metadata"]
    assert added == [str(response.id)]
    await _assert_progressions(db, [str(response.id)])


async def test_unserializable_output_reserved_metadata_collision_is_projection_only(
    history, caplog
):
    db, persist, added = history
    metadata = {"lionagi_unserializable": ["user-private-value"], "owner": "kept"}
    _, response = _linked_messages(_nested(300), metadata)
    before = dict(response.metadata)
    output_before = response.content.output

    with caplog.at_level(logging.WARNING, logger="lionagi.cli"):
        await persist(response)

    row = await db.get_message(str(response.id))
    assert row is not None
    assert row["content"]["output"] == _OUTPUT_MARKER
    assert row["node_metadata"]["lionagi_unserializable"] == ["output"]
    assert row["node_metadata"]["owner"] == "kept"
    assert dict(response.metadata) == before
    assert response.content.output is output_before
    assert len([r for r in caplog.records if "lionagi_unserializable" in r.getMessage()]) == 1
    assert "user-private-value" not in caplog.text
    assert _SECRET not in caplog.text
    assert added == [str(response.id)]
    await _assert_progressions(db, [str(response.id)])


async def test_deep_reserved_metadata_does_not_mark_a_shallow_output(history, caplog):
    db, persist, added = history
    deep_metadata = _nested(300)
    _, response = _linked_messages(
        {"answer": "ordinary"}, {"lionagi_unserializable": deep_metadata}
    )
    metadata_before = response.metadata["lionagi_unserializable"]
    output_before = response.content.output

    with caplog.at_level(logging.WARNING, logger="lionagi.cli"):
        await persist(response)

    # The pre-existing error path drops this unrelated unserializable row;
    # it must not be silently rewritten as a problem with its shallow output.
    assert await db.get_message(str(response.id)) is None
    assert response.metadata["lionagi_unserializable"] is metadata_before
    assert response.content.output is output_before
    assert added == []
    await _assert_progressions(db, [])
    assert any("live persist write failed" in record.getMessage() for record in caplog.records)
    assert not any(
        "replaced reserved metadata key" in record.getMessage() for record in caplog.records
    )


async def test_recursion_error_at_serialization_marks_only_the_durable_output(
    history, monkeypatch, caplog
):
    db, persist, added = history
    _, response = _linked_messages({"answer": "ordinary"}, {"owner": "kept"})
    content_before = response.content
    output_before = response.content.output
    metadata_before = dict(response.metadata)
    serialize = ActionResponse.to_dict

    def fail_original_only(message, *args, **kwargs):
        if message is response:
            raise RecursionError(_SECRET)
        return serialize(message, *args, **kwargs)

    monkeypatch.setattr(ActionResponse, "to_dict", fail_original_only)
    with caplog.at_level(logging.WARNING, logger="lionagi.cli"):
        await persist(response)

    row = await db.get_message(str(response.id))
    assert row is not None
    assert row["content"]["output"] == {
        "_lionagi_unserializable": {"field": "output", "exception": "RecursionError"}
    }
    assert row["node_metadata"]["lionagi_unserializable"] == ["output"]
    assert row["node_metadata"]["owner"] == "kept"
    assert response.content is content_before
    assert response.content.output is output_before
    assert dict(response.metadata) == metadata_before
    assert _SECRET not in caplog.text
    assert added == [str(response.id)]
    await _assert_progressions(db, [str(response.id)])


@pytest.mark.parametrize("failure", ["compatibility", "cancellation"])
async def test_unserializable_output_does_not_swallow_store_refusal_or_cancellation(
    history, monkeypatch, failure
):
    db, persist, added = history
    request, response = _linked_messages(_nested(300))
    await persist(request)
    refusal = StateCompatibilityRefused("persist_live_message", "messages@1", "refused")
    error = refusal if failure == "compatibility" else asyncio.CancelledError("cancelled")

    async def refuse_write(*args, **kwargs):
        raise error

    monkeypatch.setattr(db, "_persist_live_message", refuse_write)
    with pytest.raises(type(error)) as caught:
        await persist(response)
    assert caught.value is error
    assert await db.get_message(str(response.id)) is None
    assert added == [str(request.id)]
    await _assert_progressions(db, [str(request.id)])
