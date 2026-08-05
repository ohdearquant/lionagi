# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for archive-then-prune retention: archive gate, refusal-on-failure,
unchanged no-archive behaviour, and bounded per-chunk commits."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
import zipfile
from pathlib import Path

import pytest

import lionagi.state.db as state_db_mod
from lionagi.state.db import StateDB
from lionagi.studio.services.retention_archive import (
    ArchiveVerificationError,
    ArchiveWriteError,
    read_archive_chunk,
    verify_archive_chunk,
    write_archive_chunk,
)

from ._helpers import run_async

# ── helpers ──────────────────────────────────────────────────────────────────


async def _make_session(db: StateDB, *, status: str, started_at: float) -> str:
    pid = str(uuid.uuid4())
    sid = str(uuid.uuid4())
    await db.create_progression(pid)
    await db.create_session(
        {
            "id": sid,
            "progression_id": pid,
            "name": f"s-{status}-{sid[:6]}",
            "status": status,
            "started_at": started_at,
            "updated_at": started_at,
        }
    )
    return sid


def _patch_db(monkeypatch, db_path: Path) -> None:
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)


def _patch_prune_config(monkeypatch, *, archive_dir: Path | None, chunk_rows: int) -> None:
    from lionagi.studio.services import db_maintenance as maint

    monkeypatch.setattr(maint, "PRUNE_ARCHIVE_DIR", archive_dir, raising=False)
    monkeypatch.setattr(maint, "PRUNE_CHUNK_ROWS", chunk_rows, raising=False)
    import lionagi.studio.config as cfg

    monkeypatch.setattr(cfg, "PRUNE_ARCHIVE_DIR", archive_dir)
    monkeypatch.setattr(cfg, "PRUNE_CHUNK_ROWS", chunk_rows)


# ── retention_archive module unit tests ─────────────────────────────────────


def test_write_archive_chunk_is_zip64_capable_and_round_trips(tmp_path):
    dest = tmp_path / "archive"
    dest.mkdir()
    tables = {
        "sessions": [{"id": "s1", "status": "completed"}],
        "messages": [{"id": "m1", "content": "x" * 1000}],
    }
    path = write_archive_chunk(dest, "prune-1-000000-abcd1234", tables)

    assert path.exists()
    assert path.suffix == ".zip"
    assert not (dest / ".prune-1-000000-abcd1234.tmp").exists()

    with zipfile.ZipFile(path) as zf:
        assert zf.testzip() is None
        names = set(zf.namelist())
        assert "manifest.json" in names
        assert "rows/sessions.jsonl" in names
        assert "rows/messages.jsonl" in names
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        assert manifest["format_version"] == 1
        assert manifest["archive_id"] == "prune-1-000000-abcd1234"
        assert manifest["row_counts"] == {"sessions": 1, "messages": 1}
        for member_name in ("rows/sessions.jsonl", "rows/messages.jsonl"):
            payload = zf.read(member_name)
            expected_digest = hashlib.sha256(payload).hexdigest()
            assert manifest["members"][member_name]["sha256"] == expected_digest
            assert manifest["members"][member_name]["rows"] == payload.count(b"\n")

    decoded = read_archive_chunk(path)
    assert decoded["tables"]["sessions"] == tables["sessions"]
    assert decoded["tables"]["messages"] == tables["messages"]
    assert decoded["row_counts"] == {"sessions": 1, "messages": 1}


def test_write_archive_chunk_members_are_genuinely_zip64_not_nominal(tmp_path):
    """Every member must be written with force_zip64=True (version-needed-to-extract
    45), not merely allowZip64=True on the archive, which only opts a member into
    ZIP64 once it individually crosses the size threshold."""
    dest = tmp_path / "archive"
    dest.mkdir()
    path = write_archive_chunk(dest, "prune-1-000000-zip64chk", {"sessions": [{"id": "s1"}]})

    with zipfile.ZipFile(path) as zf:
        for name in zf.namelist():
            zi = zf.getinfo(name)
            assert zi.extract_version >= 45, f"{name} was not written force_zip64"


