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

import re
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
    ("GET", "/api/schedules/{schedule_id}/status"),
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


_PARAM_NAME_RE = re.compile(r"\(\?P<\w+>")


def _normalize_route_match_shape(path_regex_pattern: str) -> str:
    """Erase parameter NAMES from a compiled Starlette path_regex pattern, keeping converter/regex semantics: the router dispatches on the compiled body, not the param spelling, so `{id}` and `{schedule_id}` shadow each other while a typed `{id:int}` (different regex body) stays a genuinely distinct shape."""
    return _PARAM_NAME_RE.sub("(?P<_>", path_regex_pattern)


def _entries_from_fastapi_app(app: fastapi.FastAPI) -> list[tuple[str, str, str]]:
    """(method, path, match_shape) for every route on a FastAPI app's route table, as a LIST -- a route registered twice at the same match shape produces two entries. HEAD is excluded (FastAPI auto-adds it for every GET, so it would double every GET row for no contract value)."""
    entries: list[tuple[str, str, str]] = []
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if path is None or methods is None:
            # Non-endpoint route entries (e.g. a static asset Mount when a
            # frontend dist is configured) carry no HTTP method set at all.
            continue
        path_regex = getattr(route, "path_regex", None)
        match_shape = _normalize_route_match_shape(path_regex.pattern) if path_regex else path
        for method in methods:
            if method == "HEAD":
                continue
            entries.append((method, path, match_shape))
    return entries


def _find_duplicate_routes(
    entries: list[tuple[str, str, str]],
) -> dict[tuple[str, str], list[str]]:
    """Return {(method, match_shape): [path, path, ...]} for every match shape registered more than once -- keying on match_shape (not the raw path) catches routes that read as different source but compile to the same dispatch target, where FastAPI's router silently drops the later registration as dead code."""
    by_shape: dict[tuple[str, str], list[str]] = {}
    for method, path, match_shape in entries:
        by_shape.setdefault((method, match_shape), []).append(path)
    return {key: paths for key, paths in by_shape.items() if len(paths) > 1}


def _live_app_route_entries() -> list[tuple[str, str, str]]:
    """(method, path, match_shape) entries for every route on the live app,
    docs routes excluded. Preserves duplicates -- see _find_duplicate_routes."""
    from lionagi.studio.app import app

    return [
        (method, path, match_shape)
        for method, path, match_shape in _entries_from_fastapi_app(app)
        if path not in _DOCS_PATHS
    ]


def _live_app_routes() -> list[tuple[str, str]]:
    """(method, path) entries only, for callers that don't need match_shape."""
    return [(method, path) for method, path, _match_shape in _live_app_route_entries()]


def test_golden_route_table_matches_pinned_snapshot():
    """Any added/renamed/removed studio endpoint must be a deliberate edit here."""
    entries = _live_app_route_entries()

    duplicates = _find_duplicate_routes(entries)
    assert not duplicates, (
        "duplicate route registration(s) found -- distinct path spellings "
        "that FastAPI's router matches identically, each shadowing a "
        f"handler behind the first match: {duplicates}"
    )

    actual = sorted({(method, path) for method, path, _match_shape in entries})
    expected = sorted(_GOLDEN_ROUTES)
    missing = set(expected) - set(actual)
    unexpected = set(actual) - set(expected)
    assert not missing, f"routes removed/renamed since golden was pinned: {sorted(missing)}"
    assert not unexpected, f"routes added since golden was pinned: {sorted(unexpected)}"
    assert actual == expected


def test_golden_route_count_pinned():
    assert len(_GOLDEN_ROUTES) == 99


def _compiled_match_shape(path_template: str) -> str:
    """Real Starlette-compiled match shape for a path template, via an actual Route object (not a hand-typed regex) so the fixtures below can't drift from Starlette's own compile_path() output."""
    from starlette.routing import Route

    async def _noop() -> None:
        return None

    return _normalize_route_match_shape(Route(path_template, endpoint=_noop).path_regex.pattern)


def test_find_duplicate_routes_detects_a_shadowed_pair():
    shape_param = _compiled_match_shape("/api/schedules/{schedule_id}")
    shape_literal = _compiled_match_shape("/api/schedules/")
    assert shape_param != shape_literal

    entries = [
        ("GET", "/api/schedules/{schedule_id}", shape_param),
        ("GET", "/api/schedules/", shape_literal),
        ("GET", "/api/schedules/{schedule_id}", shape_param),
    ]
    assert _find_duplicate_routes(entries) == {
        ("GET", shape_param): ["/api/schedules/{schedule_id}", "/api/schedules/{schedule_id}"]
    }


