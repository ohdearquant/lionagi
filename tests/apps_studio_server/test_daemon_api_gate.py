# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Studio daemon API contract gate.

Regression class this guards, in plain terms: CLI/daemon contract drift that
was discovered live rather than caught by CI. Two known instances of this
class: a URL-prefix bug where a client (mis-)configured base URL caused every
request to double up the "/api" prefix (".../api/api/..."), and a CWD bug
where a spawned subprocess inherited the daemon's working directory instead
of resolving one explicitly, so behavior depended on how the daemon itself
happened to be launched. Neither was caught by a test; both were found by an
operator hitting a broken request in practice.

This suite pins the daemon's actual, observed contract:

  1. The full sorted (method, path) route table (excluding FastAPI's
     auto-generated docs/schema routes) as a golden list. Adding, renaming,
     or removing an endpoint forces a deliberate edit here instead of
     silently drifting.
  2. The `/api` prefix contract: it appears exactly once in every mounted
     API route path (the shape of the double-prefix regression class).
  3. Per-endpoint success/404/422/400 response shape (status code + sorted
     top-level JSON object keys, not full payloads) for the admin, session,
     and schedule endpoint families -- the areas gate 5 was scoped to.

Everything here is read empirically from the routers themselves
(lionagi/studio/services/admin.py, sessions.py, schedules.py) and from
running the live app against a temp SQLite DB, per the existing suite's
`_patch_db` / `monkeypatch.setattr(..., DEFAULT_DB_PATH, ...)` pattern (see
test_admin.py, test_schedule_runs_route.py) -- never against ~/.lionagi.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi", reason="studio extra not installed")
from fastapi.testclient import TestClient  # noqa: E402

from lionagi.state.db import StateDB  # noqa: E402

from ._helpers import run_async  # noqa: E402

# ---------------------------------------------------------------------------
# 1. Golden route table -- excludes FastAPI's auto-generated docs/schema routes.
# ---------------------------------------------------------------------------

_DOCS_PATHS = frozenset({"/openapi.json", "/docs", "/redoc", "/docs/oauth2-redirect"})

