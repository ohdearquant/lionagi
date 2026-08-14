# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Focused tests for the run_progress Operator read tool and resolve_run()."""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any

import pytest

import lionagi.state.db as state_db_mod

aiosqlite = pytest.importorskip("aiosqlite", reason="aiosqlite not installed")
fastapi = pytest.importorskip("fastapi", reason="studio extra not installed")

from lionagi.state.db import StateDB  # noqa: E402

pytestmark = pytest.mark.asyncio


async def seed_session(
    db_path: Path,
    *,
    session_id: str,
    status: str = "completed",
    name: str | None = None,
    playbook_name: str | None = None,
    agent_name: str | None = None,
    model: str | None = None,
    started_at: float | None = None,
    ended_at: float | None = None,
    project: str | None = None,
    updated_at: float | None = None,
) -> None:
    prog_id = f"{session_id}-prog"
    async with StateDB(db_path) as db:
        await db.create_progression(prog_id)
        await db.create_session(
            {
                "id": session_id,
                "progression_id": prog_id,
                "name": name or f"run-{session_id}",
                "playbook_name": playbook_name,
                "status": status,
                "agent_name": agent_name,
                "model": model,
                "started_at": started_at,
                "ended_at": ended_at,
                "project": project,
                "updated_at": updated_at if updated_at is not None else time.time(),
                "invocation_kind": "agent",
                "source_kind": "live",
            }
        )


async def seed_branch(
    db_path: Path,
    *,
    branch_id: str,
    session_id: str,
    name: str = "worker",
    agent_name: str | None = None,
    status: str | None = None,
    started_at: float | None = None,
    ended_at: float | None = None,
) -> None:
    prog_id = f"{branch_id}-prog"
    async with StateDB(db_path) as db:
        await db.create_progression(prog_id)
        await db.create_branch(
            {
                "id": branch_id,
                "created_at": 200.0,
                "name": name,
                "session_id": session_id,
                "progression_id": prog_id,
                "model": "gpt-5",
                "provider": "openai",
                "agent_name": agent_name or name,
            }
        )
        fields: dict[str, Any] = {}
        if status is not None:
            fields["status"] = status
        if started_at is not None:
            fields["started_at"] = started_at
        if ended_at is not None:
            fields["ended_at"] = ended_at
        if fields:
            await db.update_branch(branch_id, **fields)


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: Any) -> Path:
    path = tmp_path / "state.db"
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", path)
    return path


async def test_run_progress_happy_path(db_path):
    from lionagi.studio.operator.run_progress import run_progress

    sid = str(uuid.uuid4())
    await seed_session(db_path, session_id=sid, status="running", started_at=1000.0)
    await seed_branch(
        db_path,
        branch_id=f"{sid}-br1",
        session_id=sid,
        name="planner",
        status="completed",
        started_at=1000.0,
        ended_at=1010.0,
    )
    await seed_branch(
        db_path,
        branch_id=f"{sid}-br2",
        session_id=sid,
        name="critic",
        status="running",
        started_at=1010.0,
    )

    result = await run_progress({"run": sid})

    assert result["found"] is True
    assert result["ambiguous"] is False
    assert result["id"] == sid
    assert result["status"] == "running"
    assert result["opsTotal"] == 2
    assert result["opsCompleted"] == 1
    assert result["opsRunning"] == 1
    assert result["opsFailed"] == 0
    assert result["opsPending"] == 0
    assert result["currentOps"] == [{"name": "critic", "agentName": "critic", "status": "running"}]
    assert result["startedAt"] == 1000.0
    assert "direct database read" in result["freshness"]


async def test_run_progress_ambiguous_reference_and_cross_project_isolation(db_path):
    """Two sessions sharing a name substring, in different projects, come back
    as candidates that each report their own (redacted) project — never the
    other session's data."""
    from lionagi.studio.operator.run_progress import run_progress

    sid_a = str(uuid.uuid4())
    sid_b = str(uuid.uuid4())
    await seed_session(
        db_path,
        session_id=sid_a,
        name="nightly-triage-alpha",
        project="/Users/admin/acme-research",
        status="completed",
    )
    await seed_session(
        db_path,
        session_id=sid_b,
        name="nightly-triage-beta",
        project="/Users/admin/acme-ops",
        status="failed",
    )

    result = await run_progress({"run": "nightly-triage"})

    assert result["found"] is True
    assert result["ambiguous"] is True
    assert result["truncated"] is False
    candidates_by_id = {c["id"]: c for c in result["candidates"]}
    assert set(candidates_by_id) == {sid_a, sid_b}
    assert candidates_by_id[sid_a]["project"] == "acme-research"
    assert candidates_by_id[sid_b]["project"] == "acme-ops"
    assert candidates_by_id[sid_a]["status"] == "completed"
    assert candidates_by_id[sid_b]["status"] == "failed"


