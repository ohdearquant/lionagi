# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Compatibility-mode signal and teardown operations (ADR-0128 Appendix A.2).

The store is the synthetic managed store of ``test_compatibility_owner_ops``: lionagi's native
schema with an owner column on the tables that carry one, a principals table, and another
lineage's version stamp.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import event

from lionagi.state.compat import StateCompatibilityRefused
from lionagi.state.db import StateDB, TransitionRejectedError
from lionagi.state.reasons import RunReasons, ShowReasons
from tests.state.test_compatibility_owner_ops import (
    OWNER,
    _managed_store,
    _profile,
    _rows,
    _setup_run,
)


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> Path:
    return await _managed_store(tmp_path / "managed.db")


@pytest_asyncio.fixture
async def db(store: Path):
    handle = StateDB(store, compatibility=_profile(store), owner="principal:a")
    await handle.open()
    try:
        yield handle
    finally:
        await handle.close()


def _dump(store: Path) -> list[str]:
    with closing(sqlite3.connect(store)) as conn:
        return list(conn.iterdump())


def _execute(store: Path, sql: str, params: tuple = ()) -> None:
    with closing(sqlite3.connect(store)) as conn:
        conn.execute(sql, params)
        conn.commit()


def _node_metadata(store: Path, session_id: str = "s1") -> dict:
    ((value,),) = _rows(store, "SELECT node_metadata FROM sessions WHERE id = ?", (session_id,))
    return json.loads(value)


async def _update_session(db: StateDB, session_id: str) -> None:
    await db.update_session(session_id, node_metadata={"run": 1})


async def _update_artifact_verification(db: StateDB, session_id: str) -> None:
    await db.update_artifact_verification(session_id, {"ok": True})


async def _merge_session_node_metadata(db: StateDB, session_id: str) -> None:
    await db.merge_session_node_metadata(session_id, {"linked": "engine-1"})


async def _update_status(db: StateDB, session_id: str) -> None:
    await db.update_status(
        "session",
        session_id,
        new_status="completed",
        reason_code=RunReasons.COMPLETED_OK,
        actor=session_id,
        expected_statuses={"running"},
    )


async def _insert_session_signal(db: StateDB, session_id: str) -> None:
    await db.insert_session_signal(session_id=session_id, kind="SessionEnd", ts=1.0, payload={})


_OPS = {
    "update_session": _update_session,
    "update_artifact_verification": _update_artifact_verification,
    "merge_session_node_metadata": _merge_session_node_metadata,
    "update_status": _update_status,
    "insert_session_signal": _insert_session_signal,
}


async def test_a_runs_teardown_writes_reach_its_session_with_its_owner(db, store):
    await _setup_run(db)
    await db.update_session("s1", first_msg_id="m-sys", last_msg_id="m-sys")
    for op in _OPS.values():
        await op(db, "s1")

    assert _rows(
        store,
        "SELECT status, first_msg_id, last_msg_id, artifact_verification_json FROM sessions",
    ) == [("completed", "m-sys", "m-sys", '{"ok": true}')]
    assert {key: _node_metadata(store)[key] for key in ("run", "linked")} == {
        "run": 1,
        "linked": "engine-1",
    }
    assert _rows(
        store,
        f"SELECT previous_status, status, {OWNER} FROM status_transitions "
        "WHERE entity_id = 's1' ORDER BY created_at",
    ) == [(None, "running", "principal:a"), ("running", "completed", "principal:a")]
    assert _rows(store, f"SELECT session_id, seq, kind, {OWNER} FROM session_signals") == [
        ("s1", 1, "SessionEnd", "principal:a")
    ]


async def test_node_metadata_is_merged_by_key_inside_the_write(db, store):
    """Keys another writer stored are kept; ``node_metadata`` is never written whole (A.2)."""
    await _setup_run(db)
    _execute(store, """UPDATE sessions SET node_metadata = '{"managed": "kept"}' WHERE id = 's1'""")

    await db.update_session("s1", node_metadata={"run": 1})
    _execute(
        store,
        "UPDATE sessions SET node_metadata = json_set(node_metadata, '$.later', 2) WHERE id = 's1'",
    )
    await db.update_session("s1", node_metadata=json.dumps({"run": 3}))

    metadata = _node_metadata(store)
    assert {key: metadata[key] for key in ("managed", "later", "run")} == {
        "managed": "kept",
        "later": 2,
        "run": 3,
    }


@pytest.mark.parametrize(
    ("stored", "given", "which"),
    [
        ("[1]", {"run": 1}, "stored"),
        ("not json", {"run": 1}, "stored"),
        (None, None, "given"),
        (None, ["run"], "given"),
        (None, "not json", "given"),
    ],
)
async def test_node_metadata_that_is_not_an_object_is_refused(db, store, stored, given, which):
    await _setup_run(db)
    if stored is not None:
        _execute(store, "UPDATE sessions SET node_metadata = ? WHERE id = 's1'", (stored,))
    before = _dump(store)

    with pytest.raises(StateCompatibilityRefused, match=f"the {which} node_metadata"):
        await db.update_session("s1", first_msg_id="m-sys", node_metadata=given)

    assert _dump(store) == before


@pytest.mark.parametrize("op", sorted(_OPS))
async def test_a_teardown_op_refuses_a_session_its_run_did_not_create(db, store, op):
    await _setup_run(db)
    before = _dump(store)
    other = StateDB(store, compatibility=_profile(store), owner="principal:a")
    await other.open()
    try:
        with pytest.raises(StateCompatibilityRefused, match="was not created by this run"):
            await _OPS[op](other, "s1")
    finally:
        await other.close()

    assert _dump(store) == before