# Sorted (method, path) pairs for every non-docs route mounted on the live
# app, pinned 2026-07-12 against the actual route table (see
# lionagi/studio/registry.py::_STUDIO_ROUTE_MODULES for the area modules that
# populate it, and app.py::_mount_studio_routes for the "/api" + route.path
# mounting rule).
_GOLDEN_ROUTES: tuple[tuple[str, str], ...] = (
    ("DELETE", "/api/agents/{name}"),
    ("DELETE", "/api/engine-defs/{def_id}"),
    ("DELETE", "/api/playbooks/{name}"),
    ("DELETE", "/api/projects/{name}"),
    ("DELETE", "/api/schedules/{schedule_id}"),
    ("DELETE", "/api/sessions/{session_id}/tags/{tag:path}"),
    ("DELETE", "/api/workflow-defs/{def_id}"),
    ("GET", "/api/admin/doctor"),
    ("GET", "/api/admin/events"),
    ("GET", "/api/admin/health"),
    ("GET", "/api/agents/"),
    ("GET", "/api/agents/{name}"),
    ("GET", "/api/approvals/evidence/verify"),
    ("GET", "/api/approvals/{approval_id}"),
    ("GET", "/api/artifacts/by-session/{session_id}"),
    ("GET", "/api/artifacts/{artifact_id}"),
    ("GET", "/api/casts/"),
    ("GET", "/api/definitions/"),
    ("GET", "/api/definitions/{kind}/{name}"),
    ("GET", "/api/definitions/{kind}/{name}/versions/{version}"),
    ("GET", "/api/engine-defs/"),
    ("GET", "/api/engine-defs/{def_id}"),
    ("GET", "/api/engine-runs/"),
    ("GET", "/api/engine-runs/{run_id}"),
    ("GET", "/api/invocations/"),
    ("GET", "/api/invocations/{invocation_id}"),
    ("GET", "/api/playbook-templates/"),
    ("GET", "/api/playbook-templates/{name}"),
    ("GET", "/api/playbooks/"),
    ("GET", "/api/playbooks/{name}"),
    ("GET", "/api/plugins"),
    ("GET", "/api/plugins/{name}"),
    ("GET", "/api/plugins/{plugin_name}/agents/{agent_name}"),
    ("GET", "/api/plugins/{plugin_name}/skills/{skill_name}"),
    ("GET", "/api/projects/"),
    ("GET", "/api/projects/{name}"),
    ("GET", "/api/runs/"),
    ("GET", "/api/runs/projects"),
    ("GET", "/api/runs/{run_id}"),
    ("GET", "/api/runs/{run_id}/file"),
    ("GET", "/api/schedules/"),
    ("GET", "/api/schedules/limits"),
    ("GET", "/api/schedules/runs/{run_id}"),
    ("GET", "/api/schedules/{schedule_id}"),
    ("GET", "/api/schedules/{schedule_id}/runs"),
    ("GET", "/api/sessions/"),
    ("GET", "/api/sessions/{session_id}"),
    ("GET", "/api/sessions/{session_id}/signals"),
    ("GET", "/api/sessions/{session_id}/stream"),
    ("GET", "/api/shows/"),
    ("GET", "/api/shows/{topic}"),
    ("GET", "/api/shows/{topic}/stream"),
    ("GET", "/api/skills/"),
    ("GET", "/api/skills/{name}"),
    ("GET", "/api/stats"),
    ("GET", "/api/stats/activity"),
    ("GET", "/api/teams/"),
    ("GET", "/api/teams/{team_id}"),
    ("GET", "/api/workflow-defs/"),
    ("GET", "/api/workflow-defs/{def_id}"),
    ("GET", "/health"),
    ("PATCH", "/api/schedules/{schedule_id}"),
    ("POST", "/api/admin/maintenance"),
    ("POST", "/api/admin/prune"),
    ("POST", "/api/admin/prune-old-data"),
    ("POST", "/api/admin/transition"),
    ("POST", "/api/agents/{name}"),
    ("POST", "/api/agents/{name}/validate"),
    ("POST", "/api/approvals/"),
    ("POST", "/api/approvals/{approval_id}/deny"),
    ("POST", "/api/approvals/{approval_id}/grant"),
    ("POST", "/api/definitions/snapshot"),
    ("POST", "/api/definitions/{kind}/{name}"),
    ("POST", "/api/definitions/{kind}/{name}/rollback"),
    ("POST", "/api/engine-defs/"),
    ("POST", "/api/invocations/{invocation_id}/cancel"),
    ("POST", "/api/launches/"),
    ("POST", "/api/leo/sessions"),
    ("POST", "/api/leo/sessions/{session_id}/messages"),
    ("POST", "/api/playbook-templates/{name}/install"),
    ("POST", "/api/playbooks/{name}"),
    ("POST", "/api/playbooks/{name}/run"),
    ("POST", "/api/playbooks/{name}/validate"),
    ("POST", "/api/projects/"),
    ("POST", "/api/projects/{name}/assign"),
    ("POST", "/api/schedules/"),
    ("POST", "/api/schedules/{schedule_id}/disable"),
    ("POST", "/api/schedules/{schedule_id}/enable"),
    ("POST", "/api/schedules/{schedule_id}/trigger"),
    ("POST", "/api/sessions/{session_id}/tags"),
    ("POST", "/api/shows/import"),
    ("POST", "/api/workflow-defs/"),
    ("POST", "/api/workflow-defs/{def_id}/run"),
    ("PUT", "/api/agents/{name}"),
    ("PUT", "/api/engine-defs/{def_id}"),
    ("PUT", "/api/playbooks/{name}"),
    ("PUT", "/api/projects/{name}"),
    ("PUT", "/api/workflow-defs/{def_id}"),
)