async def test_run_progress_not_found(db_path):
    from lionagi.studio.operator.run_progress import run_progress

    async with StateDB(db_path):
        pass

    result = await run_progress({"run": str(uuid.uuid4())})
    assert result["found"] is False
    assert isinstance(result.get("reason"), str) and result["reason"]


async def test_run_progress_zero_ops(db_path):
    from lionagi.studio.operator.run_progress import run_progress

    sid = str(uuid.uuid4())
    await seed_session(db_path, session_id=sid, status="running", started_at=500.0)

    result = await run_progress({"run": sid})

    assert result["found"] is True
    assert result["opsTotal"] == 0
    assert result["opsCompleted"] == 0
    assert result["opsRunning"] == 0
    assert result["opsFailed"] == 0
    assert result["opsPending"] == 0
    assert result["currentOps"] == []


async def test_run_progress_failed_run(db_path):
    from lionagi.studio.operator.run_progress import run_progress

    sid = str(uuid.uuid4())
    await seed_session(db_path, session_id=sid, status="failed", started_at=10.0, ended_at=20.0)
    await seed_branch(
        db_path,
        branch_id=f"{sid}-br1",
        session_id=sid,
        name="worker",
        status="failed",
        started_at=10.0,
        ended_at=15.0,
    )
    await seed_branch(
        db_path,
        branch_id=f"{sid}-br2",
        session_id=sid,
        name="pending-op",
    )

    result = await run_progress({"run": sid})

    assert result["status"] == "failed"
    assert result["opsTotal"] == 2
    assert result["opsFailed"] == 1
    assert result["opsCompleted"] == 0
    assert result["opsRunning"] == 0
    assert result["opsPending"] == 1
    assert result["elapsedSeconds"] == pytest.approx(10.0)


async def test_run_progress_terminal_null_end_reports_unknown_duration(db_path, monkeypatch):
    """An old terminal row is not still running merely because its end is absent."""
    from lionagi.studio.operator.run_progress import run_progress

    sid = str(uuid.uuid4())
    await seed_session(db_path, session_id=sid, status="running", started_at=10.0)
    # Simulate a historical writer after this database's migration marker was
    # recorded, so the read path itself must remain honest even before repair.
    async with StateDB(db_path) as db:
        await db.execute(
            "UPDATE sessions SET status = 'completed', ended_at = NULL WHERE id = :id",
            {"id": sid},
        )
    monkeypatch.setattr(time, "time", lambda: 10_000.0)

    result = await run_progress({"run": sid})

    assert result["status"] == "completed"
    assert result["endedAt"] is None
    assert result["endedAtApproximate"] is False
    assert result["elapsedSeconds"] is None


async def test_run_progress_approximate_end_does_not_claim_measured_duration(db_path):
    from lionagi.studio.operator.run_progress import run_progress

    sid = str(uuid.uuid4())
    await seed_session(
        db_path,
        session_id=sid,
        status="running",
        started_at=10.0,
        updated_at=20.0,
    )
    async with StateDB(db_path) as db:
        await db.execute(
            "UPDATE sessions SET status = 'completed', ended_at = 20.0, "
            "ended_at_is_approximate = 1 WHERE id = :id",
            {"id": sid},
        )

    result = await run_progress({"run": sid})

    assert result["endedAt"] == 20.0
    assert result["endedAtApproximate"] is True
    assert result["elapsedSeconds"] is None


async def test_run_progress_completed_empty_branch_counts_as_failed(db_path):
    """ADR-0064: completed_empty is terminal but unsuccessful -- a branch
    that produced no trusted evidence must not be counted as an op success."""
    from lionagi.studio.operator.run_progress import run_progress

    sid = str(uuid.uuid4())
    await seed_session(db_path, session_id=sid, status="failed", started_at=10.0, ended_at=20.0)
    await seed_branch(
        db_path,
        branch_id=f"{sid}-br1",
        session_id=sid,
        name="worker",
        status="completed_empty",
        started_at=10.0,
        ended_at=15.0,
    )

    result = await run_progress({"run": sid})

    assert result["opsTotal"] == 1
    assert result["opsCompleted"] == 0
    assert result["opsFailed"] == 1
    assert result["opsRunning"] == 0
    assert result["opsPending"] == 0


