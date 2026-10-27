# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Worker evidence schema, generation writes and versioned store compatibility."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

import lionagi.state.db as state_db
from lionagi.state.compat import StateCompatibilityRefused
from lionagi.state.db import SCHEMA_VERSION, SchemaTooNewError, StateDB
from lionagi.state.reasons import RunReasons, SessionReasons
from lionagi.state.schema_migrations import MIGRATION_COLUMNS, MIGRATION_INDEXES
from tests.state.test_compatibility_owner_ops import _manage, _profile, _session

_V4_SCHEMA = Path(__file__).parents[1] / "fixtures" / "state_schema_v4.sql"
_GENERATION = "2c0ec3f5-241b-4b31-b371-99598dd089d1"
_NEXT_GENERATION = "39268e73-3b2f-467e-9fd2-a805e9c95212"
_SPAWN = "3eab41d9-f7c9-46af-a038-53e13672fc07"


def _dump(path: Path) -> list[str]:
    with closing(sqlite3.connect(path)) as connection:
        return list(connection.iterdump())


def _version_four_store(path: Path, *, legacy: bool, generation: str | None = None) -> None:
    schema = _V4_SCHEMA.read_text()
    assert "VALUES ('version', '4');" in schema
    assert "cli_worker_generation" not in schema and "cli_worker_spawns" not in schema
    if legacy:
        current = "  status          TEXT,\n"
        assert schema.count(current) == 1
        schema = schema.replace(
            current,
            "  status          TEXT CHECK(status IS NULL OR status IN "
            "('running', 'completed', 'failed', 'aborted')),\n",
        )
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(schema)
        connection.execute("INSERT INTO progressions VALUES ('old-pool', 1.0, '[]')")
        connection.execute(
            "INSERT INTO sessions (id, created_at, progression_id, updated_at, status) "
            "VALUES ('old-session', 1.0, 'old-pool', 2.0, 'running')"
        )
        if generation is not None:
            connection.execute("ALTER TABLE sessions ADD COLUMN cli_worker_generation TEXT")
            connection.execute(
                "UPDATE sessions SET cli_worker_generation = ? WHERE id = 'old-session'",
                (generation,),
            )
        connection.commit()


def _worker_row(**changes) -> dict:
    return {
        "spawn_id": _SPAWN,
        "session_id": "removed-session",
        "generation_id": _GENERATION,
        "run_id": "removed-run",
        "engine_run_id": "removed-engine",
        "branch_id": "removed-branch",
        "pid": 42,
        "pgid": 42,
        "pid_create_time": 10.0,
        "pid_host": "test-host",
        "pid_boot_time": 1.0,
        "process_identity_mode": "local",
        "recorded_at": 11.0,
        "confirmed_empty_at": 12.0,
        **changes,
    }


def _insert_worker(connection: sqlite3.Connection, values: dict) -> None:
    columns = ", ".join(values)
    placeholders = ", ".join("?" for _ in values)
    connection.execute(
        f"INSERT INTO cli_worker_spawns ({columns}) VALUES ({placeholders})",  # noqa: S608
        tuple(values.values()),
    )


@pytest.mark.asyncio(loop_scope="function")
@pytest.mark.parametrize(
    ("legacy", "generation"),
    [(False, None), (True, None), (True, _GENERATION)],
    ids=["version-four", "legacy-check", "reconciled-legacy-check"],
)
async def test_version_four_upgrade_preserves_unknown_and_recorded_generations(
    tmp_path: Path, legacy: bool, generation: str | None
) -> None:
    path = tmp_path / "old.db"
    _version_four_store(path, legacy=legacy, generation=generation)
    async with StateDB(path) as store:
        row = await store.get_session("old-session")
        assert row is not None and row["status"] == "running"
        assert row.get("cli_worker_generation") == generation
        assert await store.schema_version() == "5"
    with closing(sqlite3.connect(path)) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(sessions)")}
        assert "cli_worker_generation" in columns
        assert connection.execute("SELECT count(*) FROM cli_worker_spawns").fetchone() == (0,)
        sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'sessions'"
        ).fetchone()[0]
        assert "'running', 'completed', 'failed', 'aborted'" not in sql


