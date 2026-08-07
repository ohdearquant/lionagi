# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Regression tests for ``StateDB._drop_legacy_definitions_kind_check``.

The migration decides whether ``definitions`` still carries the
pre-skill-editor 2-value ``kind`` CHECK by parsing the CHECK constraint's
own value set out of ``sqlite_master.sql`` -- not by searching the whole
CREATE TABLE statement for the substring ``'skill'``, which false-positives
on unrelated columns (e.g. a ``message`` column whose own default literal
happens to be ``'skill'``).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import aiosqlite
import pytest
from sqlalchemy import text

from lionagi.state.db import StateDB

# ── Legacy fixture builders ─────────────────────────────────────────────────


def _create_legacy_definitions_db(db_path: Path, *, unrelated_skill_literal: bool = False) -> None:
    """Full current schema, except ``definitions.kind`` still carries the
    legacy 2-value CHECK that predates the skill editor. When
    ``unrelated_skill_literal`` is set, the (untouched) ``message`` column
    definition is also given a ``'skill'`` default literal -- a value that
    has nothing to do with the CHECK constraint, but which the old
    substring-based detector would misread as "already migrated"."""
    from lionagi.state.db import _SCHEMA_PATH

    schema_sql = _SCHEMA_PATH.read_text()
    current_check = "CHECK(kind IN ('agent', 'playbook', 'skill')),  -- ADR-0016 editable set"
    legacy_check = "CHECK(kind IN ('agent', 'playbook')),  -- ADR-0016 editable set"
    assert schema_sql.count(current_check) == 1, (
        "definitions.kind CHECK definition not found (or found more than once) in schema.sql "
        "-- schema.sql layout changed, update this fixture"
    )
    legacy_sql = schema_sql.replace(current_check, legacy_check, 1)

    if unrelated_skill_literal:
        current_message_col = "  message     TEXT                        -- optional edit note"
        legacy_message_col = "  message     TEXT DEFAULT 'skill'         -- optional edit note"
        assert legacy_sql.count(current_message_col) == 1, (
            "definitions.message column definition not found (or found more than once) in "
            "schema.sql -- schema.sql layout changed, update this fixture"
        )
        legacy_sql = legacy_sql.replace(current_message_col, legacy_message_col, 1)

    with sqlite3.connect(db_path) as conn:
        conn.executescript(legacy_sql)


async def _definitions_create_sql(db_path: Path) -> str:
    async with aiosqlite.connect(db_path) as conn:
        cur = await conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='definitions'"
        )
        row = await cur.fetchone()
    assert row is not None
    return row[0]


# ── Arm 1: exact legacy schema migrates ──────────────────────────────────────


async def test_exact_legacy_schema_migrates(tmp_path: Path) -> None:
    """A definitions table with exactly the legacy 2-value CHECK is rebuilt
    on open() to admit 'skill', and a skill can then be saved."""
    db_path = tmp_path / "legacy-definitions.db"
    _create_legacy_definitions_db(db_path)

    create_sql_before = await _definitions_create_sql(db_path)
    assert StateDB._definitions_kind_check_values(create_sql_before) == frozenset(
        {"agent", "playbook"}
    )

    state = StateDB(db_path)
    await state.open()
    try:
        version = await state.save_definition(
            kind="skill", name="demo-skill", path="skills/demo.md", content="# demo"
        )
        assert version == 1
    finally:
        await state.close()

    create_sql_after = await _definitions_create_sql(db_path)
    assert StateDB._definitions_kind_check_values(create_sql_after) == frozenset(
        {"agent", "playbook", "skill"}
    )


# ── Arm 2: exact widened (current) schema skips the rebuild ────────────────


async def test_current_schema_skips_rebuild(tmp_path: Path) -> None:
    """A definitions table that already admits 'skill' is left untouched --
    no rebuild runs, and the CREATE TABLE SQL is byte-for-byte identical
    after open()."""
    db_path = tmp_path / "current-definitions.db"
    with sqlite3.connect(db_path) as conn:
        from lionagi.state.db import _SCHEMA_PATH

        conn.executescript(_SCHEMA_PATH.read_text())

    create_sql_before = await _definitions_create_sql(db_path)
    assert StateDB._definitions_kind_check_values(create_sql_before) == frozenset(
        {"agent", "playbook", "skill"}
    )

    state = StateDB(db_path)
    await state.open()
    try:
        version = await state.save_definition(
            kind="skill", name="demo-skill", path="skills/demo.md", content="# demo"
        )
        assert version == 1
    finally:
        await state.close()

    create_sql_after = await _definitions_create_sql(db_path)
    assert create_sql_after == create_sql_before