async def test_run_progress_hex_prefix_resolution(db_path):
    from lionagi.studio.operator.run_progress import run_progress

    sid = "a1b2c3d4-1111-2222-3333-444455556666"
    await seed_session(db_path, session_id=sid, status="completed")

    result = await run_progress({"run": "a1b2c3d4"})

    assert result["found"] is True
    assert result["ambiguous"] is False
    assert result["id"] == sid


async def test_run_progress_dashed_id_prefix_resolution(db_path):
    """fetch_unique_row() matches any literal id prefix, not just an 8+ char
    run of bare hex — a prefix spanning the first dash must resolve too, the
    same way `cancel_run`/`li kill` already accept it."""
    from lionagi.studio.operator.run_progress import run_progress

    sid = "a1b2c3d4-1111-2222-3333-444455556666"
    await seed_session(db_path, session_id=sid, status="completed")

    result = await run_progress({"run": "a1b2c3d4-11"})

    assert result["found"] is True
    assert result["ambiguous"] is False
    assert result["id"] == sid


async def test_run_progress_colliding_id_prefix_is_ambiguous(db_path):
    """Two ids sharing a literal prefix must come back as candidates, not a
    silent pick — mirrors fetch_unique_row()'s own AmbiguousIdError contract."""
    from lionagi.studio.operator.run_progress import run_progress

    sid_a = "deadbeef-1111-2222-3333-444455556666"
    sid_b = "deadbeef-9999-8888-7777-666655554444"
    await seed_session(db_path, session_id=sid_a, name="run-a", status="completed")
    await seed_session(db_path, session_id=sid_b, name="run-b", status="running")

    result = await run_progress({"run": "deadbeef"})

    assert result["found"] is True
    assert result["ambiguous"] is True
    assert {c["id"] for c in result["candidates"]} == {sid_a, sid_b}


async def test_run_progress_current_view(db_path, monkeypatch):
    from lionagi.studio.operator.application_mcp import navigate  # noqa: F401
    from lionagi.studio.operator.run_progress import run_progress
    from lionagi.studio.operator.store import OperatorStore

    sid = str(uuid.uuid4())
    await seed_session(
        db_path, session_id=sid, status="completed", project="/Users/admin/test-project"
    )

    store = OperatorStore(db_path)
    cid = (await store.create_conversation())["id"]
    accepted = await store.submit_turn(
        cid,
        instruction="how is the nightly run going?",
        context={
            "space": "history",
            "route": "/fleet",
            "filters": {},
            "selection": {"s": sid},
            "project": "/Users/admin/test-project",
        },
        expected_last_sequence=0,
    )
    assert await store.mark_running(accepted["requestId"])
    monkeypatch.setenv("LIONAGI_OPERATOR_DB_PATH", str(db_path))
    monkeypatch.setenv("LIONAGI_OPERATOR_CONVERSATION_ID", cid)
    monkeypatch.setenv("LIONAGI_OPERATOR_REQUEST_ID", accepted["requestId"])

    result = await run_progress({"run": "current"})

    assert result["found"] is True
    assert result["id"] == sid


async def test_run_progress_current_view_unknown_returns_not_found(db_path, monkeypatch):
    from lionagi.studio.operator.run_progress import run_progress
    from lionagi.studio.operator.store import OperatorStore

    store = OperatorStore(db_path)
    cid = (await store.create_conversation())["id"]
    accepted = await store.submit_turn(
        cid,
        instruction="how is it going?",
        context={
            "space": "mission",
            "route": "/",
            "filters": {},
            "project": "/Users/admin/test-project",
        },
        expected_last_sequence=0,
    )
    assert await store.mark_running(accepted["requestId"])
    monkeypatch.setenv("LIONAGI_OPERATOR_DB_PATH", str(db_path))
    monkeypatch.setenv("LIONAGI_OPERATOR_CONVERSATION_ID", cid)
    monkeypatch.setenv("LIONAGI_OPERATOR_REQUEST_ID", accepted["requestId"])

    result = await run_progress({"run": "current"})
    assert result["found"] is False
    assert isinstance(result.get("reason"), str) and result["reason"]


async def test_run_progress_too_short_reference_not_found(db_path):
    from lionagi.studio.operator.run_progress import run_progress

    async with StateDB(db_path):
        pass

    result = await run_progress({"run": "ab"})
    assert result["found"] is False
    assert isinstance(result.get("reason"), str) and result["reason"]