def test_write_archive_chunk_round_trips_blob_columns_exactly(tmp_path):
    """A BLOB column (e.g. messages.embedding) must survive archive+restore as
    the exact same bytes, not the Python repr of a ``bytes`` object."""
    dest = tmp_path / "archive"
    dest.mkdir()
    raw = b"\x9a\x99\x19@\x00\xffnot-utf8\xf0"
    tables = {"messages": [{"id": "m1", "embedding": raw}]}
    path = write_archive_chunk(dest, "prune-1-000000-blob0001", tables)

    decoded = read_archive_chunk(path)
    assert decoded["tables"]["messages"][0]["embedding"] == raw
    assert isinstance(decoded["tables"]["messages"][0]["embedding"], bytes)


def test_write_archive_chunk_captures_preimages_as_sibling_members(tmp_path):
    dest = tmp_path / "archive"
    dest.mkdir()
    tables = {"sessions": [{"id": "s1", "status": "completed"}]}
    preimages = {"artifacts": [{"id": "a1", "session_id": "s1"}]}
    path = write_archive_chunk(dest, "prune-1-000000-preimg01", tables, preimages=preimages)

    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        assert "preimages/artifacts.jsonl" in names
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        assert manifest["preimage_row_counts"] == {"artifacts": 1}

    decoded = read_archive_chunk(path)
    assert decoded["preimages"]["artifacts"] == preimages["artifacts"]
    assert decoded["tables"]["sessions"] == tables["sessions"]


def test_write_archive_chunk_removes_published_file_on_post_rename_verify_failure(
    tmp_path, monkeypatch
):
    """If verification of the *published* (post-rename) file fails, the
    final path must not be left behind under its final name."""
    import lionagi.studio.services.retention_archive as ra_mod

    dest = tmp_path / "archive"
    dest.mkdir()
    archive_id = "prune-1-000000-badpost1"

    real_verify = ra_mod.verify_archive_chunk
    calls = {"n": 0}

    def flaky_verify(path):
        calls["n"] += 1
        if calls["n"] == 2:
            # second call is the post-rename verification of the final path
            raise ArchiveVerificationError("simulated post-rename corruption")
        return real_verify(path)

    monkeypatch.setattr(ra_mod, "verify_archive_chunk", flaky_verify)

    with pytest.raises(ArchiveVerificationError):
        write_archive_chunk(dest, archive_id, {"sessions": [{"id": "a"}]})

    assert not (dest / f"{archive_id}.zip").exists()
    assert not (dest / f".{archive_id}.tmp").exists()


def test_verify_archive_chunk_detects_digest_tampering(tmp_path):
    dest = tmp_path / "archive"
    dest.mkdir()
    archive_id = "prune-1-000000-tampered"
    path = write_archive_chunk(dest, archive_id, {"sessions": [{"id": "a"}]})

    # Verified as published.
    manifest = verify_archive_chunk(path)
    assert manifest["members"]["rows/sessions.jsonl"]["rows"] == 1

    # Rewrite the row payload without updating the manifest digest.
    with zipfile.ZipFile(path, "a") as zf:
        zf.writestr("rows/sessions.jsonl", '{"id":"tampered-row"}\n')

    with pytest.raises(ArchiveVerificationError):
        verify_archive_chunk(path)


def test_write_archive_chunk_never_overwrites_existing_file(tmp_path):
    dest = tmp_path / "archive"
    dest.mkdir()
    archive_id = "prune-1-000000-fixedid1"
    write_archive_chunk(dest, archive_id, {"sessions": [{"id": "a"}]})
    with pytest.raises(ArchiveWriteError):
        # os.replace would silently clobber a plain file; simulate a
        # "final path is a directory" collision so the second write cannot
        # be mistaken for a legitimate overwrite.
        (dest / f"{archive_id}.zip").unlink()
        (dest / f"{archive_id}.zip").mkdir()
        write_archive_chunk(dest, archive_id, {"sessions": [{"id": "b"}]})


