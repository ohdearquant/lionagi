# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Compatibility-mode ownership (ADR-0128 D5) and a run's creating operations (Appendix A).

The managed store here is lionagi's own native schema with an owner column added to the tables
that carry one, a principals table, and another lineage's version stamp: the shape of a store a
newer schema manages, built synthetically.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text

from lionagi.state.compat import (
    CompatibilityProfile,
    OwnerRule,
    StateCompatibilityRefused,
    TableWrites,
    _operation,
    read_closure,
)
from lionagi.state.db import StateDB

OWNER = "owner_principal_id"
_OWNED = ("sessions", "branches", "progressions", "messages", "status_transitions", "invocations")
_TABLES = (*_OWNED, "message_types", "principals")

_INSERTS = {
    "progressions": {"id", "created_at", "collection", OWNER},
    "sessions": {
        "id",
        "cc_session_id",
        "run_id",
        "created_at",
        "node_metadata",
        "name",
        "user",
        "progression_id",
        "first_msg_id",
        "last_msg_id",
        "updated_at",
        "playbook_name",
        "agent_name",
        "invocation_kind",
        "show_topic",
        "show_play_name",
        "artifacts_path",
        "artifact_contract_json",
        "artifact_verification_json",
        "source_kind",
        "status",
        "started_at",
        "ended_at",
        "ended_at_is_approximate",
        "last_message_at",
        "invocation_id",
        "model",
        "provider",
        "effort",
        "agent_hash",
        "project",
        "project_source",
        "duration_ms",
        OWNER,
    },
    "branches": {
        "id",
        "created_at",
        "node_metadata",
        "user",
        "name",
        "session_id",
        "progression_id",
        "system_msg_id",
        "model",
        "provider",
        "agent_name",
        OWNER,
    },
    "messages": {
        "id",
        "created_at",
        "node_metadata",
        "content",
        "embedding",
        "sender",
        "recipient",
        "channel",
        "role",
        "lion_class",
        OWNER,
    },
    "message_types": {"lion_class"},
    "status_transitions": {
        "id",
        "entity_type",
        "entity_id",
        "previous_status",
        "status",
        "reason_code",
        "reason_summary",
        "evidence_refs",
        "source",
        "actor",
        "created_at",
        "metadata",
        OWNER,
    },
}
_UPDATES = {
    "sessions": {
        "status_reason_code",
        "status_reason_summary",
        "status_evidence_refs",
        "last_message_at",
        "updated_at",
    },
    "progressions": {"collection"},
    "branches": {"system_msg_id"},
    "messages": {
        "node_metadata",
        "content",
        "embedding",
        "sender",
        "recipient",
        "channel",
        "role",
        "lion_class",
    },
    "invocations": {"session_count", "updated_at"},
}
_OPERATIONS = {
    "create_progression",
    "create_session",
    "create_branch",
    "insert_message",
    "persist_live_message",
}


_OWNER_COLUMN = "TEXT NOT NULL DEFAULT 'principal:default'"


async def _managed_store(path: Path, owner_sql: str = _OWNER_COLUMN) -> Path:
    native = StateDB(path)
    await native.open()
    await native.close()
    with closing(sqlite3.connect(path)) as conn:
        for table in _OWNED:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {OWNER} {owner_sql}")
        conn.executescript(
            """
            CREATE TABLE principals (id TEXT PRIMARY KEY, kind TEXT NOT NULL);
            INSERT INTO principals VALUES
                ('principal:a', 'human'), ('principal:b', 'human'), ('principal:default', 'human');
            UPDATE schema_meta SET value = '900' WHERE key = 'version';
            """
        )
        conn.commit()
    return path


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f'PRAGMA table_xinfo("{table}")')]


def _profile(store: Path, inserts: dict | None = None, **changes) -> CompatibilityProfile:
    inserts = _INSERTS if inserts is None else inserts
    with closing(sqlite3.connect(store)) as conn:
        closure = read_closure(conn, _TABLES)
        writes = {}
        for table in set(inserts) | set(_UPDATES):
            insert = inserts.get(table, set())
            unwritten = (
                {c: "left to the store's default" for c in _columns(conn, table) if c not in insert}
                if insert
                else {}
            )
            writes[table] = TableWrites(
                insert=insert, update=_UPDATES.get(table, set()), unwritten=unwritten
            )
    fields = {
        "name": "owned",
        "version": 1,
        "closure": closure,
        "writes": writes,
        "operations": _OPERATIONS,
        "owner": OwnerRule(column=OWNER, principals="principals"),
    }
    fields.update(changes)
    return CompatibilityProfile(**fields)


def _rows(store: Path, sql: str, params: tuple = ()) -> list:
    with closing(sqlite3.connect(store)) as conn:
        return conn.execute(sql, params).fetchall()


def _execute(store: Path, sql: str, params: tuple = ()) -> None:
    with closing(sqlite3.connect(store)) as conn:
        conn.execute(sql, params)
        conn.commit()


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


def _session(session_id: str = "s1", **fields) -> dict:
    return {"id": session_id, "progression_id": "sp", "status": "running", **fields}


def _system_message(message_id: str = "m-sys", content: str = "you are a test") -> dict:
    return {
        "id": message_id,
        "created_at": 1.0,
        "content": {"text": content},
        "role": "system",
        "node_metadata": {"lion_class": "System"},
    }


async def _setup_run(db: StateDB) -> None:
    """The writes a fresh agent run's setup makes, in its order."""
    await db.create_progression("sp")
    await db.create_progression("bp")
    await db.create_session(_session())
    await db.insert_message(_system_message())
    await db.create_branch(
        {"id": "b1", "session_id": "s1", "progression_id": "bp", "system_msg_id": "m-sys"}
    )


async def test_a_run_writes_its_owner_on_every_row_it_creates(db, store):
    await _setup_run(db)

    for table, row_id in (
        ("progressions", "sp"),
        ("progressions", "bp"),
        ("sessions", "s1"),
        ("messages", "m-sys"),
        ("branches", "b1"),
    ):
        assert _rows(store, f"SELECT {OWNER} FROM {table} WHERE id = ?", (row_id,)) == [
            ("principal:a",)
        ], table
    transitions = _rows(
        store,
        f"SELECT previous_status, status, {OWNER} FROM status_transitions WHERE entity_id = 's1'",
    )
    assert transitions == [(None, "running", "principal:a")]


async def test_project_registration_is_left_to_the_managing_application(db, store):
    await db.create_progression("sp")
    await db.create_session(_session(project="acme/widgets", project_source="git_remote"))

    assert _rows(store, "SELECT project FROM sessions WHERE id = 's1'") == [("acme/widgets",)]
    assert _rows(store, "SELECT COUNT(*) FROM projects") == [(0,)]


async def test_an_owner_that_is_not_a_principal_is_refused_at_open(store):
    handle = StateDB(store, compatibility=_profile(store), owner="principal:nobody")

    with pytest.raises(StateCompatibilityRefused, match="not in principals") as refused:
        await handle.open()

    assert refused.value.operation == "open"
    assert handle._engine is None


