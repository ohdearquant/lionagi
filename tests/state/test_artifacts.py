# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for lionagi.state.artifacts.ArtifactStore.

Covers write/read round-trips, SHA-256 integrity, query filters, and
the append-only surface invariant.
"""

from __future__ import annotations

import time
import uuid

import pytest

from lionagi.state.artifacts import ArtifactRow, ArtifactStore
from lionagi.state.db import StateDB

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
async def db():
    """Fresh in-memory StateDB for each test."""
    state = StateDB(":memory:")
    await state.open()
    yield state
    await state.close()


@pytest.fixture
async def store(db):
    """ArtifactStore backed by in-memory StateDB."""
    return ArtifactStore(db)


async def _make_invocation(db: StateDB, skill: str = "test-skill") -> dict:
    inv = {
        "id": uuid.uuid4().hex[:12],
        "skill": skill,
        "started_at": time.time(),
    }
    await db.create_invocation(inv)
    return inv


async def _make_session(db: StateDB) -> dict:
    prog_id = str(uuid.uuid4())
    await db.create_progression(prog_id)
    s = {"id": str(uuid.uuid4()), "progression_id": prog_id}
    await db.create_session(s)
    return s


# ── Write and read round-trip ─────────────────────────────────────────────────


async def test_write_and_read_round_trip(store: ArtifactStore, db: StateDB):
    """Write an artifact then query it back; all fields should survive intact."""
    inv = await _make_invocation(db)
    content = {"passed": True, "summary": "all green", "count": 42}

    row = await store.write(
        kind="ci_result",
        name="pytest-run-1",
        content=content,
        invocation_id=inv["id"],
    )

    assert isinstance(row, ArtifactRow)
    assert row.kind == "ci_result"
    assert row.name == "pytest-run-1"
    assert row.content == content
    assert row.invocation_id == inv["id"]
    assert row.session_id is None
    assert row.sha256  # non-empty
    assert row.created_at > 0
    assert row.updated_at > 0

    # Fetch the same artifact via get()
    fetched = await store.get(row.id)
    assert fetched is not None
    assert fetched.id == row.id
    assert fetched.content == content
    assert fetched.sha256 == row.sha256


async def test_write_returns_artifact_row_instance(store: ArtifactStore, db: StateDB):
    """write() always returns an ArtifactRow, not a raw dict or str."""
    inv = await _make_invocation(db)
    result = await store.write(
        kind="review_verdict",
        name="round-1",
        content={"verdict": "APPROVE"},
        invocation_id=inv["id"],
    )
    assert isinstance(result, ArtifactRow)


# ── SHA-256 verification ──────────────────────────────────────────────────────


async def test_sha256_verification_positive(store: ArtifactStore, db: StateDB):
    """verify() returns True for an untampered artifact."""
    inv = await _make_invocation(db)
    row = await store.write(
        kind="ci_result",
        name="run-1",
        content={"passed": True},
        invocation_id=inv["id"],
    )
    assert store.verify(row) is True


async def test_verify_detects_tampered_content(store: ArtifactStore, db: StateDB):
    """verify() returns False when content is mutated after retrieval."""
    inv = await _make_invocation(db)
    row = await store.write(
        kind="ci_result",
        name="run-2",
        content={"passed": True},
        invocation_id=inv["id"],
    )
    # Tamper: modify a field in-place
    row.content["passed"] = False
    assert store.verify(row) is False


async def test_verify_detects_added_key(store: ArtifactStore, db: StateDB):
    """verify() returns False when an extra key is injected into content."""
    inv = await _make_invocation(db)
    row = await store.write(
        kind="ci_result",
        name="run-3",
        content={"passed": True},
        invocation_id=inv["id"],
    )
    row.content["injected"] = "evil"
    assert store.verify(row) is False


async def test_verify_empty_sha256_returns_false(store: ArtifactStore):
    """verify() returns False when the sha256 field is empty."""
    row = ArtifactRow(
        id="abc123",
        kind="ci_result",
        name="x",
        content={"k": "v"},
        sha256="",
        created_at=1.0,
        updated_at=1.0,
    )
    assert store.verify(row) is False


# ── Query filters ─────────────────────────────────────────────────────────────


async def test_query_by_invocation_id(store: ArtifactStore, db: StateDB):
    """query(invocation_id=...) returns only artifacts for that invocation."""
    inv1 = await _make_invocation(db, skill="skill-a")
    inv2 = await _make_invocation(db, skill="skill-b")

    await store.write(kind="ci_result", name="r1", content={"n": 1}, invocation_id=inv1["id"])
    await store.write(kind="ci_result", name="r2", content={"n": 2}, invocation_id=inv1["id"])
    await store.write(kind="ci_result", name="r3", content={"n": 3}, invocation_id=inv2["id"])

    results = await store.query(invocation_id=inv1["id"])
    assert len(results) == 2
    names = {r.name for r in results}
    assert names == {"r1", "r2"}


async def test_query_by_session_id(store: ArtifactStore, db: StateDB):
    """query(session_id=...) returns only artifacts for that session."""
    ses1 = await _make_session(db)
    ses2 = await _make_session(db)

    await store.write(
        kind="gate_verdict", name="g1", content={"passed": True}, session_id=ses1["id"]
    )
    await store.write(
        kind="gate_verdict", name="g2", content={"passed": False}, session_id=ses2["id"]
    )

    results = await store.query(session_id=ses1["id"])
    assert len(results) == 1
    assert results[0].name == "g1"


async def test_query_by_kind_filter(store: ArtifactStore, db: StateDB):
    """query(invocation_id=..., kind=...) filters down to the requested kind."""
    inv = await _make_invocation(db)

    await store.write(kind="ci_result", name="ci", content={"ok": True}, invocation_id=inv["id"])
    await store.write(
        kind="review_verdict", name="rv", content={"v": "APPROVE"}, invocation_id=inv["id"]
    )

    ci_results = await store.query(invocation_id=inv["id"], kind="ci_result")
    assert len(ci_results) == 1
    assert ci_results[0].kind == "ci_result"

    rv_results = await store.query(invocation_id=inv["id"], kind="review_verdict")
    assert len(rv_results) == 1
    assert rv_results[0].kind == "review_verdict"


async def test_query_kind_filter_no_matches(store: ArtifactStore, db: StateDB):
    """query returns [] when kind filter matches nothing."""
    inv = await _make_invocation(db)
    await store.write(kind="ci_result", name="ci", content={}, invocation_id=inv["id"])

    results = await store.query(invocation_id=inv["id"], kind="nonexistent_kind")
    assert results == []


async def test_query_empty_invocation_returns_empty(store: ArtifactStore, db: StateDB):
    """query() with a valid but artifact-free invocation returns []."""
    inv = await _make_invocation(db)
    results = await store.query(invocation_id=inv["id"])
    assert results == []


# ── Append-only surface ───────────────────────────────────────────────────────


async def test_append_only_no_update_method(store: ArtifactStore):
    """ArtifactStore must not expose update or delete methods."""
    assert not hasattr(store, "update"), "ArtifactStore must not expose .update()"
    assert not hasattr(store, "delete"), "ArtifactStore must not expose .delete()"
    assert not hasattr(store, "remove"), "ArtifactStore must not expose .remove()"


# ── get() edge cases ─────────────────────────────────────────────────────────


async def test_get_missing_id_returns_none(store: ArtifactStore):
    """get() returns None for an unknown artifact id."""
    result = await store.get("not_a_real_id")
    assert result is None


# ── Write idempotency (inherited from StateDB) ────────────────────────────────


async def test_write_idempotent_preserves_id(store: ArtifactStore, db: StateDB):
    """Calling write() twice with the same natural key returns the same id."""
    inv = await _make_invocation(db)

    first = await store.write(
        kind="ci_result", name="run", content={"n": 1}, invocation_id=inv["id"]
    )
    second = await store.write(
        kind="ci_result", name="run", content={"n": 2}, invocation_id=inv["id"]
    )

    assert first.id == second.id


async def test_write_idempotent_updates_sha256(store: ArtifactStore, db: StateDB):
    """Second write with updated content produces a new SHA-256."""
    inv = await _make_invocation(db)

    first = await store.write(
        kind="ci_result", name="run", content={"n": 1}, invocation_id=inv["id"]
    )
    second = await store.write(
        kind="ci_result", name="run", content={"n": 2}, invocation_id=inv["id"]
    )

    # Different content → different hash
    assert first.sha256 != second.sha256
    # New hash still verifies correctly
    assert store.verify(second) is True


# ── File path passthrough ─────────────────────────────────────────────────────


async def test_write_with_file_path(store: ArtifactStore, db: StateDB):
    """file_path is stored and returned when provided."""
    inv = await _make_invocation(db)
    row = await store.write(
        kind="log",
        name="build-log",
        content={"lines": 100},
        invocation_id=inv["id"],
        file_path="/tmp/build.log",
    )
    assert row.file_path == "/tmp/build.log"

    fetched = await store.get(row.id)
    assert fetched is not None
    assert fetched.file_path == "/tmp/build.log"
