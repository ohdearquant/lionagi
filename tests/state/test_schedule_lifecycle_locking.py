# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Every schedule lifecycle write reads its previous state under a row lock.

A lifecycle write reads the current ``enabled`` flag, mutates the row, and
appends a transition describing the move. The transition is only truthful if
nothing changes the row between the read and the mutation. PostgreSQL allows a
concurrent writer in that window unless the read takes ``FOR UPDATE``, so the
audit ledger can otherwise record a move that never happened, or one naming a
schedule another transaction has already deleted.

SQLite serializes write transactions on its own and rejects the clause, which
is why the lock is dialect-conditional rather than unconditional. That makes
the SQL text itself the thing worth pinning, alongside the fact that every
entry point actually goes through the locking read rather than its own.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

import lionagi.state.db as state_db_mod
from lionagi.state.db import StateDB


def _patch_db(monkeypatch: pytest.MonkeyPatch, db_path: Path) -> None:
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)


def _spec(schedule_id: str, name: str) -> dict[str, object]:
    return {
        "id": schedule_id,
        "name": name,
        "trigger_type": "interval",
        "interval_sec": 300,
        "action_kind": "agent",
        "action_prompt": "check the fleet",
    }


class _RecordingConn:
    """Stands in for a live connection, capturing the SQL it is handed."""

    def __init__(self, row: dict | None) -> None:
        self.statements: list[str] = []
        self._row = row

    async def execute(self, statement, params=None):  # noqa: ANN001 - test double
        self.statements.append(str(statement))
        row = self._row

        class _Result:
            def mappings(self):
                class _Mappings:
                    def first(self):
                        return row

                return _Mappings()

        return _Result()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("dialect", "expect_lock"),
    [("postgresql", True), ("sqlite", False)],
)
async def test_locking_read_takes_for_update_only_where_it_is_supported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dialect: str, expect_lock: bool
) -> None:
    _patch_db(monkeypatch, tmp_path / "state.db")
    async with StateDB() as db:
        monkeypatch.setattr(db, "dialect", dialect)
        conn = _RecordingConn({"enabled": 1})

        assert await db._lock_schedule_enabled_in_tx(conn, "sched-1") is True  # noqa: SLF001

    assert len(conn.statements) == 1
    assert ("FOR UPDATE" in conn.statements[0]) is expect_lock


@pytest.mark.asyncio
async def test_locking_read_reports_a_missing_row_as_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller has to tell "disabled" from "gone" to avoid attributing a
    transition to a schedule that no longer exists; both would otherwise
    arrive as a falsy value."""
    _patch_db(monkeypatch, tmp_path / "state.db")
    async with StateDB() as db:
        assert await db._lock_schedule_enabled_in_tx(_RecordingConn(None), "absent") is None  # noqa: SLF001
        assert await db._lock_schedule_enabled_in_tx(_RecordingConn({"enabled": 0}), "x") is False  # noqa: SLF001


@pytest.mark.asyncio
async def test_every_lifecycle_mutation_reads_through_the_locking_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each entry point that appends a transition obtains its previous state
    from the locking read.

    The lock is only worth anything if no path keeps its own unsynchronized
    ``SELECT enabled``. Asserting per entry point is what makes a single
    reverted call site visible: a check that only counted calls would stay
    green while one path went back to reading the row unlocked.
    """
    _patch_db(monkeypatch, tmp_path / "state.db")

    seen: list[str] = []
    original = StateDB._lock_schedule_enabled_in_tx  # noqa: SLF001

    async def _spy(self, conn, schedule_id):  # noqa: ANN001 - test double
        seen.append(schedule_id)
        return await original(self, conn, schedule_id)

    monkeypatch.setattr(StateDB, "_lock_schedule_enabled_in_tx", _spy)

    ids = {name: uuid.uuid4().hex[:12] for name in ("update", "apply", "omitted", "delete")}

    async with StateDB() as db:
        for label, schedule_id in ids.items():
            await db.create_schedule(_spec(schedule_id, f"lock-{label}"))

        await db.update_schedule(ids["update"], enabled=0, lifecycle_actor="test")
        await db.apply_schedule_set(
            creates=[],
            updates=[(ids["apply"], {"enabled": 0})],
            disables=[ids["omitted"]],
        )
        await db.delete_schedule(ids["delete"], lifecycle_actor="test")

    for label, schedule_id in ids.items():
        assert schedule_id in seen, f"{label} path did not read through the locking helper"
