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


def test_schedule_summary_batches_recent_runs_with_stable_order(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    now = time.time()

    async def seed():
        first = await _seed_schedule()
        second = await _seed_schedule()
        await _seed_run(first, status="failed", fired_at=now - 10, run_id="run-a")
        await _seed_run(first, status="completed", fired_at=now, run_id="run-b")
        await _seed_run(first, status="failed", fired_at=now, run_id="run-c")
        await _seed_run(second, status="completed", fired_at=now, run_id="run-d")
        return first, second

    first, second = asyncio.run(seed())
    response = _make_client().get("/api/schedules/summary", params={"recent_runs_limit": 2})

    assert response.status_code == 200
    body = response.json()
    assert body["summary_version"] == 1
    assert body["recent_runs_limit"] == 2
    assert {schedule["id"] for schedule in body["schedules"]} == {first, second}
    assert body["run_summaries"][first]["state"] == "ok"
    first_runs = body["run_summaries"][first]["runs"]
    assert [run["id"] for run in first_runs] == ["run-c", "run-b"]
    assert [run["fired_at"] for run in first_runs] == pytest.approx([now, now])
    assert body["run_summaries"][second]["state"] == "ok"
    assert [run["id"] for run in body["run_summaries"][second]["runs"]] == ["run-d"]


def test_schedule_summary_marks_each_slice_failed_when_batch_read_fails(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)

    async def seed():
        return await _seed_schedule(), await _seed_schedule()

    first, second = asyncio.run(seed())

    async def fail_batch(*args, **kwargs):
        raise RuntimeError("summary read failed")

    monkeypatch.setattr(StateDB, "list_schedule_runs_batch", fail_batch, raising=False)

    response = _make_client().get("/api/schedules/summary")

    assert response.status_code == 200
    summaries = response.json()["run_summaries"]
    assert summaries[first] == {"state": "error", "runs": []}
    assert summaries[second] == {"state": "error", "runs": []}


# Every surface below serves the same declared run shape, so they are checked together:
# a projection applied to one emitter and not its siblings is the defect it was meant to fix.
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


def test_the_summary_slice_serves_only_the_declared_run_fields(tmp_path, monkeypatch):
    schedule_id = _seeded_schedule_with_one_run(monkeypatch, tmp_path / "state.db")

    body = _make_client().get("/api/schedules/summary").json()
    runs = body["run_summaries"][schedule_id]["runs"]

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
    real_batch = StateDB.list_schedule_runs_batch

    async def batch_with_a_new_column(self, ids, **kwargs):
        grouped = await real_batch(self, ids, **kwargs)
        for rows in grouped.values():
            for row in rows:
                row["a_column_nobody_has_declared"] = "secret-value"
        return grouped

    monkeypatch.setattr(StateDB, "list_schedule_runs_batch", batch_with_a_new_column)

    body = _make_client().get("/api/schedules/summary").json()
    runs = body["run_summaries"][schedule_id]["runs"]

    assert len(runs) == 1
    assert "a_column_nobody_has_declared" not in runs[0]
    assert "secret-value" not in json.dumps(body)
    assert runs[0]["status"] == "completed"


def test_the_summary_slice_never_serves_the_content_bearing_columns(tmp_path, monkeypatch):
    """Named one by one, since an allow-list can be widened without anyone rereading it."""
    schedule_id = _seeded_schedule_with_one_run(monkeypatch, tmp_path / "state.db")

    body = _make_client().get("/api/schedules/summary").json()
    runs = body["run_summaries"][schedule_id]["runs"]

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
    runs = _make_client().get("/api/schedules/summary").json()["run_summaries"][schedule_id]["runs"]

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
    runs = _make_client().get("/api/schedules/summary").json()["run_summaries"][schedule_id]["runs"]

    assert runs[0]["error_class"] == "unclassified"
    assert "WeirdError" not in json.dumps(runs[0])
    assert "secret" not in json.dumps(runs[0])


def test_a_run_that_did_not_fail_carries_no_classification(tmp_path, monkeypatch):
    schedule_id = _seeded_schedule_with_one_run(monkeypatch, tmp_path / "state.db")

    runs = _make_client().get("/api/schedules/summary").json()["run_summaries"][schedule_id]["runs"]

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
        "/api/schedules/summary",
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

    from lionagi.studio.services.schedules import _SCHEDULE_SUMMARY_FIELDS

    source = _Path("apps/studio/frontend/src/lib/types.ts")
    if not source.exists():  # the frontend is not vendored into every checkout
        pytest.skip("frontend source not present")

    block = _re.search(
        r"^export interface ScheduleSummary \{(.*?)^\}", source.read_text(), _re.S | _re.M
    )
    assert block, "ScheduleSummary interface not found"
    declared = _re.findall(r"^\s{2}(\w+)\??:", block.group(1), _re.M)

    assert declared, "no fields parsed from the client interface"
    assert not [name for name in declared if name not in _SCHEDULE_SUMMARY_FIELDS]


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
