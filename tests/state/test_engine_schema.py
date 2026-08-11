# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for engine.py URL utilities and schema_meta.py MetaData parity."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

from lionagi.state.engine import (
    dialect_of,
    make_engine,
    make_readonly_engine,
    mask_db_url,
    normalize_state_db_url,
)
from lionagi.state.schema_meta import metadata

# normalize_state_db_url


def test_normalize_none_returns_sqlite_default():
    url = normalize_state_db_url(None)
    assert url.startswith("sqlite+aiosqlite:///")
    assert "state.db" in url


def test_normalize_path_object():
    p = Path("/tmp/test_lion.db")
    url = normalize_state_db_url(p)
    assert url.startswith("sqlite+aiosqlite:///")
    assert "test_lion.db" in url
    # The path portion must be absolute.
    path_part = url[len("sqlite+aiosqlite:///") :]
    assert Path(path_part).is_absolute()


def test_normalize_bare_string_path():
    url = normalize_state_db_url("/tmp/foo.db")
    assert url.startswith("sqlite+aiosqlite:///")
    assert "foo.db" in url


def test_normalize_bare_string_relative():
    url = normalize_state_db_url("relative/path.db")
    assert url.startswith("sqlite+aiosqlite:///")
    assert "path.db" in url
    # Must be absolute after normalization.
    stripped = url[len("sqlite+aiosqlite:///") :]
    assert Path(stripped).is_absolute()


def test_normalize_sqlite_plain_scheme():
    # Four-slash (absolute) and three-slash (relative) must preserve slash count;
    # a regression re-introduces the "sqlite+aiosqlite://////" corruption.
    assert normalize_state_db_url("sqlite:////tmp/x.db") == "sqlite+aiosqlite:////tmp/x.db"
    assert normalize_state_db_url("sqlite:///rel.db") == "sqlite+aiosqlite:///rel.db"


def test_normalize_sqlite_already_qualified():
    original = "sqlite+aiosqlite:////tmp/y.db"
    assert normalize_state_db_url(original) == original


def test_normalize_postgres_short_scheme():
    url = normalize_state_db_url("postgres://user:pw@host/db")
    assert url.startswith("postgresql+asyncpg://")


def test_normalize_postgresql_scheme():
    url = normalize_state_db_url("postgresql://user:pw@host/db")
    assert url.startswith("postgresql+asyncpg://")


def test_normalize_postgresql_asyncpg_already_qualified():
    original = "postgresql+asyncpg://user:pw@host/db"
    assert normalize_state_db_url(original) == original


# mask_db_url


def test_mask_no_password():
    url = "sqlite+aiosqlite:////tmp/state.db"
    assert mask_db_url(url) == url


def test_mask_password_replaced():
    url = "postgresql+asyncpg://user:supersecretpassword@localhost/db"
    masked = mask_db_url(url)
    assert "supersecretpassword" not in masked  # full secret never present
    assert "supers" in masked  # first-6 prefix shown for long secrets
    assert "[19 chars]" in masked


def test_mask_medium_password():
    # 10-char secret is below the reveal threshold → no prefix, length only.
    url = "postgresql+asyncpg://u:0123456789@host/db"
    masked = mask_db_url(url)
    assert "0123456789" not in masked
    assert "012345" not in masked  # no prefix revealed below threshold
    assert "[10 chars]" in masked


def test_mask_short_password():
    url = "postgresql+asyncpg://admin:abc@host/db"
    masked = mask_db_url(url)
    assert "abc" not in masked  # short secret must not be exposed at all
    assert "[3 chars]" in masked


# dialect_of


def test_dialect_sqlite():
    assert dialect_of("sqlite+aiosqlite:////tmp/x.db") == "sqlite"
    assert dialect_of("sqlite:///x.db") == "sqlite"


def test_dialect_postgresql():
    assert dialect_of("postgresql+asyncpg://host/db") == "postgresql"
    assert dialect_of("postgres://host/db") == "postgresql"


# make_engine (SQLite only — sync verification)


def test_make_engine_sqlite_creates_engine():
    url = "sqlite+aiosqlite:///:memory:"
    engine = make_engine(url)
    assert engine is not None
    assert "sqlite" in str(engine.url)
    # Cleanup.
    import asyncio

    asyncio.run(engine.dispose())


