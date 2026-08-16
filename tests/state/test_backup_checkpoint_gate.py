# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""A pre-rebuild backup must refuse rather than copy a partially checkpointed store.

`PRAGMA wal_checkpoint(TRUNCATE)` asks for a full checkpoint and reports whether
it got one. Where it does not, committed transactions are still in the `-wal`
sidecar, and the main database file on its own is a rollback artifact predating
them. Copying anyway produces a backup that restores cleanly while silently
missing data, on the one path whose entire purpose is being able to go back.
"""

from pathlib import Path

import pytest

from lionagi.state.db import BackupNotTrustworthyError, StateDB

pytestmark = pytest.mark.asyncio


async def _seeded_db(tmp_path: Path) -> StateDB:
    db = StateDB(path=tmp_path / "state.db")
    await db.open()
    return db


def _backups(tmp_path: Path) -> list[Path]:
    return sorted(tmp_path.glob("state.db.pre-*.bak"))


async def test_backup_refuses_when_the_checkpoint_reports_busy(tmp_path, monkeypatch):
    db = await _seeded_db(tmp_path)
    try:
        # busy=1 with pages left behind: SQLite falls back from TRUNCATE when it
        # cannot get the locks in time, and says so in the value nobody read.
        async def _busy(mode: str = "PASSIVE"):
            return (1, 7, 3)

        monkeypatch.setattr(db, "checkpoint", _busy)

        with pytest.raises(BackupNotTrustworthyError) as excinfo:
            await db._backup_before_rebuild("probe")

        assert "busy=1" in str(excinfo.value)
        assert _backups(tmp_path) == [], "a backup file was written despite the refusal"
    finally:
        await db.close()


async def test_backup_refuses_when_pages_are_left_uncheckpointed(tmp_path, monkeypatch):
    """busy=0 is not on its own a complete checkpoint.

    A checkpoint can report that it did not block and still leave frames behind,
    so the page counts are the check and the busy flag alone is not.
    """
    db = await _seeded_db(tmp_path)
    try:

        async def _partial(mode: str = "PASSIVE"):
            return (0, 9, 4)

        monkeypatch.setattr(db, "checkpoint", _partial)

        with pytest.raises(BackupNotTrustworthyError):
            await db._backup_before_rebuild("probe")

        assert _backups(tmp_path) == []
    finally:
        await db.close()


async def test_backup_proceeds_on_a_complete_checkpoint(tmp_path, monkeypatch):
    """Control: the gate refuses partial checkpoints, not every checkpoint.

    Without this the two tests above would pass against a backup path that had
    simply been broken outright.
    """
    db = await _seeded_db(tmp_path)
    try:

        async def _complete(mode: str = "PASSIVE"):
            return (0, 5, 5)

        monkeypatch.setattr(db, "checkpoint", _complete)

        await db._backup_before_rebuild("probe")

        assert len(_backups(tmp_path)) == 1, "a complete checkpoint must still back up"
    finally:
        await db.close()


async def test_backup_proceeds_on_a_real_checkpoint(tmp_path):
    """Second control, unmocked: the real pragma against a real store passes.

    The three tests above all replace `checkpoint`, so together they cannot say
    whether a genuine checkpoint satisfies the predicate. If real sqlite
    returned something the gate rejects, every rebuild would refuse.
    """
    db = await _seeded_db(tmp_path)
    try:
        await db._backup_before_rebuild("probe")
        assert len(_backups(tmp_path)) == 1
    finally:
        await db.close()
