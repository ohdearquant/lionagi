# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""`lifecycle` is reachable from the MCP surface, so its read must be a read.

The ordinary store open reconciles the schema (`create_all`, index
reconciliation, seed inserts), so a reporting path that used it would write
to the store it reports on -- failing on `INSERT INTO schema_meta` against a
store it may not write to, while presenting itself as a read. Only
``readonly=True`` avoids that.

These are behavioural probes, not an assertion about which keyword was
passed: a keyword check passes as soon as someone writes the keyword, but
what has to hold is that the path does not need write access.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

pytest.importorskip("aiosqlite", reason="aiosqlite not installed")

from lionagi.cli.machine import lifecycle_data


def _redirect(monkeypatch, db_path: Path) -> None:
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", db_path)


def _make_store(db_path: Path) -> None:
    """A real, fully reconciled store, so a later open has nothing to migrate."""
    from lionagi.state.db import StateDB

    async def _open():
        async with StateDB(db_path) as db:
            await db.fetch_all("SELECT 1")

    asyncio.run(_open())


def test_reading_a_run_does_not_bring_a_store_into_existence(monkeypatch, tmp_path):
    db_path = tmp_path / "state.db"
    _redirect(monkeypatch, db_path)

    answer = lifecycle_data("some-run")

    assert answer["lifecycle"]["available"] is False
    assert answer["lifecycle"]["reason_code"] == "not_found"
    assert not db_path.exists(), "a read created the store it was reporting on"


def test_reading_a_run_runs_none_of_the_schema_writes(monkeypatch, tmp_path):
    """The probe that fails if the connection is not read-only.

    A read-only open skips three things by construction: the writable engine, the
    reserved `BEGIN IMMEDIATE` lock, and schema reconciliation. Each of those
    mutates the file -- PRAGMAs persisted into the header, a write lock, the
    schema and seed writes that produced the observed `INSERT INTO schema_meta`.
    Asserting none of them ran tests the property that matters, whereas denying
    writes on the file would fail for an unrelated reason: SQLite in WAL mode
    needs to write a sidecar even for a genuine read-only read.
    """
    from lionagi.state import db as db_module

    db_path = tmp_path / "state.db"
    _make_store(db_path)
    _redirect(monkeypatch, db_path)

    ran: list[str] = []

    async def _refuse_schema(self):
        ran.append("_apply_schema")

    monkeypatch.setattr(db_module.StateDB, "_apply_schema", _refuse_schema)
    monkeypatch.setattr(
        db_module, "make_engine", lambda *a, **k: ran.append("make_engine") or pytest.fail()
    )
    monkeypatch.setattr(
        db_module,
        "_install_begin_immediate",
        lambda *a, **k: ran.append("_install_begin_immediate"),
    )

    answer = lifecycle_data("some-run")

    assert ran == [], f"the read performed writable-open work: {ran}"
    lifecycle = answer["lifecycle"]
    assert lifecycle["available"] is True, (
        f"the read did not reach the store ({lifecycle.get('reason_code')}: "
        f"{lifecycle.get('detail')})"
    )
    # An established answer about a run with no sessions, which is a different
    # thing from not having been able to look.
    assert lifecycle["value"]["found"] is False


def test_the_store_is_unchanged_by_reading_it(monkeypatch, tmp_path):
    db_path = tmp_path / "state.db"
    _make_store(db_path)
    _redirect(monkeypatch, db_path)
    before = db_path.read_bytes()

    lifecycle_data("some-run")

    assert db_path.read_bytes() == before, "reading a run modified the store's bytes"
