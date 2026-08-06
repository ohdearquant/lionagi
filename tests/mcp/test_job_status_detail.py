# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""``job.status(detail=True)``: the execution-graph/artifact/stall view.

The base payload is asserted unchanged so existing callers see no difference;
the richer view is asserted only for a caller that opts in, reusing the same
StateDB session a run's own studio surface renders from rather than a
fixture invented for this file alone (see tests/apps_studio_server/test_runs_detail.py
for the seeding idioms mirrored here).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

import lionagi.state.db as state_db_mod
from lionagi.mcp import config, dispatch

aiosqlite = pytest.importorskip("aiosqlite", reason="aiosqlite not installed")

from lionagi.state.db import StateDB  # noqa: E402


def call(**kwargs):
    return asyncio.run(dispatch.request(**kwargs))


@pytest.fixture
def isolated_state(monkeypatch, tmp_path: Path):
    """Job records and the StateDB both point at tmp_path; nothing real is touched."""
    monkeypatch.setattr(config, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    db_path = tmp_path / "state.db"
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)
    return db_path


async def seed_session(
    db_path: Path,
    *,
    session_id: str,
    run_id: str,
    status: str = "running",
    node_metadata: dict | None = None,
    artifact_contract_json: dict | None = None,
    artifact_verification_json: dict | None = None,
) -> None:
    prog_id = f"{session_id}-prog"
    async with StateDB(db_path) as db:
        await db.create_progression(prog_id)
        await db.create_session(
            {
                "id": session_id,
                "run_id": run_id,
                "progression_id": prog_id,
                "name": f"run-{session_id}",
                "status": status,
                "started_at": 1000.0,
                "artifact_contract_json": artifact_contract_json,
                "artifact_verification_json": artifact_verification_json,
                "node_metadata": node_metadata,
                "invocation_kind": "flow",
                "source_kind": "live",
            }
        )


async def seed_branch(
    db_path: Path,
    *,
    branch_id: str,
    session_id: str,
    name: str,
    status: str | None = None,
    started_at: float | None = None,
    ended_at: float | None = None,
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
                "agent_name": name,
            }
        )
        fields = {
            k: v
            for k, v in {"status": status, "started_at": started_at, "ended_at": ended_at}.items()
            if v is not None
        }
        if fields:
            await db.update_branch(branch_id, **fields)


async def seed_message(db_path: Path, *, msg_id: str, created_at: float) -> None:
    async with StateDB(db_path) as db:
        await db.insert_message(
            {
                "id": msg_id,
                "created_at": created_at,
                "content": {"text": "working"},
                "sender": "worker",
                "recipient": "user",
                "role": "assistant",
                "node_metadata": {},
            }
        )


def job_status(run_id: str, *, detail: bool | None = None) -> dict[str, Any]:
    args = {"run_id": run_id}
    if detail is not None:
        args["detail"] = detail
    return call(ops=[{"op": "job.status", "args": args}])["ops"][0]["result"]


async def job_status_async(run_id: str, *, detail: bool | None = None) -> dict[str, Any]:
    args = {"run_id": run_id}
    if detail is not None:
        args["detail"] = detail
    answer = await dispatch.request(ops=[{"op": "job.status", "args": args}])
    return answer["ops"][0]["result"]


# ── the default payload stays as it was ─────────────────────────────────────


def test_the_default_payload_carries_no_detail_key(isolated_state):
    result = job_status("no-such-run")
    assert "detail" not in result


def test_detail_false_carries_no_detail_key(isolated_state):
    result = job_status("no-such-run", detail=False)
    assert "detail" not in result


# ── fail-soft when there is nothing to read ─────────────────────────────────


def test_detail_true_with_no_session_recorded_is_soft(isolated_state):
    result = job_status("no-such-run", detail=True)
    assert result["detail"] == {"detail_unavailable": "no_session_recorded_for_run"}


# ── the richer view for a seeded run ────────────────────────────────────────


