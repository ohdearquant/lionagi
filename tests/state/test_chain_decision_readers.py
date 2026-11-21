# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Readers retain their evidence contracts when a chain decision is present."""

from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from lionagi.state import db as db_mod
from lionagi.state.db import StateDB
from lionhub.orchestration.inspection import resolve_any_target
from lionhub.serve.cli import monitor, status

pytestmark = pytest.mark.asyncio(loop_scope="function")

_FAIL_DECISION = {"kind": "chain_decision", "v": 1, "ref": "on_fail"}
_NO_DECISION = {"kind": "chain_decision", "v": 1, "ref": "none"}
_FAILURE = {"kind": "failed_operation", "id": "failed-node", "label": "Failure record"}


@asynccontextmanager
async def _isolated_postgres_store(pg_url, monkeypatch):
    """Connect every reader and scheduler handle to a private database."""
    url = make_url(pg_url)
    name = f"chain_test_{uuid.uuid4().hex[:16]}"
    admin = create_async_engine(url, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            identifier = conn.dialect.identifier_preparer.quote_identifier(name)
            await conn.exec_driver_sql(f"CREATE DATABASE {identifier}")
            try:
                store_url = url.set(database=name).render_as_string(hide_password=False)
                with monkeypatch.context() as patch:
                    patch.setattr(
                        db_mod,
                        "settings",
                        db_mod.settings.model_copy(update={"LIONAGI_STATE_DB_URL": store_url}),
                    )
                    async with StateDB(url=store_url) as state:
                        yield state
            finally:
                await conn.exec_driver_sql(f"DROP DATABASE {identifier}")
    finally:
        await admin.dispose()


@pytest_asyncio.fixture(loop_scope="function")
async def db(request, tmp_path, monkeypatch):
    if getattr(request, "param", "sqlite") == "postgresql":
        async with _isolated_postgres_store(
            request.getfixturevalue("pg_url"), monkeypatch
        ) as state:
            yield state
        return

    path = tmp_path / "state.db"
    monkeypatch.setattr(db_mod, "DEFAULT_DB_PATH", path)
    monkeypatch.setattr(
        db_mod,
        "settings",
        db_mod.settings.model_copy(update={"LIONAGI_STATE_DB_URL": None}),
    )
    async with StateDB(path) as state:
        yield state


async def _put_refs(db, table, entity_id, refs):
    # Synthetic reader input, including shapes that ordinary writers never produce.
    await db.execute(
        f"UPDATE {table} SET status_evidence_refs = :refs WHERE id = :id",  # noqa: S608
        {"refs": json.dumps(refs), "id": entity_id},
    )


async def _seed_session_and_invocation(db):
    await db.create_invocation(
        {"id": "invocation", "skill": "agent", "status": "completed", "started_at": 100.0}
    )
    await db.create_progression("session-progression")
    await db.create_session(
        {
            "id": "session",
            "progression_id": "session-progression",
            "invocation_id": "invocation",
            "name": "Reader example",
            "status": "completed",
            "source_kind": "live",
        }
    )
    await _put_refs(db, "sessions", "session", [_FAILURE, _FAIL_DECISION])
    await _put_refs(db, "invocations", "invocation", [_FAIL_DECISION])


async def _seed_schedule_run(db, *, run_id="occurrence", on_success=None):
    await db.create_schedule(
        {
            "id": "schedule",
            "name": "nightly",
            "trigger_type": "interval",
            "interval_sec": 60,
            "action_kind": "agent",
            "on_success": on_success,
        }
    )
    await db.create_schedule_run(
        {
            "id": run_id,
            "schedule_id": "schedule",
            "trigger_context": {},
            "action_kind": "agent",
            "action_args": [],
            "status": "completed",
            "fired_at": 100.0,
            "exit_code": 0,
        }
    )


async def test_cli_session_detail_and_status_render_evidence(db):
    await _seed_session_and_invocation(db)
    await _seed_schedule_run(db)
    await _put_refs(db, "schedule_runs", "occurrence", [_FAIL_DECISION])
    session = await db.get_session("session")

    detail = await monitor._detail_session(db, session)
    assert "    Failure record" in detail.splitlines()
    assert "    chain_decision" in detail.splitlines()

    view = await status._build_view(db, command="status", entity_type="session", row=session)
    assert view["status_evidence_refs"] == [_FAILURE, _FAIL_DECISION]
    text = status._render_human(view)
    assert "    Failure record: failed-node" in text.splitlines()
    assert "    chain_decision: on_fail" in text.splitlines()
    assert await resolve_any_target(db, "occurrence") is None


async def test_monitor_wait_reads_decision_from_the_terminal_row(db):
    await _seed_schedule_run(db, on_success={"kind": "agent"})
    await _put_refs(db, "schedule_runs", "occurrence", [_NO_DECISION])
    pending = {"occurrence": await db.get_schedule_run("occurrence")}
    chain = monitor._new_chain_state(pending, chain=True)
    done = []

    await monitor._poll_pending_once(db, pending, {}, done)
    await monitor._advance_chains(db, pending, done, chain_state=chain, processed=0)

    assert done[0]["status_evidence_refs"] == [_NO_DECISION]
    assert chain["resolved_roots"] == {"occurrence"}
    assert chain["awaiting_grace"] == {}


@pytest.mark.parametrize("db", ["sqlite", "postgresql"], indirect=True)
async def test_latency_excludes_only_invocations_with_a_reaper_ref(db):
    for invocation_id, elapsed in (("finished", 30.0), ("reaped", 7200.0)):
        await db.create_invocation(
            {
                "id": invocation_id,
                "skill": "agent",
                "status": "timed_out",
                "started_at": 100.0,
                "ended_at": 100.0 + elapsed,
            }
        )
        await _put_refs(db, "invocations", invocation_id, [_FAIL_DECISION])

    assert await db.metric_value("p95_latency_ms", window_start=0.0) == 7_200_000.0
    await _put_refs(
        db, "invocations", "reaped", [_FAIL_DECISION, {"kind": "invocation", "id": "reaped"}]
    )
    assert await db.metric_value("p95_latency_ms", window_start=0.0) == 30_000.0


async def test_schedule_history_keeps_definition_evidence_and_omits_occurrences(db):
    await _seed_schedule_run(db, run_id="schedule")
    assert await db.update_status(
        "schedule_run",
        "schedule",
        new_status="completed",
        reason_code="run.completed.ok",
    )
    for entity_type, ref in (("schedule", _NO_DECISION), ("schedule_run", _FAIL_DECISION)):
        await db.execute(
            "UPDATE status_transitions SET evidence_refs = :refs "
            "WHERE entity_type = :type AND entity_id = 'schedule'",
            {"refs": json.dumps([ref]), "type": entity_type},
        )
    occurrence = await db.fetch_one(
        "SELECT evidence_refs FROM status_transitions WHERE entity_type = 'schedule_run' "
        "AND entity_id = 'schedule'"
    )
    assert occurrence is not None

    history = await db.get_schedule_history("schedule")

    assert history["available"] is True
    [event] = history["events"]
    assert event["entity_type"] == "schedule"
    assert event["evidence_refs"] == [_NO_DECISION]


@pytest.mark.parametrize("db", ["sqlite", "postgresql"], indirect=True)
async def test_skipped_occurrence_keeps_evidence_in_process_and_reason_on_the_wire(db):
    pytest.importorskip("fastapi", reason="studio extra not installed")
    from lionhub.serve.daemon.services import run_view, schedules
    from tests.studio.test_schedule_skip_reason_projection import (
        _CAPACITY_REASON,
        _CAPACITY_SUMMARY,
        _assert_reason,
        _seed_skipped_run,
    )

    await _seed_skipped_run(_CAPACITY_REASON, _CAPACITY_SUMMARY)
    await _put_refs(db, "schedule_runs", "deferred", [_NO_DECISION])
    await db.execute(
        "UPDATE status_transitions SET evidence_refs = :refs WHERE id = "
        "(SELECT id FROM status_transitions WHERE entity_type = 'schedule_run' "
        "AND entity_id = 'deferred' ORDER BY created_at DESC, id DESC LIMIT 1)",
        {"refs": json.dumps([_NO_DECISION])},
    )

    row = await db.get_schedule_run("deferred")
    detail = await run_view.get_run_view(db, "deferred")
    [listed] = await run_view.list_run_views(db, "nightly")
    for view in (row, detail, listed):
        assert view["status_evidence_refs"] == [_NO_DECISION]
    for view in (detail, listed):
        _assert_reason(view, _CAPACITY_REASON, _CAPACITY_SUMMARY)
    wire_detail = await schedules.get_schedule_run_route("deferred")
    wire_list = await schedules.list_schedule_runs_route("nightly", status=None, limit=20, offset=0)
    for view in (wire_detail, wire_list["runs"][0]):
        assert "status_evidence_refs" not in view
        _assert_reason(view, _CAPACITY_REASON, _CAPACITY_SUMMARY)


async def test_operator_failure_progress_ignores_decisions_even_with_an_id(db):
    pytest.importorskip("fastapi", reason="studio extra not installed")
    from lionagi_studio.operator.run_progress import _evidence_failed_op_ids

    await _seed_session_and_invocation(db)
    await _put_refs(
        db,
        "sessions",
        "session",
        [_FAILURE, _FAIL_DECISION, {**_NO_DECISION, "id": "unrelated-node"}],
    )

    assert _evidence_failed_op_ids(await db.get_session("session")) == {"failed-node"}


async def test_studio_details_pass_evidence_through_before_operator_projection(db):
    pytest.importorskip("fastapi", reason="studio extra not installed")
    from lionagi_studio.operator import application_mcp
    from lionhub.serve.daemon.services import invocations, runs, sessions

    await _seed_session_and_invocation(db)
    session = await sessions.get_session("session")
    run = await runs.get_run("session")
    invocation = await invocations.get_invocation("invocation")
    [listed_invocation] = await invocations.list_invocations()
    for item in (session, run):
        assert item["status_evidence_refs"] == [_FAILURE, _FAIL_DECISION]
    for item in (invocation, listed_invocation):
        assert item["status_evidence_refs"] == [_FAIL_DECISION]

    operator_session = await application_mcp.session_detail({"session_id": "session"})
    operator_invocation = await application_mcp.get_invocation({"invocation_id": "invocation"})
    for key, item in (("session", operator_session), ("invocation", operator_invocation)):
        assert item["known"] is True
        assert item["id"] == key
        assert "status_evidence_refs" not in item