def test_find_duplicate_routes_empty_for_unique_entries():
    shape = _compiled_match_shape("/api/schedules/")
    entries = [("GET", "/api/schedules/", shape), ("POST", "/api/schedules/", shape)]
    assert _find_duplicate_routes(entries) == {}


def test_normalize_route_match_shape_erases_param_names_but_keeps_converters():
    """The reviewer's exact distinction: two untyped params with different
    names collapse to one shape; a typed converter stays distinct from the
    untyped param even when the name matches."""
    shape_schedule_id = _compiled_match_shape("/api/schedules/{schedule_id}")
    shape_id = _compiled_match_shape("/api/schedules/{id}")
    shape_id_int = _compiled_match_shape("/api/schedules/{id:int}")

    assert shape_schedule_id == shape_id
    assert shape_id_int != shape_id


def test_duplicate_route_registration_is_caught_on_a_live_fastapi_app():
    """End-to-end proof, not just a property of the tuple-list helper: since the studio_route registry's own dedup guard already raises on a same-shape duplicate, this mounts two identical-path routes directly on a throwaway FastAPI app to prove _live_app_routes() would still catch it if that guard were ever bypassed."""
    from fastapi import FastAPI

    app = FastAPI()

    async def _handler() -> dict:
        return {}

    app.add_api_route("/api/schedules/{schedule_id}", _handler, methods=["GET"])
    app.add_api_route(
        "/api/schedules/{schedule_id}", _handler, methods=["GET"]
    )  # shadows the first

    duplicates = _find_duplicate_routes(_entries_from_fastapi_app(app))
    assert len(duplicates) == 1
    (method, _match_shape), paths = next(iter(duplicates.items()))
    assert method == "GET"
    assert paths == ["/api/schedules/{schedule_id}", "/api/schedules/{schedule_id}"]


def test_duplicate_route_registration_with_different_param_names_is_caught():
    """The reviewer's exact regression: /api/schedules/{schedule_id} and /api/schedules/{id} read as different source but compile to the same dispatch target, so the gate must name them as duplicates even though no two path strings are equal -- one route goes through an APIRouter + prefix (like real studio routers), proving match-shape normalization survives prefix compilation."""
    from fastapi import APIRouter, FastAPI

    app = FastAPI()

    async def _direct_handler() -> dict:
        return {}

    router = APIRouter(prefix="/api")

    @router.get("/schedules/{id}")
    async def _router_handler() -> dict:
        return {}

    app.include_router(router)
    app.add_api_route("/api/schedules/{schedule_id}", _direct_handler, methods=["GET"])

    duplicates = _find_duplicate_routes(_entries_from_fastapi_app(app))
    assert len(duplicates) == 1
    (method, _match_shape), paths = next(iter(duplicates.items()))
    assert method == "GET"
    assert sorted(paths) == ["/api/schedules/{id}", "/api/schedules/{schedule_id}"]


# ---------------------------------------------------------------------------
# 2. The /api prefix contract -- the double-prefix regression class.
# ---------------------------------------------------------------------------


def test_api_prefix_appears_exactly_once_in_every_route_path():
    """Guards the double-prefix regression class: a route-registration change that prepends '/api' a second time would produce '/api/api/...' paths, caught here by counting occurrences of the literal '/api' substring rather than just checking the leading prefix."""
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
    """Point every service module's DB reference at a fresh temp path; must run before any seeding call since StateDB() re-reads DEFAULT_DB_PATH fresh per instantiation, and admin.py/sessions.py additionally cache the path in their own module-level `_DB`."""
    import lionagi.state.db as state_db_mod
    import lionagi.studio.services.admin as admin_mod
    import lionagi.studio.services.db_maintenance as db_maintenance_mod
    import lionagi.studio.services.schedules as schedules_mod
    import lionagi.studio.services.sessions as sessions_mod

    # An environment with LIONAGI_STUDIO_AUTH_TOKEN set (a dev machine, CI)
    # would make every unauthenticated request in this file 401 before it
    # ever reaches the handler being pinned -- neutralize it, matching the
    # delenv pattern the rest of this suite uses (test_security_batch1.py,
    # test_audit_remediation.py, test_launches_api.py, test_startup_warnings.py).
    monkeypatch.delenv("LIONAGI_STUDIO_AUTH_TOKEN", raising=False)

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
    # db_maintenance imports DEFAULT_DB_PATH by value, so the state_db_mod
    # patch above never reaches its own module-level binding.
    monkeypatch.setattr(db_maintenance_mod, "DEFAULT_DB_PATH", db_path)


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
        "authored_spec",
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
        "managed_by",
        "max_runs",
        "missed_fire_policy",
        "name",
        "next_fire_at",
        "notify_command",
        "notify_on",
        "on_fail",
        "on_success",
        "overlap_policy",
        "owner_key",
        "poll_interval_sec",
        "poller_consecutive_401",
        "project",
        "rate_limit",
        "recent_runs",
        "resolved_digest",
        "resolved_target",
        "resolved_timezone",
        "spec_version",
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


