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
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import event

from lionagi.hooks.bus import HookPoint
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


@pytest.mark.parametrize("link", ["first_msg_id", "last_msg_id"])
async def test_a_session_update_links_only_rows_its_run_created(db, store, link):
    await _setup_run(db)
    _execute(
        store,
        f"INSERT INTO messages (id, created_at, content, role, lion_class, {OWNER}) "
        "VALUES ('foreign', 1.0, '{}', 'system', 1, 'principal:a')",
    )
    before = _dump(store)

    with pytest.raises(StateCompatibilityRefused, match="was not created by this run"):
        await db.update_session("s1", **{link: "foreign"})

    assert _dump(store) == before


async def test_a_session_update_leaves_a_non_link_column_alone(db, store):
    """``progression_id`` is the session's own progression, not an updatable column, so a link
    check on it would be dead code: the column allowlist refuses the write first."""
    await _setup_run(db)
    before = _dump(store)

    with pytest.raises(ValueError, match="Invalid column"):
        await db.update_session("s1", progression_id="foreign")

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
        columns, placeholders, values = owner_insert(self, table)
        if table != "status_transitions":
            return columns, placeholders, values
        # The owner is named, so the statement check admits it and SQLite fails it.
        return f'{columns}, "no_such_column"', f"{placeholders}, :_owner0", values

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


async def test_teardowns_own_node_metadata_write_still_merges(db, store):
    """The teardown's node_metadata-only write goes through the same by-key merge, so a key
    another writer stored survives it."""
    from lionagi.cli import _runs

    await _setup_run(db)
    _execute(store, """UPDATE sessions SET node_metadata = '{"managed": "kept"}' WHERE id = 's1'""")

    await _runs._teardown_common(
        db,
        session_id="s1",
        session_prog_id="sp",
        status="completed",
        exception=None,
        artifacts_path=None,
        artifact_contract=None,
        extras={"run": 1},
    )

    assert _node_metadata(store) == {"managed": "kept", "run": 1}


async def test_a_profile_that_admits_the_phase_lets_teardown_clear_it(tmp_path):
    """A profile that grants ``sessions.current_phase`` admits the clear, and a phase the run
    wrote is gone beside the terminal status."""
    from lionagi.cli import _runs
    from lionagi.state.compat import TableWrites

    store = await _managed_store(tmp_path / "phase.db")
    base = _profile(store)
    writes = dict(base.writes)
    sessions = writes["sessions"]
    writes["sessions"] = TableWrites(
        insert=sessions.insert,
        update=sessions.update | {"current_phase"},
        unwritten=sessions.unwritten,
    )
    db = StateDB(store, compatibility=_profile(store, writes=writes), owner="principal:a")
    await db.open()
    try:
        await _setup_run(db)
        _execute(store, "UPDATE sessions SET current_phase = 'plan' WHERE id = 's1'")

        await _runs._teardown_common(
            db,
            session_id="s1",
            session_prog_id="sp",
            status="completed",
            exception=None,
            artifacts_path=None,
            artifact_contract=None,
        )
    finally:
        await db.close()

    assert _rows(store, "SELECT status, current_phase FROM sessions WHERE id = 's1'") == [
        ("completed", None)
    ]


async def test_a_native_teardown_still_clears_the_phase(tmp_path):
    """Natively the run owns the column, so the terminal write clears it as it always has."""
    from lionagi.cli import _runs

    db = StateDB(tmp_path / "native.db")
    await db.open()
    try:
        await db.create_progression("sp")
        await db.create_session(
            {"id": "s1", "progression_id": "sp", "status": "running", "current_phase": "plan"}
        )

        await _runs._teardown_common(
            db,
            session_id="s1",
            session_prog_id="sp",
            status="completed",
            exception=None,
            artifacts_path=None,
            artifact_contract=None,
        )
        row = await db.get_session("s1")
    finally:
        await db.close()

    assert row["status"] == "completed"
    assert row["current_phase"] is None


async def test_the_reason_code_warning_still_names_the_caller_in_compatibility_mode(db, store):
    """The operation wrapper adds a frame in compatibility mode too, and the deprecation warning
    still points at the caller."""
    await _setup_run(db)

    with pytest.warns(DeprecationWarning, match="called without reason_code") as caught:
        await db.update_session("s1", status="completed", reason_actor="s1")

    assert [warning.filename for warning in caught] == [__file__]