def test_write_archive_chunk_missing_destination_raises_and_leaves_no_tmp(tmp_path):
    dest = tmp_path / "does-not-exist"
    with pytest.raises(ArchiveWriteError):
        write_archive_chunk(dest, "prune-1-000000-deadbeef", {"sessions": []})
    assert not dest.exists()


# ── prune_old_data integration: archive gate + chunking ────────────────────


def test_unset_archive_dir_preserves_no_archive_contract(tmp_path, monkeypatch):
    """No LIONAGI_STUDIO_PRUNE_ARCHIVE_DIR configured -> unchanged behaviour, no files written."""
    from lionagi.studio.services import db_maintenance as maint

    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    _patch_prune_config(monkeypatch, archive_dir=None, chunk_rows=100)

    old_ts = time.time() - 40 * 86400

    async def seed():
        async with StateDB(db_path) as db:
            return await _make_session(db, status="completed", started_at=old_ts)

    sid = run_async(seed())
    result = run_async(maint.prune_old_data(keep_days=30, actor="test"))

    assert result == {"sessions_pruned": 1, "runs_pruned": 0, "dispatch_purged": 0}
    assert list(tmp_path.glob("**/*.zip")) == []

    async def remaining():
        async with StateDB(db_path) as db:
            return await db.get_session(sid)

    assert run_async(remaining()) is None


def test_archive_before_delete_writes_compressed_receipt_for_pruned_rows(tmp_path, monkeypatch):
    from lionagi.studio.services import db_maintenance as maint

    db_path = tmp_path / "state.db"
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    _patch_db(monkeypatch, db_path)
    _patch_prune_config(monkeypatch, archive_dir=archive_dir, chunk_rows=100)

    old_ts = time.time() - 40 * 86400

    async def seed():
        async with StateDB(db_path) as db:
            return await _make_session(db, status="completed", started_at=old_ts)

    sid = run_async(seed())
    result = run_async(maint.prune_old_data(keep_days=30, actor="test"))

    assert result["sessions_pruned"] == 1
    archives = sorted(archive_dir.glob("*.zip"))
    assert len(archives) == 1
    decoded = read_archive_chunk(archives[0])
    archived_ids = {row["id"] for row in decoded["tables"]["sessions"]}
    assert archived_ids == {sid}

    async def remaining():
        async with StateDB(db_path) as db:
            return await db.get_session(sid)

    assert run_async(remaining()) is None


def test_archive_write_failure_refuses_deletion_for_that_chunk(tmp_path, monkeypatch):
    """A failing archive write must not delete the rows it failed to archive."""
    from lionagi.studio.services import db_maintenance as maint

    db_path = tmp_path / "state.db"
    # A file, not a directory: every write_archive_chunk call inside prune
    # raises ArchiveWriteError (open() on a path whose parent is a file).
    archive_dir = tmp_path / "not-a-dir"
    archive_dir.write_text("blocked")
    _patch_db(monkeypatch, db_path)
    _patch_prune_config(monkeypatch, archive_dir=archive_dir, chunk_rows=100)

    old_ts = time.time() - 40 * 86400

    async def seed():
        async with StateDB(db_path) as db:
            return await _make_session(db, status="completed", started_at=old_ts)

    sid = run_async(seed())

    with pytest.raises(Exception):
        run_async(maint.prune_old_data(keep_days=30, actor="test"))

    async def remaining():
        async with StateDB(db_path) as db:
            return await db.get_session(sid)

    # Refused: the session that failed to archive is still present.
    assert run_async(remaining()) is not None


