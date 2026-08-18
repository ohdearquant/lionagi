from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

pytest.importorskip("aiosqlite", reason="aiosqlite not installed")

from lionagi.state.db import StateDB


async def _session(
    db: StateDB,
    session_id: str,
    progression_id: str,
    *,
    invocation_id: str | None = None,
    invocation_kind: str | None = None,
) -> None:
    await db.create_progression(progression_id)
    await db.create_session(
        {
            "id": session_id,
            "created_at": 100.0,
            "started_at": 100.0,
            "progression_id": progression_id,
            "status": "running",
            "invocation_id": invocation_id,
            "invocation_kind": invocation_kind,
        }
    )


async def test_engine_session_and_diagnostic_row_share_one_lineage(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    async with StateDB(db_path) as db:
        await db.create_invocation({"id": "inv-engine", "skill": "engine", "started_at": 90.0})
        await _session(db, "parent-session", "parent-prog")
        await _session(
            db,
            "signal-session",
            "signal-prog",
            invocation_id="inv-engine",
            invocation_kind="engine",
        )
        await db.insert_engine_run(
            run_id="engine-run",
            kind="research",
            spec_json={"topic": "private prompt"},
            started_at=100.0,
            session_id="parent-session",
        )
        await db.set_engine_run_lineage(
            "engine-run",
            invocation_id="inv-engine",
            signal_session_id="signal-session",
            parent_session_id="parent-session",
        )
        row = await db.get_engine_run("engine-run")

    assert row is not None
    assert row["invocation_id"] == "inv-engine"
    assert row["signal_session_id"] == "signal-session"
    assert row["parent_session_id"] == "parent-session"
    # The legacy field remains readable but is never mistaken for the signal session.
    assert row["session_id"] == "parent-session"


async def test_engine_outcome_is_bounded_and_list_summary_never_hydrates_spec(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "state.db"
    sentinel = "sk-super-secret-input-1234567890"
    async with StateDB(db_path) as db:
        await db.insert_engine_run(
            run_id="engine-run",
            kind="planning",
            spec_json={"prompt": sentinel + ("x" * 2_000_000)},
            started_at=100.0,
        )
        outcome = {
            "version": 1,
            "status": "completed",
            "degraded": True,
            "degrade_reason": "budget",
            "skipped": ["stage-a"],
            "result": {"kind": "text", "size_bytes": 2_000_000},
        }
        await db.record_engine_run_outcome("engine-run", outcome)
        rows = await db.list_engine_run_summaries(limit=10)

    assert len(rows) == 1
    assert "spec_json" not in rows[0]
    assert rows[0]["outcome_json"] == outcome
    assert sentinel not in repr(rows[0])
    assert len(repr(rows[0])) < 16_384


async def test_engine_run_cursor_index_is_composite_and_seekable(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    async with StateDB(db_path):
        pass

    conn = sqlite3.connect(db_path)
    try:
        indexes = {
            row[1]: tuple(col[2] for col in conn.execute(f"PRAGMA index_info({row[1]})"))
            for row in conn.execute("PRAGMA index_list(engine_runs)")
        }
        plan = list(
            conn.execute(
                "EXPLAIN QUERY PLAN "
                "SELECT id, kind, status, started_at FROM engine_runs "
                "WHERE (started_at, id) < (?, ?) "
                "ORDER BY started_at DESC, id DESC LIMIT ?",
                (100.0, "z", 20),
            )
        )
    finally:
        conn.close()

    assert indexes["idx_engine_runs_started_id"] == ("started_at", "id")
    assert any("idx_engine_runs_started_id" in str(row) for row in plan)
