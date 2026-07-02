# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""ADR-0021 artifacts table tests."""

from __future__ import annotations

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


async def _make_invocation(db: StateDB, **fields) -> dict:
    inv = {
        "id": uuid.uuid4().hex[:12],
        "skill": fields.pop("skill", "codex-pr-review"),
        "started_at": fields.pop("started_at", time.time()),
        **fields,
    }
    await db.create_invocation(inv)
    return inv


async def _make_session(db: StateDB, **fields) -> dict:
    prog_id = str(uuid.uuid4())
    await db.create_progression(prog_id)
    s = {"id": str(uuid.uuid4()), "progression_id": prog_id, **fields}
    await db.create_session(s)
    return s


# ── Insert + read ────────────────────────────────────────────────────────────


async def test_insert_artifact_with_invocation_link(db: StateDB):
    inv = await _make_invocation(db)
    art_id = await db.insert_artifact(
        invocation_id=inv["id"],
        kind="review_verdict",
        name="Round 1 verdict",
        content={"verdict": "APPROVE", "summary": "LGTM", "round": 1},
    )
    assert isinstance(art_id, str) and len(art_id) == 12

    rows = await db.list_artifacts_for_invocation(inv["id"])
    assert len(rows) == 1
    assert rows[0]["kind"] == "review_verdict"
    assert rows[0]["name"] == "Round 1 verdict"


async def test_insert_artifact_with_session_link(db: StateDB):
    s = await _make_session(db, status="completed")
    await db.insert_artifact(
        session_id=s["id"],
        kind="gate_verdict",
        name="play-gate",
        content={"summary": "ok", "gate_passed": True, "feedback": "all green", "passed": True},
    )
    rows = await db.list_artifacts_for_session(s["id"])
    assert len(rows) == 1
    assert rows[0]["kind"] == "gate_verdict"


async def test_get_artifact(db: StateDB):
    inv = await _make_invocation(db)
    art_id = await db.insert_artifact(
        invocation_id=inv["id"],
        kind="ci_result",
        name="CI",
        content={"summary": "all green", "passed": True},
    )
    row = await db.get_artifact(art_id)
    assert row is not None
    assert row["kind"] == "ci_result"


async def test_get_artifact_missing_returns_none(db: StateDB):
    assert await db.get_artifact("notarealid") is None


# ── Validation ───────────────────────────────────────────────────────────────


async def test_insert_artifact_requires_kind(db: StateDB):
    with pytest.raises(ValueError, match="kind is required"):
        await db.insert_artifact(kind="", name="x", content={})


async def test_insert_artifact_requires_name(db: StateDB):
    with pytest.raises(ValueError, match="name is required"):
        await db.insert_artifact(kind="x", name="", content={})


# ── FK cascade ────────────────────────────────────────────────────────────────


async def test_artifacts_cascade_when_invocation_deleted(db: StateDB):
    """Invocation FK uses ON DELETE CASCADE — orphan artifacts shouldn't linger."""
    inv = await _make_invocation(db)
    art_id = await db.insert_artifact(
        invocation_id=inv["id"],
        kind="review_verdict",
        name="x",
        content={"verdict": "APPROVE"},
    )
    await db.db.execute("DELETE FROM invocations WHERE id = ?", (inv["id"],))
    await db.db.commit()
    assert await db.get_artifact(art_id) is None


# ── Idempotency ───────────────────────────────────────────────────────────────


async def test_insert_artifact_is_idempotent(db: StateDB):
    """Upsert keeps count at 1 when the same natural key is reused."""
    inv = await _make_invocation(db)
    for _ in range(2):
        await db.insert_artifact(
            invocation_id=inv["id"],
            kind="review_verdict",
            name="Round 1",
            content={"verdict": "APPROVE"},
        )
    rows = await db.list_artifacts_for_invocation(inv["id"])
    assert len(rows) == 1


async def test_insert_artifact_preserves_id_on_upsert(db: StateDB):
    """Second insert with the same natural key must return the original id."""
    inv = await _make_invocation(db)
    first_id = await db.insert_artifact(
        invocation_id=inv["id"],
        kind="review_verdict",
        name="Round 1",
        content={"verdict": "REQUEST_CHANGES"},
    )
    second_id = await db.insert_artifact(
        invocation_id=inv["id"],
        kind="review_verdict",
        name="Round 1",
        content={"verdict": "APPROVE"},
    )
    assert first_id == second_id, "upsert must not generate a new id on conflict"


async def test_insert_artifact_idempotent_updates_content(db: StateDB):
    """Second insert with the same key replaces the content."""
    inv = await _make_invocation(db)
    await db.insert_artifact(
        invocation_id=inv["id"],
        kind="review_verdict",
        name="Round 1",
        content={"verdict": "REQUEST_CHANGES"},
    )
    await db.insert_artifact(
        invocation_id=inv["id"],
        kind="review_verdict",
        name="Round 1",
        content={"verdict": "APPROVE"},
    )
    rows = await db.list_artifacts_for_invocation(inv["id"])
    assert len(rows) == 1
    assert rows[0]["content"]["verdict"] == "APPROVE"


# ── Ordering ──────────────────────────────────────────────────────────────────


async def test_list_artifacts_ordered_by_created_at(db: StateDB):
    import asyncio

    inv = await _make_invocation(db)
    ids = []
    for i in range(3):
        ids.append(
            await db.insert_artifact(
                invocation_id=inv["id"],
                kind="review_verdict",
                name=f"round-{i + 1}",
                content={"verdict": "APPROVE", "round": i + 1},
            )
        )
        await asyncio.sleep(0.01)

    rows = await db.list_artifacts_for_invocation(inv["id"])
    assert [r["id"] for r in rows] == ids
