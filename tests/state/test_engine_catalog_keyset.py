# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Catalog reads use bound keyset queries and leave each row's JSON undecoded."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from lionagi.state.db import StateDB, _get_engine_catalog_row, _list_engine_catalog_rows


async def _insert(db: StateDB, *, def_id, name: str, kind: str, options) -> None:
    option_value = (
        "CAST(CAST(:options AS TEXT) AS JSON)" if db.dialect == "postgresql" else ":options"
    )
    statement = (
        "INSERT INTO engine_defs "
        "(id, name, kind, model, max_depth, max_agents, options, description, created_at, updated_at) "
        "VALUES (:id, :name, :kind, :model, :max_depth, :max_agents, "
        + option_value
        + ", :description, :created_at, :updated_at)"
    )
    async with db._tx() as conn:
        await conn.execute(
            text(statement),
            {
                "id": def_id,
                "name": name,
                "kind": kind,
                "model": "test-model",
                "max_depth": 3,
                "max_agents": 4,
                "options": options,
                "description": "preserved",
                "created_at": 10.0,
                "updated_at": 20.0,
            },
        )


async def _raw_rows(db: StateDB):
    async with db._read() as conn:
        rows = (
            (await conn.execute(text("SELECT * FROM engine_defs ORDER BY name"))).mappings().all()
        )
    return [dict(row) for row in rows]


async def test_sqlite_catalog_keysets_are_bounded_filtered_and_read_only(tmp_path):
    special = "quote' / % _ ? 雪"
    long_id = "z" * 5000
    async with StateDB(tmp_path / "catalog.db") as db:
        for def_id, name, kind in [
            ("", "empty", "alpha"),
            ("a", "first", "alpha"),
            (b"between", "blob", "alpha"),
            (special, "special", "beta"),
            (long_id, "long", "alpha"),
        ]:
            await _insert(db, def_id=def_id, name=name, kind=kind, options='{"ok": true}')
        before = await _raw_rows(db)
        async with db._read() as conn:
            schema_version = (await conn.execute(text("PRAGMA schema_version"))).scalar_one()
            blob_id = (
                await conn.execute(
                    text("SELECT id, typeof(id) FROM engine_defs WHERE name = 'blob'")
                )
            ).one()
        assert tuple(blob_id) == (b"between", "blob")

        first = await _list_engine_catalog_rows(db, kind=None, after=None, limit=1)
        assert [row["id"] for row in first] == [""]
        after_empty = await _list_engine_catalog_rows(db, kind=None, after="", limit=2)
        assert [row["id"] for row in after_empty] == ["a", special]
        last = await _list_engine_catalog_rows(db, kind=None, after=special, limit=2)
        assert [row["id"] for row in last] == [long_id]
        assert [
            row["id"]
            for row in await _list_engine_catalog_rows(db, kind="alpha", after="", limit=10)
        ] == ["a", long_id]
        assert (
            await _list_engine_catalog_rows(db, kind="alpha' OR 1=1 --", after=None, limit=9) == []
        )
        assert await _list_engine_catalog_rows(db, kind=None, after=long_id, limit=1) == []
        assert (await _get_engine_catalog_row(db, ""))["name"] == "empty"
        assert (await _get_engine_catalog_row(db, special))["name"] == "special"
        assert (await _get_engine_catalog_row(db, long_id))["name"] == "long"
        assert await _get_engine_catalog_row(db, "between") is None
        assert await _get_engine_catalog_row(db, b"between") is None
        assert await _get_engine_catalog_row(db, "missing' OR 1=1 --") is None
        assert await _raw_rows(db) == before
        async with db._read() as conn:
            assert (
                await conn.execute(text("PRAGMA schema_version"))
            ).scalar_one() == schema_version


async def test_sqlite_raw_options_preserve_malformed_deep_and_binary_values(tmp_path):
    malformed = '{"private": broken'
    deep = "[" * 1200 + "0" + "]" * 1200
    binary = b"\x00\xffbad"
    async with StateDB(tmp_path / "raw.db") as db:
        for def_id, raw in [("a", malformed), ("b", deep), ("c", binary)]:
            await _insert(db, def_id=def_id, name=def_id, kind="raw", options=raw)
        before = await _raw_rows(db)
        rows = await _list_engine_catalog_rows(db, kind="raw", after=None, limit=10)
        assert [(row["id"], row["options"]) for row in rows] == [
            ("a", malformed),
            ("b", deep),
            ("c", binary),
        ]
        assert (await _get_engine_catalog_row(db, "b"))["options"] == deep
        assert await _raw_rows(db) == before