# Success-path contracts for the same five mutation routes above. The 404
# parametrization only proves "unknown id is rejected"; it says nothing
# about what a successful call returns, so a handler's success shape could
# regress (e.g. PATCH's {"ok": True} silently becoming {}) without failing
# anything in this file. Each test creates its own schedule through the
# same temp-DB path the rest of the family uses, then pins the real response.


def test_schedules_patch_success_response_shape(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    schedule_id = _create_gate_schedule(db_path)
    client = _make_client()

    r = client.patch(f"/api/schedules/{schedule_id}", json={"description": "gate patch"})
    assert r.status_code == 200
    assert r.json() == {"ok": True}

    # The write actually landed, not just an accepted-and-dropped no-op.
    detail = client.get(f"/api/schedules/{schedule_id}")
    assert detail.json()["description"] == "gate patch"


def test_schedules_delete_success_response_shape(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    schedule_id = _create_gate_schedule(db_path)
    client = _make_client()

    r = client.delete(f"/api/schedules/{schedule_id}")
    assert r.status_code == 200
    assert r.json() == {"ok": True}

    # The schedule is actually gone, not just acknowledged.
    assert client.get(f"/api/schedules/{schedule_id}").status_code == 404


def test_schedules_enable_success_response_shape(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    schedule_id = _create_gate_schedule(db_path)
    client = _make_client()

    r = client.post(f"/api/schedules/{schedule_id}/enable")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "enabled": True}


def test_schedules_disable_success_response_shape(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    schedule_id = _create_gate_schedule(db_path)
    client = _make_client()

    r = client.post(f"/api/schedules/{schedule_id}/disable")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "enabled": False}

    # The write actually landed: a disabled schedule reads back falsy.
    # SQLite has no native boolean -- the column round-trips as the
    # integer 0/1, not Python False/True.
    detail = client.get(f"/api/schedules/{schedule_id}")
    assert not detail.json()["enabled"]


def test_schedules_trigger_success_response_shape(tmp_path, monkeypatch):
    """scheduler.fire_now() is mocked out (a real fire spawns a background subprocess with no place in a response-shape pin), isolating the route handler's own success contract -- {'ok': True, 'run_id': ...} -- from the scheduler engine's fire machinery, covered elsewhere."""
    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    schedule_id = _create_gate_schedule(db_path)
    client = _make_client()

    from lionagi.studio.scheduler.engine import scheduler

    async def _fake_fire_now(_schedule_id: str) -> str:
        return "gate-fake-run-id"

    monkeypatch.setattr(scheduler, "fire_now", _fake_fire_now)

    r = client.post(f"/api/schedules/{schedule_id}/trigger")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "run_id": "gate-fake-run-id"}


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


def test_schedule_status_response_shape(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    schedule_id = _create_gate_schedule(db_path)
    client = _make_client()

    r = client.get(f"/api/schedules/{schedule_id}/status")
    assert r.status_code == 200
    body = r.json()
    assert sorted(body.keys()) == ["exit_code", "latest_run", "schedule"]
    assert sorted(body["schedule"].keys()) == [
        "cron_expr",
        "enabled",
        "id",
        "interval_sec",
        "name",
        "next_fire_at",
        "trigger_type",
    ]
    assert body["latest_run"] is None
    assert body["exit_code"] == 2


def test_schedule_status_404_shape(tmp_path, monkeypatch):
    _patch_db(monkeypatch, tmp_path / "state.db")
    client = _make_client()

    r = client.get("/api/schedules/does-not-exist/status")
    assert r.status_code == 404
    assert sorted(r.json().keys()) == ["detail"]