async def test_detail_true_carries_nodes_for_a_planned_graph(isolated_state):
    db_path = isolated_state
    run_id = "20260101T000000-abc123"
    session_id = "11111111-1111-1111-1111-111111111111"
    meta = {
        "agents": [
            {"id": "a1", "name": "collector"},
            {"id": "a2", "name": "writer"},
        ],
        "operations": [
            {"id": "collect", "agent_id": "a1", "depends_on": []},
            {"id": "write", "agent_id": "a2", "depends_on": ["collect"]},
        ],
    }
    await seed_session(db_path, session_id=session_id, run_id=run_id, node_metadata=meta)
    await seed_branch(
        db_path,
        branch_id=f"{session_id}-br1",
        session_id=session_id,
        name="collect",
        status="completed",
        started_at=1000.0,
        ended_at=1010.0,
    )
    await seed_branch(
        db_path,
        branch_id=f"{session_id}-br2",
        session_id=session_id,
        name="write",
        status="running",
        started_at=1010.0,
    )

    result = await job_status_async(run_id, detail=True)
    detail = result["detail"]
    assert "detail_unavailable" not in detail

    nodes = {n["id"]: n for n in detail["nodes"]}
    assert set(nodes) == {"collect", "write"}
    assert nodes["collect"]["role"] == "collector"
    assert nodes["collect"]["status"] == "completed"
    assert nodes["collect"]["duration_s"] == pytest.approx(10.0)
    assert nodes["write"]["status"] == "running"
    assert nodes["write"]["started_at"] == 1010.0
    assert nodes["write"]["duration_s"] is not None and nodes["write"]["duration_s"] >= 0

    # The still-running node shows up as a stall signal; the completed one does
    # not. No message was ever recorded for it, and a heartbeat is never
    # persisted for a node that is still running (see _build_stalls) — the
    # signal is honestly absent rather than fabricated from started_at.
    stalls = {s["node"]: s for s in detail["stalls"]}
    assert set(stalls) == {"write"}
    assert stalls["write"]["idle_source"] == "none"
    assert stalls["write"]["seconds_idle"] is None
    assert stalls["write"]["last_activity"] is None


async def test_detail_true_derives_stall_idle_from_last_message_at(isolated_state):
    """When the running node's branch has a recorded message, the stall signal
    is derived from that persisted timestamp and names its source — never from
    started_at, which conflates "long-running" with "stalled"."""
    db_path = isolated_state
    run_id = "20260101T000000-mno345"
    session_id = "55555555-5555-5555-5555-555555555555"
    meta = {
        "agents": [{"id": "a1", "name": "writer"}],
        "operations": [{"id": "write", "agent_id": "a1", "depends_on": []}],
    }
    await seed_session(db_path, session_id=session_id, run_id=run_id, node_metadata=meta)
    await seed_branch(
        db_path,
        branch_id=f"{session_id}-br1",
        session_id=session_id,
        name="write",
        status="running",
        started_at=1000.0,
        msg_ids=["m1"],
    )
    await seed_message(db_path, msg_id="m1", created_at=1050.0)

    result = await job_status_async(run_id, detail=True)
    stall = result["detail"]["stalls"][0]
    assert stall["node"] == "write"
    assert stall["idle_source"] == "last_message_at"
    assert stall["last_activity"] == 1050.0
    assert isinstance(stall["seconds_idle"], float) and stall["seconds_idle"] >= 0


async def test_detail_true_falls_back_to_branches_with_no_planned_graph(isolated_state):
    """A plain `li agent` run has no operations/agents metadata at all — every
    branch is still reported as its own node."""
    db_path = isolated_state
    run_id = "20260101T000000-def456"
    session_id = "22222222-2222-2222-2222-222222222222"
    await seed_session(db_path, session_id=session_id, run_id=run_id, status="completed")
    await seed_branch(
        db_path,
        branch_id=f"{session_id}-br1",
        session_id=session_id,
        name="worker",
        status="completed",
        started_at=1000.0,
        ended_at=1005.0,
    )

    result = await job_status_async(run_id, detail=True)
    detail = result["detail"]
    assert len(detail["nodes"]) == 1
    node = detail["nodes"][0]
    assert node["role"] == "worker"
    assert node["status"] == "completed"
    assert node["duration_s"] == pytest.approx(5.0)
    assert node["spawned_by"] is None