async def test_sqlite_catalog_excludes_null_ids_from_legacy_table(tmp_path):
    db = StateDB(tmp_path / "legacy.db")
    # Older SQLite tables can contain NULL under a TEXT PRIMARY KEY declaration.
    # Construct that catalog directly so opening/migrating a real store is not needed.
    db._engine = create_async_engine(db.url)
    try:
        async with db._engine.begin() as conn:
            await conn.execute(
                text(
                    "CREATE TABLE engine_defs (id TEXT PRIMARY KEY, name TEXT, kind TEXT, "
                    "model TEXT, max_depth INTEGER, max_agents INTEGER, options JSON, "
                    "description TEXT, created_at FLOAT, updated_at FLOAT)"
                )
            )
            await conn.execute(
                text("INSERT INTO engine_defs (id, name, kind) VALUES (NULL, 'null', 'k')")
            )
            await conn.execute(
                text("INSERT INTO engine_defs (id, name, kind) VALUES ('a', 'a', 'k')")
            )
        rows = await _list_engine_catalog_rows(db, kind="k", after=None, limit=5)
        assert [row["id"] for row in rows] == ["a"]
        assert (await _get_engine_catalog_row(db, "a"))["name"] == "a"
        async with db._read() as conn:
            assert (await conn.execute(text("SELECT count(*) FROM engine_defs"))).scalar_one() == 2
    finally:
        await db._engine.dispose()


async def test_postgres_catalog_queries_do_not_use_sqlite_typeof():
    """Exercise both generated PG statements even when a PG server is unavailable."""

    class Recorder:
        def __init__(self):
            self.statements = []

        async def execute(self, statement, params):
            self.statements.append((str(statement), dict(params)))
            return self

        def mappings(self):
            return self

        def all(self):
            return []

        def first(self):
            return None

    recorder = Recorder()

    class PostgresHandle:
        dialect = "postgresql"

        @asynccontextmanager
        async def _read(self):
            yield recorder

    db = PostgresHandle()
    assert await _list_engine_catalog_rows(db, kind="k", after="", limit=2) == []
    assert await _get_engine_catalog_row(db, "id") is None
    assert len(recorder.statements) == 2
    for statement, _ in recorder.statements:
        assert "typeof(" not in statement.lower()
        assert "CAST(options AS TEXT) AS options" in statement
        assert "id IS NOT NULL" in statement
    assert recorder.statements[0][1] == {"kind": "k", "after": "", "limit": 2}
    assert recorder.statements[1][1] == {"id": "id"}


@pytest.mark.integration
async def test_postgres_catalog_keyset_keeps_json_raw_and_reads_only(pg_url):
    # A unique kind confines queries and cleanup to this test's own rows even with an env URL.
    prefix = uuid.uuid4().hex
    kind = f"catalog-{prefix}"
    ids = [f"{prefix}-a", f"{prefix}-b", f"{prefix}-雪"]
    inserted = []
    deep = "[" * 33 + "0" + "]" * 33
    async with StateDB(url=pg_url) as db:
        try:
            for def_id, options in zip(ids, ['{"ok": true}', deep, "null"], strict=True):
                await _insert(db, def_id=def_id, name=def_id, kind=kind, options=options)
                inserted.append(def_id)
            async with db._read() as conn:
                ordered = list(
                    (
                        await conn.execute(
                            text("SELECT id FROM engine_defs WHERE kind = :kind ORDER BY id ASC"),
                            {"kind": kind},
                        )
                    ).scalars()
                )
            assert len(ordered) == len(ids) and set(ordered) == set(ids)
            first = await _list_engine_catalog_rows(db, kind=kind, after=None, limit=2)
            assert [row["id"] for row in first] == ordered[:2]
            last = await _list_engine_catalog_rows(db, kind=kind, after=ordered[1], limit=2)
            assert [row["id"] for row in last] == ordered[2:]
            options_by_id = {row["id"]: row["options"] for row in first + last}
            assert options_by_id == dict(zip(ids, ['{"ok": true}', deep, "null"], strict=True))
            assert (await _get_engine_catalog_row(db, ids[1]))["options"] == deep
            assert await _get_engine_catalog_row(db, f"{prefix}-missing") is None
            assert (
                await _list_engine_catalog_rows(db, kind=f"{kind}' OR 1=1 --", after=None, limit=5)
                == []
            )
            assert (
                await _list_engine_catalog_rows(db, kind=kind, after=None, limit=10) == first + last
            )
        finally:
            async with db._tx() as conn:
                for def_id in inserted:
                    await conn.execute(
                        text("DELETE FROM engine_defs WHERE id = :id"), {"id": def_id}
                    )


