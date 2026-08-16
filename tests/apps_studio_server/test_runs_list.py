"""Tests for paginated, filtered runs list."""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi", reason="studio extra not installed")
from fastapi.testclient import TestClient  # noqa: E402

from lionagi.state.db import StateDB  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _seed_sessions(db_path: Path, sessions: list[dict]) -> None:
    async with StateDB(db_path) as db:
        for s in sessions:
            pid = str(uuid.uuid4())
            await db.create_progression(pid)
            payload = {
                "id": s.get("id", str(uuid.uuid4())),
                "progression_id": pid,
                "name": s.get("name"),
                "status": s.get("status", "completed"),
                "playbook_name": s.get("playbook_name"),
                "agent_name": s.get("agent_name"),
                "started_at": s.get("started_at", time.time()),
                "project": s.get("project"),
            }
            # Only forward updated_at when set — create_session treats a present
            # key as authoritative and would otherwise write a NULL timestamp.
            if "updated_at" in s:
                payload["updated_at"] = s["updated_at"]
            if "invocation_kind" in s:
                payload["invocation_kind"] = s["invocation_kind"]
            await db.create_session(payload)


def _make_client(tmp_path, monkeypatch, db_path: Path) -> TestClient:
    import lionagi.state.db as state_db_mod

    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)

    from lionagi.studio.app import app

    return TestClient(app, base_url="http://127.0.0.1:8765")