def test_chunk_selection_is_stable_and_bounded(tmp_path, monkeypatch):
    """PRUNE_CHUNK_ROWS=2 over 5 candidates -> chunks of [2, 2, 1], no id repeats."""
    from lionagi.studio.services import db_maintenance as maint

    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    _patch_prune_config(monkeypatch, archive_dir=None, chunk_rows=2)

    old_ts = time.time() - 40 * 86400
    seen_chunks: list[list[str]] = []
    original = maint._prune_session_chunk

    async def spy(conn, session_ids, **kwargs):
        seen_chunks.append(sorted(session_ids))
        return await original(conn, session_ids, **kwargs)

    monkeypatch.setattr(maint, "_prune_session_chunk", spy)

    async def seed():
        async with StateDB(db_path) as db:
            return [
                await _make_session(db, status="completed", started_at=old_ts) for _ in range(5)
            ]

    ids = run_async(seed())
    result = run_async(maint.prune_old_data(keep_days=30, actor="test"))

    assert result["sessions_pruned"] == 5
    assert [len(c) for c in seen_chunks] == [2, 2, 1]
    flat = [i for c in seen_chunks for i in c]
    assert sorted(flat) == sorted(ids)
    assert len(flat) == len(set(flat))


def test_interrupt_after_n_chunks_keeps_completed_chunks(tmp_path, monkeypatch):
    """An exception after N chunks commit must leave those N chunks' deletions in place."""
    from lionagi.studio.services import db_maintenance as maint

    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    _patch_prune_config(monkeypatch, archive_dir=None, chunk_rows=1)

    old_ts = time.time() - 40 * 86400

    async def seed():
        async with StateDB(db_path) as db:
            return [
                await _make_session(db, status="completed", started_at=old_ts) for _ in range(3)
            ]

    ids = run_async(seed())

    calls = {"n": 0}

    async def flaky_after_commit(*, chunk_index, chunk_ids):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated interruption after 2 committed chunks")

    monkeypatch.setattr(maint, "_after_prune_chunk_committed", flaky_after_commit)

    with pytest.raises(RuntimeError):
        run_async(maint.prune_old_data(keep_days=30, actor="test"))

    async def remaining_ids():
        async with StateDB(db_path) as db:
            rows = await db.fetch_all("SELECT id FROM sessions")
            return {r["id"] for r in rows}

    rem = run_async(remaining_ids())
    # Exactly 2 of the 3 sessions were pruned: their chunks already committed
    # (and released the write lock) before the interruption hook fired; the
    # 3rd chunk, never attempted, survives.
    assert len(rem) == 1
    assert rem.issubset(set(ids))


def test_rerun_after_interruption_prunes_only_remaining_and_keeps_prior_archives(
    tmp_path, monkeypatch
):
    from lionagi.studio.services import db_maintenance as maint

    db_path = tmp_path / "state.db"
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    _patch_db(monkeypatch, db_path)
    _patch_prune_config(monkeypatch, archive_dir=archive_dir, chunk_rows=1)

    old_ts = time.time() - 40 * 86400

    async def seed():
        async with StateDB(db_path) as db:
            return [
                await _make_session(db, status="completed", started_at=old_ts) for _ in range(3)
            ]

    ids = run_async(seed())

    calls = {"n": 0}

    async def flaky_after_commit(*, chunk_index, chunk_ids):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated interruption")

    monkeypatch.setattr(maint, "_after_prune_chunk_committed", flaky_after_commit)
    with pytest.raises(RuntimeError):
        run_async(maint.prune_old_data(keep_days=30, actor="test"))

    archives_after_interrupt = sorted(archive_dir.glob("*.zip"))
    assert len(archives_after_interrupt) == 2

    # Rerun cleanly (no injected failure): only the remaining session is pruned.
    async def noop_after_commit(*, chunk_index, chunk_ids):
        return None

    monkeypatch.setattr(maint, "_after_prune_chunk_committed", noop_after_commit)
    result = run_async(maint.prune_old_data(keep_days=30, actor="test"))
    assert result["sessions_pruned"] == 1

    # Prior archives are untouched; a new one was added for the rerun.
    archives_after_rerun = sorted(archive_dir.glob("*.zip"))
    assert len(archives_after_rerun) == 3
    for old in archives_after_interrupt:
        assert old in archives_after_rerun

    async def remaining_ids():
        async with StateDB(db_path) as db:
            rows = await db.fetch_all("SELECT id FROM sessions")
            return {r["id"] for r in rows}

    assert run_async(remaining_ids()) == set()