async def test_run_progress_candidate_cap_and_truncation(db_path):
    from lionagi.studio.operator.run_progress import run_progress

    ids = []
    for index in range(12):
        sid = str(uuid.uuid4())
        ids.append(sid)
        await seed_session(
            db_path,
            session_id=sid,
            name=f"sweep-run-{index:02d}",
            status="completed",
            updated_at=time.time() + index,
        )

    result = await run_progress({"run": "sweep-run"})

    assert result["found"] is True
    assert result["ambiguous"] is True
    assert len(result["candidates"]) == 10
    assert result["truncated"] is True


async def test_run_progress_rejects_unknown_fields(db_path):
    from pydantic import ValidationError

    from lionagi.studio.operator.run_progress import RunProgressInput

    with pytest.raises(ValidationError):
        RunProgressInput.model_validate({"run": "x", "unexpected": True})


# ── DAG progress: planned graph nodes with no materialized branch yet ─────


async def test_run_progress_dag_progress_derives_from_graph_not_branches(db_path):
    """A DAG can have planned nodes with no materialized branch yet; opsTotal
    must reflect the graph, not len(branches), and each node's status must be
    honestly reported -- including "unknown" for a node this tool cannot map
    to any recorded lifecycle signal."""
    from lionagi.studio.operator.run_progress import run_progress

    sid = str(uuid.uuid4())
    node_metadata = {
        "early_graph": {
            "nodes": [
                {"id": "plan", "label": "plan"},
                {"id": "work", "label": "work"},
                {"id": "review", "label": "review"},
            ],
            "edges": [
                {"id": "e-plan-work", "source": "plan", "target": "work", "mode": "simple"},
                {"id": "e-work-review", "source": "work", "target": "review", "mode": "simple"},
            ],
        }
    }
    async with StateDB(db_path) as db:
        prog_id = f"{sid}-prog"
        await db.create_progression(prog_id)
        await db.create_session(
            {
                "id": sid,
                "progression_id": prog_id,
                "name": "graph-run",
                "status": "running",
                "started_at": 1000.0,
                "node_metadata": node_metadata,
                "invocation_kind": "agent",
                "source_kind": "live",
                "updated_at": time.time(),
            }
        )
        # Only "plan" and "work" have ever materialized as branches -- "review"
        # is a planned node with no branch row at all yet.
        await db.create_progression(f"{sid}-plan-prog")
        await db.create_branch(
            {
                "id": f"{sid}-plan",
                "created_at": 1000.0,
                "name": "plan",
                "session_id": sid,
                "progression_id": f"{sid}-plan-prog",
                "model": "gpt-5",
                "provider": "openai",
                "agent_name": "plan",
            }
        )
        await db.update_branch(
            f"{sid}-plan", status="completed", started_at=1000.0, ended_at=1005.0
        )
        await db.insert_session_signal(
            session_id=sid, kind="NodeCompleted", op_id="op-1", ts=1005.0, payload={"name": "plan"}
        )
        await db.insert_session_signal(
            session_id=sid, kind="NodeStarted", op_id="op-2", ts=1006.0, payload={"name": "work"}
        )

    result = await run_progress({"run": sid})

    assert result["hasGraph"] is True
    assert result["opsTotal"] == 3
    assert result["opsCompleted"] == 1
    assert result["opsRunning"] == 1
    assert result["opsFailed"] == 0
    # "review" has no branch and no signal -- it folds into the pending
    # scalar bucket (not yet observably started) while still being named
    # "unknown" in the per-node projection below.
    assert result["opsPending"] == 1

    dag = result["dagProgress"]
    assert dag["total"] == 3
    assert dag["completed"] == 1
    assert dag["running"] == 1
    assert dag["failed"] == 0
    assert dag["pending"] == 1
    assert dag["unknownCount"] == 1
    by_id = {node["id"]: node for node in dag["nodes"]}
    assert by_id["plan"]["status"] == "succeeded"
    assert by_id["work"]["status"] == "running"
    assert by_id["review"]["status"] == "unknown"


