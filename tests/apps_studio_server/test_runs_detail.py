# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for get_run() reading from StateDB with correct key contract."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest

aiosqlite = pytest.importorskip("aiosqlite", reason="aiosqlite not installed")
fastapi = pytest.importorskip("fastapi", reason="studio extra not installed")

from lionagi.state.db import StateDB  # noqa: E402
from lionagi.state.reasons import RunReasons  # noqa: E402

# ---------------------------------------------------------------------------
# Shared seed helpers (mirror test_sessions_detail.py idioms)
# ---------------------------------------------------------------------------


async def seed_session(
    db_path: Path,
    *,
    session_id: str,
    status: str = "completed",
    agent_name: str | None = None,
    model: str | None = None,
    started_at: float | None = None,
    ended_at: float | None = None,
    artifacts_path: str | None = None,
    artifact_contract_json: dict | None = None,
    artifact_verification_json: dict | None = None,
    node_metadata: dict | None = None,
    invocation_kind: str = "agent",
) -> None:
    prog_id = f"{session_id}-prog"
    async with StateDB(db_path) as db:
        await db.create_progression(prog_id)
        await db.create_session(
            {
                "id": session_id,
                "progression_id": prog_id,
                "name": f"run-{session_id}",
                "status": status,
                "agent_name": agent_name,
                "model": model,
                "started_at": started_at,
                "ended_at": ended_at,
                "artifacts_path": artifacts_path,
                "artifact_contract_json": artifact_contract_json,
                "artifact_verification_json": artifact_verification_json,
                "node_metadata": node_metadata,
                "invocation_kind": invocation_kind,
                "source_kind": "live",
            }
        )


async def seed_branch(
    db_path: Path,
    *,
    branch_id: str,
    session_id: str,
    name: str = "worker",
    model: str = "gpt-5",
    msg_ids: list[str] | None = None,
) -> None:
    prog_id = f"{branch_id}-prog"
    async with StateDB(db_path) as db:
        if msg_ids:
            await db.create_progression(prog_id, msg_ids)
        else:
            await db.create_progression(prog_id)
        await db.create_branch(
            {
                "id": branch_id,
                "created_at": 200.0,
                "name": name,
                "session_id": session_id,
                "progression_id": prog_id,
                "model": model,
                "provider": "openai",
                "agent_name": name,
            }
        )


@pytest.fixture
def patched_runs_svc(tmp_path: Path, monkeypatch: Any):
    """Patch sessions service to point at a tmp DB; return (svc, db_path)."""
    import lionagi.studio.services.sessions as sessions_mod

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(sessions_mod, "_DB", str(db_path))
    monkeypatch.setattr(sessions_mod, "DEFAULT_DB_PATH", db_path)

    import lionagi.studio.services.runs as runs_svc

    return runs_svc, db_path


# ---------------------------------------------------------------------------
# Test 1 — get_run returns None for a missing run id
# ---------------------------------------------------------------------------


async def test_get_run_returns_none_for_missing_id(patched_runs_svc):
    svc, db_path = patched_runs_svc
    # Create DB file with no matching session
    async with StateDB(db_path) as db:
        await db.create_progression("init")

    result = await svc.get_run("nonexistent-id")
    assert result is None


# ---------------------------------------------------------------------------
# Test 2 — get_run returns None when DB file does not exist
# ---------------------------------------------------------------------------


async def test_get_run_returns_none_when_db_absent(patched_runs_svc):
    svc, db_path = patched_runs_svc
    # Do NOT create the DB file

    result = await svc.get_run("any-id")
    assert result is None


# ---------------------------------------------------------------------------
# Test 3 — get_run returns all required keys for a DB-seeded run
# ---------------------------------------------------------------------------


_REQUIRED_KEYS = {
    "run_id",
    "state_root",
    "artifact_root",
    "worker_name",
    "task",
    "status",
    "step_count",
    "started_at",
    "finished_at",
    "model",
    "error",
    "cwd",
    "steps",
    "graph",
    "manifest",
    "branches",
    "artifact_contract_json",
    "artifact_verification_json",
}


async def test_get_run_returns_all_required_keys(patched_runs_svc):
    svc, db_path = patched_runs_svc
    sid = str(uuid.uuid4())
    await seed_session(db_path, session_id=sid, status="completed")

    result = await svc.get_run(sid)

    assert result is not None
    missing = _REQUIRED_KEYS - result.keys()
    assert not missing, f"Missing keys in get_run response: {missing}"