async def test_the_reason_code_warning_still_names_the_caller(tmp_path):
    """The operation wrapper adds a frame, and a deprecation warning still points at the caller."""
    from tests.state.test_db import _make_session

    native = StateDB(tmp_path / "native.db")
    await native.open()
    try:
        session = await _make_session(native, status="running")
        with pytest.warns(DeprecationWarning, match="called without reason_code") as caught:
            await native.update_session(session["id"], status="completed")
    finally:
        await native.close()

    assert [warning.filename for warning in caught] == [__file__]


# --- The teardown's handle reaches the default handlers, and its status write comes last ---------


def _profile_with_branch_finalization(store: Path):
    """The test profile, admitting a branch's terminal write and the run's usage columns.

    ``finalize_branch`` is named so the default BRANCH_END handler's own write is admitted, and
    the columns it and the default SESSION_END handler set are in the profile's update sets.
    """
    from lionagi.state.compat import CompatibilityProfile, TableWrites

    base = _profile(store)
    writes = dict(base.writes)
    branch = writes["branches"]
    writes["branches"] = TableWrites(
        insert=branch.insert,
        update=branch.update | {"status", "ended_at"},
        unwritten=branch.unwritten,
    )
    sessions = writes["sessions"]
    writes["sessions"] = TableWrites(
        insert=sessions.insert,
        update=sessions.update | {"input_tokens", "output_tokens", "num_turns"},
        unwritten=sessions.unwritten,
    )
    return CompatibilityProfile(
        name=base.name,
        version=base.version,
        closure=base.closure,
        writes=writes,
        operations=base.operations | {"finalize_branch"},
        owner=base.owner,
    )


async def test_the_teardowns_own_handle_finalizes_its_branch(tmp_path, monkeypatch):
    """The teardown hands its handle to the default BRANCH_END handler, which finalizes the run's
    branch through it: the branch is the run's own, so the write is admitted and the run
    completes."""
    from lionagi.cli import _runs
    from lionagi.session.session import Session
    from lionagi.state import db as db_mod

    store = await _managed_store(tmp_path / "managed.db")
    monkeypatch.setattr(db_mod, "DEFAULT_DB_PATH", store)
    handle = StateDB(
        store, compatibility=_profile_with_branch_finalization(store), owner="principal:a"
    )
    await handle.open()
    try:
        await _setup_run(handle)
        ctx = {
            "db": handle,
            "session_id": "s1",
            "session_prog_id": "sp",
            "session": Session(),
            "shared_db_registered": False,
            "branch": SimpleNamespace(id="b1"),
        }
        result = await _runs.teardown_persist(ctx, status="completed")
        branch_status = _rows(store, "SELECT status FROM branches WHERE id = 'b1'")
        session_status = _rows(store, "SELECT status FROM sessions WHERE id = 's1'")
    finally:
        await db_mod.close_shared_db()

    assert ctx.get("store_refusal") is None, repr(ctx.get("store_refusal"))
    assert result == "completed", (result, ctx.get("terminal_reason"))
    assert branch_status == [("completed",)], branch_status
    assert session_status == [("completed",)], session_status


async def test_an_unregistered_handle_never_reaches_a_foreign_branch(tmp_path, monkeypatch):
    """With the run's handle unregistered, the default BRANCH_END handler still writes through the
    handle the teardown handed it, so a branch row another principal owns is left untouched and no
    native handle is opened for the file."""
    from lionagi.cli import _runs
    from lionagi.session.session import Session
    from lionagi.state import db as db_mod

    store = await _managed_store(tmp_path / "managed.db")
    monkeypatch.setattr(db_mod, "DEFAULT_DB_PATH", store)
    handle = StateDB(
        store, compatibility=_profile_with_branch_finalization(store), owner="principal:a"
    )
    await handle.open()
    try:
        await _setup_run(handle)
        _execute(store, f"UPDATE branches SET {OWNER} = 'principal:b' WHERE id = 'b1'")
        before = _rows(store, f"SELECT status, {OWNER} FROM branches WHERE id = 'b1'")
        ctx = {
            "db": handle,
            "session_id": "s1",
            "session_prog_id": "sp",
            "session": Session(),
            "shared_db_registered": False,
            "branch": SimpleNamespace(id="b1"),
        }
        await _runs.teardown_persist(ctx, status="completed")
        after = _rows(store, f"SELECT status, {OWNER} FROM branches WHERE id = 'b1'")
        shared = [getattr(value, "mode", None) for value in db_mod._SHARED.values()]
    finally:
        await db_mod.close_shared_db()

    assert after == before, f"the foreign branch row changed: {before} -> {after}"
    assert "native" not in shared, f"a native handle was opened for the store: {shared}"


