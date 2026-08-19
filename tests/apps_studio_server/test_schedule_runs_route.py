# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for the schedule and schedule-run response surfaces.

Covers pagination and status filtering on the run list, and the projection every
list surface applies: run lists serve a classification of a failure rather than the
text that produced it, and schedule records are served through an allow-list. The
raw traceback stays reachable through the single-run detail route.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi", reason="studio extra not installed")
from fastapi.testclient import TestClient  # noqa: E402

import lionagi.state.db as state_db_mod
from lionagi.state.db import StateDB  # noqa: E402
from lionagi.studio.services.schedules import (  # noqa: E402
    _UNCLASSIFIED_ERROR,
    create_schedule,
)


async def _seed_schedule() -> str:
    created = await create_schedule(
        {
            "name": f"runs-route-test-{uuid.uuid4().hex[:8]}",
            "trigger_type": "cron",
            "cron_expr": "0 18 * * *",
            "action_kind": "agent",
            "action_prompt": "ping",
        }
    )
    return created["id"]


async def _seed_run(
    schedule_id: str,
    *,
    status: str,
    fired_at: float,
    error_detail: str | None = None,
    chain_depth: int = 0,
    run_id: str | None = None,
    chain_parent_id: str | None = None,
) -> str:
    resolved_run_id = run_id or str(uuid.uuid4())
    async with StateDB() as db:
        await db.create_schedule_run(
            {
                "id": resolved_run_id,
                "schedule_id": schedule_id,
                "trigger_context": {"source": "cron"},
                "action_kind": "agent",
                "action_args": {"prompt": "ping"},
                "status": status,
                "chain_depth": chain_depth,
                "chain_parent_id": chain_parent_id,
                "fired_at": fired_at,
                "error_detail": error_detail,
            }
        )
    return resolved_run_id


def _patch_db(monkeypatch, db_path: Path) -> None:
    """Point both the StateDB default and the schedules service's own bound
    name at the temp path -- must run before any seeding, or seed writes
    land in the real default DB."""
    import lionagi.studio.services.schedules as schedules_mod

    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)


def _make_client() -> TestClient:
    from lionagi.studio.app import app

    return TestClient(app, base_url="http://127.0.0.1:8765")