def test_a_profile_that_records_owners_needs_an_owner_and_only_it_takes_one(tmp_path):
    reference = tmp_path / "reference.db"
    with closing(sqlite3.connect(reference)) as conn:
        conn.executescript(
            "CREATE TABLE principals (id TEXT PRIMARY KEY);"
            f"CREATE TABLE runs (id TEXT PRIMARY KEY, {OWNER} TEXT NOT NULL DEFAULT 'x');"
        )
        closure = read_closure(conn, ["principals", "runs"])
    owned = CompatibilityProfile(
        name="owned",
        version=1,
        closure=closure,
        writes={"runs": TableWrites(insert={"id", OWNER})},
        operations={"start"},
        owner=OwnerRule(column=OWNER, principals="principals"),
    )
    plain = CompatibilityProfile(
        name="plain",
        version=1,
        closure=closure,
        writes={"runs": TableWrites(insert={"id", OWNER})},
        operations={"start"},
    )

    with pytest.raises(ValueError, match="an owner is required"):
        StateDB(reference, compatibility=owned)
    with pytest.raises(ValueError, match="records no owner"):
        StateDB(reference, compatibility=plain, owner="principal:a")
    with pytest.raises(ValueError, match="compatibility mode only"):
        StateDB(reference, owner="principal:a")


@pytest.mark.parametrize(
    "writes, owner, message",
    [
        (
            {"runs": TableWrites(insert={"id"}, unwritten={OWNER: "the default names one"})},
            OwnerRule(column=OWNER, principals="principals"),
            "a default is never the owner",
        ),
        (
            {"runs": TableWrites(insert={"id", OWNER}, update={OWNER})},
            OwnerRule(column=OWNER, principals="principals"),
            "never updated",
        ),
        (
            {"runs": TableWrites(insert={"id", OWNER})},
            OwnerRule(column=OWNER, principals="people"),
            "not in the closure",
        ),
        (
            {"runs": TableWrites(insert={"id", OWNER})},
            OwnerRule(column=OWNER, principals="principals", key="handle"),
            "no column handle",
        ),
    ],
    ids=["default-owner", "updated-owner", "no-principals-table", "no-principals-key"],
)
def test_a_profile_that_could_leave_or_change_the_owner_is_refused(
    tmp_path, writes, owner, message
):
    reference = tmp_path / "reference.db"
    with closing(sqlite3.connect(reference)) as conn:
        conn.executescript(
            "CREATE TABLE principals (id TEXT PRIMARY KEY);"
            f"CREATE TABLE runs (id TEXT PRIMARY KEY, {OWNER} TEXT NOT NULL DEFAULT 'x');"
        )
        closure = read_closure(conn, ["principals", "runs"])

    with pytest.raises(ValueError, match=message):
        CompatibilityProfile(
            name="owned",
            version=1,
            closure=closure,
            writes=writes,
            operations={"start"},
            owner=owner,
        )


@pytest.mark.parametrize("table", ["progressions", "sessions", "branches"])
async def test_an_id_that_already_exists_is_refused_and_left_as_it_is(db, store, table):
    await db.create_progression("sp")
    await db.create_progression("bp")
    if table == "progressions":
        _execute(
            store, "INSERT INTO progressions (id, created_at, collection) VALUES ('p0', 0, '[]')"
        )
        call = db.create_progression("p0")
    elif table == "sessions":
        _execute(
            store,
            f"INSERT INTO sessions (id, created_at, updated_at, progression_id, {OWNER}) "
            "VALUES ('s0', 0, 0, 'sp', 'principal:b')",
        )
        call = db.create_session(_session("s0"))
    else:
        await db.create_session(_session())
        _execute(
            store,
            "INSERT INTO branches (id, created_at, session_id, progression_id) "
            "VALUES ('b0', 0, 's1', 'bp')",
        )
        call = db.create_branch({"id": "b0", "session_id": "s1", "progression_id": "bp"})
    before = _rows(store, f"SELECT * FROM {table} ORDER BY id")

    with pytest.raises(StateCompatibilityRefused, match="already exists"):
        await call

    assert _rows(store, f"SELECT * FROM {table} ORDER BY id") == before


@pytest.mark.parametrize(
    "link, value",
    [("session_id", "s-other"), ("progression_id", "p-other"), ("system_msg_id", "m-other")],
)
async def test_a_branch_links_only_rows_its_run_created(db, store, link, value):
    await db.create_progression("sp")
    await db.create_progression("bp")
    await db.create_session(_session())
    _execute(
        store, "INSERT INTO progressions (id, created_at, collection) VALUES ('p-other', 0, '[]')"
    )
    _execute(
        store,
        f"INSERT INTO sessions (id, created_at, updated_at, progression_id, {OWNER}) "
        "VALUES ('s-other', 0, 0, 'p-other', 'principal:a')",
    )
    _execute(
        store,
        "INSERT INTO messages (id, created_at, content, role, lion_class) "
        "VALUES ('m-other', 0, '{}', 'system', 1)",
    )
    branch = {"id": "b1", "session_id": "s1", "progression_id": "bp", link: value}

    with pytest.raises(StateCompatibilityRefused, match="was not created by this run"):
        await db.create_branch(branch)

    assert _rows(store, "SELECT COUNT(*) FROM branches") == [(0,)]


def _foreign_rows(store: Path) -> None:
    """A progression and a message another principal's run wrote."""
    _execute(
        store,
        f"INSERT INTO progressions (id, created_at, collection, {OWNER}) "
        "VALUES ('p-other', 0, '[]', 'principal:b')",
    )
    _execute(
        store,
        f"INSERT INTO messages (id, created_at, content, role, lion_class, {OWNER}) "
        "VALUES ('m-other', 0, '{}', 'system', 1, 'principal:b')",
    )


@pytest.mark.parametrize(
    "link, value",
    [("progression_id", "p-other"), ("first_msg_id", "m-other"), ("last_msg_id", "m-other")],
)
async def test_a_session_links_only_rows_its_run_created(db, store, link, value):
    await db.create_progression("sp")
    await db.insert_message(_system_message())
    _foreign_rows(store)

    with pytest.raises(StateCompatibilityRefused, match="was not created by this run"):
        await db.create_session(_session(**{link: value}))
    await db.create_session(_session("s2", first_msg_id="m-sys", last_msg_id="m-sys"))

    assert _rows(store, "SELECT id FROM sessions ORDER BY id") == [("s2",)]
    assert _rows(store, "SELECT entity_id FROM status_transitions") == [("s2",)]


async def test_a_progression_lists_only_messages_its_run_created(db, store):
    await db.insert_message(_system_message())
    _foreign_rows(store)

    with pytest.raises(StateCompatibilityRefused, match="'m-other' was not created by this run"):
        await db.create_progression("p1", ["m-sys", "m-other"])
    await db.create_progression("p2", ["m-sys"])

    assert _rows(store, "SELECT id, collection FROM progressions ORDER BY id") == [
        ("p-other", "[]"),
        ("p2", '["m-sys"]'),
    ]