# ── Arm 3: the false-positive fixture -- old CHECK + unrelated 'skill' ─────


async def test_legacy_schema_with_unrelated_skill_literal_still_migrates(tmp_path: Path) -> None:
    """The core regression: a legacy 2-value CHECK table whose CREATE TABLE
    SQL *also* contains an unrelated ``'skill'`` substring (a ``message``
    column default) must still be recognized as legacy and migrated. A
    substring search for ``'skill'`` over the whole statement would
    misidentify this table as already migrated and skip the rebuild,
    leaving the old 2-value CHECK in force -- so a 'skill' write would then
    fail with a CHECK constraint violation."""
    db_path = tmp_path / "legacy-definitions-false-positive.db"
    _create_legacy_definitions_db(db_path, unrelated_skill_literal=True)

    create_sql_before = await _definitions_create_sql(db_path)
    # The false-positive condition: the raw SQL contains 'skill' as a
    # substring, yet the CHECK constraint itself only admits the legacy set.
    assert "'skill'" in create_sql_before
    assert StateDB._definitions_kind_check_values(create_sql_before) == frozenset(
        {"agent", "playbook"}
    )

    state = StateDB(db_path)
    await state.open()
    try:
        version = await state.save_definition(
            kind="skill", name="demo-skill", path="skills/demo.md", content="# demo"
        )
        assert version == 1
    finally:
        await state.close()

    create_sql_after = await _definitions_create_sql(db_path)
    assert StateDB._definitions_kind_check_values(create_sql_after) == frozenset(
        {"agent", "playbook", "skill"}
    )


async def test_old_substring_detector_would_have_false_positived(tmp_path: Path) -> None:
    """Documents the defect directly: the false-positive fixture satisfies
    the retired substring marker while the CHECK constraint is still the
    legacy 2-value set, proving the substring check alone cannot
    distinguish the two states."""
    db_path = tmp_path / "legacy-definitions-false-positive-2.db"
    _create_legacy_definitions_db(db_path, unrelated_skill_literal=True)
    create_sql = await _definitions_create_sql(db_path)

    false_positive_detector = "'skill'" in create_sql
    old_check_still_present = StateDB._definitions_kind_check_values(create_sql) == frozenset(
        {"agent", "playbook"}
    )
    assert false_positive_detector is True
    assert old_check_still_present is True


# ── Arm 4: failed copy retries ───────────────────────────────────────────────


async def test_rebuild_crash_mid_sequence_rolls_back_and_reopen_completes(tmp_path: Path) -> None:
    """The raw-driver rebuild is one BEGIN IMMEDIATE transaction. A crash
    injected during the INSERT..SELECT copy must roll the whole rebuild
    back -- leaving the legacy table intact with its rows -- so a later
    open() can retry and complete the migration cleanly."""
    db_path = tmp_path / "legacy-definitions-crash.db"
    _create_legacy_definitions_db(db_path)
    with sqlite3.connect(db_path) as seed:
        seed.execute(
            "INSERT INTO definitions (id, kind, name, path, content, version, created_at) "
            "VALUES ('def-1', 'agent', 'demo', 'agents/demo.md', 'x', 1, 0)"
        )
        seed.commit()

    real_execute = aiosqlite.Connection.execute
    crash = {"armed": True}

    def _crashing_execute(self, sql, *args, **kwargs):
        if crash["armed"] and "INSERT INTO definitions_new" in str(sql):
            raise sqlite3.OperationalError("disk I/O error")
        return real_execute(self, sql, *args, **kwargs)

    aiosqlite.Connection.execute = _crashing_execute
    try:
        state = StateDB(db_path)
        with pytest.raises(sqlite3.OperationalError):
            await state.open()
        await state.close()
    finally:
        aiosqlite.Connection.execute = real_execute

    # Atomic rollback: legacy table intact with its row, no stray _new table.
    with sqlite3.connect(db_path) as check:
        tables = {
            r[0]
            for r in check.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'definitions%'"
            )
        }
        assert tables == {"definitions"}, tables
        rows = list(check.execute("SELECT id FROM definitions"))
        assert rows == [("def-1",)]

    # And the DB is healable: a clean re-open completes the rebuild, and the
    # pre-existing row survives the rebuild.
    crash["armed"] = False
    state = StateDB(db_path)
    await state.open()
    try:
        create_sql = await _definitions_create_sql(db_path)
        assert StateDB._definitions_kind_check_values(create_sql) == frozenset(
            {"agent", "playbook", "skill"}
        )
        async with state._read() as conn:
            row = (
                (await conn.execute(text("SELECT id FROM definitions WHERE id = 'def-1'")))
                .mappings()
                .first()
            )
        assert row is not None
    finally:
        await state.close()