@pytest.mark.asyncio(loop_scope="function")
async def test_worker_schema_matches_raw_creation_and_native_metadata(tmp_path: Path) -> None:
    raw, native = tmp_path / "raw.db", tmp_path / "native.db"
    with closing(sqlite3.connect(raw)) as connection:
        connection.executescript(state_db._SCHEMA_PATH.read_text())
    async with StateDB(native):
        pass
    definitions = []
    for path in (raw, native):
        with closing(sqlite3.connect(path)) as connection:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
            assert "cli_worker_spawns" in tables, "worker identities require durable storage"
            columns = list(connection.execute("PRAGMA table_info(cli_worker_spawns)"))
            assert len(columns) == 14
            info = {row[1]: row for row in columns}
            assert info["spawn_id"][2:4] == ("TEXT", 1) and info["spawn_id"][5] == 1
            assert {name for name, row in info.items() if row[3] == 0} == {
                "run_id",
                "engine_run_id",
                "branch_id",
                "confirmed_empty_at",
            }
            assert list(connection.execute("PRAGMA foreign_key_list(cli_worker_spawns)")) == []
            assert [
                row[2]
                for row in connection.execute("PRAGMA index_info(idx_cli_worker_spawns_session)")
            ] == ["session_id", "confirmed_empty_at", "generation_id"]
            assert "idx_cli_worker_spawns_session" in {
                row[1] for row in connection.execute("PRAGMA index_list(cli_worker_spawns)")
            }
            generation = next(
                row
                for row in connection.execute("PRAGMA table_info(sessions)")
                if row[1] == "cli_worker_generation"
            )
            assert generation[2:5] == ("TEXT", 0, None)
            definitions.append(
                [
                    (
                        row[1],
                        "REAL"
                        if row[2] in {"FLOAT", "REAL", "DOUBLE", "DOUBLE PRECISION"}
                        else row[2],
                        row[3],
                        row[4],
                        row[5],
                    )
                    for row in columns
                ]
            )
            connection.execute("PRAGMA foreign_keys = ON")
            _insert_worker(connection, _worker_row())
            assert connection.execute(
                "SELECT session_id, branch_id, confirmed_empty_at FROM cli_worker_spawns"
            ).fetchone() == ("removed-session", "removed-branch", 12.0)
    assert definitions[0] == definitions[1]
    assert dict(MIGRATION_COLUMNS["sessions"]).get("cli_worker_generation") == "TEXT"
    index = (
        "CREATE INDEX IF NOT EXISTS idx_cli_worker_spawns_session "
        "ON cli_worker_spawns(session_id, confirmed_empty_at, generation_id)"
    )
    assert all(index in MIGRATION_INDEXES[dialect] for dialect in ("sqlite", "postgresql"))
    assert SCHEMA_VERSION == "5"


@pytest.mark.asyncio(loop_scope="function")
@pytest.mark.parametrize("schema_source", ["raw", "native"])
@pytest.mark.parametrize(
    ("changes", "constraint"),
    [
        ({"pid": 1, "pgid": 1}, "ck_cli_worker_spawns_process_group"),
        ({"pgid": 43}, "ck_cli_worker_spawns_process_group"),
        ({"process_identity_mode": "remote"}, "ck_cli_worker_spawns_identity_mode"),
        ({"spawn_id": None}, "NOT NULL"),
    ],
    ids=["reserved-pid", "different-group", "unsupported-mode", "missing-attempt"],
)
async def test_worker_identity_constraints_refuse_invalid_rows(
    tmp_path, changes, constraint, schema_source
) -> None:
    path = tmp_path / "schema.db"
    if schema_source == "raw":
        with closing(sqlite3.connect(path)) as connection:
            connection.executescript(state_db._SCHEMA_PATH.read_text())
    else:
        async with StateDB(path):
            pass
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'cli_worker_spawns'"
        ).fetchone(), "worker identities require durable storage"
        with pytest.raises(sqlite3.IntegrityError, match=constraint):
            _insert_worker(connection, _worker_row(**changes))
        assert connection.execute("SELECT count(*) FROM cli_worker_spawns").fetchone() == (0,)


@pytest.mark.asyncio(loop_scope="function")
@pytest.mark.parametrize("generation", [None, _GENERATION.upper()], ids=["unknown", "recorded"])
async def test_native_session_creation_keeps_the_optional_generation(tmp_path, generation) -> None:
    async with StateDB(tmp_path / "session.db") as store:
        await store.create_progression("pool")
        await store.create_session(
            {
                "id": "session",
                "progression_id": "pool",
                "status": "running",
                "cli_worker_generation": generation,
            }
        )
        row = await store.get_session("session")
        assert row is not None
        assert row.get("cli_worker_generation") == (_GENERATION if generation else None)


@pytest.mark.asyncio(loop_scope="function")
@pytest.mark.parametrize("entry", ["create", "update", "status"])
@pytest.mark.parametrize("value", [True, "not-a-uuid"], ids=["boolean", "malformed"])
async def test_invalid_generation_refuses_without_session_or_history_writes(tmp_path, entry, value):
    path = tmp_path / "session.db"
    async with StateDB(path) as store:
        await store.create_progression("pool")
        await store.create_session({"id": "session", "progression_id": "pool", "status": "running"})
        before = _dump(path)
        with pytest.raises(ValueError, match="cli_worker_generation"):
            if entry == "create":
                await store.create_session(
                    {
                        "id": "other",
                        "progression_id": "pool",
                        "status": "running",
                        "cli_worker_generation": value,
                    }
                )
            elif entry == "update":
                await store.update_session("session", cli_worker_generation=value)
            else:
                await store.update_status(
                    "session",
                    "session",
                    new_status="running",
                    reason_code=RunReasons.STARTED_OK,
                    extra_fields={"cli_worker_generation": value},
                )
        assert _dump(path) == before