@pytest.mark.parametrize(
    "taken",
    ["DELETE FROM messages WHERE id = 'm-sys'", f"UPDATE messages SET {OWNER} = 'principal:b'"],
    ids=["deleted", "given to another owner"],
)
async def test_a_message_the_store_took_back_is_never_rewritten(db, store, taken):
    await db.insert_message(_system_message(content="first"))
    _execute(store, taken)
    before = _rows(store, f"SELECT id, content, {OWNER} FROM messages")

    with pytest.raises(StateCompatibilityRefused, match="is gone or no longer belongs"):
        await db.insert_message(_system_message(content="second"))

    assert _rows(store, f"SELECT id, content, {OWNER} FROM messages") == before


async def test_a_message_this_run_wrote_is_rewritten_and_any_other_refused(db, store):
    await db.insert_message(_system_message(content="first"))
    await db.insert_message(_system_message(content="second"))
    _execute(
        store,
        f"INSERT INTO messages (id, created_at, content, role, lion_class, {OWNER}) "
        "VALUES ('m-other', 0, '{\"text\": \"theirs\"}', 'system', 1, 'principal:a')",
    )

    with pytest.raises(StateCompatibilityRefused, match="already exists"):
        await db.insert_message(_system_message("m-other", content="mine"))

    assert _rows(store, "SELECT content FROM messages WHERE id = 'm-sys'") == [
        ('{"text": "second"}',)
    ]
    assert _rows(store, "SELECT content FROM messages WHERE id = 'm-other'") == [
        ('{"text": "theirs"}',)
    ]


async def test_an_invocation_must_belong_to_the_owner(db, store):
    for invocation, owner in (("inv-a", "principal:a"), ("inv-b", "principal:b")):
        _execute(
            store,
            f"INSERT INTO invocations (id, skill, started_at, created_at, updated_at, {OWNER}) "
            "VALUES (?, 'review', 0, 0, 0, ?)",
            (invocation, owner),
        )
    await db.create_progression("sp")

    with pytest.raises(StateCompatibilityRefused, match="does not belong to 'principal:a'"):
        await db.create_session(_session("s-b", invocation_id="inv-b"))
    await db.create_session(_session("s-a", invocation_id="inv-a"))

    assert _rows(store, "SELECT id FROM sessions ORDER BY id") == [("s-a",)]
    assert _rows(store, "SELECT id, session_count FROM invocations ORDER BY id") == [
        ("inv-a", 1),
        ("inv-b", 0),
    ]


async def test_an_empty_invocation_id_is_checked_like_any_other(db, store):
    _execute(
        store,
        f"INSERT INTO invocations (id, skill, started_at, created_at, updated_at, {OWNER}) "
        "VALUES ('', 'review', 0, 0, 0, 'principal:b')",
    )
    await db.create_progression("sp")

    with pytest.raises(StateCompatibilityRefused, match="does not belong to 'principal:a'"):
        await db.create_session(_session(invocation_id=""))

    assert _rows(store, "SELECT COUNT(*) FROM sessions") == [(0,)]
    assert _rows(store, "SELECT session_count FROM invocations WHERE id = ''") == [(0,)]


async def test_a_link_by_an_id_that_is_not_a_string_is_refused(db, store):
    """A set lookup by Python equality would take 1.0 for the 1 this run created."""
    await db.insert_message(_system_message(1))
    _execute(
        store,
        f"INSERT INTO messages (id, created_at, content, role, lion_class, {OWNER}) "
        "VALUES ('1.0', 0, '{}', 'system', 1, 'principal:b')",
    )

    with pytest.raises(StateCompatibilityRefused, match="1.0 is not a string id"):
        await db.create_progression("pn", [1.0])

    assert _rows(store, "SELECT COUNT(*) FROM progressions") == [(0,)]


async def test_an_owner_removed_after_open_is_refused_at_the_next_write(db, store):
    await db.create_progression("sp")
    _execute(store, "DELETE FROM principals WHERE id = 'principal:a'")

    with pytest.raises(StateCompatibilityRefused, match="the write transaction: the owner"):
        await db.create_progression("sp2")

    assert _rows(store, "SELECT id FROM progressions") == [("sp",)]


@pytest.mark.parametrize(
    "default", ["", " NOT NULL DEFAULT 'principal:default'"], ids=["nullable", "defaulted"]
)
async def test_an_owned_message_type_is_written_with_the_owner(tmp_path, default):
    store = await _managed_store(tmp_path / "managed.db")
    _execute(store, f"ALTER TABLE message_types ADD COLUMN {OWNER} TEXT{default}")
    profile = _profile(store, inserts={**_INSERTS, "message_types": {"lion_class", OWNER}})
    handle = StateDB(store, compatibility=profile, owner="principal:a")
    await handle.open()
    try:
        await handle.insert_message(_system_message())
    finally:
        await handle.close()

    assert _rows(store, f"SELECT {OWNER} FROM message_types WHERE lion_class = 'System'") == [
        ("principal:a",)
    ]


class _LeavesOutTheOwner(StateDB):
    @_operation("create_progression")
    async def create_progression(self, progression_id: str, collection=None) -> None:
        async with self._tx() as conn:
            await conn.execute(text(self.statement), {"id": progression_id})


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO progressions (id, created_at, collection) VALUES (:id, 1.0, '[]')",
        "INSERT INTO progressions VALUES (:id, 1.0, '[]', 'principal:a')",
        "INSERT INTO 'progressions' (id, created_at, collection) VALUES (:id, 1.0, '[]')",
        "INSERT INTO main.'progressions' (id, created_at, collection) VALUES (:id, 1.0, '[]')",
    ],
    ids=["columns without the owner", "no column list", "string name", "schema and string name"],
)
async def test_an_insert_into_an_owned_table_that_leaves_out_the_owner_is_refused(store, statement):
    """The authorizer sees the table an INSERT writes, never its columns."""
    handle = _LeavesOutTheOwner(store, compatibility=_profile(store), owner="principal:a")
    handle.statement = statement
    await handle.open()
    try:
        with pytest.raises(StateCompatibilityRefused, match="leaves out the owner column"):
            await handle.create_progression("p1")
    finally:
        await handle.close()

    assert _rows(store, "SELECT COUNT(*) FROM progressions") == [(0,)]


class _RawWrite(StateDB):
    @_operation("create_progression")
    async def create_progression(self, progression_id: str, collection=None) -> None:
        async with self._tx() as conn:
            await conn.execute(text(self.statement), {"id": progression_id, **self.params})


def _invocation(store: Path, owner: str) -> None:
    _execute(
        store,
        f"INSERT INTO invocations (id, skill, started_at, created_at, updated_at, {OWNER}) "
        "VALUES ('p1', 'skill', 0.0, 0.0, 0.0, ?)",
        (owner,),
    )


async def _raw_write(store: Path, statement: str, params: dict) -> None:
    handle = _RawWrite(store, compatibility=_profile(store), owner="principal:a")
    handle.statement, handle.params = statement, params
    await handle.open()
    try:
        await handle.create_progression("p1")
    finally:
        await handle.close()


_PROGRESSION = f"INSERT INTO progressions (id, created_at, collection, {OWNER}) "