# make_readonly_engine


def test_make_readonly_engine_rejects_postgres():
    with pytest.raises(ValueError, match="only supports sqlite"):
        make_readonly_engine("postgresql+asyncpg://host/db")


def test_make_readonly_engine_rejects_memory():
    with pytest.raises(ValueError, match=":memory:"):
        make_readonly_engine("sqlite+aiosqlite:///:memory:")


async def test_make_readonly_engine_can_read_existing_file(tmp_path):
    db_file = tmp_path / "ro_test.db"
    write_engine = create_async_engine(f"sqlite+aiosqlite:///{db_file}", echo=False)
    async with write_engine.begin() as conn:
        await conn.execute(sa.text("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)"))
        await conn.execute(sa.text("INSERT INTO t (v) VALUES ('hello')"))
    await write_engine.dispose()

    ro_engine = make_readonly_engine(f"sqlite+aiosqlite:///{db_file}")
    try:
        async with ro_engine.connect() as conn:
            rows = (await conn.execute(sa.text("SELECT v FROM t"))).all()
            assert [r[0] for r in rows] == ["hello"]
    finally:
        await ro_engine.dispose()


async def test_make_readonly_engine_rejects_writes(tmp_path):
    db_file = tmp_path / "ro_write_test.db"
    write_engine = create_async_engine(f"sqlite+aiosqlite:///{db_file}", echo=False)
    async with write_engine.begin() as conn:
        await conn.execute(sa.text("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)"))
    await write_engine.dispose()

    ro_engine = make_readonly_engine(f"sqlite+aiosqlite:///{db_file}")
    try:
        with pytest.raises(Exception, match="readonly|read-only"):
            async with ro_engine.begin() as conn:
                await conn.execute(sa.text("INSERT INTO t (v) VALUES ('nope')"))
    finally:
        await ro_engine.dispose()


# Schema-parity: MetaData vs schema.sql (SQLite leg, always runs)

ALL_TABLES = {
    "schema_meta",
    "message_types",
    "messages",
    "progressions",
    "projects",
    "sessions",
    "branches",
    "definitions",
    "shows",
    "plays",
    "teams",
    "team_messages",
    "invocations",
    "schedules",
    "schedule_runs",
    "admin_events",
    "artifacts",
    "status_transitions",
    "terminal_deliveries",
    "session_signals",
    "engine_runs",
    "engine_defs",
    "workflow_defs",
    "session_controls",
    "dispatch_outbox",
    "run_tags",
    "approvals",
    "approval_evidence",
    "workers",
    "attention_dispositions",
    "attention_disposition_history",
    "attention_disposition_revisions",
}


@pytest.fixture
async def sqlite_meta_engine(tmp_path):
    """AsyncEngine pointing at a fresh SQLite file for MetaData.create_all."""
    db_file = tmp_path / "meta_test.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_file}", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield engine
    await engine.dispose()


async def test_metadata_creates_all_tables(sqlite_meta_engine):
    """metadata.create_all() builds every expected table in SQLite."""
    async with sqlite_meta_engine.connect() as conn:
        tables = await conn.run_sync(lambda sync_conn: set(sa.inspect(sync_conn).get_table_names()))
    assert ALL_TABLES == tables


async def test_metadata_column_parity_vs_schema_sql(tmp_path, sqlite_meta_engine):
    """Column sets from MetaData match column sets from real schema.sql."""
    from lionagi.state.db import _SCHEMA_PATH  # existing constant in db.py

    # Build a second SQLite DB from the raw schema.sql script.
    raw_db = tmp_path / "raw_schema.db"
    schema_text = _SCHEMA_PATH.read_text()
    conn_raw = sqlite3.connect(str(raw_db))
    # Strip PRAGMAs so executescript doesn't complain about schema state.
    lines = [ln for ln in schema_text.splitlines() if not ln.strip().upper().startswith("PRAGMA")]
    conn_raw.executescript("\n".join(lines))
    conn_raw.commit()

    # Collect columns from the raw DB.
    raw_cols: dict[str, set[str]] = {}
    cursor = conn_raw.cursor()
    for table in ALL_TABLES:
        cursor.execute(f"PRAGMA table_info({table})")  # noqa: S608
        raw_cols[table] = {row[1] for row in cursor.fetchall()}
    conn_raw.close()

    # Collect columns from the MetaData DB.
    def _get_meta_cols(sync_conn):
        insp = sa.inspect(sync_conn)
        return {t: {c["name"] for c in insp.get_columns(t)} for t in ALL_TABLES}

    async with sqlite_meta_engine.connect() as conn:
        meta_cols = await conn.run_sync(_get_meta_cols)

    mismatches: list[str] = []
    for table in sorted(ALL_TABLES):
        only_raw = raw_cols[table] - meta_cols[table]
        only_meta = meta_cols[table] - raw_cols[table]
        if only_raw or only_meta:
            mismatches.append(
                f"{table}: only_in_schema_sql={only_raw!r} only_in_metadata={only_meta!r}"
            )

    assert not mismatches, "Column-set mismatch:\n" + "\n".join(mismatches)