@pytest.mark.asyncio(loop_scope="function")
@pytest.mark.parametrize("entry", ["update", "status"])
async def test_resume_status_and_new_generation_land_in_one_write(tmp_path, entry):
    path = tmp_path / "session.db"
    async with StateDB(path) as store:
        await store.create_progression("pool")
        await store.create_session(
            {
                "id": "session",
                "progression_id": "pool",
                "status": "completed",
                "cli_worker_generation": _GENERATION,
            }
        )
        with closing(sqlite3.connect(path)) as connection:
            assert (
                connection.execute(
                    "SELECT previous_status, status FROM status_transitions WHERE entity_id='session'"
                ).fetchall()
                == []
            )
            connection.execute(
                "CREATE TRIGGER require_atomic_reopen BEFORE UPDATE OF status, cli_worker_generation ON sessions "
                "WHEN NOT (OLD.status = 'completed' AND NEW.status = 'running' "
                f"AND OLD.cli_worker_generation = '{_GENERATION}' "
                f"AND NEW.cli_worker_generation = '{_NEXT_GENERATION}') "
                "BEGIN SELECT RAISE(ABORT, 'status and generation must reopen together'); END"
            )
            connection.commit()
        options = {
            "reason_code": SessionReasons.REOPENED_BY_RESUME,
            "override": True,
            "override_actor": "resume",
            "override_justification": "start a new execution",
        }
        if entry == "update":
            await store.update_session(
                "session",
                status="running",
                cli_worker_generation=_NEXT_GENERATION.upper(),
                **options,
            )
        else:
            extra = {"cli_worker_generation": _NEXT_GENERATION.upper()}
            assert await store.update_status(
                "session", "session", new_status="running", extra_fields=extra, **options
            )
            assert extra == {"cli_worker_generation": _NEXT_GENERATION.upper()}
        row = await store.get_session("session")
        assert (row["status"], row["cli_worker_generation"]) == ("running", _NEXT_GENERATION)
        with closing(sqlite3.connect(path)) as connection:
            assert connection.execute(
                "SELECT previous_status, status FROM status_transitions WHERE entity_id='session' ORDER BY created_at"
            ).fetchall() == [("completed", "running")]


@pytest.mark.asyncio(loop_scope="function")
@pytest.mark.parametrize("supplied", [False, True], ids=["absent", "explicit-null"])
async def test_legacy_compatibility_creation_does_not_write_a_generation_column(tmp_path, supplied):
    path = tmp_path / "managed.db"
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(_V4_SCHEMA.read_text())
    _manage(path)
    async with StateDB(path, compatibility=_profile(path), owner="principal:a") as store:
        await store.create_progression("sp")
        fields = {"cli_worker_generation": None} if supplied else {}
        await store.create_session(_session(**fields))
    with closing(sqlite3.connect(path)) as connection:
        assert "cli_worker_generation" not in {
            row[1] for row in connection.execute("PRAGMA table_info(sessions)")
        }
        assert connection.execute("SELECT id, status FROM sessions").fetchall() == [
            ("s1", "running")
        ]
        assert connection.execute(
            "SELECT value FROM schema_meta WHERE key='version'"
        ).fetchone() == ("900",)


@pytest.mark.asyncio(loop_scope="function")
async def test_compatibility_generation_create_refuses_before_any_write(tmp_path):
    path = tmp_path / "managed.db"
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(_V4_SCHEMA.read_text())
    _manage(path)
    async with StateDB(path, compatibility=_profile(path), owner="principal:a") as store:
        await store.create_progression("sp")
        before = _dump(path)
        with pytest.raises(StateCompatibilityRefused, match="cli_worker_generation"):
            await store.create_session(_session(cli_worker_generation=_GENERATION))
        assert _dump(path) == before


@pytest.mark.asyncio(loop_scope="function")
async def test_version_four_writer_refuses_the_version_five_store(tmp_path, monkeypatch):
    path = tmp_path / "new.db"
    async with StateDB(path):
        pass
    before = _dump(path)
    monkeypatch.setattr(state_db, "SCHEMA_VERSION", "4")
    store = StateDB(path)
    try:
        with pytest.raises(SchemaTooNewError, match="schema version 5"):
            await store.open()
    finally:
        await store.close()
    assert _dump(path) == before
