# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Payload-shape regression tests for StateDB.insert_message — pins NOT NULL invariants and verifies large payload roundtrip up to ~1MB."""

from __future__ import annotations

import json
import time
import uuid

import pytest

from lionagi.state.db import StateDB


@pytest.fixture
async def db():
    state = StateDB(":memory:")
    await state.open()
    yield state
    await state.close()


def _base_msg(**overrides) -> dict:
    msg = {
        "id": str(uuid.uuid4()),
        "created_at": time.time(),
        "node_metadata": {},
        "content": {"text": "hello"},
        "role": "user",
        "sender": "u",
        "recipient": "a",
        "channel": "c",
    }
    msg.update(overrides)
    return msg


# ── Required-field rejection ─────────────────────────────────────────────────


async def test_insert_message_rejects_null_content(db: StateDB):
    """content is NOT NULL; ValueError is raised before SQLite can silently swallow the violation through INSERT OR IGNORE."""
    msg = _base_msg(content=None)
    with pytest.raises(ValueError, match="content is NOT NULL"):
        await db.insert_message(msg)


async def test_insert_message_rejects_empty_role(db: StateDB):
    """role must be a non-empty string."""
    msg = _base_msg(role="")
    with pytest.raises(ValueError, match="role must be a non-empty string"):
        await db.insert_message(msg)


async def test_insert_message_rejects_non_string_role(db: StateDB):
    msg = _base_msg(role=42)
    with pytest.raises(ValueError, match="role must be a non-empty string"):
        await db.insert_message(msg)


async def test_insert_message_rejects_missing_role(db: StateDB):
    msg = _base_msg()
    del msg["role"]
    with pytest.raises(ValueError, match="role must be a non-empty string"):
        await db.insert_message(msg)


# ── Large-payload roundtrip ──────────────────────────────────────────────────


@pytest.mark.parametrize("size_kb", [10, 100, 1024])
async def test_insert_message_roundtrips_large_content(
    db: StateDB,
    size_kb: int,
):
    """Content up to ~1MB must roundtrip without truncation or encoding damage."""
    payload = "x" * (size_kb * 1024)
    msg = _base_msg(content={"text": payload})
    await db.insert_message(msg)

    got = await db.get_message(msg["id"])
    assert got is not None
    content = got["content"]
    if isinstance(content, str):
        content = json.loads(content)
    assert content["text"] == payload


async def test_insert_message_handles_deep_nested_metadata(db: StateDB):
    """Deeply-nested node_metadata must roundtrip without collapsing or escaping pathologies."""
    deep = {"level": 0}
    cursor = deep
    for i in range(1, 50):
        cursor["nested"] = {"level": i}
        cursor = cursor["nested"]

    msg = _base_msg(node_metadata=deep)
    await db.insert_message(msg)

    got = await db.get_message(msg["id"])
    assert got is not None
    nm = got["node_metadata"]
    if isinstance(nm, str):
        nm = json.loads(nm)
    # Walk back down and confirm depth.
    cursor = nm
    depth = 0
    while "nested" in cursor:
        cursor = cursor["nested"]
        depth += 1
    assert depth == 49


# ── Re-fire of mutated message (ON CONFLICT DO UPDATE) ───────────────────────


async def test_insert_message_re_fire_updates_content(db: StateDB):
    """ON CONFLICT DO UPDATE: a re-fire with a mutated message must overwrite the stored content, not silently keep the old row."""
    msg = _base_msg(content={"text": "first"})
    await db.insert_message(msg)

    msg2 = dict(msg)
    msg2["content"] = {"text": "second"}
    msg2["sender"] = "different-sender"
    await db.insert_message(msg2)

    got = await db.get_message(msg["id"])
    content = got["content"]
    if isinstance(content, str):
        content = json.loads(content)
    assert content["text"] == "second"
    assert got["sender"] == "different-sender"

    from sqlalchemy import text

    async with db._read() as conn:
        row = (
            (
                await conn.execute(
                    text("SELECT COUNT(*) AS n FROM messages WHERE id = :id"),
                    {"id": msg["id"]},
                )
            )
            .mappings()
            .first()
        )
    assert row["n"] == 1


# ── Unicode + binary embedding ───────────────────────────────────────────────


async def test_insert_message_roundtrips_unicode_content(db: StateDB):
    payload = "你好 🦁 lionagi café 日本語"
    msg = _base_msg(content={"text": payload})
    await db.insert_message(msg)

    got = await db.get_message(msg["id"])
    content = got["content"]
    if isinstance(content, str):
        content = json.loads(content)
    assert content["text"] == payload


async def test_insert_message_roundtrips_embedding_blob(db: StateDB):
    import struct

    # A small float32 packed blob mimicking an embedding vector.
    vec = struct.pack("4f", 0.1, 0.2, 0.3, 0.4)
    msg = _base_msg(embedding=vec)
    await db.insert_message(msg)

    got = await db.get_message(msg["id"])
    assert got["embedding"] == vec