async def test_run_progress_terminal_run_reconciles_stale_node_lanes(db_path):
    """A run that died without emitting node-terminal signals must not keep
    projecting agents mid-flight: on a terminal run, in-flight lanes read
    "aborted", never-started lanes read "skipped", and the op the run's own
    failure evidence names reads "failed"."""
    from lionagi.studio.operator.run_progress import run_progress

    sid = str(uuid.uuid4())
    node_metadata = {
        "early_graph": {
            "nodes": [
                {"id": "coordinator", "label": "coordinator"},
                {"id": "explorer", "label": "explorer"},
                {"id": "critic", "label": "critic"},
                {"id": "reporter", "label": "reporter"},
            ],
            "edges": [],
        }
    }
    async with StateDB(db_path) as db:
        prog_id = f"{sid}-prog"
        await db.create_progression(prog_id)
        await db.create_session(
            {
                "id": sid,
                "progression_id": prog_id,
                "name": "dead-play",
                "status": "running",
                "started_at": 1000.0,
                "node_metadata": node_metadata,
                "invocation_kind": "agent",
                "source_kind": "live",
                "updated_at": time.time(),
            }
        )
        # coordinator succeeded; explorer started and never terminated;
        # critic was queued (and is the op the failure evidence names);
        # reporter never emitted any signal at all.
        await db.insert_session_signal(
            session_id=sid,
            kind="NodeCompleted",
            op_id="op-1",
            ts=1005.0,
            payload={"name": "coordinator"},
        )
        await db.insert_session_signal(
            session_id=sid,
            kind="NodeStarted",
            op_id="op-2",
            ts=1006.0,
            payload={"name": "explorer"},
        )
        await db.insert_session_signal(
            session_id=sid,
            kind="NodeQueued",
            op_id="op-3",
            ts=1007.0,
            payload={"name": "critic"},
        )
        await db.update_status(
            "session",
            sid,
            new_status="failed",
            reason_code="run.failed.exception",
            reason_summary="1 operation(s) failed: critic.",
            evidence_refs=[{"kind": "failed_operation", "id": "critic", "label": "critic"}],
        )

    result = await run_progress({"run": sid})

    dag = result["dagProgress"]
    by_id = {node["id"]: node for node in dag["nodes"]}
    assert by_id["coordinator"]["status"] == "succeeded"
    assert by_id["explorer"]["status"] == "aborted"
    assert by_id["critic"]["status"] == "failed"
    assert by_id["reporter"]["status"] == "skipped"
    assert dag["running"] == 0
    assert dag["failed"] == 2  # the named failure plus the aborted node
    assert dag["abortedCount"] == 1
    assert dag["skippedCount"] == 1
    assert result["opsRunning"] == 0
    assert result["opsFailed"] == 2


async def test_run_progress_dag_keeps_escalated_distinct_from_failed(db_path):
    """A hard escalation is pending follow-up, not a failed operation; a
    genuine NodeFailed in the same recorded-state shape remains failed."""
    from lionagi.studio.operator.run_progress import run_progress

    sid = str(uuid.uuid4())
    escalated_op_id = "op-needs-help"
    failed_op_id = "op-broken"
    node_metadata = {
        "early_graph": {
            "nodes": [
                {"id": "needs-help", "label": "needs-help"},
                {"id": "broken", "label": "broken"},
            ],
            "edges": [],
        }
    }
    async with StateDB(db_path) as db:
        prog_id = f"{sid}-prog"
        await db.create_progression(prog_id)
        await db.create_session(
            {
                "id": sid,
                "progression_id": prog_id,
                "name": "escalation-run",
                "status": "running",
                "started_at": 1000.0,
                "node_metadata": node_metadata,
                "invocation_kind": "agent",
                "source_kind": "live",
                "updated_at": time.time(),
            }
        )
        await db.insert_session_signal(
            session_id=sid,
            kind="NodeEscalated",
            op_id=escalated_op_id,
            ts=1001.0,
            payload={"name": "needs-help", "route": "higher_tier"},
        )
        await db.insert_session_signal(
            session_id=sid,
            kind="NodeSpawned",
            op_id="op-retry",
            ts=1002.0,
            payload={
                "parent_id": escalated_op_id,
                "independent": True,
            },
        )
        await db.insert_session_signal(
            session_id=sid,
            kind="NodeFailed",
            op_id=failed_op_id,
            ts=1003.0,
            payload={"name": "broken"},
        )

    result = await run_progress({"run": sid})

    assert result["opsTotal"] == 2
    assert result["opsFailed"] == 1
    assert result["opsPending"] == 1

    dag = result["dagProgress"]
    assert dag["failed"] == 1
    assert dag["pending"] == 1
    by_id = {node["id"]: node for node in dag["nodes"]}
    assert by_id["needs-help"]["status"] == "escalated"
    assert by_id["broken"]["status"] == "failed"