def test_completed_and_failed_runs_serialize_with_200(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    now = time.time()

    async def seed():
        sid = await _seed_schedule()
        await _seed_run(sid, status="completed", fired_at=now - 20)
        await _seed_run(
            sid,
            status="failed",
            fired_at=now - 10,
            error_detail=(
                "Traceback (most recent call last):\n"
                '  File "engine.py", line 42, in fire\n'
                "pydantic_core.ValidationError: Provider must be specified\n"
            ),
        )
        return sid

    sid = asyncio.run(seed())
    client = _make_client()

    resp = client.get(f"/api/schedules/{sid}/runs", params={"limit": 25})

    assert resp.status_code == 200
    body = resp.json()
    assert body["limit"] == 25
    assert body["offset"] == 0
    assert len(body["runs"]) == 2
    assert {r["status"] for r in body["runs"]} == {"completed", "failed"}

    failed = next(r for r in body["runs"] if r["status"] == "failed")
    # A run list serves the classification; the traceback that produced it is reachable
    # only by opening the single run.
    assert failed["error_class"] == _UNCLASSIFIED_ERROR
    assert "error_detail" not in failed
    assert "trigger_context" not in failed
    assert "action_args" not in failed


def test_unknown_schedule_id_returns_empty_200(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    client = _make_client()

    resp = client.get("/api/schedules/does-not-exist/runs", params={"limit": 25})

    assert resp.status_code == 200
    assert resp.json() == {"runs": [], "limit": 25, "offset": 0, "has_next": False}


def test_status_filter_and_pagination(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    now = time.time()

    async def seed():
        sid = await _seed_schedule()
        for i in range(3):
            await _seed_run(sid, status="completed", fired_at=now - i)
        await _seed_run(sid, status="failed", fired_at=now - 100)
        return sid

    sid = asyncio.run(seed())
    client = _make_client()

    resp = client.get(f"/api/schedules/{sid}/runs", params={"status": "failed", "limit": 25})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["runs"]) == 1
    assert body["runs"][0]["status"] == "failed"

    resp = client.get(f"/api/schedules/{sid}/runs", params={"limit": 2})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["runs"]) == 2
    assert body["has_next"] is True


_DECLARED_RUN_FIELDS = {
    "id",
    "schedule_id",
    "invocation_id",
    "action_kind",
    "status",
    "exit_code",
    "chain_depth",
    "fired_at",
    "ended_at",
    "error_class",
}
# Named one by one rather than left to the allow-list, because each is content-bearing and
# each was called out by name: trigger_context carries whole external event payloads and
# error_detail carries subprocess stderr and exception text.
_CONTENT_BEARING_COLUMNS = ("trigger_context", "error_detail")
# Written by the seeder above and never part of the declared shape.
_OPERATIONAL_COLUMN = "action_args"


def _seeded_schedule_with_one_run(monkeypatch, db_path: Path) -> str:
    _patch_db(monkeypatch, db_path)

    async def seed():
        schedule_id = await _seed_schedule()
        await _seed_run(schedule_id, status="completed", fired_at=time.time())
        return schedule_id

    return asyncio.run(seed())


def _served_runs(schedule_id: str) -> list[dict]:
    """The projected run rows, read from the schedule detail's recent-run slice."""
    return _make_client().get(f"/api/schedules/{schedule_id}").json()["recent_runs"]


def test_the_detail_slice_serves_only_the_declared_run_fields(tmp_path, monkeypatch):
    schedule_id = _seeded_schedule_with_one_run(monkeypatch, tmp_path / "state.db")

    body = _make_client().get(f"/api/schedules/{schedule_id}").json()
    runs = body["recent_runs"]

    assert len(runs) == 1
    assert set(runs[0]) <= _DECLARED_RUN_FIELDS
    assert _OPERATIONAL_COLUMN not in runs[0]
    assert runs[0]["status"] == "completed"


def test_the_seeded_run_really_carries_the_operational_column(tmp_path, monkeypatch):
    """The control. Without this the three assertions above pass on an empty column set."""
    schedule_id = _seeded_schedule_with_one_run(monkeypatch, tmp_path / "state.db")

    async def read():
        async with StateDB() as db:
            return await db.list_schedule_runs(schedule_id, limit=10)

    stored = asyncio.run(read())

    assert len(stored) == 1
    assert _OPERATIONAL_COLUMN in stored[0]
    for column in _CONTENT_BEARING_COLUMNS:
        assert column in stored[0]


def test_a_column_added_to_schedule_runs_is_not_served_until_it_is_named(tmp_path, monkeypatch):
    """The projection is an allow-list, so a column added later is private by default.

    A deny-list naming today's operational columns would pass every test above and serve
    this one, which is the failure mode the allow-list exists to prevent.
    """
    schedule_id = _seeded_schedule_with_one_run(monkeypatch, tmp_path / "state.db")
    real_list = StateDB.list_schedule_runs

    async def list_with_a_new_column(self, *args, **kwargs):
        rows = await real_list(self, *args, **kwargs)
        for row in rows:
            row["a_column_nobody_has_declared"] = "secret-value"
        return rows

    monkeypatch.setattr(StateDB, "list_schedule_runs", list_with_a_new_column)

    body = _make_client().get(f"/api/schedules/{schedule_id}").json()
    runs = body["recent_runs"]

    assert len(runs) == 1
    assert "a_column_nobody_has_declared" not in runs[0]
    assert "secret-value" not in json.dumps(body)
    assert runs[0]["status"] == "completed"


def test_the_detail_slice_never_serves_the_content_bearing_columns(tmp_path, monkeypatch):
    """Named one by one, since an allow-list can be widened without anyone rereading it."""
    schedule_id = _seeded_schedule_with_one_run(monkeypatch, tmp_path / "state.db")

    body = _make_client().get(f"/api/schedules/{schedule_id}").json()
    runs = body["recent_runs"]

    assert len(runs) == 1
    for column in _CONTENT_BEARING_COLUMNS:
        assert column not in runs[0]


def test_a_failed_run_is_served_as_a_classification_not_its_error_text(tmp_path, monkeypatch):
    _patch_db(monkeypatch, tmp_path / "state.db")
    detail = "Traceback (most recent call last):\n  ...\nModuleNotFoundError: No module named 'x'"

    async def seed():
        schedule_id = await _seed_schedule()
        await _seed_run(schedule_id, status="failed", fired_at=time.time(), error_detail=detail)
        return schedule_id

    schedule_id = asyncio.run(seed())
    runs = _served_runs(schedule_id)

    assert runs[0]["error_class"] == "missingDependency"
    assert "No module named" not in json.dumps(runs[0])


def test_an_unrecognised_error_is_served_as_a_class_not_its_last_line(tmp_path, monkeypatch):
    """The arm that leaks: falling back to the traceback's last line ships the exception text."""
    _patch_db(monkeypatch, tmp_path / "state.db")
    detail = "Traceback (most recent call last):\n  ...\nWeirdError: /srv//secret/path exploded"

    async def seed():
        schedule_id = await _seed_schedule()
        await _seed_run(schedule_id, status="failed", fired_at=time.time(), error_detail=detail)
        return schedule_id

    schedule_id = asyncio.run(seed())
    runs = _served_runs(schedule_id)

    assert runs[0]["error_class"] == "unclassified"
    assert "WeirdError" not in json.dumps(runs[0])
    assert "secret" not in json.dumps(runs[0])


def test_a_run_that_did_not_fail_carries_no_classification(tmp_path, monkeypatch):
    schedule_id = _seeded_schedule_with_one_run(monkeypatch, tmp_path / "state.db")

    runs = _served_runs(schedule_id)

    assert runs[0]["error_class"] is None


def test_every_error_class_the_server_serves_has_a_translation():
    """The class name is now the whole payload, so an untranslated one renders as a bare key.

    This replaces a check that the client's own classifier agreed with the server's. There
    is only one classifier now, which removes that drift and creates this one instead.
    """
    import json as _json
    from pathlib import Path as _Path

    from lionagi.studio.services.schedules import _ERROR_CLASS_PATTERNS

    source = _Path("apps/studio/frontend/src/messages/en.json")
    if not source.exists():  # the frontend is not vendored into every checkout
        pytest.skip("frontend source not present")

    translated = _json.loads(source.read_text())["schedules"]["error"]
    served = [key for _, key in _ERROR_CLASS_PATTERNS] + [_UNCLASSIFIED_ERROR]

    assert served, "no server classes enumerated"
    assert not [key for key in served if key not in translated]


# Columns the schedules table carries that no response surface has a reader for. Each is
# either authored content (a spec, a flow document), an executable instruction (a command
# and its arguments), a notification target, or ownership/poll bookkeeping.
_PRIVATE_SCHEDULE_COLUMNS = {
    "action_command": "deploy-prod",
    "action_command_args": ["--token", "tok"],
    "action_extra_args": ["--x"],
    "action_flow_yaml": "steps:\n  - run: deploy",
    "authored_spec": "internal spec text",
    "notify_command": "page",
    "notify_on": ["fail"],
    "owner_key": "owner-abc",
    "github_cursor": "2026-01-01T00:00:00Z",
}


def _seed_rich_schedule(monkeypatch, db_path: Path) -> str:
    """A schedule carrying every private column, plus one failed run carrying raw text."""
    _patch_db(monkeypatch, db_path)

    async def seed():
        schedule_id = await _seed_schedule()
        async with StateDB() as db:
            await db.update_schedule(schedule_id, **_PRIVATE_SCHEDULE_COLUMNS)
        await _seed_run(
            schedule_id,
            status="failed",
            fired_at=time.time(),
            error_detail="PermissionError: /home/someone/.ssh/id_rsa",
        )
        return schedule_id

    return asyncio.run(seed())


def _keys_anywhere(payload) -> set[str]:
    """Every mapping key in the response tree, at any nesting depth.

    Nested is the point: a record can carry a list of runs and a run list can carry a
    record, so a check that only reads the top level passes while the nested rows leak.
    """
    if isinstance(payload, dict):
        found = set(payload)
        for value in payload.values():
            found |= _keys_anywhere(value)
        return found
    if isinstance(payload, list):
        found: set[str] = set()
        for item in payload:
            found |= _keys_anywhere(item)
        return found
    return set()


def _list_surfaces(schedule_id: str) -> tuple[str, ...]:
    return (
        "/api/schedules/",
        f"/api/schedules/{schedule_id}",
        f"/api/schedules/{schedule_id}/runs",
        f"/api/schedules/{schedule_id}/status",
    )


def test_the_seeded_schedule_really_carries_the_private_columns(tmp_path, monkeypatch):
    """Positive control: without this, the sweep below passes on an empty schedule."""
    schedule_id = _seed_rich_schedule(monkeypatch, tmp_path / "state.db")

    from lionagi.studio.services.schedules import get_schedule

    row = asyncio.run(get_schedule(schedule_id))
    unset = sorted(name for name in _PRIVATE_SCHEDULE_COLUMNS if not row.get(name))
    assert not unset, f"seeder failed to set {unset}; the sweep would prove nothing"


def test_no_list_surface_serves_a_private_schedule_column(tmp_path, monkeypatch):
    schedule_id = _seed_rich_schedule(monkeypatch, tmp_path / "state.db")
    client = _make_client()

    for path in _list_surfaces(schedule_id):
        resp = client.get(path)
        assert resp.status_code == 200, path
        leaked = sorted(_keys_anywhere(resp.json()) & set(_PRIVATE_SCHEDULE_COLUMNS))
        assert not leaked, f"{path} serves {leaked}"


def test_no_list_surface_serves_the_content_bearing_run_columns(tmp_path, monkeypatch):
    schedule_id = _seed_rich_schedule(monkeypatch, tmp_path / "state.db")
    client = _make_client()

    for path in _list_surfaces(schedule_id):
        resp = client.get(path)
        assert resp.status_code == 200, path
        leaked = sorted(_keys_anywhere(resp.json()) & set(_CONTENT_BEARING_COLUMNS))
        assert not leaked, f"{path} serves {leaked}"


def test_the_single_run_detail_route_still_serves_the_raw_error_text(tmp_path, monkeypatch):
    """The documented expansion path keeps the raw text, and proves the sweep can see it.

    Both halves matter. The first is the contract: a reader who explicitly opens one run
    gets the traceback. The second is what makes the two sweeps above meaningful -- the
    same recursive key search finds error_detail here, so a clean result there is the
    surface being projected rather than the search being blind.
    """
    _patch_db(monkeypatch, tmp_path / "state.db")

    async def seed():
        schedule_id = await _seed_schedule()
        return await _seed_run(
            schedule_id,
            status="failed",
            fired_at=time.time(),
            error_detail="PermissionError: /home/someone/.ssh/id_rsa",
        )

    run_id = asyncio.run(seed())
    body = _make_client().get(f"/api/schedules/runs/{run_id}").json()

    assert "error_detail" in _keys_anywhere(body)
    assert "id_rsa" in body["error_detail"]


def test_the_schedule_allow_list_serves_everything_the_client_declares():
    """Every field the web client declares must survive the projection.

    Containment rather than equality, because the served set is deliberately wider: the
    CLI reads a remaining-runs counter and a spend rollup that no web view renders. The
    private-by-default half of the contract is the pinned response shape in the daemon
    API gate, which no new column can enter without being named there.
    """
    import re as _re
    from pathlib import Path as _Path

    from lionagi.studio.services.schedules import (
        _SCHEDULE_RECORD_FIELDS,
        _SCHEDULE_SUMMARY_FIELDS,
    )

    source = _Path("apps/studio/frontend/src/lib/types.ts")
    if not source.exists():  # the frontend is not vendored into every checkout
        pytest.skip("frontend source not present")

    text = source.read_text()

    def declared_in(interface: str) -> list[str]:
        block = _re.search(rf"^export interface {interface}[^{{]*\{{(.*?)^\}}", text, _re.S | _re.M)
        assert block, f"{interface} interface not found"
        return _re.findall(r"^\s{2}(\w+)\??:", block.group(1), _re.M)

    summary_declared = declared_in("ScheduleSummary")
    assert summary_declared, "no fields parsed from the client interface"
    assert not [name for name in summary_declared if name not in _SCHEDULE_SUMMARY_FIELDS]

    # The record view serves both sets, so what the client declares only on the detail
    # type has to be reachable there and nowhere narrower.
    record_declared = declared_in("ScheduleDetail")
    served_by_record = set(_SCHEDULE_SUMMARY_FIELDS) | set(_SCHEDULE_RECORD_FIELDS)
    assert not [
        name for name in record_declared if name not in served_by_record and name != "recent_runs"
    ]
    # ...and the split is real: nothing record-only may sit in the list projection.
    assert not set(_SCHEDULE_RECORD_FIELDS) & set(_SCHEDULE_SUMMARY_FIELDS)


# A string that exists nowhere else, so finding it in a response is unambiguous.
_RAW_ERROR_SENTINEL = "SENTINEL-a7f3c2-/home/someone/.ssh/id_rsa-do-not-serve"


def _seed_failed_run_with_sentinel(monkeypatch, db_path: Path) -> tuple[str, str]:
    _patch_db(monkeypatch, db_path)

    async def seed():
        schedule_id = await _seed_schedule()
        run_id = await _seed_run(
            schedule_id,
            status="failed",
            fired_at=time.time(),
            error_detail=(
                f"Traceback (most recent call last):\nPermissionError: {_RAW_ERROR_SENTINEL}"
            ),
        )
        return schedule_id, run_id

    return asyncio.run(seed())


def test_no_list_surface_serves_the_raw_error_text_under_any_name(tmp_path, monkeypatch):
    """Search the response bytes, not its field names.

    A field allow-list is blind by construction to content that is re-emitted under a
    different name -- the run-view reconciler does exactly that, using the raw error text
    as its outcome summary. Only a value search sees that, so this is the check that
    covers derived fields nobody has thought of yet.
    """
    schedule_id, _ = _seed_failed_run_with_sentinel(monkeypatch, tmp_path / "state.db")
    client = _make_client()

    for path in _list_surfaces(schedule_id):
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert _RAW_ERROR_SENTINEL not in resp.text, f"{path} serves the raw error text"


def test_the_run_view_outcome_really_carries_the_raw_text_before_projection(tmp_path, monkeypatch):
    """Positive control for the value sweep: the reconciler does put the text in outcome.

    Without this the sweep above passes whenever the reconciler happens not to fall
    through to the occurrence, which is most of the time.
    """
    schedule_id, _ = _seed_failed_run_with_sentinel(monkeypatch, tmp_path / "state.db")

    from lionagi.studio.services.schedules import list_schedule_run_views

    rows = asyncio.run(list_schedule_run_views(schedule_id))
    assert rows, "no run views built"
    assert _RAW_ERROR_SENTINEL in rows[0]["outcome"]["summary"]


def test_the_projected_outcome_keeps_its_code_and_carries_the_class(tmp_path, monkeypatch):
    """Sanitising the summary must not empty the outcome the CLI renders."""
    schedule_id, _ = _seed_failed_run_with_sentinel(monkeypatch, tmp_path / "state.db")

    body = _make_client().get(f"/api/schedules/{schedule_id}/runs").json()
    outcome = body["runs"][0]["outcome"]

    assert outcome["code"]
    assert outcome["source"] == "occurrence"
    assert outcome["summary"] == "permission"
    assert body["runs"][0]["error_class"] == "permission"


def test_the_single_run_route_still_carries_the_raw_text(tmp_path, monkeypatch):
    """The detail path is the documented reader of the text, and the sweep's control."""
    _, run_id = _seed_failed_run_with_sentinel(monkeypatch, tmp_path / "state.db")

    resp = _make_client().get(f"/api/schedules/runs/{run_id}")

    assert resp.status_code == 200
    assert _RAW_ERROR_SENTINEL in resp.text


# `li schedule list|runs|status` renders these. The CLI is a second consumer of the same
# HTTP surfaces and its reads are invisible from the web client's declared types, so an
# allow-list derived from those types alone drops them and breaks the CLI in silence.
_CLI_SCHEDULE_FIELDS = ("id", "name", "enabled", "trigger_type", "max_runs", "remaining_runs")
_CLI_RUN_LIST_FIELDS = (
    "id",
    "status",
    "fired_at",
    "duration_ms",
    "outcome",
    "invocation_id",
    "artifacts",
)
_CLI_STATUS_RUN_FIELDS = ("outcome", "artifacts", "session_ids", "ended_at", "invocation_id")


def _seed_capped_schedule_with_run(monkeypatch, db_path: Path) -> str:
    _patch_db(monkeypatch, db_path)

    async def seed():
        created = await create_schedule(
            {
                "name": f"cli-fields-{uuid.uuid4().hex[:8]}",
                "trigger_type": "cron",
                "cron_expr": "0 18 * * *",
                "action_kind": "agent",
                "action_prompt": "ping",
                "max_runs": 5,
            }
        )
        await _seed_run(created["id"], status="failed", fired_at=time.time(), error_detail="boom")
        return created["id"]

    return asyncio.run(seed())


def test_the_schedule_list_still_serves_what_the_cli_renders(tmp_path, monkeypatch):
    schedule_id = _seed_capped_schedule_with_run(monkeypatch, tmp_path / "state.db")

    body = _make_client().get("/api/schedules/").json()
    row = next(s for s in body["schedules"] if s["id"] == schedule_id)

    missing = [name for name in _CLI_SCHEDULE_FIELDS if name not in row]
    assert not missing, f"`li schedule list` reads {missing}"


def test_the_run_list_still_serves_what_the_cli_renders(tmp_path, monkeypatch):
    schedule_id = _seed_capped_schedule_with_run(monkeypatch, tmp_path / "state.db")

    body = _make_client().get(f"/api/schedules/{schedule_id}/runs").json()

    missing = [name for name in _CLI_RUN_LIST_FIELDS if name not in body["runs"][0]]
    assert not missing, f"`li schedule runs` reads {missing}"


def test_the_status_view_still_serves_what_the_cli_renders(tmp_path, monkeypatch):
    schedule_id = _seed_capped_schedule_with_run(monkeypatch, tmp_path / "state.db")

    body = _make_client().get(f"/api/schedules/{schedule_id}/status").json()

    missing = [name for name in _CLI_STATUS_RUN_FIELDS if name not in body["latest_run"]]
    assert not missing, f"`li schedule status` reads {missing}"


# The outcome reconciler prefers a session's reason over an invocation's over the
# occurrence's error text. Both preferred branches carry `status_reason_summary`,
# a free-text column, so a check scoped to the occurrence branch covers the case
# that loses and misses the two that win.
def _seed_run_linked_to(
    monkeypatch, db_path: Path, *, invocation, sessions, error_detail: str | None = None
) -> tuple[str, str]:
    from lionagi.studio.services import run_view

    _patch_db(monkeypatch, db_path)

    async def seed():
        schedule_id = await _seed_schedule()
        run_id = await _seed_run(
            schedule_id, status="failed", fired_at=time.time(), error_detail=error_detail
        )
        return schedule_id, run_id

    ids = asyncio.run(seed())

    async def _linked(_db, _run):
        return invocation, list(sessions)

    monkeypatch.setattr(run_view, "_linked", _linked)
    return ids


def _terminal_session_status() -> str:
    from lionagi.state.db import SESSION_TERMINAL_STATUSES

    return sorted(SESSION_TERMINAL_STATUSES)[0]


def _terminal_invocation_status() -> str:
    from lionagi.state.db import INVOCATION_TERMINAL_STATUSES

    return sorted(INVOCATION_TERMINAL_STATUSES)[0]


def test_a_session_reported_summary_really_reaches_the_outcome(tmp_path, monkeypatch):
    """The control. Without it the sweep below passes on an outcome nothing populated."""
    from lionagi.studio.services.run_view import build_outcome

    session = {
        "id": "sess-1",
        "status": _terminal_session_status(),
        "status_reason_summary": f"PermissionError: {_RAW_ERROR_SENTINEL}",
    }
    outcome = build_outcome({"status": "failed"}, None, [session])

    assert outcome["source"] == "session"
    assert _RAW_ERROR_SENTINEL in outcome["summary"]
    assert outcome["summary_reported"] is True


def test_no_list_surface_serves_a_session_reported_summary(tmp_path, monkeypatch):
    session = {
        "id": "sess-1",
        "status": _terminal_session_status(),
        "status_reason_summary": f"PermissionError: {_RAW_ERROR_SENTINEL}",
    }
    schedule_id, _ = _seed_run_linked_to(
        monkeypatch,
        tmp_path / "state.db",
        invocation={"id": "inv-1", "status": _terminal_invocation_status()},
        sessions=[session],
    )
    client = _make_client()

    for path in _list_surfaces(schedule_id):
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert _RAW_ERROR_SENTINEL not in resp.text, f"{path} serves a session-reported summary"


def test_no_list_surface_serves_an_invocation_reported_summary(tmp_path, monkeypatch):
    schedule_id, _ = _seed_run_linked_to(
        monkeypatch,
        tmp_path / "state.db",
        invocation={
            "id": "inv-1",
            "status": _terminal_invocation_status(),
            "status_reason_summary": f"PermissionError: {_RAW_ERROR_SENTINEL}",
        },
        sessions=[],
    )
    client = _make_client()

    for path in _list_surfaces(schedule_id):
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert _RAW_ERROR_SENTINEL not in resp.text, f"{path} serves an invocation-reported summary"


def test_a_generated_summary_is_served_verbatim(tmp_path, monkeypatch):
    """The other half: classifying everything would destroy the useful summaries.

    A summary this module generated carries no caller text, so it stays readable.
    """
    from lionagi.studio.services.run_view import build_outcome
    from lionagi.studio.services.schedules import _run_view

    outcome = build_outcome(
        {"status": "completed", "exit_code": 0}, {"id": "inv-1", "status": "completed"}, []
    )

    assert outcome["summary_reported"] is False
    served = _run_view({"outcome": outcome})
    assert served["outcome"]["summary"] == outcome["summary"]
    assert served["error_class"] is None


def test_no_list_surface_serves_the_record_only_schedule_fields(tmp_path, monkeypatch):
    """The prompt text and the policy objects reach the edit form and nothing else.

    They are named fields rather than private columns, so the key sweep over the
    private set cannot see them; this names them directly.
    """
    from lionagi.studio.services.schedules import _SCHEDULE_RECORD_FIELDS

    # Named here rather than imported: a watch set taken from the constant under test
    # follows that constant, so moving a field out of it moves the field out of the
    # test at the same time and the check reports clean on the change it exists to catch.
    watched = ("action_prompt", "on_success", "on_fail", "action_cwd")
    assert set(watched) == set(_SCHEDULE_RECORD_FIELDS), (
        "record-only set changed; decide per field whether a list surface may serve it, "
        "then update this literal"
    )

    schedule_id = _seed_rich_schedule(monkeypatch, tmp_path / "state.db")
    client = _make_client()

    record = client.get(f"/api/schedules/{schedule_id}").json()
    assert [name for name in watched if name in record] == list(watched), (
        "the record view must still serve them"
    )

    for path in ("/api/schedules/", f"/api/schedules/{schedule_id}/status"):
        resp = client.get(path)
        assert resp.status_code == 200, path
        leaked = sorted(_keys_anywhere(resp.json()) & set(watched))
        assert not leaked, f"{path} serves {leaked}"


def test_the_served_class_describes_the_outcome_the_surface_serves(tmp_path, monkeypatch):
    """One failure, one classification. The occurrence row loses to the session.

    error_class and outcome.summary answer the same question on the same row, so a
    class read off a layer the outcome did not come from contradicts the summary
    printed beside it.
    """
    schedule_id, _ = _seed_run_linked_to(
        monkeypatch,
        tmp_path / "state.db",
        invocation={"id": "inv-1", "status": _terminal_invocation_status()},
        sessions=[
            {
                "id": "sess-1",
                "status": _terminal_session_status(),
                "status_reason_summary": f"PermissionError: {_RAW_ERROR_SENTINEL}",
            }
        ],
        error_detail="ConnectionError: connection refused",
    )
    client = _make_client()

    # The contradiction needs both layers populated and classifying differently.
    from lionagi.studio.services.schedules import _error_class

    assert _error_class("ConnectionError: connection refused") == "network"
    assert _error_class(f"PermissionError: {_RAW_ERROR_SENTINEL}") == "permission"

    for path in (f"/api/schedules/{schedule_id}/runs", f"/api/schedules/{schedule_id}/status"):
        body = client.get(path).json()
        row = body["runs"][0] if "runs" in body else body["latest_run"]
        assert row["outcome"]["source"] == "session", path
        assert row["error_class"] == row["outcome"]["summary"] == "permission", path

    # Same rule for the run slice nested inside the record.
    nested = _served_runs(schedule_id)[0]
    assert nested["error_class"] == "permission"


def test_a_failure_whose_winning_outcome_names_no_error_still_serves_a_class(tmp_path, monkeypatch):
    """A run that failed without reporting why still has to reach its error detail.

    The winning layer here is a session that failed and filled in no reason, so the
    summary beside it is a status word this module wrote. There is no caller
    classification for the occurrence text to contradict, and the occurrence still
    holds that text, so it is what the class describes. Suppressing it instead takes
    the detail-expansion path down for exactly the runs that need it.
    """
    schedule_id, _ = _seed_run_linked_to(
        monkeypatch,
        tmp_path / "state.db",
        invocation={"id": "inv-1", "status": _terminal_invocation_status()},
        sessions=[{"id": "sess-1", "status": _terminal_session_status()}],
        error_detail="ConnectionError: connection refused",
    )
    client = _make_client()

    for path in (f"/api/schedules/{schedule_id}/runs", f"/api/schedules/{schedule_id}/status"):
        body = client.get(path).json()
        row = body["runs"][0] if "runs" in body else body["latest_run"]
        assert row["outcome"]["source"] == "session", path
        assert row["outcome"]["summary_reported"] is False, path
        assert row["error_class"] == "network", path

    assert _served_runs(schedule_id)[0]["error_class"] == "network"


def test_the_record_route_serves_the_persisted_execution_root(tmp_path, monkeypatch):
    """`li schedule create --machine` reads this route back for exactly this field.

    Its own docstring says the caller is shown the execution root that was actually
    persisted, which the command resolves from its environment when the caller named
    neither a cwd nor a project. Echoing the request back would not answer that.
    """
    import re as _re
    from pathlib import Path as _Path

    schedule_id = _seed_rich_schedule(monkeypatch, tmp_path / "state.db")
    record = _make_client().get(f"/api/schedules/{schedule_id}").json()
    assert "action_cwd" in record

    source = _Path("lionagi/cli/machine_schedule.py").read_text()
    create = _re.search(r"^def _create\(argv.*?^def ", source, _re.S | _re.M)
    assert create, "_create not found"
    assert '_studio(f"/{_quote(schedule_id)}")' in create.group(0), (
        "the read-back moved; re-derive which route this test is about"
    )


# The run-side counterpart to the private-schedule-column sweep above. The run join
# carries more than the view names, and the routes that serve a view returned the joined
# row verbatim before the view existed, so the allow-list is the only thing standing
# between these columns and the wire.
_PRIVATE_RUN_COLUMNS = ("action_args", "resume_packet")


def _seed_run_with_private_columns(monkeypatch, db_path: Path) -> str:
    _patch_db(monkeypatch, db_path)

    async def seed():
        schedule_id = await _seed_schedule()
        run_id = await _seed_run(schedule_id, status="completed", fired_at=time.time())
        async with StateDB() as db:
            await db.update_schedule_run(run_id, resume_packet={"cursor": "/private/host/state"})
        return schedule_id

    return asyncio.run(seed())


def test_the_seeded_run_really_carries_the_private_run_columns(tmp_path, monkeypatch):
    """Positive control: without this the sweep below passes on a run that never had
    the columns, which is the shape a broken seeder fails in."""
    schedule_id = _seed_run_with_private_columns(monkeypatch, tmp_path / "state.db")

    from lionagi.studio.services.schedules import list_schedule_run_views

    rows = asyncio.run(list_schedule_run_views(schedule_id))
    assert len(rows) == 1
    unset = sorted(name for name in _PRIVATE_RUN_COLUMNS if not rows[0].get(name))
    assert not unset, f"the join did not carry {unset}; the sweep would prove nothing"


def test_no_list_surface_serves_a_private_run_column(tmp_path, monkeypatch):
    schedule_id = _seed_run_with_private_columns(monkeypatch, tmp_path / "state.db")
    client = _make_client()

    for path in _list_surfaces(schedule_id):
        resp = client.get(path)
        assert resp.status_code == 200, path
        leaked = sorted(_keys_anywhere(resp.json()) & set(_PRIVATE_RUN_COLUMNS))
        assert not leaked, f"{path} serves {leaked}"


# The single-run route is the one surface that was never projected: it returned the
# joined row, and its chain_children the raw child rows, so every private run column
# reached the wire on a route the API answers without a token.
def _seed_chained_run_with_private_columns(monkeypatch, db_path: Path) -> tuple[str, str]:
    _patch_db(monkeypatch, db_path)

    async def seed():
        schedule_id = await _seed_schedule()
        fired = time.time()
        parent_id = await _seed_run(schedule_id, status="completed", fired_at=fired)
        child_id = await _seed_run(
            schedule_id,
            status="completed",
            fired_at=fired + 1,
            chain_depth=1,
            chain_parent_id=parent_id,
        )
        async with StateDB() as db:
            for rid in (parent_id, child_id):
                await db.update_schedule_run(rid, resume_packet={"cursor": "/private/host/state"})
        return parent_id, child_id

    return asyncio.run(seed())


def test_the_record_path_really_carries_the_private_run_columns(tmp_path, monkeypatch):
    """Positive control for the record sweep, and it has to be the record path's own
    read: the list control above proves nothing about the row this route builds, which
    comes from a different service function and adds the child rows."""
    parent_id, _ = _seed_chained_run_with_private_columns(monkeypatch, tmp_path / "state.db")

    from lionagi.studio.services.schedules import get_schedule_run

    row = asyncio.run(get_schedule_run(parent_id))
    unset = sorted(name for name in _PRIVATE_RUN_COLUMNS if not row.get(name))
    assert not unset, f"the record read did not carry {unset}; the sweep would prove nothing"
    assert row["chain_children"], "no child row; the nested half of the sweep would prove nothing"
    child_unset = sorted(
        name for name in _PRIVATE_RUN_COLUMNS if not row["chain_children"][0][name]
    )
    assert not child_unset, f"the child row did not carry {child_unset}"


def test_the_single_run_record_serves_no_private_run_column(tmp_path, monkeypatch):
    parent_id, child_id = _seed_chained_run_with_private_columns(monkeypatch, tmp_path / "state.db")
    client = _make_client()

    for run_id in (parent_id, child_id):
        body = client.get(f"/api/schedules/runs/{run_id}").json()
        leaked = sorted(_keys_anywhere(body) & set(_PRIVATE_RUN_COLUMNS))
        assert not leaked, f"the record for {run_id} serves {leaked}"


def test_the_single_run_record_serves_no_trigger_payload(tmp_path, monkeypatch):
    """trigger_context carries whole external event payloads and no client reads it.
    Named separately from the sweep because it is the one field this route used to serve
    that the web client still declared, so dropping it is a client-visible decision."""
    parent_id, _ = _seed_chained_run_with_private_columns(monkeypatch, tmp_path / "state.db")

    body = _make_client().get(f"/api/schedules/runs/{parent_id}").json()

    assert "trigger_context" not in _keys_anywhere(body)


def test_the_run_allow_list_serves_everything_the_client_declares():
    """Every field the web client declares on a run must survive the record projection.

    Containment rather than equality: the served set is wider, because the CLI renders a
    duration and artifact paths that no web view reads.
    """
    import re as _re
    from pathlib import Path as _Path

    from lionagi.studio.services.schedules import _RUN_RECORD_FIELDS

    source = _Path("apps/studio/frontend/src/lib/types.ts")
    if not source.exists():  # the frontend is not vendored into every checkout
        pytest.skip("frontend source not present")

    block = _re.search(
        r"^export interface ScheduleRunSummary[^{]*\{(.*?)^\}", source.read_text(), _re.S | _re.M
    )
    assert block, "ScheduleRunSummary interface not found"
    declared = _re.findall(r"^\s{2}(\w+)\??:", block.group(1), _re.M)
    assert declared, "no fields parsed from the client interface"

    # outcome and error_class are computed by the projection rather than named in it.
    served = set(_RUN_RECORD_FIELDS) | {"outcome", "error_class"}
    assert not [name for name in declared if name not in served]


def test_the_single_run_record_keeps_a_session_reported_summary(tmp_path, monkeypatch):
    """The record route is the one place raw failure text is reachable, and when a
    session is the winning layer the summary IS that text: the occurrence carries no
    error_detail of its own, so classifying the summary here would leave this route with
    nothing to show for exactly those runs. The list surface for the same run still
    classifies it, which is what makes the split real rather than a leak."""
    session = {
        "id": "sess-1",
        "status": _terminal_session_status(),
        "status_reason_summary": f"PermissionError: {_RAW_ERROR_SENTINEL}",
    }
    schedule_id, run_id = _seed_run_linked_to(
        monkeypatch,
        tmp_path / "state.db",
        invocation={"id": "inv-1", "status": _terminal_invocation_status()},
        sessions=[session],
    )
    client = _make_client()

    body = client.get(f"/api/schedules/runs/{run_id}").json()
    assert body["outcome"]["source"] == "session"
    assert _RAW_ERROR_SENTINEL in body["outcome"]["summary"]
    assert body.get("error_detail") is None, "the occurrence has no text of its own here"

    listed = client.get(f"/api/schedules/{schedule_id}/runs")
    assert _RAW_ERROR_SENTINEL not in listed.text, "the list surface still classifies it"
