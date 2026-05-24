# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""ADR-0029 DB integration tests for artifact_contract_json / artifact_verification_json."""

from __future__ import annotations

import sqlite3
import tempfile
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


def uid() -> str:
    return str(uuid.uuid4())


async def _make_session(db: StateDB, **fields) -> str:
    prog_id = uid()
    await db.create_progression(prog_id)
    sid = uid()
    await db.create_session(
        {"id": sid, "progression_id": prog_id, "created_at": time.time(), **fields}
    )
    return sid


# ── test_create_session_with_contract ────────────────────────────────────────


async def test_create_session_with_contract(db: StateDB):
    """artifact_contract_json is stored and decoded on fetch."""
    contract = {"expected": [{"id": "report", "path": "report.md", "required": True}]}
    sid = await _make_session(db, artifact_contract_json=contract)
    row = await db.get_session(sid)
    assert row is not None
    stored = row["artifact_contract_json"]
    assert isinstance(stored, dict)
    assert stored["expected"][0]["id"] == "report"


# ── test_create_session_without_contract ─────────────────────────────────────


async def test_create_session_without_contract(db: StateDB):
    """Sessions without artifact_contract_json store NULL and raise no error."""
    sid = await _make_session(db)
    row = await db.get_session(sid)
    assert row is not None
    assert row["artifact_contract_json"] is None


# ── test_update_artifact_verification ────────────────────────────────────────


async def test_update_artifact_verification(db: StateDB):
    """update_artifact_verification() persists and round-trips the verification dict."""
    sid = await _make_session(db)
    verification = {
        "status": "passed",
        "checked_at": time.time(),
        "missing_required": [],
        "missing_optional": [],
        "produced": [{"id": "report", "path": "report.md", "size": 128, "present": True}],
    }
    await db.update_artifact_verification(sid, verification)
    row = await db.get_session(sid)
    assert row is not None
    stored = row["artifact_verification_json"]
    assert isinstance(stored, dict)
    assert stored["status"] == "passed"
    assert stored["produced"][0]["id"] == "report"


# ── test_migration_adds_columns ───────────────────────────────────────────────


async def test_migration_adds_columns():
    """reconcile_schema() adds both new columns to a pre-ADR-0029 DB."""
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        db_path = f.name
        # Build an old-style schema without the two new columns.
        old = sqlite3.connect(db_path)
        old.executescript(
            """
            CREATE TABLE IF NOT EXISTS progressions (
                id TEXT PRIMARY KEY,
                created_at REAL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                progression_id TEXT,
                status TEXT,
                created_at REAL
            );
            """
        )
        old.close()

        # Open with current StateDB — reconcile_schema() should add the columns.
        state = StateDB(db_path)
        await state.open()
        try:
            cur = await state.db.execute("PRAGMA table_info(sessions)")
            cols = {r["name"] for r in await cur.fetchall()}
            assert "artifact_contract_json" in cols, "migration missing artifact_contract_json"
            assert "artifact_verification_json" in cols, (
                "migration missing artifact_verification_json"
            )
        finally:
            await state.close()