def _live_app_routes() -> list[tuple[str, str]]:
    """(method, path) pairs for every route on the live app, docs routes excluded."""
    from lionagi.studio.app import app

    pairs: set[tuple[str, str]] = set()
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if path is None or methods is None:
            # Non-endpoint route entries (e.g. a static asset Mount when a
            # frontend dist is configured) carry no HTTP method set at all.
            continue
        if path in _DOCS_PATHS:
            continue
        for method in methods:
            if method == "HEAD":
                # FastAPI auto-adds HEAD for every GET; it is not an
                # independently-registered endpoint and would double every
                # GET row in the golden list for no contract value.
                continue
            pairs.add((method, path))
    return sorted(pairs)


def test_golden_route_table_matches_pinned_snapshot():
    """Any added/renamed/removed studio endpoint must be a deliberate edit here."""
    actual = _live_app_routes()
    expected = sorted(_GOLDEN_ROUTES)
    missing = set(expected) - set(actual)
    unexpected = set(actual) - set(expected)
    assert not missing, f"routes removed/renamed since golden was pinned: {sorted(missing)}"
    assert not unexpected, f"routes added since golden was pinned: {sorted(unexpected)}"
    assert actual == expected


def test_golden_route_count_pinned():
    assert len(_GOLDEN_ROUTES) == 98


# ---------------------------------------------------------------------------
# 2. The /api prefix contract -- the double-prefix regression class.
# ---------------------------------------------------------------------------


def test_api_prefix_appears_exactly_once_in_every_route_path():
    """Every mounted studio API route path carries exactly one "/api" segment.

    Guards the double-prefix regression class directly: a client (or a
    future route-registration change) that prepends "/api" a second time
    would produce paths like "/api/api/sessions/", which this test catches
    by counting occurrences of the literal "/api" substring rather than
    just checking the leading prefix.
    """
    for _method, path in _live_app_routes():
        if path == "/health":
            assert "/api" not in path
            continue
        assert path.startswith("/api/"), f"non-/health route missing /api prefix: {path!r}"
        assert path.count("/api") == 1, f"route path repeats the /api prefix: {path!r}"


# ---------------------------------------------------------------------------
# Shared fixtures/helpers for the per-family response-shape pins below.
# ---------------------------------------------------------------------------