@pytest.mark.parametrize(
    ("statement", "params", "reason"),
    [
        (_PROGRESSION + "VALUES (:id, 1.0, '[]', 'principal:b')", {}, "other than the owner"),
        (_PROGRESSION + "VALUES (:id, 1.0, '[]', NULL)", {}, "other than the owner"),
        (
            _PROGRESSION
            + "VALUES (:id, 1.0, '[]', (SELECT id FROM principals WHERE id = 'principal:b'))",
            {},
            "other than the owner",
        ),
        (
            _PROGRESSION + "VALUES (:id, 1.0, '[]', :o)",
            {"o": "principal:b"},
            "other than the owner",
        ),
        # The owner sits in another column's slot: the owner's own slot is what is read.
        (
            _PROGRESSION + "VALUES (:id, 1.0, :col, :o)",
            {"col": "principal:a", "o": "principal:b"},
            "other than the owner",
        ),
        (
            _PROGRESSION + "VALUES (:id, 1.0, '[]', :a), ('p2', 1.0, '[]', :b)",
            {"a": "principal:a", "b": "principal:b"},
            "other than the owner",
        ),
        (_PROGRESSION + "SELECT :id, 1.0, '[]', 'principal:b'", {}, "from a query"),
        (
            _PROGRESSION + "VALUES (:id, 1.0, '[]', :o) ON CONFLICT (id) DO UPDATE SET "
            "collection = excluded.collection",
            {"o": "principal:a"},
            "upsert",
        ),
        (
            "UPDATE invocations SET updated_at = 1.0 WHERE id = :id",
            {},
            "does not restrict its rows",
        ),
        (
            f"UPDATE invocations SET updated_at = 1.0 WHERE id = :id AND {OWNER} = :o",
            {"o": "principal:b"},
            "does not restrict its rows",
        ),
        (
            f"UPDATE invocations SET updated_at = 1.0 WHERE id = :id OR {OWNER} = :o",
            {"o": "principal:a"},
            "not a plain conjunction",
        ),
        (
            f"UPDATE invocations SET updated_at = 1.0 WHERE session_count BETWEEN 0 AND {OWNER} = :o",
            {"o": "principal:a"},
            "not a plain conjunction",
        ),
        (
            f"UPDATE invocations SET updated_at = 1.0 WHERE id = :id AND NOT {OWNER} = :o",
            {"o": "principal:a"},
            "does not restrict its rows",
        ),
        (
            f"UPDATE 'invocations' SET updated_at = 1.0 WHERE {OWNER} = :o",
            {"o": "principal:b"},
            "does not restrict its rows",
        ),
    ],
    ids=[
        "owner literal",
        "owner null",
        "owner from a query",
        "owner bound to another",
        "owner slot holds another",
        "one row of two another",
        "rows from a query",
        "upsert that updates",
        "update without the owner",
        "update bound to another",
        "update with or",
        "update with between",
        "update with not",
        "update of a string name",
    ],
)
async def test_a_write_that_does_not_bind_the_owner_is_refused(store, statement, params, reason):
    """The authorizer sees neither the values a write binds nor the rows its WHERE clause picks."""
    _invocation(store, "principal:b")
    with pytest.raises(StateCompatibilityRefused, match=reason):
        await _raw_write(store, statement, params)

    assert _rows(store, "SELECT COUNT(*) FROM progressions") == [(0,)]
    assert _rows(store, "SELECT updated_at FROM invocations WHERE id = 'p1'") == [(0.0,)]


@pytest.mark.parametrize(
    ("statement", "written", "expected"),
    [
        (
            _PROGRESSION + "VALUES (:id, 1.0, '[]', :o)",
            f"SELECT {OWNER} FROM progressions WHERE id = 'p1'",
            [("principal:a",)],
        ),
        (
            f"UPDATE invocations SET updated_at = 1.0 WHERE id = :id AND {OWNER} = :o",
            "SELECT updated_at FROM invocations WHERE id = 'p1'",
            [(1.0,)],
        ),
    ],
    ids=["insert", "update"],
)
async def test_a_write_that_binds_the_owner_is_admitted(store, statement, written, expected):
    _invocation(store, "principal:a")
    await _raw_write(store, statement, {"o": "principal:a"})

    assert _rows(store, written) == expected


_MIRROR = "CREATE TRIGGER mirror AFTER INSERT ON messages BEGIN {body}; END"


@pytest.mark.parametrize(
    "body",
    [
        "INSERT INTO progressions (id, created_at, collection) VALUES (NEW.id, 1.0, '[]')",
        f"INSERT INTO progressions (id, created_at, collection, {OWNER}) "
        "VALUES (NEW.id, 1.0, '[]', 'principal:b')",
        f"INSERT INTO 'progressions' (id, created_at, collection, {OWNER}) "
        f"SELECT NEW.id, 1.0, '[]', NEW.{OWNER}",
        "UPDATE progressions SET collection = '[]' WHERE id = NEW.id",
        f"UPDATE progressions SET collection = '[]' WHERE id = NEW.id OR {OWNER} = NEW.{OWNER}",
    ],
    ids=["owner left out", "owner literal", "rows from a query", "update by id", "update with or"],
)
async def test_a_trigger_that_writes_an_owned_table_without_the_owner_is_refused(tmp_path, body):
    """A trigger's statements pass no statement check, so admission reads them."""
    store = await _managed_store(tmp_path / "managed.db")
    _execute(store, _MIRROR.format(body=body))
    handle = StateDB(store, compatibility=_profile(store), owner="principal:a")
    with pytest.raises(StateCompatibilityRefused, match="trigger mirror"):
        await handle.open()
    assert handle._engine is None


@pytest.mark.parametrize(
    "body",
    [
        f"INSERT INTO progressions (id, created_at, collection, {OWNER}) "
        f"VALUES (NEW.id, 1.0, '[]', NEW.{OWNER})",
        f"UPDATE progressions SET collection = '[]' WHERE id = NEW.id AND {OWNER} = NEW.{OWNER}",
    ],
    ids=["insert", "update"],
)
async def test_a_trigger_that_copies_the_owner_is_admitted(tmp_path, body):
    store = await _managed_store(tmp_path / "managed.db")
    _execute(store, _MIRROR.format(body=body))
    handle = StateDB(store, compatibility=_profile(store), owner="principal:a")
    await handle.open()
    await handle.close()


async def test_an_invocation_is_refused_when_the_profile_records_no_owner(store):
    profile = _profile(store)
    plain = CompatibilityProfile(
        name="plain",
        version=1,
        closure=profile.closure,
        writes={
            name: spec
            for name, spec in profile.writes.items()
            if name not in ("sessions", "status_transitions")
        }
        | {
            "sessions": TableWrites(
                insert=_INSERTS["sessions"] - {OWNER},
                update=_UPDATES["sessions"],
                unwritten={
                    c: "left to the store's default"
                    for c in profile.writes["sessions"].unwritten.keys() | {OWNER}
                },
            )
        },
        operations=_OPERATIONS,
    )
    handle = StateDB(store, compatibility=plain)
    await handle.open()
    try:
        await handle.create_progression("sp")
        with pytest.raises(StateCompatibilityRefused, match="records no owner"):
            await handle.create_session(_session(invocation_id="inv-a"))
    finally:
        await handle.close()

    assert _rows(store, "SELECT COUNT(*) FROM sessions") == [(0,)]