async def test_run_progress_escalated_count_separates_the_two_kinds_of_pending(db_path):
    """The scalar buckets must sum to total, so an escalation folds into
    pending — but a caller reading only the scalars still has to tell a node
    that is merely waiting to start from one that has stopped and is asking
    for a decision. The graph here makes those two different numbers: one
    escalated node and one node with no lifecycle signal at all both land in
    pending, and escalatedCount must name only the first."""
    from lionagi.studio.operator.run_progress import run_progress

    sid = str(uuid.uuid4())
    node_metadata = {
        "early_graph": {
            "nodes": [
                {"id": "needs-help", "label": "needs-help"},
                {"id": "broken", "label": "broken"},
                {"id": "waiting", "label": "waiting"},
            ],
            "edges": [],
        }
    }
    async with StateDB(db_path) as db:
        prog_id = f"{sid}-prog"
        await db.create_progression(prog_id)
        await db.create_session(
            {
                "id": sid,
                "progression_id": prog_id,
                "name": "escalation-count-run",
                "status": "running",
                "started_at": 1000.0,
                "node_metadata": node_metadata,
                "invocation_kind": "agent",
                "source_kind": "live",
                "updated_at": time.time(),
            }
        )
        await db.insert_session_signal(
            session_id=sid,
            kind="NodeEscalated",
            op_id="op-needs-help",
            ts=1001.0,
            payload={"name": "needs-help", "route": "higher_tier"},
        )
        await db.insert_session_signal(
            session_id=sid,
            kind="NodeFailed",
            op_id="op-broken",
            ts=1002.0,
            payload={"name": "broken"},
        )
        # "waiting" deliberately gets no signal, so it reaches pending by the
        # other route and the two counts cannot be the same number.

    dag = (await run_progress({"run": sid}))["dagProgress"]

    assert dag["total"] == 3
    assert dag["failed"] == 1
    assert dag["pending"] == 2
    assert dag["unknownCount"] == 1
    assert dag["escalatedCount"] == 1
    # The scalars still account for every node.
    assert dag["completed"] + dag["running"] + dag["failed"] + dag["pending"] == dag["total"]


async def test_run_progress_escalated_count_is_present_as_zero_when_nothing_escalated(
    db_path,
):
    """Present on every response, including as zero. A count that appears only
    when non-zero is the one callers never wire up, because every run they
    develop against lacks it."""
    from lionagi.studio.operator.run_progress import run_progress

    sid = str(uuid.uuid4())
    node_metadata = {"early_graph": {"nodes": [{"id": "broken", "label": "broken"}], "edges": []}}
    async with StateDB(db_path) as db:
        prog_id = f"{sid}-prog"
        await db.create_progression(prog_id)
        await db.create_session(
            {
                "id": sid,
                "progression_id": prog_id,
                "name": "no-escalation-run",
                "status": "running",
                "started_at": 1000.0,
                "node_metadata": node_metadata,
                "invocation_kind": "agent",
                "source_kind": "live",
                "updated_at": time.time(),
            }
        )
        await db.insert_session_signal(
            session_id=sid,
            kind="NodeFailed",
            op_id="op-broken",
            ts=1001.0,
            payload={"name": "broken"},
        )

    dag = (await run_progress({"run": sid}))["dagProgress"]

    assert "escalatedCount" in dag
    assert dag["escalatedCount"] == 0
    assert dag["failed"] == 1


async def test_run_progress_no_graph_has_null_dag_progress(db_path):
    from lionagi.studio.operator.run_progress import run_progress

    sid = str(uuid.uuid4())
    await seed_session(db_path, session_id=sid, status="completed")

    result = await run_progress({"run": sid})

    assert result["hasGraph"] is False
    assert result["dagProgress"] is None


# ── reference resolution: project isolation ───────────────────────────────


async def _make_running_turn_with_project(db_path: Path, *, project: str | None) -> tuple[str, str]:
    from lionagi.studio.operator.store import OperatorStore

    store = OperatorStore(db_path)
    conversation = await store.create_conversation()
    cid = conversation["id"]
    context: dict[str, Any] = {"space": "mission", "route": "/", "filters": {}}
    if project is not None:
        context["project"] = project
    accepted = await store.submit_turn(
        cid,
        instruction="how is the nightly run going?",
        context=context,
        expected_last_sequence=0,
    )
    assert await store.mark_running(accepted["requestId"])
    return cid, accepted["requestId"]