# ---------------------------------------------------------------------------
# Test 4 — get_run maps session fields to correct response keys
# ---------------------------------------------------------------------------


async def test_get_run_maps_session_fields_correctly(patched_runs_svc):
    svc, db_path = patched_runs_svc
    sid = str(uuid.uuid4())
    await seed_session(
        db_path,
        session_id=sid,
        status="failed",
        agent_name="researcher",
        model="openai/gpt-5",
        started_at=1000.0,
        ended_at=2000.0,
    )

    result = await svc.get_run(sid)

    assert result is not None
    assert result["run_id"] == sid
    assert result["status"] == "failed"
    assert result["worker_name"] == "researcher"
    assert result["model"] == "openai/gpt-5"
    assert result["started_at"] == 1000.0
    assert result["finished_at"] == 2000.0
    # DB-path fields with no direct equivalent
    assert result["error"] is None
    assert result["cwd"] is None
    assert result["manifest"] == {}
    assert result["task"] == ""


# ---------------------------------------------------------------------------
# Test 5 — step_count matches branch count; steps list is populated
# ---------------------------------------------------------------------------


async def test_get_run_step_count_and_steps_from_branches(patched_runs_svc):
    svc, db_path = patched_runs_svc
    sid = str(uuid.uuid4())
    await seed_session(db_path, session_id=sid)
    await seed_branch(db_path, branch_id=f"{sid}-br1", session_id=sid, name="alpha")
    await seed_branch(db_path, branch_id=f"{sid}-br2", session_id=sid, name="beta")

    result = await svc.get_run(sid)

    assert result is not None
    assert result["step_count"] == 2
    assert isinstance(result["steps"], list)
    assert len(result["steps"]) == 2
    step_names = {s["step"] for s in result["steps"]}
    assert step_names == {"alpha", "beta"}


# ---------------------------------------------------------------------------
# Test 6 — artifact_contract_json and artifact_verification_json are passed through
# ---------------------------------------------------------------------------


async def test_get_run_passes_artifact_json_fields(patched_runs_svc):
    svc, db_path = patched_runs_svc
    sid = str(uuid.uuid4())
    contract = {"files": ["report.md"], "required": True}
    verification = {"passed": True, "score": 1.0}
    await seed_session(
        db_path,
        session_id=sid,
        artifact_contract_json=contract,
        artifact_verification_json=verification,
    )

    result = await svc.get_run(sid)

    assert result is not None
    assert result["artifact_contract_json"] == contract
    assert result["artifact_verification_json"] == verification


# ---------------------------------------------------------------------------
# Test 7 — graph is populated from node_metadata when present
# ---------------------------------------------------------------------------


async def test_get_run_graph_from_node_metadata(patched_runs_svc):
    svc, db_path = patched_runs_svc
    sid = str(uuid.uuid4())
    meta = {
        "agents": [{"id": "a1", "name": "Analyst", "model": "gpt-5"}],
        "operations": [{"id": "collect", "agent_id": "a1", "depends_on": []}],
    }
    await seed_session(db_path, session_id=sid, node_metadata=meta)

    result = await svc.get_run(sid)

    assert result is not None
    graph = result["graph"]
    assert graph is not None
    assert len(graph["nodes"]) == 1
    assert graph["nodes"][0]["id"] == "collect"


# ---------------------------------------------------------------------------
# Test 8 — graph is None when no node_metadata present
# ---------------------------------------------------------------------------


async def test_get_run_graph_is_none_without_node_metadata(patched_runs_svc):
    svc, db_path = patched_runs_svc
    sid = str(uuid.uuid4())
    await seed_session(db_path, session_id=sid, node_metadata=None)

    result = await svc.get_run(sid)

    assert result is not None
    assert result["graph"] is None


# ---------------------------------------------------------------------------
# Test 9 — steps is None when no branches exist
# ---------------------------------------------------------------------------


async def test_get_run_steps_is_none_with_no_branches(patched_runs_svc):
    svc, db_path = patched_runs_svc
    sid = str(uuid.uuid4())
    await seed_session(db_path, session_id=sid)

    result = await svc.get_run(sid)

    assert result is not None
    assert result["steps"] is None
    assert result["step_count"] == 0
    assert result["branches"] == []


# ---------------------------------------------------------------------------
# Test 10 — HTTP endpoint returns 404 for missing run
# ---------------------------------------------------------------------------


