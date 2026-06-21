# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Round-trip tests: status_reason_code/status_reason_summary flow through
both serializer paths in lionagi.studio.services.invocations."""

from __future__ import annotations

import time
import uuid

import lionagi.state.db as state_db_mod
import lionagi.studio.services.invocations as invocations_mod
from lionagi.state.db import StateDB
from lionagi.state.reasons import RunReasons


async def _create_invocation(db: StateDB, *, status: str = "running") -> str:
    inv_id = uuid.uuid4().hex[:12]
    now = time.time()
    await db.db.execute(
        "INSERT INTO invocations (id, skill, status, created_at, started_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (inv_id, "test:skill", status, now, now, now),
    )
    await db.db.commit()
    return inv_id


async def test_get_invocation_returns_reason_fields_when_set(tmp_path, monkeypatch):
    """get_invocation serializer includes status_reason_code and status_reason_summary
    with their exact DB values when the invocation has been transitioned to a terminal
    status with a reason."""
    db_path = tmp_path / "state.db"
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(invocations_mod, "DEFAULT_DB_PATH", db_path)

    async with StateDB(db_path) as db:
        inv_id = await _create_invocation(db)
        await db.update_invocation(
            inv_id,
            status="failed",
            ended_at=time.time(),
            reason_code=RunReasons.FAILED_EXCEPTION,
            reason_summary="RuntimeError: boom",
            evidence_refs=[{"kind": "session", "id": "s-1"}],
        )

    result = await invocations_mod.get_invocation(inv_id)

    assert result is not None
    assert result["status_reason_code"] == RunReasons.FAILED_EXCEPTION
    assert result["status_reason_summary"] == "RuntimeError: boom"
    assert result["status_evidence_refs"] == [{"kind": "session", "id": "s-1"}]


async def test_get_invocation_returns_none_reason_fields_when_unset(tmp_path, monkeypatch):
    """get_invocation serializer returns None for both reason fields when the
    invocation has never been transitioned (columns are NULL)."""
    db_path = tmp_path / "state.db"
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(invocations_mod, "DEFAULT_DB_PATH", db_path)

    async with StateDB(db_path) as db:
        inv_id = await _create_invocation(db)

    result = await invocations_mod.get_invocation(inv_id)

    assert result is not None
    assert result["status_reason_code"] is None
    assert result["status_reason_summary"] is None
    assert result["status_evidence_refs"] is None


async def test_list_invocations_includes_reason_fields(tmp_path, monkeypatch):
    """list_invocations serializer includes status_reason_code and
    status_reason_summary with exact values for both the set and null cases."""
    db_path = tmp_path / "state.db"
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(invocations_mod, "DEFAULT_DB_PATH", db_path)

    async with StateDB(db_path) as db:
        inv_a = await _create_invocation(db)
        await db.update_invocation(
            inv_a,
            status="failed",
            ended_at=time.time(),
            reason_code=RunReasons.FAILED_EXCEPTION,
            reason_summary="RuntimeError: boom",
            evidence_refs=[{"kind": "session", "id": "s-a"}],
        )
        inv_b = await _create_invocation(db)
        # Leave inv_b as running — reason columns stay NULL.

    rows = await invocations_mod.list_invocations()

    by_id = {r["id"]: r for r in rows}

    assert inv_a in by_id, "row A must appear in list"
    assert by_id[inv_a]["status_reason_code"] == RunReasons.FAILED_EXCEPTION
    assert by_id[inv_a]["status_reason_summary"] == "RuntimeError: boom"
    assert by_id[inv_a]["status_evidence_refs"] == [{"kind": "session", "id": "s-a"}]

    assert inv_b in by_id, "row B must appear in list"
    assert by_id[inv_b]["status_reason_code"] is None
    assert by_id[inv_b]["status_reason_summary"] is None
    assert by_id[inv_b]["status_evidence_refs"] is None