def _profile_that_admits_no_signal(store: Path):
    """The test profile without the signal table: a lifecycle emit's own signal record is refused.

    The sink keeps the refusal rather than raising it, which is the case the teardown's last
    check catches.
    """
    from lionagi.state.compat import CompatibilityProfile

    base = _profile(store)
    writes = {name: spec for name, spec in base.writes.items() if name != "session_signals"}
    branch = writes["branches"]
    from lionagi.state.compat import TableWrites

    writes["branches"] = TableWrites(
        insert=branch.insert,
        update=branch.update | {"status", "ended_at"},
        unwritten=branch.unwritten,
    )
    sessions = writes["sessions"]
    writes["sessions"] = TableWrites(
        insert=sessions.insert,
        update=sessions.update | {"input_tokens", "output_tokens", "num_turns"},
        unwritten=sessions.unwritten,
    )
    return CompatibilityProfile(
        name=base.name,
        version=base.version,
        closure=base.closure,
        writes=writes,
        operations=base.operations - {"insert_session_signal"} | {"finalize_branch"},
        owner=base.owner,
    )


async def test_a_refusal_recorded_during_the_emits_leaves_the_run_failed(tmp_path, monkeypatch):
    """The terminal status write is the run's last store write, so a refusal the signal sink
    records while the emits run -- kept, not raised -- leaves the run failed with that refusal
    rather than beside a status the run had already written."""
    from lionagi.cli import _runs
    from lionagi.session.session import Session
    from lionagi.state import db as db_mod

    store = await _managed_store(tmp_path / "managed.db")
    monkeypatch.setattr(db_mod, "DEFAULT_DB_PATH", store)
    handle = StateDB(
        store, compatibility=_profile_that_admits_no_signal(store), owner="principal:a"
    )
    await handle.open()
    await db_mod.register_shared_db(handle)
    try:
        await _setup_run(handle)
        session = Session()
        session.observer.bind_db_persistence("s1", db=handle)
        ctx = {
            "db": handle,
            "session_id": "s1",
            "session_prog_id": "sp",
            "session": session,
            "shared_db_registered": True,
        }

        result = await _runs.teardown_persist(ctx, status="completed")
        stored = _rows(store, "SELECT status FROM sessions WHERE id = 's1'")
    finally:
        await db_mod.close_shared_db()

    assert result == "failed", (result, ctx.get("terminal_reason"))
    refusal = ctx.get("store_refusal")
    assert isinstance(refusal, StateCompatibilityRefused), repr(refusal)
    assert "insert_session_signal refused" in str(refusal), str(refusal)
    assert "insert_session_signal refused" in ctx["terminal_reason"][1]
    assert stored == [("running",)], stored


async def test_the_terminal_status_write_happens_once(db, store):
    """A teardown with no refusal writes the terminal status exactly once, after the emits."""
    from lionagi.cli import _runs
    from lionagi.session.session import Session

    await _setup_run(db)
    session = Session()
    writes = {"n": 0}
    real_update_status = db.update_status

    async def counting_update_status(*args, **kwargs):
        if args and args[0] == "session":
            writes["n"] += 1
        return await real_update_status(*args, **kwargs)

    db.update_status = counting_update_status
    ctx = {
        "db": db,
        "session_id": "s1",
        "session_prog_id": "sp",
        "session": session,
        "shared_db_registered": False,
    }

    result = await _runs.teardown_persist(ctx, status="completed")

    assert result == "completed", (result, ctx.get("terminal_reason"))
    assert writes["n"] == 1, f"the terminal status write ran {writes['n']} times"
    assert _rows(store, "SELECT status FROM sessions WHERE id = 's1'") == [("completed",)]


