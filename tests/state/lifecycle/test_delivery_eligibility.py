# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Eligibility is written once and is the common push and replay boundary."""

from __future__ import annotations

import ast
import re
import time
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import event

from lionagi.state.db import StateDB
from lionagi.state.lifecycle import ActorRecord, ReasonRecord, TransitionCommand
from lionagi.state.lifecycle.callbacks import DEFAULT_TERMINAL_CALLBACKS, TerminalCallbackRegistry
from lionagi.state.lifecycle.deliveries import reconcile_unacknowledged, register_consumer
from lionagi.state.lifecycle.service import SQLAlchemyLifecycleService
from lionagi.state.terminal_eligibility import is_delivery_eligible
from lionhub.orchestration.dispatch.outbox import enqueue_dispatch
from tests.state.lifecycle.test_transition_matrix_contract import (
    _EXPECTED_EDGES,
    _EXPECTED_TERMINAL,
    _make_entity,
    _version,
)
from tests.state.lifecycle.test_transition_matrix_contract import (
    _command as _matrix_command,
)


@pytest_asyncio.fixture
async def db():
    state = StateDB(":memory:")
    await state.open()
    try:
        yield state
    finally:
        await state.close()


async def _session(db: StateDB) -> str:
    sid, pid = str(uuid.uuid4()), str(uuid.uuid4())
    await db.create_progression(pid)
    await db.create_session({"id": sid, "progression_id": pid, "status": "running"})
    return sid


def _command(sid: str, target: str) -> TransitionCommand:
    return TransitionCommand(
        entity_type="session",
        entity_id=sid,
        to_status=target,
        reason=ReasonRecord(code="session.stale.no_heartbeat"),
        actor=ActorRecord(type="executor", id="test"),
    )


@pytest.mark.parametrize(
    ("kind", "before", "after", "write_kind", "expected"),
    [
        ("session", None, "failed", "initialization", False),
        ("session", None, "failed", "transition", True),
        ("session", "running", "completed", "transition", True),
        ("session", "completed", "completed", "transition", False),
        ("session", "pending", "running", "transition", False),
        ("invocation", "running", "failed", "transition", True),
        ("schedule_run", None, "failed", "initialization", False),
        ("schedule_run", "running", "failed", "transition", True),
        ("play", "running", "gate_failed", "transition", True),
        ("play", "running", "failed", "transition", False),
        ("dispatch", None, "pending", "initialization", False),
        ("dispatch", "delivering", "delivered", "transition", False),
        ("show", "running", "completed", "transition", False),
    ],
)
def test_eligibility_classification(kind, before, after, write_kind, expected):
    assert is_delivery_eligible(kind, before, after, write_kind=write_kind) is expected


@pytest.mark.asyncio
async def test_new_history_push_and_replay_agree_including_reaper(db):
    events = []
    callbacks = TerminalCallbackRegistry()
    callbacks.register("test", lambda envelope: events.append(envelope.event_id))
    service = SQLAlchemyLifecycleService(db, terminal_callbacks=callbacks)
    db._lifecycle_service()._terminal_callbacks = callbacks
    await register_consumer(db, "test", start="all")

    first = await _session(db)
    completed = await service.transition(_command(first, "completed"))
    same = await service.transition(_command(first, "completed"))
    second = await _session(db)
    await db.execute("UPDATE sessions SET status = NULL WHERE id = ?", (second,))
    await db.update_status(
        "session",
        second,
        new_status="failed",
        reason_code="session.stale.no_heartbeat",
        source="system",
        actor="reaper",
        expected_statuses={None},
    )
    rows = await db.fetch_all(
        "SELECT id, entity_id, previous_status, status, delivery_eligible "
        "FROM status_transitions WHERE entity_id IN (?, ?)",
        (first, second),
    )
    ids = {row["id"] for row in rows if row["delivery_eligible"]}
    replayed = {row["transition_id"] for row in await reconcile_unacknowledged(db, "test")}
    assert set(events) == ids == replayed
    assert completed.transition_id in ids
    assert same.transition_id not in ids
    assert any(
        row["entity_id"] == second
        and row["previous_status"] is None
        and row["status"] == "failed"
        and row["delivery_eligible"]
        for row in rows
    )
    assert all(not row["delivery_eligible"] for row in rows if row["status"] == "running")