async def test_branches_index_matches_runtime_migration_definition(tmp_path, sqlite_meta_engine):
    """The provisioned branches session index matches the runtime migration.

    Target 3 (perf-baseline ranked_targets.md): idx_branches_session used to
    be a bare (session_id) index, forcing a temp B-tree sort for the
    session-detail branch listing (ORDER BY created_at). schema.sql and
    schema_meta.py must both declare the same composite/covering
    (session_id, created_at) index as schema_migrations.MIGRATION_INDEXES,
    with no bare idx_branches_session left in either provisioning definition
    and no table/column change.
    """
    import re

    from lionagi.state.db import _SCHEMA_PATH
    from lionagi.state.schema_migrations import MIGRATION_INDEXES

    # Raw schema.sql indexes.
    raw_db = tmp_path / "raw_branches_idx.db"
    schema_text = _SCHEMA_PATH.read_text()
    lines = [ln for ln in schema_text.splitlines() if not ln.strip().upper().startswith("PRAGMA")]
    conn_raw = sqlite3.connect(str(raw_db))
    conn_raw.executescript("\n".join(lines))
    conn_raw.commit()
    cursor = conn_raw.cursor()
    cursor.execute("PRAGMA index_list(branches)")
    raw_index_names = {row[1] for row in cursor.fetchall()}
    cursor.execute("PRAGMA index_info(idx_branches_session_created)")
    raw_composite_cols = [row[2] for row in cursor.fetchall()]
    conn_raw.close()

    assert "idx_branches_session_created" in raw_index_names
    assert "idx_branches_session" not in raw_index_names
    assert raw_composite_cols == ["session_id", "created_at"]

    # SQLAlchemy metadata indexes.
    def _get_meta_indexes(sync_conn):
        insp = sa.inspect(sync_conn)
        return {idx["name"]: idx["column_names"] for idx in insp.get_indexes("branches")}

    async with sqlite_meta_engine.connect() as conn:
        meta_indexes = await conn.run_sync(_get_meta_indexes)

    assert "idx_branches_session_created" in meta_indexes
    assert "idx_branches_session" not in meta_indexes
    assert meta_indexes["idx_branches_session_created"] == raw_composite_cols

    # Runtime migration definition (schema_migrations.MIGRATION_INDEXES) must
    # declare the identical column order.
    (composite_sql,) = (
        sql for sql in MIGRATION_INDEXES["sqlite"] if "idx_branches_session_created" in sql
    )
    migration_cols = [
        c.strip() for c in re.search(r"branches\(([^)]+)\)", composite_sql).group(1).split(",")
    ]
    assert migration_cols == raw_composite_cols