def _profile_without_node_metadata_grant(store: Path):
    """The teardown profile that admits the usage columns but not ``sessions.node_metadata``."""
    from lionagi.state.compat import CompatibilityProfile, TableWrites

    base = _profile_with_branch_finalization(store)
    writes = dict(base.writes)
    sessions = writes["sessions"]
    writes["sessions"] = TableWrites(
        insert=sessions.insert,
        update=sessions.update - {"node_metadata"},
        unwritten=sessions.unwritten,
    )
    return CompatibilityProfile(
        name=base.name,
        version=base.version,
        closure=base.closure,
        writes=writes,
        operations=base.operations,
        owner=base.owner,
    )


async def test_a_failed_teardown_writes_the_terminal_status_without_a_node_metadata_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lifecycle emit a teardown owns writes no error into ``node_metadata``, so a profile that
    admits no such column still takes the terminal status write."""
    from lionagi.cli import _runs
    from lionagi.session.session import Session
    from lionagi.state import db as db_mod

    store = await _managed_store(tmp_path / "managed.db")
    monkeypatch.setattr(db_mod, "DEFAULT_DB_PATH", store)
    handle = StateDB(
        store, compatibility=_profile_without_node_metadata_grant(store), owner="principal:a"
    )
    await handle.open()
    try:
        await _setup_run(handle)
        ctx = {
            "db": handle,
            "session_id": "s1",
            "session_prog_id": "sp",
            "session": Session(),
            "shared_db_registered": False,
            "branch": SimpleNamespace(id="b1"),
        }
        result = await _runs.teardown_persist(ctx, status="failed", exception=RuntimeError("boom"))
        stored = _rows(store, "SELECT status, node_metadata FROM sessions WHERE id = 's1'")
    finally:
        await db_mod.close_shared_db()

    assert result == "failed", (result, ctx.get("terminal_reason"))
    assert ctx.get("store_refusal") is None, repr(ctx.get("store_refusal"))
    assert stored[0][0] == "failed", stored
    assert "error" not in (stored[0][1] or ""), stored


async def test_a_concurrent_node_metadata_update_survives_a_failed_teardown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The handler reads the session before the teardown writes; a write another writer lands
    between those points is not merged over from that earlier read."""
    from lionagi.cli import _runs
    from lionagi.session.session import Session
    from lionagi.state import db as db_mod

    store = await _managed_store(tmp_path / "managed.db")
    monkeypatch.setattr(db_mod, "DEFAULT_DB_PATH", store)
    handle = StateDB(
        store, compatibility=_profile_with_branch_finalization(store), owner="principal:a"
    )
    await handle.open()
    try:
        await _setup_run(handle)
        _execute(
            store,
            """UPDATE sessions SET node_metadata = '{"managed": "v1"}' WHERE id = 's1'""",
        )

        real_get_session = handle.get_session
        real_finalize_branch = handle.finalize_branch
        in_session_end = {"active": False}

        async def finalize_branch_then_mark(*args, **kwargs):
            result = await real_finalize_branch(*args, **kwargs)
            in_session_end["active"] = True
            return result

        async def get_session_with_concurrent_write(session_id: str):
            row = await real_get_session(session_id)
            if in_session_end["active"]:
                in_session_end["active"] = False
                _execute(
                    store,
                    """UPDATE sessions SET node_metadata = '{"managed": "v2", "fresh": "kept"}'
                       WHERE id = 's1'""",
                )
            return row

        handle.finalize_branch = finalize_branch_then_mark  # type: ignore[method-assign]
        handle.get_session = get_session_with_concurrent_write  # type: ignore[method-assign]

        ctx = {
            "db": handle,
            "session_id": "s1",
            "session_prog_id": "sp",
            "session": Session(),
            "shared_db_registered": False,
            "branch": SimpleNamespace(id="b1"),
        }
        result = await _runs.teardown_persist(ctx, status="failed", exception=RuntimeError("boom"))
        ((raw,),) = _rows(store, "SELECT node_metadata FROM sessions WHERE id = 's1'")
    finally:
        await db_mod.close_shared_db()

    assert result == "failed", (result, ctx.get("terminal_reason"))
    assert json.loads(raw) == {"managed": "v2", "fresh": "kept"}