@pytest.mark.integration
async def test_postgres_http_catalog_isolates_stored_options_and_default_create(
    pg_url, monkeypatch
):
    """An API-created default and corrupt legacy values coexist without leaking raw data."""
    pytest.importorskip("fastapi", reason="studio extra not installed")
    httpx = pytest.importorskip("httpx", reason="httpx not installed")

    import lionagi.studio.services.engine_defs as engine_defs
    from lionagi.studio.app import create_app

    prefix = f"catalog{uuid.uuid4().hex}"
    ids = [f"{prefix}-{suffix}" for suffix in ("valid", "array", "deep", "unknown")]
    inserted = []
    created_id = None
    marker = "CATALOG_PRIVATE_DEEP_SENTINEL"
    array_marker = "CATALOG_PRIVATE_ARRAY_SENTINEL"
    deep = '{"private":' + "[" * 32 + f'"{marker}"' + "]" * 32 + "}"
    raw_values = ['{"export_dir":"safe"}', f'["{array_marker}"]', deep, f'["{array_marker}"]']
    monkeypatch.setattr(engine_defs, "StateDB", lambda: StateDB(url=pg_url))
    monkeypatch.setattr(engine_defs, "state_db_known_absent", lambda: False)
    async with StateDB(url=pg_url) as db:
        try:
            for row_id, raw in zip(ids, raw_values, strict=True):
                kind = "future-kind" if row_id == ids[3] else "research"
                await _insert(db, def_id=row_id, name=row_id, kind=kind, options=raw)
                inserted.append(row_id)
            original = {row_id: await _get_engine_catalog_row(db, row_id) for row_id in ids}
            transport = httpx.ASGITransport(app=create_app())
            async with httpx.AsyncClient(
                transport=transport, base_url="http://127.0.0.1:8765"
            ) as client:
                # The existing create contract writes JSON null for omitted options on PostgreSQL.
                created = await client.post(
                    "/api/engine-defs/", json={"name": prefix, "kind": "research"}
                )
                assert created.status_code == 201, created.text
                created_id = created.json()["id"]
                expected = {created_id, *ids}
                observed = {}
                cursor = None
                for _ in range(1000):
                    params = {"limit": 200}
                    if cursor is not None:
                        params["cursor"] = cursor
                    response = await client.get("/api/engine-catalog/presets", params=params)
                    assert response.status_code == 200, response.text
                    assert marker not in response.text and array_marker not in response.text
                    body = response.json()
                    observed.update(
                        {row["id"]: row for row in body["items"] if row["id"] in expected}
                    )
                    cursor = body["next_cursor"]
                    if expected <= observed.keys() or cursor is None:
                        break
                assert set(observed) == expected
                assert observed[created_id]["availability"] == "available"
                assert observed[created_id]["options"] == {}
                assert observed[created_id]["reasons"] == []
                assert observed[ids[0]]["availability"] == "available"
                assert observed[ids[0]]["options"] == {"export_dir": "safe"}
                assert observed[ids[0]]["reasons"] == []
                assert observed[ids[1]]["availability"] == "unavailable"
                assert observed[ids[1]]["reasons"] == ["options_not_object"]
                assert observed[ids[1]]["options"] is None
                assert observed[ids[2]]["availability"] == "unavailable"
                assert observed[ids[2]]["reasons"] == ["options_undecodable"]
                assert observed[ids[2]]["options"] is None
                assert observed[ids[3]]["availability"] == "unavailable"
                assert observed[ids[3]]["reasons"] == ["kind_not_registered", "options_not_object"]
                assert observed[ids[3]]["options"] is None
                assert all("availability_reason" not in row for row in observed.values())
                for row_id in expected:
                    detail = await client.get("/api/engine-catalog/preset", params={"id": row_id})
                    assert detail.status_code == 200, detail.text
                    assert detail.json() == observed[row_id]
                    assert marker not in detail.text and array_marker not in detail.text
            assert {row_id: await _get_engine_catalog_row(db, row_id) for row_id in ids} == original
        finally:
            async with db._tx() as conn:
                for row_id in [*inserted, created_id]:
                    if row_id is not None:
                        await conn.execute(
                            text("DELETE FROM engine_defs WHERE id = :id"), {"id": row_id}
                        )


