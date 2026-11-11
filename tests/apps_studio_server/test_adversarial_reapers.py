# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Adversarial edge-case tests for studio lifecycle reaper mechanisms."""

from __future__ import annotations

import time
import uuid
from pathlib import Path

import pytest

import lionhub.orchestration.scheduler.liveness as scheduler_liveness

pytest.importorskip("fastapi", reason="studio extra not installed")

import lionagi.state.db as state_db_mod
from lionagi.state.db import StateDB  # noqa: E402

from ._helpers import run_async  # noqa: E402

# ── shared DB helpers ─────────────────────────────────────────────────────────


def _monkey_db(monkeypatch, db_path: Path) -> None:

    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)


async def _seed_session(
    db_path: Path,
    *,
    status: str | None = "running",
    started_at: float | None = None,
    updated_at: float | None = None,
    artifacts_path: str | None = None,
    node_metadata: dict | None = None,
) -> str:
    sid = str(uuid.uuid4())
    now = time.time()
    async with StateDB(db_path) as db:
        pid = str(uuid.uuid4())
        await db.create_progression(pid)
        await db.create_session(
            {
                "id": sid,
                "progression_id": pid,
                "name": "adv-test-session",
                "status": status,
                "started_at": started_at or now,
                "node_metadata": node_metadata,
            }
        )
        updates: dict = {}
        if updated_at is not None:
            updates["updated_at"] = updated_at
        if artifacts_path is not None:
            updates["artifacts_path"] = artifacts_path
        if status is None:
            await db.execute("UPDATE sessions SET status = NULL WHERE id = ?", (sid,))
        if updates:
            sets = ", ".join(f"{k} = ?" for k in updates)
            vals = list(updates.values()) + [sid]
            await db.execute(
                f"UPDATE sessions SET {sets} WHERE id = ?",  # noqa: S608
                vals,
            )
    return sid


async def _seed_invocation(
    db_path: Path,
    *,
    status: str = "running",
    started_at: float | None = None,
    updated_at: float | None = None,
    session_count: int = 0,
) -> str:
    iid = uuid.uuid4().hex[:12]
    now = time.time()
    async with StateDB(db_path) as db:
        await db.create_invocation(
            {
                "id": iid,
                "skill": "adv:test",
                "started_at": started_at or now,
                "status": status,
                "session_count": session_count,
            }
        )
        if updated_at is not None:
            await db.execute(
                "UPDATE invocations SET updated_at = ? WHERE id = ?", (updated_at, iid)
            )
    return iid


async def _get_session_status(db_path: Path, sid: str) -> str | None:
    async with StateDB(db_path) as db:
        row = await db.get_session(sid)
    return row["status"] if row else None


async def _get_inv_status(db_path: Path, iid: str) -> str | None:
    async with StateDB(db_path) as db:
        row = await db.get_invocation(iid)
    return row["status"] if row else None


async def _count_transitions(db_path: Path, entity_id: str) -> int:
    async with StateDB(db_path) as db:
        row = await db.fetch_one(
            "SELECT COUNT(*) AS n FROM status_transitions WHERE entity_id = ?", (entity_id,)
        )
        return row["n"] if row else 0


# ── adversarial: invocation deadline false-positive guards ───────────────────


# ── adversarial: null-status session double-write guard ──────────────────────


# ── adversarial: phantom detection reuse + no regression ─────────────────────


def test_1172_admin_prune_phantom_delegates_no_delete(tmp_path, monkeypatch):
    """prune_phantom_sessions() transitions the row, not deletes it (regression guard)."""
    db_path = tmp_path / "state.db"
    _monkey_db(monkeypatch, db_path)

    missing_dir = str(tmp_path / "ghost2")
    stale_time = time.time() - 7200
    sid = run_async(
        _seed_session(
            db_path,
            status="running",
            started_at=stale_time,
            updated_at=stale_time,
            artifacts_path=missing_dir,
        )
    )

    from lionagi.studio.services.admin import prune_phantom_sessions

    count = run_async(prune_phantom_sessions(stale_hours=1.0))
    assert count == 1

    # Row PRESERVED (not deleted), status transitioned
    status = run_async(_get_session_status(db_path, sid))
    assert status == "failed"
    assert run_async(_count_transitions(db_path, sid)) >= 1


# ── adversarial: prune FK integrity and data preservation ────────────────────


# ── shared state store: rows this machine cannot see are not this machine's to judge ──