async def test_run_progress_text_search_is_scoped_to_the_turns_project(db_path, monkeypatch):
    """A name-substring reference must not resolve to -- or even reveal the
    existence of -- a same-named run in a different project, when the calling
    turn names one."""
    from lionagi.studio.operator.run_progress import run_progress

    sid_in_scope = str(uuid.uuid4())
    sid_out_of_scope = str(uuid.uuid4())
    await seed_session(
        db_path,
        session_id=sid_in_scope,
        name="nightly-triage",
        project="/Users/admin/acme-research",
        status="completed",
    )
    await seed_session(
        db_path,
        session_id=sid_out_of_scope,
        name="nightly-triage",
        project="/Users/admin/acme-ops",
        status="completed",
    )

    cid, request_id = await _make_running_turn_with_project(
        db_path, project="/Users/admin/acme-research"
    )
    monkeypatch.setenv("LIONAGI_OPERATOR_DB_PATH", str(db_path))
    monkeypatch.setenv("LIONAGI_OPERATOR_CONVERSATION_ID", cid)
    monkeypatch.setenv("LIONAGI_OPERATOR_REQUEST_ID", request_id)

    result = await run_progress({"run": "nightly-triage"})

    assert result["found"] is True
    assert result["ambiguous"] is False
    assert result["id"] == sid_in_scope


async def test_run_progress_exact_id_of_a_foreign_project_run_is_not_found(db_path, monkeypatch):
    """An exact-id/prefix match must obey the same project ownership the
    text-search arm already enforces."""
    from lionagi.studio.operator.run_progress import run_progress

    foreign = str(uuid.uuid4())
    await seed_session(
        db_path,
        session_id=foreign,
        name="foreign-run",
        project="/Users/admin/acme-ops",
        status="running",
    )

    cid, request_id = await _make_running_turn_with_project(
        db_path, project="/Users/admin/acme-research"
    )
    monkeypatch.setenv("LIONAGI_OPERATOR_DB_PATH", str(db_path))
    monkeypatch.setenv("LIONAGI_OPERATOR_CONVERSATION_ID", cid)
    monkeypatch.setenv("LIONAGI_OPERATOR_REQUEST_ID", request_id)

    result = await run_progress({"run": foreign})
    assert result["found"] is False
    assert isinstance(result.get("reason"), str) and result["reason"]

    # A prefix match must be scoped the same way.
    result = await run_progress({"run": foreign[:8]})
    assert result["found"] is False
    assert isinstance(result.get("reason"), str) and result["reason"]


async def test_run_progress_current_of_a_foreign_project_run_is_not_found(db_path, monkeypatch):
    """The 'current' reference must not bypass project ownership either --
    the human's own selection is not itself proof of ownership."""
    from lionagi.studio.operator.run_progress import run_progress
    from lionagi.studio.operator.store import OperatorStore

    foreign = str(uuid.uuid4())
    await seed_session(
        db_path,
        session_id=foreign,
        name="foreign-run",
        project="/Users/admin/acme-ops",
        status="completed",
    )

    store = OperatorStore(db_path)
    cid = (await store.create_conversation())["id"]
    accepted = await store.submit_turn(
        cid,
        instruction="how is this run going?",
        context={
            "space": "history",
            "route": "/fleet",
            "filters": {},
            "project": "/Users/admin/acme-research",
            "selection": {"s": foreign},
        },
        expected_last_sequence=0,
    )
    assert await store.mark_running(accepted["requestId"])
    monkeypatch.setenv("LIONAGI_OPERATOR_DB_PATH", str(db_path))
    monkeypatch.setenv("LIONAGI_OPERATOR_CONVERSATION_ID", cid)
    monkeypatch.setenv("LIONAGI_OPERATOR_REQUEST_ID", accepted["requestId"])

    result = await run_progress({"run": "current"})
    assert result["found"] is False
    assert isinstance(result.get("reason"), str) and result["reason"]


async def test_run_progress_ambiguous_id_prefix_hides_foreign_project_candidates(
    db_path, monkeypatch
):
    """A short id-prefix collision must drop foreign-project candidates and
    resolve uniquely when only one owned row survives."""
    from lionagi.studio.operator.run_progress import run_progress

    owned = "deadbeef-1111-2222-3333-444455556666"
    foreign = "deadbeef-9999-8888-7777-666655554444"
    await seed_session(
        db_path, session_id=owned, name="owned-run", project="/Users/admin/acme-research"
    )
    await seed_session(
        db_path, session_id=foreign, name="foreign-run", project="/Users/admin/acme-ops"
    )

    cid, request_id = await _make_running_turn_with_project(
        db_path, project="/Users/admin/acme-research"
    )
    monkeypatch.setenv("LIONAGI_OPERATOR_DB_PATH", str(db_path))
    monkeypatch.setenv("LIONAGI_OPERATOR_CONVERSATION_ID", cid)
    monkeypatch.setenv("LIONAGI_OPERATOR_REQUEST_ID", request_id)

    result = await run_progress({"run": "deadbeef"})

    assert result["found"] is True
    assert result["ambiguous"] is False
    assert result["id"] == owned