@pytest.mark.parametrize("link", ["first_msg_id", "last_msg_id", "progression_id"])
async def test_a_session_update_links_only_rows_its_run_created(db, store, link):
    await _setup_run(db)
    _execute(
        store,
        f"INSERT INTO progressions (id, created_at, collection, {OWNER}) "
        "VALUES ('foreign', 1.0, '[]', 'principal:a')",
    )
    before = _dump(store)

    with pytest.raises(StateCompatibilityRefused, match="was not created by this run"):
        await db.update_session("s1", **{link: "foreign"})

    assert _dump(store) == before


@pytest.mark.parametrize("op", sorted(_OPS))
async def test_a_teardown_op_refuses_a_session_that_no_longer_carries_the_owner(db, store, op):
    await _setup_run(db)
    _execute(store, f"UPDATE sessions SET {OWNER} = 'principal:b' WHERE id = 's1'")
    before = _dump(store)

    with pytest.raises(StateCompatibilityRefused, match="does not belong to 'principal:a'"):
        await _OPS[op](db, "s1")

    assert _dump(store) == before


async def test_every_row_a_teardown_op_writes_carries_the_owner(db, store):
    """Beside the check before them, the statements themselves carry the owner (D5)."""
    await _setup_run(db)
    writes = []

    def spy(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith(("UPDATE", "INSERT")):
            writes.append((statement, parameters))

    event.listen(db._engine.sync_engine, "before_cursor_execute", spy)
    try:
        for op in _OPS.values():
            await op(db, "s1")
    finally:
        event.remove(db._engine.sync_engine, "before_cursor_execute", spy)

    assert sorted(" ".join(statement.split()[:3]) for statement, _ in writes) == [
        "INSERT INTO session_signals",
        "INSERT INTO status_transitions",
        "UPDATE sessions SET",
        "UPDATE sessions SET",
        "UPDATE sessions SET",
        "UPDATE sessions SET",
    ]
    for statement, parameters in writes:
        values = parameters.values() if isinstance(parameters, dict) else parameters
        assert f'"{OWNER}"' in statement, statement
        assert "principal:a" in values, statement


async def test_a_session_moves_only_by_its_own_run_and_never_by_override(db, store):
    await _setup_run(db)
    before = _dump(store)

    with pytest.raises(StateCompatibilityRefused, match="override transition is refused"):
        await db.update_status(
            "session",
            "s1",
            new_status="completed",
            reason_code=RunReasons.COMPLETED_OK,
            actor="s1",
            override=True,
            override_actor="operator",
            override_justification="repair",
        )
    with pytest.raises(StateCompatibilityRefused, match="only its run's session, not a show"):
        await db.update_status(
            "show",
            "show-1",
            new_status="completed",
            reason_code=ShowReasons.COMPLETED_FINAL_GATE,
            actor="s1",
        )

    assert _dump(store) == before


async def test_a_rejected_transition_records_its_admin_event_with_the_owner(db, store):
    await _setup_run(db)
    await _update_status(db, "s1")

    with pytest.raises(TransitionRejectedError):
        await db.update_status(
            "session",
            "s1",
            new_status="failed",
            reason_code=RunReasons.FAILED_EXCEPTION,
            actor="s1",
        )

    assert _rows(store, "SELECT status FROM sessions") == [("completed",)]
    assert _rows(store, f"SELECT action, target_id, {OWNER} FROM admin_events") == [
        ("status_transition_rejected", "s1", "principal:a")
    ]


async def test_a_status_and_its_transition_record_land_together_or_not_at_all(
    db, store, monkeypatch
):
    """A failed transition record rolls the status back with it (A.3)."""
    from lionagi.state.lifecycle import service

    await _setup_run(db)
    owner_insert = service.SQLAlchemyLifecycleService._owner_insert

    def broken(self, table):
        if table != "status_transitions":
            return owner_insert(self, table)
        return ', "no_such_column"', ", :_owner0", {"_owner0": "principal:a"}

    monkeypatch.setattr(service.SQLAlchemyLifecycleService, "_owner_insert", broken)
    before = _dump(store)

    with pytest.raises(Exception, match="no_such_column"):
        await _update_status(db, "s1")

    assert _dump(store) == before


async def test_teardown_hands_a_compatibility_store_only_its_own_metadata_keys(db, store):
    """A key another writer changes between teardown's read and its write keeps its value (A.2)."""
    from lionagi.cli import _runs

    await _setup_run(db)
    _execute(store, """UPDATE sessions SET node_metadata = '{"managed": "old"}' WHERE id = 's1'""")
    update_session = db.update_session

    async def after_another_writer(session_id, **fields):
        _execute(
            store,
            "UPDATE sessions SET node_metadata = json_set(node_metadata, '$.managed', 'new') "
            "WHERE id = ?",
            (session_id,),
        )
        await update_session(session_id, **fields)

    db.update_session = after_another_writer
    status = await _runs._teardown_common(
        db,
        session_id="s1",
        session_prog_id="sp",
        status="completed",
        exception=None,
        artifacts_path=None,
        artifact_contract=None,
        extras={"khive_injection": {"hits": 2}},
        identity_markers={"marker": 7},
    )

    metadata = _node_metadata(store)
    assert metadata["managed"] == "new"
    assert metadata["khive_injection"] == {"hits": 2}
    assert metadata["marker"] == 7
    ((stored_status,),) = _rows(store, "SELECT status FROM sessions")
    assert stored_status == status != "running"
    assert _rows(
        store,
        f"SELECT previous_status, {OWNER} FROM status_transitions "
        "WHERE entity_id = 's1' AND previous_status IS NOT NULL",
    ) == [("running", "principal:a")]