async def test_detail_true_reports_artifact_contract_satisfaction(isolated_state):
    db_path = isolated_state
    run_id = "20260101T000000-ghi789"
    session_id = "33333333-3333-3333-3333-333333333333"
    contract = {
        "expected": [
            {"id": "collect__out", "path": "collect/out.md", "required": True},
            {"id": "write__final", "path": "write/final.md", "required": True},
        ]
    }
    verification = {
        "status": "warning",
        "produced": [{"id": "collect__out", "path": "collect/out.md", "size": 10, "present": True}],
        "missing_required": [{"id": "write__final", "path": "write/final.md", "required": True}],
        "missing_optional": [],
    }
    await seed_session(
        db_path,
        session_id=session_id,
        run_id=run_id,
        status="running",
        artifact_contract_json=contract,
        artifact_verification_json=verification,
    )

    result = await job_status_async(run_id, detail=True)
    entries = {e["path"]: e for e in result["detail"]["artifact_contract"]}
    assert entries["collect/out.md"]["required_by"] == "collect"
    assert entries["collect/out.md"]["satisfied"] is True
    assert entries["write/final.md"]["required_by"] == "write"
    assert entries["write/final.md"]["satisfied"] is False


async def test_detail_true_is_soft_when_graph_metadata_is_malformed(isolated_state):
    """A node id that is not hashable (e.g. a list) breaks a dict lookup deep
    inside node/stall reshaping. That reshape sits behind the same fail-soft
    boundary as every other detail-build failure, so this must answer with
    detail_unavailable rather than raise past job.status."""
    db_path = isolated_state
    run_id = "20260101T000000-pqr678"
    session_id = "66666666-6666-6666-6666-666666666666"
    # early_graph is passed through node_metadata verbatim, with no shape
    # validation — the same injection point a corrupted or hand-edited
    # checkpoint could produce.
    meta = {"early_graph": {"nodes": [{"id": []}], "edges": []}}
    await seed_session(db_path, session_id=session_id, run_id=run_id, node_metadata=meta)

    result = await job_status_async(run_id, detail=True)
    assert "detail_unavailable" in result["detail"]


async def test_detail_true_is_soft_when_graph_nodes_is_a_dict(isolated_state):
    """``graph.nodes`` read back as a dict (not a list) must not be iterated as
    if it were a list of node dicts — that silently yields zero nodes with no
    indication anything was wrong. A malformed container shape must say
    detail_unavailable, never a fabricated empty-but-successful detail."""
    db_path = isolated_state
    run_id = "20260101T000000-stu901"
    session_id = "77777777-7777-7777-7777-777777777777"
    meta = {"early_graph": {"nodes": {"collect": {"id": "collect"}}, "edges": []}}
    await seed_session(db_path, session_id=session_id, run_id=run_id, node_metadata=meta)

    result = await job_status_async(run_id, detail=True)
    assert result["detail"] == {"detail_unavailable": "malformed_session_detail"}


async def test_detail_true_is_soft_when_no_metadata_or_branches_recorded(isolated_state):
    """A session with no graph, no branches, and no node_metadata at all never
    had anything to build a detail view from. Falling back to "zero branches
    means zero nodes" would answer with an indistinguishable empty-but-clean
    detail, so this must say detail_unavailable instead."""
    db_path = isolated_state
    run_id = "20260101T000000-vwx234"
    session_id = "88888888-8888-8888-8888-888888888888"
    await seed_session(db_path, session_id=session_id, run_id=run_id)

    result = await job_status_async(run_id, detail=True)
    assert result["detail"] == {"detail_unavailable": "no_session_detail"}


async def test_detail_true_is_soft_when_the_studio_extra_is_absent(isolated_state, monkeypatch):
    """A run whose session IS recorded but whose detail-building service
    cannot be imported still answers, rather than raising past job.status."""
    from lionagi.mcp import _run_detail

    # Simulate the studio extra being unavailable regardless of what this
    # test environment actually has installed.
    monkeypatch.setattr(
        _run_detail,
        "build_run_detail",
        lambda run_id: asyncio.sleep(
            0, result={"detail_unavailable": "studio_extra_not_installed"}
        ),
    )

    run_id = "20260101T000000-jkl012"
    session_id = "44444444-4444-4444-4444-444444444444"
    await seed_session(isolated_state, session_id=session_id, run_id=run_id)

    result = await job_status_async(run_id, detail=True)
    assert result["detail"] == {"detail_unavailable": "studio_extra_not_installed"}