# ── reference resolution: missing owner context fails closed ──────────────


async def test_run_progress_turn_with_no_project_context_fails_closed(db_path, monkeypatch):
    """A turn whose identity is present but whose own context names no
    project must never fall back to enumerating every project's runs:
    prefix and name-substring references are refused with a typed error
    whose message names the remedy. The one deliberate exception is an
    exact full-UUID reference -- it identifies at most one row and cannot
    enumerate, the same position run_detail takes for a bare id."""
    from lionagi.studio.operator.run_progress import MissingOwnerContextError, resolve_run

    sid = str(uuid.uuid4())
    await seed_session(db_path, session_id=sid, name="nightly-triage", status="completed")

    cid, request_id = await _make_running_turn_with_project(db_path, project=None)
    monkeypatch.setenv("LIONAGI_OPERATOR_DB_PATH", str(db_path))
    monkeypatch.setenv("LIONAGI_OPERATOR_CONVERSATION_ID", cid)
    monkeypatch.setenv("LIONAGI_OPERATOR_REQUEST_ID", request_id)

    with pytest.raises(MissingOwnerContextError, match="full 36-character id"):
        await resolve_run("nightly-triage")

    with pytest.raises(MissingOwnerContextError):
        await resolve_run(sid[:8])

    result = await resolve_run(sid)
    assert result == {"found": True, "ambiguous": False, "session_id": sid}

    # A nonexistent exact UUID is a clean not-found, not an error -- and not
    # a fall-through into the fenced text-search arms.
    result = await resolve_run(str(uuid.uuid4()))
    assert result["found"] is False
    assert isinstance(result.get("reason"), str) and result["reason"]


async def test_run_progress_current_resolves_on_a_turn_with_no_project(db_path, monkeypatch):
    """'Cancel/inspect the run the human is looking at' must work from any
    view: the current-view selection is a full id the human's own browser
    reported, so it rides the exact-id arm through the project fence. The
    seeded row deliberately has no project -- the arm must not depend on
    one."""
    from lionagi.studio.operator.run_progress import run_progress
    from lionagi.studio.operator.store import OperatorStore

    sid = str(uuid.uuid4())
    await seed_session(db_path, session_id=sid, status="completed", project=None)

    store = OperatorStore(db_path)
    cid = (await store.create_conversation())["id"]
    accepted = await store.submit_turn(
        cid,
        instruction="how is this run going?",
        context={
            "space": "mission",
            "route": "/",
            "filters": {},
            "selection": {"s": sid},
        },
        expected_last_sequence=0,
    )
    assert await store.mark_running(accepted["requestId"])
    monkeypatch.setenv("LIONAGI_OPERATOR_DB_PATH", str(db_path))
    monkeypatch.setenv("LIONAGI_OPERATOR_CONVERSATION_ID", cid)
    monkeypatch.setenv("LIONAGI_OPERATOR_REQUEST_ID", accepted["requestId"])

    result = await run_progress({"run": "current"})

    assert result["found"] is True
    assert result["id"] == sid


async def test_run_progress_no_identity_at_all_stays_unscoped(db_path):
    """Distinct from the above: when the turn identity environment is
    entirely absent (no durable turn exists at all -- the pre-existing
    direct-call/test convenience), resolution still falls open rather than
    raising. Only a real turn with a missing project mapping fails closed."""
    from lionagi.studio.operator.run_progress import resolve_run

    sid = str(uuid.uuid4())
    await seed_session(db_path, session_id=sid, name="nightly-triage", status="completed")

    result = await resolve_run(sid)
    assert result == {"found": True, "ambiguous": False, "session_id": sid}


def test_terminal_safe_health_drops_only_the_vacuous_healthy():
    """Health is a liveness concept: for a terminal run the classifier's
    "healthy" only means "no residue", and projected beside status=failed it
    reads as a claim about the run's outcome. The projection drops exactly
    that pairing -- a live run's "healthy" and a terminal run's pathological
    verdict (leftover locks) both pass through unchanged."""
    from lionagi.studio.operator.run_progress import _terminal_safe_health

    for terminal in ("completed", "completed_empty", "failed", "timed_out", "aborted", "cancelled"):
        assert _terminal_safe_health({"status": terminal, "effective_health": "healthy"}) is None

    assert _terminal_safe_health({"status": "running", "effective_health": "healthy"}) == "healthy"
    assert _terminal_safe_health({"status": "failed", "effective_health": "zombie"}) == "zombie"
    assert _terminal_safe_health({"status": "running", "effective_health": "stale"}) == "stale"
