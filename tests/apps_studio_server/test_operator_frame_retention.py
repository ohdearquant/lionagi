# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""The durable Operator frame record must be bounded and say when it was capped."""

from __future__ import annotations

import pytest

from lionagi.studio.operator import store as store_mod
from lionagi.studio.operator.store import OperatorStore
from lionagi.studio.services._db import open_db


async def _start_turn(store: OperatorStore) -> tuple[str, str]:
    conversation = await store.create_conversation(title="Retention")
    conversation_id = conversation["id"]
    accepted = await store.submit_turn(
        conversation_id,
        instruction="hello",
        context={"space": "mission", "route": "/", "filters": {}},
        expected_last_sequence=0,
    )
    request_id = accepted["requestId"]
    assert await store.mark_running(request_id)
    return conversation_id, request_id


async def _stored(store: OperatorStore, request_id: str) -> tuple[int, int]:
    """Return the real row count and payload bytes SQLite holds for one turn."""
    async with open_db(str(store.path())) as db:
        row = await (
            await db.execute(
                "SELECT COUNT(*) AS rows_stored, "
                "COALESCE(SUM(LENGTH(CAST(payload_json AS BLOB))), 0) AS bytes_stored "
                "FROM studio_operator_frames WHERE request_id=?",
                (request_id,),
            )
        ).fetchone()
    return int(row["rows_stored"]), int(row["bytes_stored"])


@pytest.mark.asyncio
async def test_frame_count_cap_bounds_rows_and_records_what_it_elided(tmp_path, monkeypatch):
    frame_limit = 12
    monkeypatch.setattr(store_mod, "MAX_FRAMES_PER_TURN", frame_limit, raising=False)
    store = OperatorStore(tmp_path / "state.db")
    conversation_id, request_id = await _start_turn(store)

    for index in range(200):
        await store.append_frame(
            conversation_id,
            request_id,
            "text",
            {"content": f"chunk {index}", "format": "plain", "role": "assistant"},
        )

    rows, _ = await _stored(store, request_id)
    # The refused frames collapse into one summary row, so the turn stays bounded.
    assert rows <= frame_limit + 1

    frames = await store.list_frames(conversation_id, limit=10_000)
    summaries = [f for f in frames if f["type"] == store_mod.TRUNCATION_FRAME_TYPE]
    assert len(summaries) == 1
    summary = summaries[0]["payload"]
    assert summary["reason"] == "frames_per_turn"
    assert summary["limits"]["maxFramesPerTurn"] == frame_limit
    stored_text = len([f for f in frames if f["type"] == "text"])
    # 200 appended + the submit frame, minus what actually survived.
    assert summary["elidedFrames"] == 201 - stored_text
    assert summary["elidedFrameTypes"] == {"text": summary["elidedFrames"]}
    assert summary["elidedBytes"] > 0
    assert summary["message"]

    # A terminal frame is never refused: the turn can always be closed.
    await store.finish_turn(request_id, outcome="completed")
    frames = await store.list_frames(conversation_id, limit=10_000)
    assert frames[-1]["type"] == "done"


@pytest.mark.asyncio
async def test_turn_byte_cap_bounds_stored_bytes_and_names_the_limit(tmp_path, monkeypatch):
    byte_limit = 8 * 1024
    monkeypatch.setattr(store_mod, "MAX_TURN_PAYLOAD_BYTES", byte_limit, raising=False)
    store = OperatorStore(tmp_path / "state.db")
    conversation_id, request_id = await _start_turn(store)

    for _ in range(200):
        await store.append_frame(
            conversation_id,
            request_id,
            "text",
            {"content": "x" * 512, "format": "plain", "role": "assistant"},
        )

    _, stored_bytes = await _stored(store, request_id)
    # Only the single summary row may be written past the cap.
    assert stored_bytes <= byte_limit + 1024

    frames = await store.list_frames(conversation_id, limit=10_000)
    summaries = [f for f in frames if f["type"] == store_mod.TRUNCATION_FRAME_TYPE]
    assert len(summaries) == 1
    summary = summaries[0]["payload"]
    assert summary["reason"] == "turn_payload_bytes"
    assert summary["limits"]["maxTurnPayloadBytes"] == byte_limit
    assert summary["elidedFrames"] > 0
    assert summary["elidedBytes"] >= summary["elidedFrames"] * 512


@pytest.mark.asyncio
async def test_oversized_payload_is_stored_truncated_and_reports_the_original_size(tmp_path):
    store = OperatorStore(tmp_path / "state.db")
    conversation_id, request_id = await _start_turn(store)
    oversized = "a" * (256 * 1024)

    frame = await store.append_frame(
        conversation_id,
        request_id,
        "text",
        {"content": oversized, "format": "plain", "role": "assistant"},
    )

    assert frame is not None
    note = frame["payload"]["truncation"]
    assert note["reason"] == "frame_payload_bytes"
    assert note["originalBytes"] > store_mod.MAX_FRAME_PAYLOAD_BYTES
    assert note["storedBytes"] <= store_mod.MAX_FRAME_PAYLOAD_BYTES
    assert "bytes elided" in frame["payload"]["content"]
    assert len(frame["payload"]["content"]) < len(oversized)

    _, stored_bytes = await _stored(store, request_id)
    assert stored_bytes <= store_mod.MAX_FRAME_PAYLOAD_BYTES + 1024

    # The truncated payload is what a reader gets back, not the original.
    frames = await store.list_frames(conversation_id, limit=10_000)
    assert frames[-1]["payload"]["truncation"]["originalBytes"] == note["originalBytes"]


@pytest.mark.asyncio
async def test_normal_short_turn_is_stored_complete_and_unmodified(tmp_path):
    """Control: the cap must not pass by truncating an ordinary turn."""
    store = OperatorStore(tmp_path / "state.db")
    conversation_id, request_id = await _start_turn(store)
    payloads = [
        {"content": f"line {index}", "format": "plain", "role": "assistant"} for index in range(5)
    ]
    for payload in payloads:
        assert (await store.append_frame(conversation_id, request_id, "text", payload)) is not None
    await store.finish_turn(request_id, outcome="completed")

    frames = await store.list_frames(conversation_id, limit=10_000)
    assert [frame["type"] for frame in frames] == ["text"] * 6 + ["done"]
    assert [frame["sequence"] for frame in frames] == list(range(1, 8))
    assert [frame["payload"] for frame in frames[1:6]] == payloads
    assert not any(
        frame["type"] == "truncation" or "truncation" in frame["payload"] for frame in frames
    )