def _patch_db(monkeypatch, db_path: Path) -> None:
    """Point every service module's DB reference at a fresh temp path.

    Must run before any seeding call -- StateDB() reads
    lionagi.state.db.DEFAULT_DB_PATH fresh at each instantiation (see
    StateDB.__init__), and admin.py/sessions.py additionally cache the path
    as a plain string in their own module-level `_DB` for aiosqlite.connect.
    """
    import lionagi.state.db as state_db_mod
    import lionagi.studio.services.admin as admin_mod
    import lionagi.studio.services.schedules as schedules_mod
    import lionagi.studio.services.sessions as sessions_mod

    # StateDB's path cascade consults settings.LIONAGI_STATE_DB_URL BEFORE
    # DEFAULT_DB_PATH, so an environment with that set (dev machine, CI)
    # would route these tests at the real configured DB — neutralize it.
    # AppSettings is frozen, so swap the module's reference for a copy.
    monkeypatch.setattr(
        state_db_mod,
        "settings",
        state_db_mod.settings.model_copy(update={"LIONAGI_STATE_DB_URL": None}),
    )
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(admin_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(admin_mod, "_DB", str(db_path))
    monkeypatch.setattr(sessions_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(sessions_mod, "_DB", str(db_path))
    monkeypatch.setattr(schedules_mod, "DEFAULT_DB_PATH", db_path)


def _make_client() -> TestClient:
    from lionagi.studio.app import app

    return TestClient(app, base_url="http://127.0.0.1:8765")


async def _seed_completed_session(db_path: Path, session_id: str) -> None:
    prog_id = f"{session_id}-prog"
    async with StateDB(db_path) as db:
        await db.create_progression(prog_id)
        await db.create_session(
            {
                "id": session_id,
                "progression_id": prog_id,
                "name": "gate-test-session",
                "status": "completed",
                "started_at": time.time() - 10,
                "ended_at": time.time(),
            }
        )


async def _seed_running_session(db_path: Path, session_id: str) -> None:
    prog_id = f"{session_id}-prog"
    async with StateDB(db_path) as db:
        await db.create_progression(prog_id)
        await db.create_session(
            {
                "id": session_id,
                "progression_id": prog_id,
                "name": "gate-test-running-session",
                "status": "running",
                "started_at": time.time(),
            }
        )


# ---------------------------------------------------------------------------
# 3a. Admin endpoint family.
# ---------------------------------------------------------------------------


def test_admin_doctor_response_shape(tmp_path, monkeypatch):
    _patch_db(monkeypatch, tmp_path / "state.db")
    client = _make_client()

    r = client.get("/api/admin/doctor")
    assert r.status_code == 200
    assert sorted(r.json().keys()) == ["db_health", "diagnostic_run_at", "phantom_sessions"]


def test_admin_health_response_shape(tmp_path, monkeypatch):
    _patch_db(monkeypatch, tmp_path / "state.db")
    client = _make_client()

    r = client.get("/api/admin/health")
    assert r.status_code == 200
    assert sorted(r.json().keys()) == ["db", "diagnostic_run_at", "sessions"]


def test_admin_events_response_shape(tmp_path, monkeypatch):
    _patch_db(monkeypatch, tmp_path / "state.db")
    client = _make_client()

    r = client.get("/api/admin/events")
    assert r.status_code == 200
    assert sorted(r.json().keys()) == ["events"]


def test_admin_transition_success_response_shape(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    sid = str(uuid.uuid4())
    run_async(_seed_running_session(db_path, sid))
    client = _make_client()

    r = client.post(
        "/api/admin/transition",
        json={"session_ids": [sid], "target_status": "failed", "reason": "gate pin"},
    )
    assert r.status_code == 200
    assert sorted(r.json().keys()) == ["event_id", "skipped", "transitioned"]


def test_admin_transition_missing_reason_400_shape(tmp_path, monkeypatch):
    """Body validates fine (session_ids + target_status present) but the
    handler's own reason_code-required check fails -- a 400, not 422."""
    _patch_db(monkeypatch, tmp_path / "state.db")
    client = _make_client()

    r = client.post(
        "/api/admin/transition",
        json={"session_ids": ["x"], "target_status": "failed"},
    )
    assert r.status_code == 400
    assert sorted(r.json().keys()) == ["detail"]


def test_admin_transition_malformed_body_422_shape(tmp_path, monkeypatch):
    """Missing required fields (session_ids, target_status) fails pydantic
    validation before the handler ever runs."""
    _patch_db(monkeypatch, tmp_path / "state.db")
    client = _make_client()

    r = client.post("/api/admin/transition", json={})
    assert r.status_code == 422
    assert sorted(r.json().keys()) == ["detail"]


def test_admin_maintenance_checkpoint_response_shape(tmp_path, monkeypatch):
    """No DB on disk yet -- checkpoint_state_db's no-op shape."""
    _patch_db(monkeypatch, tmp_path / "state.db")
    client = _make_client()

    r = client.post("/api/admin/maintenance", json={"action": "checkpoint"})
    assert r.status_code == 200
    assert sorted(r.json().keys()) == ["action", "busy", "checkpointed", "log_pages", "mode"]


def test_admin_maintenance_malformed_action_422_shape(tmp_path, monkeypatch):
    _patch_db(monkeypatch, tmp_path / "state.db")
    client = _make_client()

    r = client.post("/api/admin/maintenance", json={"action": "not-a-real-action"})
    assert r.status_code == 422
    assert sorted(r.json().keys()) == ["detail"]


def test_admin_prune_old_data_response_shape(tmp_path, monkeypatch):
    _patch_db(monkeypatch, tmp_path / "state.db")
    client = _make_client()

    r = client.post("/api/admin/prune-old-data", json={})
    assert r.status_code == 200
    assert sorted(r.json().keys()) == ["dispatch_purged", "runs_pruned", "sessions_pruned"]


# ---------------------------------------------------------------------------
# 3b. Sessions endpoint family.
# ---------------------------------------------------------------------------

_SESSION_DETAIL_KEYS = sorted(
    [
        "agent_hash",
        "agent_name",
        "artifact_contract_json",
        "artifact_verification_json",
        "artifacts_path",
        "branches",
        "created_at",
        "duration_ms",
        "effort",
        "ended_at",
        "graph",
        "id",
        "invocation_id",
        "invocation_kind",
        "last_message_at",
        "message_cursor",
        "message_limit",
        "message_next_cursor",
        "message_stats",
        "model",
        "name",
        "node_metadata",
        "playbook_name",
        "project",
        "project_source",
        "provider",
        "segments",
        "show_play_name",
        "show_topic",
        "source_kind",
        "source_show",
        "started_at",
        "status",
        "status_evidence_refs",
        "status_reason_code",
        "status_reason_summary",
        "updated_at",
    ]
)


def test_sessions_list_response_shape(tmp_path, monkeypatch):
    _patch_db(monkeypatch, tmp_path / "state.db")
    client = _make_client()

    r = client.get("/api/sessions/")
    assert r.status_code == 200
    assert sorted(r.json().keys()) == ["sessions"]


def test_sessions_detail_response_shape(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    sid = str(uuid.uuid4())
    run_async(_seed_completed_session(db_path, sid))
    client = _make_client()

    r = client.get(f"/api/sessions/{sid}")
    assert r.status_code == 200
    assert sorted(r.json().keys()) == _SESSION_DETAIL_KEYS


def test_sessions_detail_404_shape(tmp_path, monkeypatch):
    _patch_db(monkeypatch, tmp_path / "state.db")
    client = _make_client()

    r = client.get(f"/api/sessions/{uuid.uuid4()}")
    assert r.status_code == 404
    assert sorted(r.json().keys()) == ["detail"]


def test_sessions_detail_malformed_cursor_400_shape(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    sid = str(uuid.uuid4())
    run_async(_seed_completed_session(db_path, sid))
    client = _make_client()

    r = client.get(f"/api/sessions/{sid}", params={"message_cursor": "not-valid-base64-json!!"})
    assert r.status_code == 400
    assert sorted(r.json().keys()) == ["detail"]


# ---------------------------------------------------------------------------
# 3c. Schedules endpoint family.
# ---------------------------------------------------------------------------

_SCHEDULE_DETAIL_KEYS = sorted(
    [
        "action_agent",
        "action_command",
        "action_command_args",
        "action_cwd",
        "action_extra_args",
        "action_flow_yaml",
        "action_kind",
        "action_model",
        "action_playbook",
        "action_project",
        "action_prompt",
        "budget_tokens",
        "budget_usd",
        "consecutive_failures",
        "created_at",
        "cron_expr",
        "description",
        "enabled",
        "github_cursor",
        "github_filter",
        "github_repo",
        "id",
        "interval_sec",
        "last_alert_at",
        "last_fired_at",
        "last_healthy_poll_at",
        "last_status",
        "max_runs",
        "missed_fire_policy",
        "name",
        "next_fire_at",
        "on_fail",
        "on_success",
        "overlap_policy",
        "poll_interval_sec",
        "poller_consecutive_401",
        "project",
        "recent_runs",
        "threshold_config",
        "trigger_type",
        "updated_at",
    ]
)


def _create_gate_schedule(db_path: Path) -> str:
    async def _create() -> str:
        import lionagi.studio.services.schedules as schedules_mod

        created = await schedules_mod.create_schedule(
            {
                "name": f"gate-test-{uuid.uuid4().hex[:8]}",
                "trigger_type": "cron",
                "cron_expr": "0 18 * * *",
                "action_kind": "agent",
                "action_prompt": "ping",
            }
        )
        return created["id"]

    return run_async(_create())


def test_schedules_list_response_shape(tmp_path, monkeypatch):
    _patch_db(monkeypatch, tmp_path / "state.db")
    client = _make_client()

    r = client.get("/api/schedules/")
    assert r.status_code == 200
    assert sorted(r.json().keys()) == ["schedules"]


def test_schedules_limits_response_shape(tmp_path, monkeypatch):
    _patch_db(monkeypatch, tmp_path / "state.db")
    client = _make_client()

    r = client.get("/api/schedules/limits")
    assert r.status_code == 200
    assert sorted(r.json().keys()) == ["current_inflight", "max_scheduled_concurrent"]


def test_schedules_create_response_shape(tmp_path, monkeypatch):
    _patch_db(monkeypatch, tmp_path / "state.db")
    client = _make_client()

    r = client.post(
        "/api/schedules/",
        json={
            "name": f"gate-create-{uuid.uuid4().hex[:8]}",
            "trigger_type": "cron",
            "cron_expr": "0 18 * * *",
            "action_kind": "agent",
            "action_prompt": "ping",
        },
    )
    assert r.status_code == 201
    assert sorted(r.json().keys()) == ["created_at", "id", "name"]


def test_schedules_create_malformed_body_422_shape(tmp_path, monkeypatch):
    """action_kind is a required pydantic field; omitting it never reaches the handler."""
    _patch_db(monkeypatch, tmp_path / "state.db")
    client = _make_client()

    r = client.post(
        "/api/schedules/",
        json={"name": "missing-action-kind", "trigger_type": "cron"},
    )
    assert r.status_code == 422
    assert sorted(r.json().keys()) == ["detail"]


def test_schedules_create_domain_validation_400_shape(tmp_path, monkeypatch):
    """Body is pydantic-valid but violates a domain rule (cron trigger with
    no cron_expr) -- create_schedule() raises ValueError, mapped to 400."""
    _patch_db(monkeypatch, tmp_path / "state.db")
    client = _make_client()

    r = client.post(
        "/api/schedules/",
        json={"name": "no-cron-expr", "trigger_type": "cron", "action_kind": "agent"},
    )
    assert r.status_code == 400
    assert sorted(r.json().keys()) == ["detail"]


def test_schedules_detail_response_shape(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    schedule_id = _create_gate_schedule(db_path)
    client = _make_client()

    r = client.get(f"/api/schedules/{schedule_id}")
    assert r.status_code == 200
    assert sorted(r.json().keys()) == _SCHEDULE_DETAIL_KEYS


def test_schedules_detail_404_shape(tmp_path, monkeypatch):
    _patch_db(monkeypatch, tmp_path / "state.db")
    client = _make_client()

    r = client.get("/api/schedules/does-not-exist")
    assert r.status_code == 404
    assert sorted(r.json().keys()) == ["detail"]


@pytest.mark.parametrize(
    "method,path_suffix",
    [
        ("patch", ""),
        ("delete", ""),
        ("post", "/enable"),
        ("post", "/disable"),
        ("post", "/trigger"),
    ],
)
def test_schedules_unknown_id_404_shape_across_mutation_routes(
    tmp_path, monkeypatch, method, path_suffix
):
    _patch_db(monkeypatch, tmp_path / "state.db")
    client = _make_client()

    kwargs = {"json": {"name": "x"}} if method == "patch" else {}
    r = getattr(client, method)(f"/api/schedules/does-not-exist{path_suffix}", **kwargs)
    assert r.status_code == 404
    assert sorted(r.json().keys()) == ["detail"]


def test_schedule_runs_list_response_shape(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    schedule_id = _create_gate_schedule(db_path)
    client = _make_client()

    r = client.get(f"/api/schedules/{schedule_id}/runs")
    assert r.status_code == 200
    assert sorted(r.json().keys()) == ["has_next", "limit", "offset", "runs"]


def test_schedule_run_detail_404_shape(tmp_path, monkeypatch):
    _patch_db(monkeypatch, tmp_path / "state.db")
    client = _make_client()

    r = client.get("/api/schedules/runs/does-not-exist")
    assert r.status_code == 404
    assert sorted(r.json().keys()) == ["detail"]