async def test_a_row_is_the_runs_only_once_its_transaction_commits(db, store, monkeypatch):
    await db.create_progression("sp")
    await db.create_progression("bp")

    async def fail(*_args, **_kwargs):
        raise RuntimeError("the lifecycle write failed")

    with monkeypatch.context() as patched:
        patched.setattr(db, "_initialize_managed_entity_in_tx", fail)
        with pytest.raises(RuntimeError, match="lifecycle write failed"):
            await db.create_session(_session())

    assert _rows(store, "SELECT COUNT(*) FROM sessions") == [(0,)]
    with pytest.raises(StateCompatibilityRefused, match="session 's1' was not created"):
        await db.create_branch({"id": "b1", "session_id": "s1", "progression_id": "bp"})


async def test_another_handle_on_the_store_did_not_create_this_runs_rows(db, store):
    await db.create_progression("sp")
    await db.create_progression("bp")
    await db.create_session(_session())
    other = StateDB(store, compatibility=db.compatibility, owner="principal:a")
    await other.open()
    try:
        with pytest.raises(StateCompatibilityRefused, match="was not created by this run"):
            await other.create_branch({"id": "b1", "session_id": "s1", "progression_id": "bp"})
    finally:
        await other.close()


def _live_message(message_id: str = "m1", content: str = "hello", role: str = "user") -> dict:
    return {
        "id": message_id,
        "created_at": 2.0,
        "content": {"text": content},
        "role": role,
        "node_metadata": {"lion_class": "Instruction"},
    }


async def test_a_live_message_joins_the_runs_session_branch_and_progressions(db, store):
    await _setup_run(db)
    ((started,),) = _rows(store, "SELECT last_message_at FROM sessions WHERE id = 's1'")

    await db._persist_live_message(
        _live_message(),
        session_id="s1",
        branch_progression_id="bp",
        session_progression_id="sp",
        activity_at=started + 10,
    )
    await db._persist_live_message(
        _live_message("m-sys2", role="system"),
        session_id="s1",
        branch_progression_id="bp",
        system_branch_id="b1",
        activity_at=started + 5,
    )

    assert _rows(store, f"SELECT {OWNER} FROM messages WHERE id IN ('m1', 'm-sys2')") == [
        ("principal:a",),
        ("principal:a",),
    ]
    assert _rows(store, "SELECT id, collection FROM progressions ORDER BY id") == [
        ("bp", '["m1","m-sys2"]'),
        ("sp", '["m1"]'),
    ]
    assert _rows(store, "SELECT system_msg_id FROM branches WHERE id = 'b1'") == [("m-sys2",)]
    assert _rows(store, "SELECT last_message_at FROM sessions WHERE id = 's1'") == [(started + 10,)]


async def test_a_live_message_written_again_is_rewritten_and_listed_once(db, store):
    await _setup_run(db)
    for content in ("first", "second"):
        await db._persist_live_message(
            _live_message(content=content), session_id="s1", branch_progression_id="bp"
        )

    assert _rows(store, "SELECT content FROM messages WHERE id = 'm1'") == [('{"text": "second"}',)]
    assert _rows(store, "SELECT collection FROM progressions WHERE id = 'bp'") == [('["m1"]',)]


def _foreign_run(store: Path) -> None:
    """A session, branch and progression another principal's run wrote."""
    _foreign_rows(store)
    _execute(
        store,
        f"INSERT INTO sessions (id, created_at, updated_at, progression_id, {OWNER}) "
        "VALUES ('s-other', 0, 0, 'p-other', 'principal:b')",
    )
    _execute(
        store,
        f"INSERT INTO branches (id, created_at, session_id, progression_id, {OWNER}) "
        "VALUES ('b-other', 0, 's-other', 'p-other', 'principal:b')",
    )


@pytest.mark.parametrize(
    "link, value",
    [
        ("session_id", "s-other"),
        ("branch_progression_id", "p-other"),
        ("session_progression_id", "p-other"),
        ("system_branch_id", "b-other"),
    ],
)
async def test_a_live_message_links_only_rows_its_run_created(db, store, link, value):
    await _setup_run(db)
    _foreign_run(store)
    links = {"session_id": "s1", "branch_progression_id": "bp", "session_progression_id": "sp"}
    links[link] = value
    before = {
        table: _rows(store, f"SELECT * FROM {table} ORDER BY id")
        for table in ("sessions", "branches", "progressions", "messages")
    }

    with pytest.raises(StateCompatibilityRefused, match="was not created by this run"):
        await db._persist_live_message(_live_message(role="system"), **links)

    for table, rows in before.items():
        assert _rows(store, f"SELECT * FROM {table} ORDER BY id") == rows, table


@pytest.mark.parametrize(
    "table, row_id", [("sessions", "s1"), ("progressions", "bp"), ("branches", "b1")]
)
async def test_a_live_message_refuses_a_row_that_no_longer_carries_the_owner(
    db, store, table, row_id
):
    await _setup_run(db)
    # The managing application handed one of the run's rows to another principal.
    _execute(store, f"UPDATE {table} SET {OWNER} = 'principal:b' WHERE id = ?", (row_id,))
    before = {
        name: _rows(store, f"SELECT * FROM {name} ORDER BY id")
        for name in ("sessions", "branches", "progressions", "messages")
    }

    with pytest.raises(StateCompatibilityRefused, match="does not belong to 'principal:a'"):
        await db._persist_live_message(
            _live_message(role="system"),
            session_id="s1",
            branch_progression_id="bp",
            system_branch_id="b1",
        )

    for name, rows in before.items():
        assert _rows(store, f"SELECT * FROM {name} ORDER BY id") == rows, name