async def test_a_registered_branch_with_no_messages_does_not_fail_the_teardown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fanout registers each worker lazily; a worker that produced no message has no branch row,
    so the teardown emits no BRANCH_END write for it and still closes the session."""
    from lionagi import Branch
    from lionagi.cli import _runs
    from lionagi.cli.orchestrate import _orchestration
    from lionagi.cli.orchestrate._orchestration import setup_orchestration_persist
    from lionagi.session.session import Session
    from lionagi.state import db as db_mod
    from lionagi.state.compat import CompatibilityProfile, TableWrites

    store = await _managed_store(tmp_path / "managed.db")
    monkeypatch.setattr(db_mod, "DEFAULT_DB_PATH", store)
    base = _profile_with_branch_finalization(store)
    writes = dict(base.writes)
    sessions = writes["sessions"]
    writes["sessions"] = TableWrites(
        insert=sessions.insert,
        update=sessions.update | {"current_phase"},
        unwritten=sessions.unwritten,
    )
    profile = CompatibilityProfile(
        name=base.name,
        version=base.version,
        closure=base.closure,
        writes=writes,
        operations=base.operations | {"finalize_branch"},
        owner=base.owner,
    )
    handle = StateDB(store, compatibility=profile, owner="principal:a")
    await handle.open()

    async def compatibility_store(*_args, **_kwargs):
        return handle

    monkeypatch.setattr(_orchestration, "_open_shared_db", compatibility_store)
    monkeypatch.setattr(
        "lionagi.cli.kill.current_pid_markers",
        lambda: {
            "pid": 1,
            "pid_create_time": 0.0,
            "pid_host": "test",
            "pid_boot_time": 0.0,
            "process_identity_mode": "local",
        },
    )
    try:
        session = Session()
        used = Branch(user=str(session.id), name="used")
        unused = Branch(user=str(session.id), name="unused-worker")
        session.branches.include(used)
        session.branches.include(unused)
        session.default_branch = used

        ctx = await setup_orchestration_persist(
            session,
            branches=[used, unused],
            invocation_kind="fanout",
            model="fixture-model",
            provider="fixture-provider",
            project="fixture-project",
        )
        assert ctx is not None
        assert ctx["worker_spawn_owner"] is None
        assert ctx["worker_spawns_unrecorded"] is True
        result = await _runs.teardown_persist(ctx, status="completed")
        stored = _rows(store, "SELECT status FROM sessions WHERE id = ?", (str(session.id),))
    finally:
        await db_mod.close_shared_db()

    assert result == "completed", (result, ctx.get("terminal_reason"))
    assert ctx.get("store_refusal") is None, repr(ctx.get("store_refusal"))
    assert stored == [("completed",)], stored


async def test_teardown_signals_carry_no_store_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The observer record of a lifecycle emit keeps the event's data but not the run's handle, so
    no closed store is retained on the observer flow or serialized into a stored signal."""
    from lionagi.cli import _runs
    from lionagi.hooks.bus import HookSignal
    from lionagi.session.session import Session
    from lionagi.state import db as db_mod

    store = await _managed_store(tmp_path / "managed.db")
    monkeypatch.setattr(db_mod, "DEFAULT_DB_PATH", store)
    handle = StateDB(
        store, compatibility=_profile_with_branch_finalization(store), owner="principal:a"
    )
    await handle.open()
    try:
        await _setup_run(handle)
        session = Session()
        session.observer.bind_db_persistence("s1", db=handle)
        ctx = {
            "db": handle,
            "session_id": "s1",
            "session_prog_id": "sp",
            "session": session,
            "shared_db_registered": False,
            "branch": SimpleNamespace(id="b1"),
        }
        result = await _runs.teardown_persist(ctx, status="completed")
        signals = [item for item in session.observer.flow.items if isinstance(item, HookSignal)]
        payloads = [
            json.loads(payload)
            for (payload,) in _rows(
                store, "SELECT payload FROM session_signals WHERE session_id = 's1'"
            )
        ]
    finally:
        await db_mod.close_shared_db()

    assert result == "completed", (result, ctx.get("terminal_reason"))
    assert signals, "the teardown recorded no lifecycle signals"
    assert all("db" not in signal.kwargs for signal in signals), signals
    assert all("db" not in payload.get("kwargs", {}) for payload in payloads), payloads