@pytest.mark.integration
@pytest.mark.parametrize(
    ("case", "raw", "expected_reasons", "physical_optional"),
    [
        ("depth32", '{"nested":' + "[" * 31 + "0" + "]" * 31 + "}", [], False),
        (
            "depth33",
            '{"nested":' + "[" * 32 + "0" + "]" * 32 + "}",
            ["options_undecodable"],
            False,
        ),
        (
            "992nested",
            '{"nested":' + "[" * 992 + "0" + "]" * 992 + "}",
            ["options_undecodable"],
            True,
        ),
        ("huge_number", '{"nested":1e1000}', ["options_undecodable"], True),
    ],
)
async def test_postgres_http_catalog_json_depth_and_number_boundaries(
    pg_url, monkeypatch, case, raw, expected_reasons, physical_optional
):
    """Test SQL JSON through real list/detail and record a server-side input refusal."""
    pytest.importorskip("fastapi", reason="studio extra not installed")
    httpx = pytest.importorskip("httpx", reason="httpx not installed")

    import lionagi.studio.services.engine_defs as engine_defs
    from lionagi.studio.app import create_app

    row_id = f"catalog-{uuid.uuid4().hex}-{case}"
    monkeypatch.setattr(engine_defs, "StateDB", lambda: StateDB(url=pg_url))
    monkeypatch.setattr(engine_defs, "state_db_known_absent", lambda: False)
    async with StateDB(url=pg_url) as db:
        inserted = False
        try:
            try:
                await _insert(db, def_id=row_id, name=row_id, kind="research", options=raw)
            except DBAPIError as exc:
                sqlstate = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
                # These optional stress inputs can exceed a server's JSON or numeric limits.
                # Any other database error remains a failure rather than a claimed limitation.
                if physical_optional and sqlstate in {"22P02", "22003", "54000", "54001"}:
                    pytest.skip(f"PostgreSQL refused {case} JSON input (SQLSTATE {sqlstate})")
                raise
            inserted = True
            original = await _get_engine_catalog_row(db, row_id)
            transport = httpx.ASGITransport(app=create_app())
            async with httpx.AsyncClient(
                transport=transport, base_url="http://127.0.0.1:8765"
            ) as client:
                cursor = None
                row = None
                for _ in range(1000):
                    params = {"kind": "research", "limit": 200}
                    if cursor is not None:
                        params["cursor"] = cursor
                    response = await client.get("/api/engine-catalog/presets", params=params)
                    assert response.status_code == 200, response.text
                    body = response.json()
                    row = next((item for item in body["items"] if item["id"] == row_id), None)
                    cursor = body["next_cursor"]
                    if row is not None or cursor is None:
                        break
                assert row is not None
                assert row["reasons"] == expected_reasons
                assert row["availability"] == ("unavailable" if expected_reasons else "available")
                assert "availability_reason" not in row
                if expected_reasons:
                    assert row["options"] is None
                    assert row["stored_option_keys"] is None
                    assert row["unused_option_keys"] is None
                else:
                    assert row["stored_option_keys"] == ["nested"]
                    assert row["options"] is not None
                detail = await client.get("/api/engine-catalog/preset", params={"id": row_id})
                assert detail.status_code == 200, detail.text
                assert detail.json() == row
            assert await _get_engine_catalog_row(db, row_id) == original
        finally:
            if inserted:
                async with db._tx() as conn:
                    await conn.execute(
                        text("DELETE FROM engine_defs WHERE id = :id"), {"id": row_id}
                    )