async def test_every_update_a_live_message_makes_carries_the_owner(db, store):
    """Beside the check before them, the updates themselves carry the owner predicate (D5)."""
    from sqlalchemy import event

    await _setup_run(db)
    updates = []

    def spy(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("UPDATE"):
            updates.append((statement, parameters))

    event.listen(db._engine.sync_engine, "before_cursor_execute", spy)
    try:
        await db._persist_live_message(
            _live_message(role="system"),
            session_id="s1",
            branch_progression_id="bp",
            session_progression_id="sp",
            system_branch_id="b1",
        )
    finally:
        event.remove(db._engine.sync_engine, "before_cursor_execute", spy)

    assert sorted(statement.split()[1] for statement, _ in updates) == [
        "branches",
        "progressions",
        "progressions",
        "sessions",
    ]
    for statement, parameters in updates:
        values = parameters.values() if isinstance(parameters, dict) else parameters
        assert f'"{OWNER}" = ' in statement, statement
        assert "principal:a" in values, statement


async def test_a_refused_setup_stops_the_run(store, monkeypatch):
    """A refusal at setup is raised, not turned into a run without persistence (ADR-0128 D6)."""
    from lionagi.cli import _runs
    from lionagi.session.branch import Branch

    profile = _profile(store, operations=_OPERATIONS - {"create_session"})
    handle = StateDB(store, compatibility=profile, owner="principal:a")
    await handle.open()

    async def compatibility_store(*args, **kwargs):
        return handle

    monkeypatch.setattr(_runs, "_open_shared_db", compatibility_store)
    degraded = []
    monkeypatch.setattr(_runs, "_record_persistence_degraded", lambda *a, **k: degraded.append(a))

    with pytest.raises(StateCompatibilityRefused, match="create_session refused"):
        await _runs.setup_agent_persist(Branch(), share_db=False)

    assert degraded == []
    assert _rows(store, "SELECT COUNT(*) FROM sessions") == [(0,)]


class _Message:
    """What a branch hands its live hook: a message with ``to_dict(mode=...)``."""

    def __init__(self, data: dict) -> None:
        self._data = data

    def to_dict(self, mode: str = "python") -> dict:
        return dict(self._data)


async def test_the_runs_live_hook_raises_a_refusal(db, store):
    """The CLI's live hook lets a refusal through instead of logging it (ADR-0128 D6)."""
    from lionagi.cli._runs import _make_message_handler

    await _setup_run(db)
    _execute(store, f"UPDATE sessions SET {OWNER} = 'principal:b' WHERE id = 's1'")
    queues: list = []
    on_message = _make_message_handler(db, "b1", "s1", "bp", "sp", message_retry_queues=queues)

    with pytest.raises(StateCompatibilityRefused, match="does not belong to 'principal:a'"):
        await on_message(_Message(_live_message()))

    assert queues[0].pending_count == 1
    assert _rows(store, "SELECT COUNT(*) FROM messages WHERE id = 'm1'") == [(0,)]


async def test_a_refused_teardown_fails_the_run_with_the_refusal(db, store):
    """Teardown neither swallows a refusal nor reads back a status it could not write."""
    from lionagi.cli._runs import _make_message_handler, teardown_persist
    from lionagi.hooks import route_message_persistence
    from lionagi.hooks.bus import HookPoint
    from lionagi.session.branch import Branch
    from lionagi.session.session import Session

    await _setup_run(db)
    _execute(store, f"UPDATE sessions SET {OWNER} = 'principal:b' WHERE id = 's1'")
    queues: list = []
    on_message = _make_message_handler(db, "b1", "s1", "bp", "sp", message_retry_queues=queues)
    with pytest.raises(StateCompatibilityRefused):
        await on_message(_Message(_live_message()))
    session, branch = Session(), Branch()
    hook = route_message_persistence(session, branch, on_message)
    ctx = {
        "db": db,
        "session_id": "s1",
        "session_prog_id": "sp",
        "branch": branch,
        "hook": hook,
        "message_retry_queues": queues,
        "shared_db_registered": False,
    }

    assert await teardown_persist(ctx, status="completed") == "failed"

    status, reason = ctx["terminal_reason"]
    assert status == "failed"
    assert "persist_live_message refused" in reason
    assert hook not in session.hooks.handlers_for(HookPoint.MESSAGE_ADD)
    assert _rows(store, "SELECT status FROM sessions WHERE id = 's1'") == [("running",)]


class _FlushQueue:
    def __init__(self, refuse: bool) -> None:
        self.refuse = refuse
        self.attempts = 0

    async def flush_final(self) -> bool:
        self.attempts += 1
        if self.refuse:
            raise StateCompatibilityRefused("persist_live_message", "owned@1", "refused")
        return True


async def test_teardown_flushes_every_queue_before_it_raises_a_refusal():
    from lionagi.cli._runs import _flush_pending_message_events

    first, second = _FlushQueue(refuse=True), _FlushQueue(refuse=False)

    with pytest.raises(StateCompatibilityRefused):
        await _flush_pending_message_events({"message_retry_queues": [first, second]})

    assert (first.attempts, second.attempts) == (1, 1)


class _RefusingFlushQueue:
    def __init__(self, target: str) -> None:
        self.target = target

    async def flush_final(self) -> bool:
        raise StateCompatibilityRefused("persist_live_message", self.target, "refused")


async def test_teardown_logs_every_refusal_after_the_one_it_raises(caplog):
    from lionagi.cli._runs import _flush_pending_message_events

    queues = [_RefusingFlushQueue("messages@1"), _RefusingFlushQueue("messages@2")]

    with pytest.raises(StateCompatibilityRefused, match="messages@1"):
        await _flush_pending_message_events({"message_retry_queues": queues})

    (record,) = [r for r in caplog.records if "messages@2" in r.getMessage()]
    assert record.levelname == "ERROR"


@pytest.mark.parametrize("entry", ["dependency-aware", "reactive", "stream"])
async def test_a_refused_write_in_a_flow_operation_stops_the_flow(entry):
    """A refusal in one operation is raised from the flow, instead of failing that operation while
    the others run on (ADR-0128 D6)."""
    import anyio

    from lionagi.operations import flow
    from lionagi.operations.builder import OperationGraphBuilder
    from lionagi.session.branch import Branch
    from lionagi.session.session import Session

    ran = []

    async def refused(**kw):
        raise StateCompatibilityRefused("persist_live_message", "messages@1", "refused")

    async def slow(**kw):
        await anyio.sleep(0.5)
        ran.append("slow")
        return "slow"

    session = Session()
    branch = Branch(name="root")
    session.include_branches(branch)
    session.default_branch = branch
    session.register_operation("refused", refused)
    session.register_operation("slow", slow)
    builder = OperationGraphBuilder()
    builder.add_operation("refused", depends_on=[])
    builder.add_operation("slow", depends_on=[])

    graph = builder.get_graph()

    with pytest.raises(StateCompatibilityRefused, match="messages@1"):
        if entry == "stream":
            async for _ in session.flow_stream(graph):
                pass
        else:
            await flow(session, graph, reactive=entry == "reactive")
    assert ran == []


async def test_a_refused_message_write_in_a_stream_run_keeps_its_type(monkeypatch):
    """A refusal is no provider failure: a stream run raises it as itself (ADR-0128 D6)."""
    import types
    from unittest.mock import AsyncMock

    from lionagi.operations.run.run import RunParam, run
    from lionagi.service.imodel import iModel
    from lionagi.service.types.stream_chunk import StreamChunk
    from lionagi.session.branch import Branch

    model = iModel(provider="openai", model="gpt-4.1-mini", api_key="test_key")
    model.endpoint = types.SimpleNamespace(
        is_cli=True, session_id=None, to_dict=lambda: {"type": "fake_cli", "session_id": None}
    )
    model.streaming_process_func = None

    async def create_event(**kw):
        return object()

    async def stream(api_call=None):
        yield StreamChunk(type="text", content="reply")

    model.create_event = create_event
    model.executor = types.SimpleNamespace(append=AsyncMock(), config={})
    model.stream = stream
    branch = Branch()
    branch.chat_model = model
    add_message = branch.msgs.a_add_message

    async def refusing(**kwargs):
        if "assistant_response" in kwargs:
            raise StateCompatibilityRefused("persist_live_message", "messages@1", "refused")
        return await add_message(**kwargs)

    monkeypatch.setattr(branch.msgs, "a_add_message", refusing)

    with pytest.raises(StateCompatibilityRefused, match="messages@1"):
        async for _ in run(branch, "do something", RunParam()):
            pass


async def test_a_refused_setup_finishes_the_runs_record(tmp_path, monkeypatch):
    """`li agent` stops at a refused setup, and its run.json reads `failed` with the refusal
    instead of `running` (ADR-0128 D6)."""
    import json
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import lionagi.cli._runs as runs_mod
    import lionagi.cli.agent as agent_mod
    from lionagi.cli._runs import allocate_run
    from lionagi.service.manager import iModelManager

    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", tmp_path / "state.db")
    monkeypatch.setattr(iModelManager, "shutdown", AsyncMock())
    monkeypatch.setattr(agent_mod, "build_chat_model", lambda *a, **kw: "codex/model")
    monkeypatch.setattr(agent_mod, "resolve_persisted_effort", lambda *a, **kw: None)
    monkeypatch.setattr(agent_mod, "save_last_branch_pointer", lambda *a, **kw: None)
    monkeypatch.setattr(
        agent_mod,
        "_provenance",
        SimpleNamespace(
            resolve_model_spec=lambda p, m: f"{p}/{m}", agent_definition_hash=lambda n: "abc"
        ),
    )
    monkeypatch.setattr(agent_mod, "resolve_artifact_contract", lambda **_: None)
    monkeypatch.setattr(runs_mod, "RUNS_ROOT", tmp_path / "runs")
    run = allocate_run(run_id="refused-setup")
    monkeypatch.setattr(agent_mod, "allocate_run", lambda: run)

    async def refused_setup(*args, **kwargs):
        raise StateCompatibilityRefused("create_session", "sessions@1", "refused")

    monkeypatch.setattr(agent_mod, "setup_agent_persist", refused_setup)

    from lionagi.cli.agent import _run_agent

    with pytest.raises(StateCompatibilityRefused):
        await _run_agent("codex/model", "do the thing")

    manifest = json.loads(run.manifest_path.read_text())
    assert manifest["status"] == "failed"
    assert manifest["ended_at"] is not None
    assert manifest["error"].startswith("The store refused a write: create_session refused")


@pytest.mark.parametrize(
    ("compatibility", "error"),
    [(None, None), ("profile", "The store refused a write: x")],
    ids=["native", "compatibility"],
)
async def test_an_orchestrate_run_json_names_the_reason_in_compatibility_mode(
    monkeypatch, compatibility, error
):
    from types import SimpleNamespace

    from lionagi.cli.orchestrate import _orchestration

    async def teardown(ctx, **kwargs):
        ctx["terminal_reason"] = ("failed", "The store refused a write: x")
        return "failed"

    monkeypatch.setattr(_orchestration, "teardown_persist", teardown)
    written: list = []
    env = SimpleNamespace(
        _live_persist={"db": SimpleNamespace(compatibility=compatibility)},
        cwd=None,
        _run_manifest={},
        run=SimpleNamespace(write_manifest=written.append),
    )

    assert await _orchestration.stop_live_persist(env, status="failed") == "failed"
    assert written[0]["status"] == "failed"
    assert written[0].get("error") == error


@pytest.mark.parametrize(
    ("compatibility", "recorded"),
    [(None, "completed"), ("profile", "failed")],
    ids=["native", "compatibility"],
)
async def test_an_operator_turn_records_the_status_its_teardown_decided_in_compatibility_mode(
    tmp_path, monkeypatch, compatibility, recorded
):
    """A turn the engine finished whose teardown the store refused reads `failed` in its run.json
    in compatibility mode (ADR-0128 D6); natively run.json keeps the turn's own status."""
    import json
    import time
    from types import SimpleNamespace

    import anyio

    import lionagi.cli._runs as runs_mod
    from lionagi.state.compat import Closure
    from lionagi.studio.operator.coordinator import OperatorCoordinator
    from lionagi.studio.operator.store import OperatorStore
    from lionagi.studio.operator.types import OperatorEngineEvent

    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", tmp_path / "state.db")
    monkeypatch.setattr(runs_mod, "RUNS_ROOT", tmp_path / "runs")

    profile = (
        CompatibilityProfile(
            name="operator-records",
            version=1,
            closure=Closure(tables=()),
            writes={},
            operations={"run"},
        )
        if compatibility == "profile"
        else None
    )

    async def setup(*args, **kwargs):
        return {"session_id": "s1", "db": SimpleNamespace(compatibility=profile)}

    async def teardown(ctx, *, status="completed", **kwargs):
        return "failed"

    monkeypatch.setattr(runs_mod, "setup_agent_persist", setup)
    monkeypatch.setattr(runs_mod, "teardown_agent_persist", teardown)

    class _Engine:
        async def _stream(self, _turn):
            yield OperatorEngineEvent(
                "text", {"content": "reply", "format": "plain", "role": "assistant"}
            )

        def stream(self, turn):
            return self._stream(turn)

    coordinator = OperatorCoordinator(
        store=OperatorStore(tmp_path / "state.db"), engine_factory=_Engine
    )
    await coordinator.startup()
    try:
        cid = (await coordinator.create_conversation())["conversation"]["id"]
        await coordinator.submit(
            cid,
            instruction="hello",
            context={"space": "mission", "route": "/", "filters": {}},
            expected_last_sequence=0,
        )
        deadline = time.monotonic() + 5
        status = None
        while time.monotonic() < deadline:
            manifests = list((tmp_path / "runs").glob("operator-*/run.json"))
            if manifests:
                status = json.loads(manifests[0].read_text()).get("status")
                if status != "running":
                    break
            await anyio.sleep(0.02)
    finally:
        await coordinator.shutdown()

    assert status == recorded


class _DriverWrite(StateDB):
    """Runs one statement with its parameters exactly as given, never re-spelled by the driver."""

    @_operation("create_progression")
    async def create_progression(self, progression_id: str, collection=None) -> None:
        async with self._tx() as conn:
            await conn.exec_driver_sql(self.statement, self.params)


async def _driver_write(store: Path, statement: str, params) -> None:
    handle = _DriverWrite(store, compatibility=_profile(store), owner="principal:a")
    handle.statement, handle.params = statement, params
    await handle.open()
    try:
        await handle.create_progression("p-probe")
    finally:
        await handle.close()


_NAMED_INSERT = "INSERT INTO progressions (id, created_at, collection, owner_principal_id) "


@pytest.mark.parametrize(
    "named", ["#id", ":id", "@id", "$id", "?1"], ids=["hash", "colon", "at", "dollar", "numbered"]
)
async def test_an_insert_whose_owner_slot_follows_a_named_parameter_is_refused(store, named):
    """SQLite numbers a named parameter like ``?``, so the ``?`` slot is not the first argument."""
    _foreign_rows(store)
    before = _rows(store, f"SELECT id, {OWNER} FROM progressions ORDER BY id")

    with pytest.raises(StateCompatibilityRefused, match="binds its values in a form"):
        await _driver_write(
            store, _NAMED_INSERT + f"VALUES ({named}, 1.0, '[]', ?)", ("principal:a", "principal:b")
        )

    assert _rows(store, f"SELECT id, {OWNER} FROM progressions ORDER BY id") == before
    assert before == [("p-other", "principal:b")]


async def test_an_update_whose_owner_term_follows_a_named_parameter_is_refused(store):
    """The ``?`` after ``#c`` is the second argument, not the first the guard reads it as."""
    _foreign_rows(store)
    before = _rows(store, f"SELECT id, content, {OWNER} FROM messages ORDER BY id")

    with pytest.raises(StateCompatibilityRefused, match="binds its values in a form"):
        await _driver_write(
            store,
            "UPDATE messages SET content = #c WHERE owner_principal_id = ?",
            ("principal:a", "principal:b"),
        )

    assert _rows(store, f"SELECT id, content, {OWNER} FROM messages ORDER BY id") == before
    assert before == [("m-other", "{}", "principal:b")]


async def test_an_update_that_binds_a_column_other_than_the_owner_is_refused(store):
    """A WHERE term on another column, even bound to the owner, does not restrict the owner."""
    _first_owned_message(store)

    with pytest.raises(StateCompatibilityRefused, match="does not restrict its rows"):
        await _driver_write(
            store, "UPDATE messages SET content = ? WHERE sender = ?", ("taken", "principal:a")
        )

    assert _rows(store, "SELECT content FROM messages WHERE id = 'm-sys'") == [("first",)]


async def test_an_executemany_whose_later_row_writes_another_owner_is_refused(store):
    with pytest.raises(StateCompatibilityRefused, match="other than the owner"):
        await _driver_write(
            store,
            _NAMED_INSERT + "VALUES (?, 1.0, '[]', ?)",
            [("p1", "principal:a"), ("p2", "principal:b")],
        )

    assert _rows(store, "SELECT COUNT(*) FROM progressions") == [(0,)]


async def test_an_executemany_bound_to_the_owner_in_every_row_is_admitted(store):
    await _driver_write(
        store,
        _NAMED_INSERT + "VALUES (?, 1.0, '[]', ?)",
        [("p1", "principal:a"), ("p2", "principal:a")],
    )

    assert _rows(store, f"SELECT id, {OWNER} FROM progressions ORDER BY id") == [
        ("p1", "principal:a"),
        ("p2", "principal:a"),
    ]


def _first_owned_message(store: Path) -> None:
    _execute(
        store,
        f"INSERT INTO messages (id, created_at, content, role, lion_class, {OWNER}) "
        "VALUES ('m-sys', 0, 'first', 'system', 1, 'principal:a')",
    )


async def test_a_trigger_named_begin_is_read_from_the_begin_after_its_table(tmp_path):
    """The trigger's name is the BEGIN keyword, so the first BEGIN is not the body's."""
    store = await _managed_store(tmp_path / "managed.db")
    _execute(
        store,
        "CREATE TRIGGER begin AFTER UPDATE ON messages BEGIN "
        "INSERT INTO progressions (id, created_at, collection) VALUES (NEW.id || '-x', 1.0, '[]'); "
        "END",
    )
    handle = StateDB(store, compatibility=_profile(store), owner="principal:a")

    with pytest.raises(StateCompatibilityRefused, match="trigger begin"):
        await handle.open()
    assert handle._engine is None


async def test_a_trigger_named_begin_that_copies_the_owner_is_admitted(tmp_path):
    store = await _managed_store(tmp_path / "managed.db")
    _execute(
        store,
        "CREATE TRIGGER begin AFTER UPDATE ON messages BEGIN "
        f"INSERT INTO progressions (id, created_at, collection, {OWNER}) "
        f"VALUES (NEW.id || '-x', 1.0, '[]', NEW.{OWNER}); END",
    )
    handle = StateDB(store, compatibility=_profile(store), owner="principal:a")
    await handle.open()
    await handle.close()


async def test_a_trigger_that_reads_old_in_an_insert_is_refused(tmp_path):
    """OLD is NULL in an INSERT trigger, so a trigger that copies it fails every insert."""
    store = await _managed_store(tmp_path / "managed.db", owner_sql="TEXT")
    _execute(
        store,
        "CREATE TRIGGER copy_old AFTER INSERT ON messages BEGIN "
        f"INSERT INTO progressions (id, created_at, collection, {OWNER}) "
        f"VALUES (NEW.id || '-p', 1.0, '[]', OLD.{OWNER}); END",
    )
    handle = StateDB(store, compatibility=_profile(store), owner="principal:a")

    with pytest.raises(StateCompatibilityRefused, match="trigger copy_old"):
        await handle.open()
    assert handle._engine is None


async def test_a_trigger_that_reads_new_in_a_delete_is_refused(tmp_path):
    """NEW is NULL in a DELETE trigger, so the copy it writes is no owner either."""
    store = await _managed_store(tmp_path / "managed.db", owner_sql="TEXT")
    _execute(
        store,
        "CREATE TRIGGER copy_new AFTER DELETE ON messages BEGIN "
        f"INSERT INTO progressions (id, created_at, collection, {OWNER}) "
        f"VALUES (OLD.id || '-p', 1.0, '[]', NEW.{OWNER}); END",
    )
    handle = StateDB(store, compatibility=_profile(store), owner="principal:a")

    with pytest.raises(StateCompatibilityRefused, match="trigger copy_new"):
        await handle.open()
    assert handle._engine is None


_NOCASE_OWNER = "TEXT NOT NULL DEFAULT 'principal:default' COLLATE NOCASE"


async def test_an_owner_column_that_does_not_compare_under_binary_is_refused(tmp_path):
    """A NOCASE owner compares a case variant as the owner, so the WHERE term does not hold."""
    store = await _managed_store(tmp_path / "managed.db", owner_sql=_NOCASE_OWNER)
    before = _rows(store, "SELECT COUNT(*) FROM messages")
    handle = StateDB(store, compatibility=_profile(store), owner="principal:a")

    with pytest.raises(StateCompatibilityRefused, match="NOCASE") as caught:
        await handle.open()

    assert "collation" in caught.value.reason.lower()
    assert handle._engine is None
    assert _rows(store, "SELECT COUNT(*) FROM messages") == before


async def test_an_owner_column_that_compares_under_binary_is_admitted(tmp_path):
    store = await _managed_store(tmp_path / "managed.db", owner_sql="TEXT COLLATE BINARY")
    handle = StateDB(store, compatibility=_profile(store), owner="principal:a")
    await handle.open()
    await handle.close()


async def test_a_trigger_whose_owner_term_is_reversed_is_admitted(tmp_path):
    store = await _managed_store(tmp_path / "managed.db")
    _execute(
        store,
        "CREATE TRIGGER rev AFTER INSERT ON messages BEGIN "
        "UPDATE progressions SET collection = '[]' WHERE id = NEW.sender "
        f"AND NEW.{OWNER} = {OWNER}; END",
    )
    handle = StateDB(store, compatibility=_profile(store), owner="principal:a")
    await handle.open()
    await handle.close()


async def test_a_trigger_whose_owner_term_is_parenthesised_is_admitted(tmp_path):
    store = await _managed_store(tmp_path / "managed.db")
    _execute(
        store,
        "CREATE TRIGGER par AFTER INSERT ON messages BEGIN "
        "UPDATE progressions SET collection = '[]' WHERE id = NEW.sender "
        f"AND ({OWNER} = NEW.{OWNER}); END",
    )
    handle = StateDB(store, compatibility=_profile(store), owner="principal:a")
    await handle.open()
    await handle.close()