def test_runs_list_paginates_with_default_shape(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    sessions = [{"id": str(uuid.uuid4()), "status": "completed"} for _ in range(25)]
    _run(_seed_sessions(db_path, sessions))
    client = _make_client(tmp_path, monkeypatch, db_path)

    r = client.get("/api/runs")
    assert r.status_code == 200
    data = r.json()
    assert len(data["runs"]) == 20
    assert data["page"] == 1
    assert data["per_page"] == 20
    assert data["total"] == 25
    assert data["total_pages"] == 2
    assert data["has_next"] is True
    assert data["has_prev"] is False


def test_runs_list_second_page(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    sessions = [{"id": str(uuid.uuid4()), "status": "completed"} for _ in range(25)]
    _run(_seed_sessions(db_path, sessions))
    client = _make_client(tmp_path, monkeypatch, db_path)

    r = client.get("/api/runs?page=2&per_page=20")
    assert r.status_code == 200
    data = r.json()
    assert len(data["runs"]) == 5
    assert data["has_next"] is False
    assert data["has_prev"] is True


def test_runs_list_filters_multi_status_and_playbook_contains(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    sessions_data = [
        {"id": str(uuid.uuid4()), "status": "running", "playbook_name": "alpha"},
        {"id": str(uuid.uuid4()), "status": "failed", "playbook_name": "beta"},
        {"id": str(uuid.uuid4()), "status": "completed", "playbook_name": "alpha-long"},
    ]
    _run(_seed_sessions(db_path, sessions_data))
    client = _make_client(tmp_path, monkeypatch, db_path)

    # status=running&status=done means running OR done/completed
    r = client.get("/api/runs?status=running&status=done&playbook=alpha")
    assert r.status_code == 200
    data = r.json()
    runs = data["runs"]
    # Should get running/alpha and completed/alpha-long but not failed/beta
    statuses = {run["status"] for run in runs}
    assert "failed" not in statuses
    playbooks = {run["playbook_name"] for run in runs}
    for pb in playbooks:
        assert pb is None or "alpha" in pb.lower()


def test_runs_list_surfaces_status_reason(tmp_path, monkeypatch):
    """GET /api/runs list rows must carry status_reason_code/summary (ADR-0057),
    the same fields the detail route (_run_row via get_run) already exposes."""
    from lionagi.state.reasons import RunReasons

    db_path = tmp_path / "state.db"
    sid = str(uuid.uuid4())
    _run(_seed_sessions(db_path, [{"id": sid, "status": "running"}]))

    async def _fail_it():
        async with StateDB(db_path) as db:
            await db.update_status(
                "session",
                sid,
                new_status="failed",
                reason_code=RunReasons.FAILED_EXIT_NONZERO,
                reason_summary="worker exited with code 1",
            )

    _run(_fail_it())
    client = _make_client(tmp_path, monkeypatch, db_path)

    r = client.get("/api/runs")
    assert r.status_code == 200
    run = r.json()["runs"][0]
    assert run["status"] == "failed"
    assert run["status_reason_code"] == RunReasons.FAILED_EXIT_NONZERO
    assert run["status_reason_summary"] == "worker exited with code 1"


def test_runs_list_invalid_page_rejected(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    client = _make_client(tmp_path, monkeypatch, db_path)
    r = client.get("/api/runs?page=0")
    assert r.status_code == 422


async def test_list_runs_offloads_process_snapshot(tmp_path, monkeypatch):
    import lionagi.state.db as state_db_mod
    import lionagi.studio.services.admin as admin_mod
    import lionagi.studio.services.runs as runs_mod

    db_path = tmp_path / "state.db"
    await _seed_sessions(db_path, [{"id": str(uuid.uuid4()), "status": "running"}])
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)
    # The host scan is cached behind module globals with a TTL. Left alone,
    # whether this test observes a capture at all depends on which tests ran
    # before it and how recently, so it passes alone and fails in a suite.
    # Clearing the cache makes the capture happen here, which is the only way
    # to say anything about the thread it happens on.
    monkeypatch.setattr(admin_mod, "_PS_SNAPSHOT_CACHE", None, raising=False)
    monkeypatch.setattr(admin_mod, "_PS_SNAPSHOT_INFLIGHT", {}, raising=False)
    monkeypatch.setattr(admin_mod, "_PS_SNAPSHOT_METRICS", None, raising=False)
    monkeypatch.setattr(admin_mod, "_PS_SNAPSHOT_SEQUENCE", 0, raising=False)
    event_loop_thread = threading.get_ident()
    snapshot_threads: list[int] = []

    def fake_snapshot() -> str:
        snapshot_threads.append(threading.get_ident())
        return ""

    monkeypatch.setattr(admin_mod, "_ps_snapshot", fake_snapshot)

    await runs_mod.list_runs(limit=20)

    assert snapshot_threads
    assert snapshot_threads[0] != event_loop_thread


@pytest.mark.parametrize("unknown_name", ["limit", "worker"])
def test_runs_list_rejects_unknown_query_parameters(tmp_path, monkeypatch, unknown_name):
    db_path = tmp_path / "state.db"
    client = _make_client(tmp_path, monkeypatch, db_path)

    response = client.get("/api/runs", params={unknown_name: "200"})

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["query", unknown_name]
    assert response.json()["detail"][0]["type"] == "extra_forbidden"


# GET /api/runs/projects — per-project counts for the lazy runs explorer


def test_runs_projects_groups_counts_and_sorted(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    base = time.time()
    sessions = (
        [
            {"id": str(uuid.uuid4()), "project": "org/alpha", "updated_at": base - 100}
            for _ in range(3)
        ]
        + [
            {"id": str(uuid.uuid4()), "project": "org/beta", "updated_at": base - 10}
            for _ in range(2)
        ]
        + [{"id": str(uuid.uuid4()), "project": None, "updated_at": base - 50}]
    )
    _run(_seed_sessions(db_path, sessions))
    client = _make_client(tmp_path, monkeypatch, db_path)

    r = client.get("/api/runs/projects")
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 6
    counts = {g["project"]: g["count"] for g in data["projects"]}
    assert counts == {"org/alpha": 3, "org/beta": 2, None: 1}
    # Sorted by last_activity desc → beta (newest) first; never shadowed by /runs/{id}.
    order = [g["project"] for g in data["projects"]]
    assert order[0] == "org/beta"
    activities = [g["last_activity"] for g in data["projects"]]
    assert activities == sorted(activities, reverse=True)


def test_runs_list_project_null_filter(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    sessions = [{"id": str(uuid.uuid4()), "project": "org/alpha"} for _ in range(2)] + [
        {"id": str(uuid.uuid4()), "project": None} for _ in range(3)
    ]
    _run(_seed_sessions(db_path, sessions))
    client = _make_client(tmp_path, monkeypatch, db_path)

    r = client.get("/api/runs?project_null=true")
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 3
    assert all(run["project"] is None for run in data["runs"])

    # A positive project filter returns only that project's runs.
    r2 = client.get("/api/runs?project=org/alpha")
    assert r2.json()["total"] == 2


# GET /api/runs?search= — session/agent name contains filter


def test_runs_list_search_matches_name_or_agent_name(tmp_path, monkeypatch):
    # search still matches the raw stored name/agent_name columns in SQL, but
    # the returned `name` field is the resolved display name: a session with
    # an agent_name gets the agent-role descriptor, not its raw stored name
    # — so the second row's displayed name is derived from its agent_name,
    # not the "unrelated" string it was seeded with.
    from lionagi.state.session_naming import agent_role_label

    started_at = 1767277320.0  # 2026-01-01T14:22:00Z
    agent_session_id = str(uuid.uuid4())
    db_path = tmp_path / "state.db"
    sessions = [
        {"id": str(uuid.uuid4()), "name": "fix flaky login test"},
        {
            "id": agent_session_id,
            "name": "unrelated",
            "agent_name": "flaky-hunter",
            "started_at": started_at,
        },
        {"id": str(uuid.uuid4()), "name": "totally different"},
    ]
    _run(_seed_sessions(db_path, sessions))
    client = _make_client(tmp_path, monkeypatch, db_path)

    r = client.get("/api/runs?search=flaky")
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 2
    names = {run["name"] for run in data["runs"]}
    assert names == {
        "fix flaky login test",
        agent_role_label("flaky-hunter", started_at, agent_session_id),
    }


def test_runs_list_search_is_case_insensitive(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    sessions = [{"id": str(uuid.uuid4()), "name": "Deploy Pipeline"}]
    _run(_seed_sessions(db_path, sessions))
    client = _make_client(tmp_path, monkeypatch, db_path)

    r = client.get("/api/runs?search=DEPLOY")
    assert r.json()["total"] == 1


def test_runs_list_search_no_match_returns_empty(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    sessions = [{"id": str(uuid.uuid4()), "name": "alpha"}]
    _run(_seed_sessions(db_path, sessions))
    client = _make_client(tmp_path, monkeypatch, db_path)

    r = client.get("/api/runs?search=zzz-no-such-thing")
    assert r.status_code == 200
    assert r.json()["total"] == 0
    assert r.json()["runs"] == []


def test_runs_list_search_escapes_percent_wildcard(tmp_path, monkeypatch):
    """A literal '%' in the query must not act as a SQL LIKE wildcard — searching
    for "50%" should match only names containing that literal substring, not
    every row in the store."""
    db_path = tmp_path / "state.db"
    sessions = [
        {"id": str(uuid.uuid4()), "name": "hit rate 50% today"},
        {"id": str(uuid.uuid4()), "name": "completely unrelated"},
    ]
    _run(_seed_sessions(db_path, sessions))
    client = _make_client(tmp_path, monkeypatch, db_path)

    r = client.get("/api/runs", params={"search": "50%"})
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 1
    assert data["runs"][0]["name"] == "hit rate 50% today"


def test_runs_list_search_escapes_underscore_wildcard(tmp_path, monkeypatch):
    """A literal '_' must match only itself, not SQL LIKE's any-single-char
    wildcard — "job_1" must not also match "jobX1"."""
    db_path = tmp_path / "state.db"
    sessions = [
        {"id": str(uuid.uuid4()), "name": "job_1 run"},
        {"id": str(uuid.uuid4()), "name": "jobX1 run"},
    ]
    _run(_seed_sessions(db_path, sessions))
    client = _make_client(tmp_path, monkeypatch, db_path)

    r = client.get("/api/runs", params={"search": "job_1"})
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 1
    assert data["runs"][0]["name"] == "job_1 run"


def test_runs_list_playbook_filter_also_escapes_wildcards(tmp_path, monkeypatch):
    """The pre-existing playbook contains-filter shares the same LIKE-building
    code path and had the same unescaped-wildcard hole — covering it here so a
    future refactor can't silently reopen it."""
    db_path = tmp_path / "state.db"
    sessions = [
        {"id": str(uuid.uuid4()), "playbook_name": "release_50%"},
        {"id": str(uuid.uuid4()), "playbook_name": "unrelated-playbook"},
    ]
    _run(_seed_sessions(db_path, sessions))
    client = _make_client(tmp_path, monkeypatch, db_path)

    r = client.get("/api/runs", params={"playbook": "50%"})
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 1
    assert data["runs"][0]["playbook_name"] == "release_50%"


def test_runs_list_search_composes_with_pagination(tmp_path, monkeypatch):
    """Search must be applied in SQL before paging, not to an already-truncated
    page — seed more matches than one page and confirm total/pagination reflect
    the full filtered result set."""
    db_path = tmp_path / "state.db"
    matching = [{"id": str(uuid.uuid4()), "name": f"target-{i}"} for i in range(25)]
    noise = [{"id": str(uuid.uuid4()), "name": f"noise-{i}"} for i in range(10)]
    _run(_seed_sessions(db_path, matching + noise))
    client = _make_client(tmp_path, monkeypatch, db_path)

    r = client.get("/api/runs?search=target&per_page=20")
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 25
    assert len(data["runs"]) == 20
    assert data["has_next"] is True
    assert all("target" in run["name"] for run in data["runs"])


# ADR-0057: UNRESPONSIVE maps to 'stale' in runs list


async def _seed_running_session_with_activity(
    db_path: Path,
    session_id: str,
    last_message_at: float,
    invocation_kind: str = "agent",
    artifacts_path: str | None = None,
) -> None:
    async with StateDB(db_path) as db:
        pid = str(uuid.uuid4())
        await db.create_progression(pid)
        await db.create_session(
            {
                "id": session_id,
                "progression_id": pid,
                "name": "test-stale",
                "status": "running",
                "invocation_kind": invocation_kind,
                "started_at": last_message_at,
                "last_message_at": last_message_at,
                "artifacts_path": artifacts_path,
            }
        )


def test_runs_list_threshold_crossing_alive_session_reports_unresponsive(tmp_path, monkeypatch):
    """Running session, process alive, past its kind-aware threshold → 'unresponsive'.

    The runs list exposes the classifier verdict verbatim: a live-but-quiet
    session is UNRESPONSIVE, distinct from a process-dead 'stale' run. The
    dashboard maps 'unresponsive' onto a "stuck" attention row.
    """
    db_path = tmp_path / "state.db"
    sid = str(uuid.uuid4())
    # last_message_at = 7h ago; agent threshold = 6h; process alive → UNRESPONSIVE
    old_activity = time.time() - 7 * 3600
    _run(_seed_running_session_with_activity(db_path, sid, last_message_at=old_activity))
    client = _make_client(tmp_path, monkeypatch, db_path)
    # Pin liveness to True so the classifier yields UNRESPONSIVE (alive + past
    # threshold), not the process-dead STALE path — the seeded session has no
    # real process to probe.
    monkeypatch.setattr("lionagi.studio.services.runs._session_liveness", lambda *a, **k: True)

    r = client.get("/api/runs")
    assert r.status_code == 200
    runs = r.json()["runs"]
    target = next((run for run in runs if run["id"] == sid), None)
    assert target is not None, "seeded session not found in runs list"
    assert target["effective_health"] == "unresponsive", (
        f"expected 'unresponsive', got {target['effective_health']!r}; "
        "a live-but-quiet session must surface as UNRESPONSIVE, not collapsed to 'stale'"
    )


def test_runs_list_confirmed_dead_process_reports_stale_despite_recent_activity(
    tmp_path, monkeypatch
):
    """A running session whose recorded process is confirmed dead must report
    'stale' even with fresh messages — positive death evidence outranks the
    activity guard."""
    db_path = tmp_path / "state.db"
    sid = str(uuid.uuid4())
    _run(
        _seed_running_session_with_activity(
            db_path, sid, last_message_at=time.time() - 30, artifacts_path=str(tmp_path)
        )
    )
    client = _make_client(tmp_path, monkeypatch, db_path)
    monkeypatch.setattr("lionagi.studio.services.runs._session_liveness", lambda *a, **k: False)

    r = client.get("/api/runs")
    assert r.status_code == 200
    target = next((run for run in r.json()["runs"] if run["id"] == sid), None)
    assert target is not None
    assert target["effective_health"] == "stale"


def test_runs_list_unknown_liveness_recent_activity_stays_healthy(tmp_path, monkeypatch):
    """Unknown liveness (externally-driven session, no matchable pid) keeps the
    activity guard: recent messages classify as healthy."""
    db_path = tmp_path / "state.db"
    sid = str(uuid.uuid4())
    _run(
        _seed_running_session_with_activity(
            db_path, sid, last_message_at=time.time() - 30, artifacts_path=str(tmp_path)
        )
    )
    client = _make_client(tmp_path, monkeypatch, db_path)
    monkeypatch.setattr("lionagi.studio.services.runs._session_liveness", lambda *a, **k: None)

    r = client.get("/api/runs")
    assert r.status_code == 200
    target = next((run for run in r.json()["runs"] if run["id"] == sid), None)
    assert target is not None
    assert target["effective_health"] == "healthy"


def test_runs_list_node_metadata_dead_pid_reports_stale_without_monkeypatch(tmp_path, monkeypatch):
    """End-to-end: a running session whose node_metadata records a pid that is
    no longer running must report 'stale' through the real oracle — the list
    query must surface node_metadata to the liveness check."""
    import subprocess

    proc = subprocess.Popen(["/bin/sleep", "0"])  # noqa: S603
    proc.wait()
    dead_pid = proc.pid

    db_path = tmp_path / "state.db"
    sid = str(uuid.uuid4())

    async def _seed() -> None:
        async with StateDB(db_path) as db:
            pid = str(uuid.uuid4())
            await db.create_progression(pid)
            await db.create_session(
                {
                    "id": sid,
                    "progression_id": pid,
                    "name": "test-dead-pid",
                    "status": "running",
                    "invocation_kind": "agent",
                    "started_at": time.time() - 60,
                    "last_message_at": time.time() - 30,
                    "artifacts_path": str(tmp_path),
                    "node_metadata": {"pid": dead_pid},
                }
            )

    _run(_seed())
    client = _make_client(tmp_path, monkeypatch, db_path)

    r = client.get("/api/runs")
    assert r.status_code == 200
    target = next((run for run in r.json()["runs"] if run["id"] == sid), None)
    assert target is not None
    assert target["effective_health"] == "stale"


def test_runs_list_reports_status_ended_at_mismatch_count(tmp_path, monkeypatch):
    """A row whose status is 'running' but whose ended_at is already stamped
    must be visible as a recomputed consistency count on the listing
    envelope, not something a caller can only find by cross-checking every
    row's two fields by hand."""
    db_path = tmp_path / "state.db"
    good_running_id = str(uuid.uuid4())
    good_completed_id = str(uuid.uuid4())
    mismatched_id = str(uuid.uuid4())

    async def _seed() -> None:
        async with StateDB(db_path) as db:
            for sid, status in (
                (good_running_id, "running"),
                (good_completed_id, "completed"),
                (mismatched_id, "running"),
            ):
                pid = str(uuid.uuid4())
                await db.create_progression(pid)
                await db.create_session(
                    {
                        "id": sid,
                        "progression_id": pid,
                        "name": "test-mismatch",
                        "status": status,
                        "started_at": time.time() - 60,
                    }
                )
            # Simulate the write-path bug directly at the data layer: ended_at
            # stamped while status is still "running".
            await db.update_session(mismatched_id, ended_at=time.time())

    _run(_seed())
    client = _make_client(tmp_path, monkeypatch, db_path)

    r = client.get("/api/runs")
    assert r.status_code == 200
    data = r.json()
    assert data["status_ended_at_mismatches"] == 1


def test_engine_child_transcript_rows_are_collapsed_out_of_the_listing(tmp_path, monkeypatch):
    """A mirrored CLI transcript stamped as another run's engine child (see
    claude_mirror.link_engine_child_session) duplicates that canonical run,
    so the listing and its total must both exclude it — while the row itself
    stays readable by id for anyone holding a direct link."""
    db_path = tmp_path / "state.db"
    canonical_id = str(uuid.uuid4())
    child_id = str(uuid.uuid4())

    async def _seed() -> None:
        async with StateDB(db_path) as db:
            for sid, name in (
                (canonical_id, "Operator"),
                (child_id, "Operator · engine transcript"),
            ):
                pid = str(uuid.uuid4())
                await db.create_progression(pid)
                await db.create_session(
                    {
                        "id": sid,
                        "progression_id": pid,
                        "name": name,
                        "status": "completed",
                        "started_at": time.time() - 60,
                    }
                )
            await db.merge_session_node_metadata(child_id, {"engine_parent_run_id": canonical_id})

    _run(_seed())
    client = _make_client(tmp_path, monkeypatch, db_path)

    r = client.get("/api/runs")
    assert r.status_code == 200
    data = r.json()
    listed = {row["id"] for row in data["runs"]}
    assert canonical_id in listed
    assert child_id not in listed
    assert data["total"] == 1

    detail = client.get(f"/api/runs/{child_id}")
    assert detail.status_code == 200


def test_terminal_rows_never_read_healthy(tmp_path, monkeypatch):
    """A finished run has no live process for "healthy" to describe, so the
    row drops the vacuous verdict instead of projecting "failed but healthy".
    A running row keeps its real classifier verdict."""
    db_path = tmp_path / "state.db"
    now = time.time()
    sessions = [
        {"id": str(uuid.uuid4()), "status": "completed", "started_at": now - 60},
        {"id": str(uuid.uuid4()), "status": "failed", "started_at": now - 60},
        {"id": str(uuid.uuid4()), "status": "cancelled", "started_at": now - 60},
        {"id": str(uuid.uuid4()), "status": "running", "started_at": now - 5},
    ]
    _run(_seed_sessions(db_path, sessions))
    client = _make_client(tmp_path, monkeypatch, db_path)

    rows = client.get("/api/runs", params={"per_page": 50}).json()["runs"]
    by_status = {r["status"]: r for r in rows}
    for status in ("completed", "failed", "cancelled"):
        assert by_status[status]["effective_health"] != "healthy"
    # The non-terminal row keeps whatever the classifier said — the fix must
    # not blank health where a live process could still carry one.
    assert by_status["running"]["effective_health"] is not None


def test_runs_list_kind_facet(tmp_path, monkeypatch):
    """?kind= narrows by invocation_kind: 'show' admits both spellings of a
    show-driven play root, 'agent' also admits legacy NULL-kind rows, and an
    unknown value is refused (422) rather than silently returning nothing."""
    db_path = tmp_path / "state.db"
    now = time.time()
    ids = {
        "agent": str(uuid.uuid4()),
        "legacy": str(uuid.uuid4()),
        "play": str(uuid.uuid4()),
        "flow": str(uuid.uuid4()),
        "show-play": str(uuid.uuid4()),
    }
    sessions = [
        {"id": ids["agent"], "invocation_kind": "agent", "started_at": now - 10},
        {"id": ids["legacy"], "started_at": now - 20},  # NULL kind
        {"id": ids["play"], "invocation_kind": "play", "started_at": now - 30},
        {"id": ids["flow"], "invocation_kind": "flow", "started_at": now - 40},
        {"id": ids["show-play"], "invocation_kind": "show-play", "started_at": now - 50},
    ]
    _run(_seed_sessions(db_path, sessions))
    client = _make_client(tmp_path, monkeypatch, db_path)

    def listed(**params):
        r = client.get("/api/runs", params=params)
        assert r.status_code == 200
        return {row["id"] for row in r.json()["runs"]}

    assert listed(kind="play") == {ids["play"]}
    assert listed(kind="show") == {ids["show-play"]}
    assert listed(kind="agent") == {ids["agent"], ids["legacy"]}
    # Facets are repeatable and OR-composed.
    assert listed(**{"kind": ["play", "flow"]}) == {ids["play"], ids["flow"]}
    # No facet — everything.
    assert listed() == set(ids.values())
    assert client.get("/api/runs", params={"kind": "bogus"}).status_code == 422


def test_codex_cost_projects_as_null_until_tracked(tmp_path, monkeypatch):
    """Codex runs' stored cost figure comes from a pricing table known to be
    wrong (spend is not actually tracked), so every projection nulls it —
    NULL already means "unreported" under the cost-visibility contract.
    Other providers' reported costs pass through untouched."""
    db_path = tmp_path / "state.db"
    codex_id = str(uuid.uuid4())
    claude_id = str(uuid.uuid4())

    async def _seed():
        async with StateDB(db_path) as db:
            for sid, provider in ((codex_id, "codex"), (claude_id, "claude_code")):
                pid = str(uuid.uuid4())
                await db.create_progression(pid)
                await db.create_session(
                    {
                        "id": sid,
                        "progression_id": pid,
                        "status": "completed",
                        "provider": provider,
                        "started_at": time.time(),
                    }
                )
                await db.update_session(sid, total_cost_usd=33.92)

    _run(_seed())
    client = _make_client(tmp_path, monkeypatch, db_path)

    rows = {r["id"]: r for r in client.get("/api/runs").json()["runs"]}
    assert rows[codex_id]["total_cost_usd"] is None
    assert rows[claude_id]["total_cost_usd"] == 33.92

    detail = client.get(f"/api/runs/{codex_id}").json()
    assert detail["total_cost_usd"] is None