_EXECUTION_EDGES = tuple(
    (kind, edge)
    for kind in ("session", "invocation", "schedule_run", "play")
    for edge in _EXPECTED_EDGES[kind]
)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,edge", _EXECUTION_EDGES)
async def test_real_execution_edge_matrix_push_equals_stored_and_replay(db, kind, edge):
    """Exercise real legal edges against the independent policy golden."""
    await register_consumer(db, "matrix", start="all")
    entity_id = await _make_entity(db, kind, edge.source)
    version = await _version(db, kind, entity_id) if edge.required_guard_fields else None
    callbacks = TerminalCallbackRegistry()
    events = []
    callbacks.register("matrix", lambda envelope: events.append(envelope.event_id))
    service = SQLAlchemyLifecycleService(db, terminal_callbacks=callbacks)
    outcome = await service.transition(
        _matrix_command(kind, entity_id, edge, expected_version=version)
    )
    assert outcome.result == "applied" and outcome.transition_id is not None
    row = await db.fetch_one(
        "SELECT delivery_eligible FROM status_transitions WHERE id = ?",
        (outcome.transition_id,),
    )
    assert row is not None and row["delivery_eligible"] is not None
    eligible = edge.target in _EXPECTED_TERMINAL[kind] and edge.source != edge.target
    assert bool(row["delivery_eligible"]) is eligible
    pending = {r["transition_id"] for r in await reconcile_unacknowledged(db, "matrix")}
    assert (outcome.transition_id in events) is eligible
    assert (outcome.transition_id in pending) is eligible
    assert events == ([outcome.transition_id] if eligible else [])


@pytest.mark.asyncio
async def test_actual_null_status_reaper_push_equals_stored_and_replay(tmp_path, monkeypatch):
    import lionagi.state.db as db_module
    import lionagi.studio.services.lifecycle as reaper

    path = tmp_path / "state.db"
    monkeypatch.setattr(db_module, "DEFAULT_DB_PATH", path)
    monkeypatch.setattr(reaper, "state_db_known_absent", lambda: False)
    monkeypatch.setattr(reaper, "process_identity_is_foreign", lambda _session: False)

    async def not_alive(_session, _artifacts):
        return False

    monkeypatch.setattr(reaper, "_resolve_liveness", not_alive)
    async with StateDB(path) as db:
        await register_consumer(db, "reaper-matrix", start="all")
        sid = await _session(db)
        await db.execute(
            "UPDATE sessions SET status = NULL, updated_at = ? WHERE id = ?",
            (time.time() - 7200, sid),
        )
    events = []
    callback_name = "reaper-" + uuid.uuid4().hex
    DEFAULT_TERMINAL_CALLBACKS.register(callback_name, lambda envelope: events.append(envelope))
    try:
        assert await reaper.reap_null_status_sessions(stale_hours=1) == 1
    finally:
        DEFAULT_TERMINAL_CALLBACKS.unregister(callback_name)
    async with StateDB(path) as db:
        rows = await db.fetch_all(
            "SELECT id, delivery_eligible FROM status_transitions "
            "WHERE entity_id = ? AND previous_status IS NULL AND status = ?",
            (sid, "failed"),
        )
        pending = {r["transition_id"] for r in await reconcile_unacknowledged(db, "reaper-matrix")}
    assert len(rows) == 1 and rows[0]["delivery_eligible"]
    assert [event.event_id for event in events] == [rows[0]["id"]]
    assert rows[0]["id"] in pending