async def test_metadata_check_constraint_parity_vs_schema_sql(tmp_path, sqlite_meta_engine):
    """Enum CHECK value-sets from MetaData match those from real schema.sql."""
    import re

    from lionagi.state.db import _SCHEMA_PATH

    in_re = re.compile(r"(\w+)\s+IN\s*\(([^)]+)\)", re.IGNORECASE)

    def _norm(vals):
        return frozenset(p.strip().strip("'").strip() for p in vals.split(",") if p.strip())

    def _checks(rows):
        out = {}
        for name, sql in rows:
            if not sql:
                continue
            for col, vals in in_re.findall(sql):
                out[(name, col)] = _norm(vals)
        return out

    raw_db = tmp_path / "raw_checks.db"
    schema_text = _SCHEMA_PATH.read_text()
    lines = [ln for ln in schema_text.splitlines() if not ln.strip().upper().startswith("PRAGMA")]
    conn_raw = sqlite3.connect(str(raw_db))
    conn_raw.executescript("\n".join(lines))
    raw_checks = _checks(
        conn_raw.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL"
        ).fetchall()
    )
    conn_raw.close()

    def _meta_rows(sync_conn):
        return list(
            sync_conn.exec_driver_sql(
                "SELECT name, sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL"
            )
        )

    async with sqlite_meta_engine.connect() as conn:
        meta_checks = _checks(await conn.run_sync(_meta_rows))

    # Guard against the regex silently extracting nothing (would make equality trivial).
    assert len(raw_checks) == 20, f"expected 20 enum CHECK columns, got {len(raw_checks)}"
    drift = {
        k: {
            "schema_sql": sorted(raw_checks.get(k) or []),
            "metadata": sorted(meta_checks.get(k) or []),
        }
        for k in set(raw_checks) | set(meta_checks)
        if raw_checks.get(k) != meta_checks.get(k)
    }
    assert not drift, f"CHECK enum drift:\n{drift}"


async def test_python_enum_sets_match_schema_sql_checks(tmp_path):
    """The Python vocabularies the writers validate against carry the same values
    as the CHECK constraints in schema.sql.

    The metadata-parity guard above covers the SQLAlchemy mirror only, so a value
    added to schema.sql and the mirror alone would be legal in the database and
    still refused by ``create_session`` — accepted by the store, rejected by the
    only code that writes to it.
    """
    import re
    import sqlite3

    from lionagi.state.db import _INVOCATION_KINDS, _SCHEMA_PATH, _SOURCE_KINDS

    raw_db = tmp_path / "python_enum_parity.db"
    schema_text = _SCHEMA_PATH.read_text()
    lines = [ln for ln in schema_text.splitlines() if not ln.strip().upper().startswith("PRAGMA")]
    conn = sqlite3.connect(str(raw_db))
    conn.executescript("\n".join(lines))
    sessions_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='sessions'"
    ).fetchone()[0]
    conn.close()

    in_re = re.compile(r"(\w+)\s+IN\s*\(([^)]+)\)", re.IGNORECASE)
    found = {
        col: frozenset(p.strip().strip("'").strip() for p in vals.split(",") if p.strip())
        for col, vals in in_re.findall(sessions_sql)
    }

    expected = {"source_kind": _SOURCE_KINDS, "invocation_kind": _INVOCATION_KINDS}
    # Fail closed: a column whose CHECK the regex missed must not read as parity.
    assert set(expected) <= set(found), f"no CHECK parsed for {set(expected) - set(found)}"
    assert {col: found[col] for col in expected} == expected


async def test_metadata_unique_enforcement_present(sqlite_meta_engine):
    """The three natural-key uniqueness rules are enforced (constraint or index)."""
    expected = {
        ("definitions", ("kind", "name", "version")),
        ("plays", ("show_id", "name")),
        ("session_signals", ("session_id", "seq")),
    }

    def _unique_keys(sync_conn):
        insp = sa.inspect(sync_conn)
        found = set()
        for table in {"definitions", "plays", "session_signals"}:
            for uc in insp.get_unique_constraints(table):
                found.add((table, tuple(uc["column_names"])))
            for ix in insp.get_indexes(table):
                if ix.get("unique"):
                    found.add((table, tuple(ix["column_names"])))
        return found

    async with sqlite_meta_engine.connect() as conn:
        found = await conn.run_sync(_unique_keys)

    for key in expected:
        assert key in found, f"missing unique enforcement: {key}; found={found}"


# Postgres leg (pg_url fixture: testcontainers, or LIONAGI_TEST_PG_URL)


