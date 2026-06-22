# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for engine.py URL utilities and schema_meta.py MetaData parity."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

from lionagi.state.engine import dialect_of, make_engine, mask_db_url, normalize_state_db_url
from lionagi.state.schema_meta import metadata

# ── normalize_state_db_url ────────────────────────────────────────────────────


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


# ── mask_db_url ───────────────────────────────────────────────────────────────


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


# ── dialect_of ────────────────────────────────────────────────────────────────


def test_dialect_sqlite():
    assert dialect_of("sqlite+aiosqlite:////tmp/x.db") == "sqlite"
    assert dialect_of("sqlite:///x.db") == "sqlite"


def test_dialect_postgresql():
    assert dialect_of("postgresql+asyncpg://host/db") == "postgresql"
    assert dialect_of("postgres://host/db") == "postgresql"


# ── make_engine (SQLite only — sync verification) ────────────────────────────


def test_make_engine_sqlite_creates_engine():
    url = "sqlite+aiosqlite:///:memory:"
    engine = make_engine(url)
    assert engine is not None
    assert "sqlite" in str(engine.url)
    # Cleanup.
    import asyncio

    asyncio.run(engine.dispose())


# ── Schema-parity: MetaData vs schema.sql (SQLite leg, always runs) ──────────

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
    "session_signals",
    "engine_runs",
    "engine_defs",
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


async def test_metadata_creates_all_21_tables(sqlite_meta_engine):
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
    assert len(raw_checks) == 14, f"expected 14 enum CHECK columns, got {len(raw_checks)}"
    drift = {
        k: {
            "schema_sql": sorted(raw_checks.get(k) or []),
            "metadata": sorted(meta_checks.get(k) or []),
        }
        for k in set(raw_checks) | set(meta_checks)
        if raw_checks.get(k) != meta_checks.get(k)
    }
    assert not drift, f"CHECK enum drift:\n{drift}"


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


# ── Postgres leg (gated by LIONAGI_TEST_PG_URL) ───────────────────────────────

_PG_URL = os.environ.get("LIONAGI_TEST_PG_URL")
pg_skip = pytest.mark.skipif(not _PG_URL, reason="LIONAGI_TEST_PG_URL not set")


@pg_skip
async def test_metadata_create_all_postgres():
    """metadata.create_all() succeeds against a live Postgres instance."""
    assert _PG_URL is not None
    engine = create_async_engine(_PG_URL, echo=False)
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