@pytest.mark.asyncio
async def test_push_obeys_value_stored_after_commit(db, monkeypatch):
    callbacks = TerminalCallbackRegistry()
    events = []
    callbacks.register("test", lambda envelope: events.append(envelope.event_id))
    service = SQLAlchemyLifecycleService(db, terminal_callbacks=callbacks)
    sid = await _session(db)
    emit = service._emit_terminal_event

    async def alter_before_emit(command, **kwargs):
        await db.execute(
            "UPDATE status_transitions SET delivery_eligible = ? WHERE id = ?",
            (False, kwargs["transition_id"]),
        )
        await emit(command, **kwargs)

    monkeypatch.setattr(service, "_emit_terminal_event", alter_before_emit)
    outcome = await service.transition(_command(sid, "completed"))
    assert outcome.result == "applied"
    assert events == []
    row = await db.fetch_one(
        "SELECT delivery_eligible FROM status_transitions WHERE id = ?", (outcome.transition_id,)
    )
    assert not row["delivery_eligible"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,edge", _EXECUTION_EDGES)
async def test_push_reads_back_only_a_row_written_eligible(db, kind, edge):
    entity_id = await _make_entity(db, kind, edge.source)
    version = await _version(db, kind, entity_id) if edge.required_guard_fields else None
    service = SQLAlchemyLifecycleService(db, terminal_callbacks=TerminalCallbackRegistry())
    reads = []

    def observed(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().startswith("SELECT delivery_eligible FROM status_transitions"):
            reads.append(statement)

    event.listen(db._engine.sync_engine, "before_cursor_execute", observed)
    try:
        outcome = await service.transition(
            _matrix_command(kind, entity_id, edge, expected_version=version)
        )
    finally:
        event.remove(db._engine.sync_engine, "before_cursor_execute", observed)
    assert outcome.result == "applied"
    eligible = edge.target in _EXPECTED_TERMINAL[kind] and edge.source != edge.target
    assert len(reads) == (1 if eligible else 0)


@pytest.mark.asyncio
async def test_dispatch_initialization_writes_false(db):
    dispatch_id = await enqueue_dispatch(db, kind="test", deliver_to="recipient")
    rows = await db.fetch_all(
        "SELECT previous_status, status, delivery_eligible FROM status_transitions "
        "WHERE entity_type = ? AND entity_id = ?",
        ("dispatch", dispatch_id),
    )
    assert len(rows) == 1
    assert rows[0]["previous_status"] is None
    assert rows[0]["status"] == "pending"
    assert rows[0]["delivery_eligible"] is not None and not rows[0]["delivery_eligible"]


@pytest.mark.asyncio
async def test_terminal_initial_state_is_not_delivery(db):
    await register_consumer(db, "initial-test", start="all")
    schedule_id, run_id = str(uuid.uuid4()), str(uuid.uuid4())
    await db.create_schedule(
        {
            "id": schedule_id,
            "name": "initial-" + schedule_id,
            "trigger_type": "interval",
            "interval_sec": 60,
            "action_kind": "agent",
        }
    )
    await db.create_schedule_run(
        {
            "id": run_id,
            "schedule_id": schedule_id,
            "trigger_context": {},
            "action_kind": "agent",
            "action_args": [],
            "status": "skipped",
            "fired_at": time.time(),
        }
    )
    row = await db.fetch_one(
        "SELECT id, previous_status, status, delivery_eligible FROM status_transitions "
        "WHERE entity_type = ? AND entity_id = ?",
        ("schedule_run", run_id),
    )
    assert row is not None and row["previous_status"] is None and row["status"] == "skipped"
    assert row["delivery_eligible"] is not None and not row["delivery_eligible"]
    pending = await reconcile_unacknowledged(db, "initial-test")
    assert row["id"] not in {item["transition_id"] for item in pending}


_HISTORY_INSERT = re.compile(
    r"\binsert\s+(?:or\s+\w+\s+)?into\s+(?:[\w\"`]+\s*\.\s*)?"
    r"[\"`\[]?status_transitions[\"`\]]?\b",
    re.IGNORECASE,
)


def _sql_string(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _sql_string(node.left) + _sql_string(node.right)
    if isinstance(node, ast.JoinedStr):
        return "".join(
            _sql_string(part)
            if isinstance(part, ast.Constant)
            else "{" + ast.unparse(part.value) + "}"
            for part in node.values
        )
    return ""


def _table_named_history(node, aliases):
    if isinstance(node, ast.Name):
        return node.id in aliases
    if isinstance(node, ast.Call):
        name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
        return (
            name.lower() == "table"
            and bool(node.args)
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "status_transitions"
        )
    if isinstance(node, ast.Subscript):
        return isinstance(node.slice, ast.Constant) and node.slice.value == "status_transitions"
    return False


def _audit_history_inserts(source):
    """Find raw SQL and direct SQLAlchemy inserts, rejecting unverifiable values."""
    tree = ast.parse(source)
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    aliases = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if value is not None and _table_named_history(value, set()):
                aliases.update(target.id for target in targets if isinstance(target, ast.Name))
    sites = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Constant, ast.JoinedStr, ast.BinOp)) and not isinstance(
            parents.get(node), (ast.JoinedStr, ast.BinOp)
        ):
            sql = _sql_string(node)
            match = _HISTORY_INSERT.search(sql)
            if match:
                # Static values or the shared compatibility-aware column/parameter pair.
                left = sql.find("(", match.end())
                right = sql.find(")", left + 1) if left >= 0 else -1
                columns = sql[left + 1 : right] if right >= 0 else ""
                declared = bool(re.search(r"\bdelivery_eligible\b", columns, re.IGNORECASE)) or (
                    "{eligibility_column}" in columns and "{eligibility_param}" in sql[right:]
                )
                sites.append((node.lineno, "raw SQL", declared))
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_insert = (
            (
                isinstance(func, ast.Name)
                and func.id == "insert"
                and bool(node.args)
                and _table_named_history(node.args[0], aliases)
            )
            or (
                isinstance(func, ast.Attribute)
                and func.attr == "insert"
                and bool(node.args)
                and _table_named_history(node.args[0], aliases)
            )
            or (
                isinstance(func, ast.Attribute)
                and func.attr == "insert"
                and _table_named_history(func.value, aliases)
            )
        )
        if not is_insert:
            continue
        parent = parents.get(node)
        values_call = parents.get(parent) if isinstance(parent, ast.Attribute) else None
        values_keys = set()
        if isinstance(values_call, ast.Call):
            values_keys.update(key.arg for key in values_call.keywords if key.arg is not None)
            for arg in values_call.args:
                if isinstance(arg, ast.Dict):
                    values_keys.update(
                        key.value
                        for key in arg.keys
                        if isinstance(key, ast.Constant) and isinstance(key.value, str)
                    )
        declared = (
            isinstance(parent, ast.Attribute)
            and parent.attr == "values"
            and isinstance(values_call, ast.Call)
            and "delivery_eligible" in values_keys
        )
        sites.append((node.lineno, "SQLAlchemy", declared))
    return sites


def test_history_insert_guard_detects_whitespace_and_sqlalchemy_forms():
    good_raw = 'query = """INSERT  \n INTO `status_transitions` (id, delivery_eligible) VALUES (:id, :delivery_eligible)"""'
    bad_raw = 'query = """insert\tinto status_transitions(id) values (:id)"""'
    good_sa = 't = Table("status_transitions", metadata)\nstmt = insert(t).values(id="x", delivery_eligible=False)'
    bad_sa = 't = Table("status_transitions", metadata)\nstmt = t.insert().values(id="x")'
    bad_returning = (
        'query = "INSERT INTO status_transitions (id) VALUES (:id) RETURNING delivery_eligible"'
    )
    good_sa_dict = 't = sa.Table("status_transitions", metadata)\nstmt = sa.insert(t).values({"delivery_eligible": False})'
    bad_split = 'query = "INSERT  \\n" + "INTO status_transitions (id) VALUES (:id)"'
    assert [site[2] for site in _audit_history_inserts(good_raw)] == [True]
    assert [site[2] for site in _audit_history_inserts(bad_raw)] == [False]
    assert [site[2] for site in _audit_history_inserts(good_sa)] == [True]
    assert [site[2] for site in _audit_history_inserts(bad_sa)] == [False]
    assert [site[2] for site in _audit_history_inserts(bad_returning)] == [False]
    assert [site[2] for site in _audit_history_inserts(good_sa_dict)] == [True]
    assert [site[2] for site in _audit_history_inserts(bad_split)] == [False]


def test_production_inserts_explicitly_supply_eligibility():
    root = Path(__file__).resolve().parents[3]
    found = []
    for package in (root / "lionagi", root / "lionhub" / "src" / "lionhub"):
        for path in package.rglob("*.py"):
            sites = _audit_history_inserts(path.read_text())
            for line, kind, declared in sites:
                found.append(path.relative_to(root).as_posix())
                assert declared, f"{path}:{line} {kind} does not explicitly write eligibility"
    assert set(found) == {
        "lionagi/state/lifecycle/service.py",
        "lionagi/studio/services/admin.py",
        "lionhub/src/lionhub/orchestration/dispatch/outbox.py",
    }
