# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Round-trip tests for UUID-containing JSON columns in StateDB — orjson serializer handles uuid.UUID where stdlib json.dumps would raise TypeError."""

from __future__ import annotations

import time
import uuid

import pytest

from lionagi.state.db import StateDB

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
async def db():
    state = StateDB(":memory:")
    await state.open()
    yield state
    await state.close()


def uid() -> str:
    return str(uuid.uuid4())


# ── Helpers ───────────────────────────────────────────────────────────────────


async def _make_session(db: StateDB) -> str:
    prog_id = uid()
    await db.create_progression(prog_id)
    session_id = uid()
    await db.create_session(
        {
            "id": session_id,
            "created_at": time.time(),
            "progression_id": prog_id,
        }
    )
    return session_id


async def _make_show(db: StateDB) -> str:
    show_id = uid()
    await db.create_show(
        {
            "id": show_id,
            "created_at": time.time(),
            "topic": "test-topic",
            "show_dir": "/tmp/test-show",
        }
    )
    return show_id


# ── Tests: UUID values round-trip through JSON columns ───────────────────────


@pytest.mark.unit
async def test_message_content_with_uuid_value_roundtrips(db: StateDB):
    """UUID values in message content/node_metadata must survive write+read via orjson (_to_json_column)."""
    raw_uuid = uuid.uuid4()
    msg_id = uid()
    await db.insert_message(
        {
            "id": msg_id,
            "created_at": time.time(),
            "node_metadata": {"ref_id": raw_uuid},  # UUID in node_metadata
            "content": {"trace_id": raw_uuid},  # UUID in content
            "role": "user",
            "sender": "test",
            "recipient": "test",
        }
    )

    row = await db.get_message(msg_id)
    assert row is not None, "message must be retrievable after insert"
    assert row["content"]["trace_id"] == str(raw_uuid)
    assert row["node_metadata"]["ref_id"] == str(raw_uuid)


@pytest.mark.unit
async def test_update_status_evidence_refs_with_uuid_roundtrips(db: StateDB):
    """evidence_refs dicts with UUID values must survive update_status via orjson-backed _json_dumps."""
    from lionagi.state.reasons import RunReasons

    session_id = await _make_session(db)
    raw_uuid = uuid.uuid4()

    # update_status requires entity_type + entity_id to exist.
    await db.update_status(
        entity_type="session",
        entity_id=session_id,
        new_status="running",
        reason_code=RunReasons.STARTED_OK,
        reason_summary="started",
        evidence_refs=[{"ref": raw_uuid}],  # dict with raw UUID — the exploit payload
        source="executor",
    )
    row = await db.get_session(session_id)
    assert row is not None


@pytest.mark.unit
async def test_update_status_metadata_with_uuid_roundtrips(db: StateDB):
    """metadata dicts with UUID objects must survive update_status via orjson-backed _json_dumps."""
    from lionagi.state.reasons import RunReasons

    session_id = await _make_session(db)
    raw_uuid = uuid.uuid4()

    await db.update_status(
        entity_type="session",
        entity_id=session_id,
        new_status="running",
        reason_code=RunReasons.STARTED_OK,
        reason_summary="started",
        metadata={"trace": raw_uuid},  # raw UUID object — the exploit payload
        source="executor",
    )
    row = await db.get_session(session_id)
    assert row is not None


@pytest.mark.unit
async def test_create_play_depends_on_with_uuid_roundtrips(db: StateDB):
    """depends_on lists with UUID objects must survive create_play via orjson-backed _json_dumps."""
    show_id = await _make_show(db)
    raw_uuid = uuid.uuid4()

    play_id = uid()
    now = time.time()
    await db.create_play(
        {
            "id": play_id,
            "show_id": show_id,
            "name": "test-play",
            "created_at": now,
            "depends_on": [raw_uuid],  # raw UUID object — the exploit payload
        }
    )

    row = await db.get_play(play_id)
    assert row is not None, "play must be retrievable after create"
    assert str(raw_uuid) in row["depends_on"]


@pytest.mark.unit
async def test_to_json_column_uuid_does_not_raise():
    """_to_json_column must not raise for dicts containing UUID values."""
    from lionagi.state.db import _to_json_column

    raw_uuid = uuid.uuid4()
    result = _to_json_column({"id": raw_uuid, "nested": {"ref": raw_uuid}})
    assert isinstance(result, str)
    assert str(raw_uuid) in result


@pytest.mark.unit
async def test_to_json_column_preserves_none_and_bytes():
    """_to_json_column must pass None and bytes through unchanged."""
    from lionagi.state.db import _to_json_column

    assert _to_json_column(None) is None
    b = b"\x00\x01\x02"
    assert _to_json_column(b) is b


@pytest.mark.unit
async def test_create_progression_with_uuid_ids_roundtrips(db: StateDB):
    """Progressions with UUID-derived string collections must roundtrip through _json_dumps."""
    prog_id = uid()
    initial = [uid(), uid()]
    await db.create_progression(prog_id, collection=initial)

    result = await db.get_progression(prog_id)
    assert result == initial