async def test_metadata_create_all_postgres(pg_url):
    """metadata.create_all() succeeds against a live Postgres instance."""
    engine = create_async_engine(pg_url, echo=False)
    try:
        # Use an isolated schema to avoid polluting the default public schema.
        test_schema = "lionagi_test_pass1"
        async with engine.begin() as conn:
            await conn.execute(sa.text(f"CREATE SCHEMA IF NOT EXISTS {test_schema}"))

        # Reflect our unscoped metadata into the test schema for creation.
        async with engine.begin() as conn:
            await conn.run_sync(lambda sync_conn: _create_in_schema(sync_conn, test_schema))

        # Verify tables exist.
        async with engine.connect() as conn:
            tables = await conn.run_sync(
                lambda sync_conn: set(sa.inspect(sync_conn).get_table_names(schema=test_schema))
            )

        assert ALL_TABLES == tables, f"Missing: {ALL_TABLES - tables}"

    finally:
        # Drop test schema and all its tables.
        async with engine.begin() as conn:
            await conn.execute(sa.text(f"DROP SCHEMA IF EXISTS {test_schema} CASCADE"))
        await engine.dispose()


def _create_in_schema(sync_conn, schema_name: str) -> None:
    """Create all MetaData tables in *schema_name* on a sync connection."""
    from lionagi.state.schema_meta import metadata as _meta

    # Build a schema-scoped MetaData by cloning table defs with the target schema.
    scoped = sa.MetaData(schema=schema_name)
    for table in _meta.sorted_tables:
        table.tometadata(scoped)
    scoped.create_all(sync_conn, checkfirst=True)


# WAL-reset precondition


@pytest.mark.parametrize(
    ("version_info", "fixed"),
    [
        ((3, 6, 23), True),  # predates WAL; journal_mode=WAL is not honoured
        ((3, 7, 0), False),  # first WAL release, first affected release
        ((3, 44, 5), False),
        ((3, 44, 6), True),  # backport branch
        ((3, 45, 0), False),  # later than the 3.44 backport, still unfixed
        ((3, 46, 0), False),
        ((3, 50, 6), False),
        ((3, 50, 7), True),  # backport branch
        ((3, 51, 0), False),
        ((3, 51, 2), False),  # last affected release
        ((3, 51, 3), True),  # fix
        ((3, 53, 0), True),
    ],
)
def test_has_wal_reset_fix(version_info, fixed):
    from lionagi.state.engine import has_wal_reset_fix

    assert has_wal_reset_fix(version_info) is fixed


class _RecordingLog:
    def __init__(self):
        self.warnings = []

    def warning(self, msg, *args):
        self.warnings.append(msg % args if args else msg)


def _engine_with_recorded_log(monkeypatch, tmp_path, version_info, version_str):
    import lionagi.state.engine as engine_mod

    recorder = _RecordingLog()
    monkeypatch.setattr(engine_mod, "_log", recorder)
    monkeypatch.setattr(engine_mod, "_wal_reset_warning_emitted", False)
    monkeypatch.setattr(sqlite3, "sqlite_version_info", version_info)
    monkeypatch.setattr(sqlite3, "sqlite_version", version_str)
    engine = engine_mod.make_engine(f"sqlite+aiosqlite:///{tmp_path / 'v.db'}")
    return recorder, engine


async def test_make_engine_warns_on_unfixed_sqlite(monkeypatch, tmp_path):
    """Enabling WAL on a library that still carries the WAL-reset race is
    reported, not assumed away."""
    recorder, engine = _engine_with_recorded_log(monkeypatch, tmp_path, (3, 46, 0), "3.46.0")
    try:
        assert len(recorder.warnings) == 1
        assert "3.46.0" in recorder.warnings[0]
        assert "3.51.3" in recorder.warnings[0]
    finally:
        await engine.dispose()


async def test_make_engine_warns_once_per_process(monkeypatch, tmp_path):
    import lionagi.state.engine as engine_mod

    recorder, engine = _engine_with_recorded_log(monkeypatch, tmp_path, (3, 46, 0), "3.46.0")
    second = engine_mod.make_engine(f"sqlite+aiosqlite:///{tmp_path / 'v2.db'}")
    try:
        assert len(recorder.warnings) == 1
    finally:
        await engine.dispose()
        await second.dispose()


async def test_make_engine_silent_on_fixed_sqlite(monkeypatch, tmp_path):
    recorder, engine = _engine_with_recorded_log(monkeypatch, tmp_path, (3, 51, 3), "3.51.3")
    try:
        assert recorder.warnings == []
    finally:
        await engine.dispose()