def test_get_run_endpoint_returns_404_for_missing(tmp_path, monkeypatch):
    fastapi = pytest.importorskip("fastapi", reason="studio extra not installed")

    import lionagi.studio.services.sessions as sessions_mod

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(sessions_mod, "_DB", str(db_path))
    monkeypatch.setattr(sessions_mod, "DEFAULT_DB_PATH", db_path)

    from fastapi.testclient import TestClient

    from lionagi.studio.app import app

    client = TestClient(app, base_url="http://127.0.0.1:8765")
    r = client.get(f"/api/runs/{uuid.uuid4()}")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Test 11 — detail route satisfies the list Run contract (no field drift)
# ---------------------------------------------------------------------------

# The fields the extension's `Run` TS interface (apps/vscode/src/api/types.ts)
# requires from GET /api/runs/{id}. The detail route once dropped these (e.g.
# invocation_id), which erased the failure-reason banner after a detail refresh.
_RUN_CONTRACT_KEYS = {
    "run_id",
    "id",
    "name",
    "playbook_name",
    "agent_name",
    "invocation_kind",
    "model",
    "provider",
    "effort",
    "status",
    "started_at",
    "ended_at",
    "created_at",
    "updated_at",
    "last_message_at",
    "effective_health",
    "branch_count",
    "message_count",
    "project",
    "project_source",
    "invocation_id",
    "status_reason_code",
    "status_reason_summary",
    "status_evidence_refs",
}


async def test_get_run_satisfies_run_list_contract(patched_runs_svc):
    svc, db_path = patched_runs_svc
    sid = str(uuid.uuid4())
    await seed_session(
        db_path,
        session_id=sid,
        status="failed",
        agent_name="researcher",
        invocation_kind="flow",
    )
    await seed_branch(db_path, branch_id=f"{sid}-br1", session_id=sid, name="alpha")
    await seed_branch(db_path, branch_id=f"{sid}-br2", session_id=sid, name="beta")

    result = await svc.get_run(sid)

    assert result is not None
    missing = _RUN_CONTRACT_KEYS - result.keys()
    assert not missing, f"detail route drifted from Run contract; missing: {missing}"
    # The field whose absence suppressed the reason banner must round-trip.
    assert "invocation_id" in result
    assert result["invocation_kind"] == "flow"
    # branch_count / message_count derive from the hydrated branches, not the JOIN.
    assert result["branch_count"] == 2


async def test_get_run_surfaces_status_reason(patched_runs_svc):
    """ADR-0028: a failed run surfaces the reason fields the detail banner reads."""
    svc, db_path = patched_runs_svc

    # A run transitioned to failed with a reason round-trips all three fields.
    sid = str(uuid.uuid4())
    await seed_session(db_path, session_id=sid, status="running")
    evidence = [{"type": "log", "path": "/tmp/run.log"}]
    async with StateDB(db_path) as db:
        await db.update_status(
            "session",
            sid,
            new_status="failed",
            reason_code=RunReasons.FAILED_EXIT_NONZERO,
            reason_summary="worker exited with code 1",
            evidence_refs=evidence,
        )

    failed = await svc.get_run(sid)
    assert failed is not None
    assert failed["status"] == "failed"
    assert failed["status_reason_code"] == RunReasons.FAILED_EXIT_NONZERO
    assert failed["status_reason_summary"] == "worker exited with code 1"
    assert failed["status_evidence_refs"] == evidence

    # A run with no recorded reason returns the fields as None, not missing.
    sid2 = str(uuid.uuid4())
    await seed_session(db_path, session_id=sid2, status="completed")
    ok = await svc.get_run(sid2)
    assert ok is not None
    assert ok["status_reason_code"] is None
    assert ok["status_reason_summary"] is None
    assert ok["status_evidence_refs"] is None


async def test_get_run_zombie_recorded_pid_reports_stale(patched_runs_svc, monkeypatch, tmp_path):
    """A "running" row whose recorded pid is confirmed dead must not read
    healthy just because it's inside the (nonexistent) zombie grace window —
    the shared liveness oracle is the single source of truth here."""
    svc, db_path = patched_runs_svc
    import lionagi.studio.services.admin as admin_mod

    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    monkeypatch.setattr(admin_mod, "_pid_is_live", lambda _pid: False)

    sid = str(uuid.uuid4())
    await seed_session(
        db_path,
        session_id=sid,
        status="running",
        artifacts_path=str(artifacts),
        node_metadata={"pid": 999999, "pid_create_time": 42.0},
    )

    result = await svc.get_run(sid)
    assert result is not None
    assert result["effective_health"] == "stale"
