# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for the read-only run-artifact file endpoint (get_run_file)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

aiosqlite = pytest.importorskip("aiosqlite", reason="aiosqlite not installed")
fastapi = pytest.importorskip("fastapi", reason="studio extra not installed")

from fastapi import HTTPException  # noqa: E402

from lionagi.state.db import StateDB  # noqa: E402


async def seed_session(db_path: Path, *, session_id: str, artifacts_path: str) -> None:
    prog_id = f"{session_id}-prog"
    async with StateDB(db_path) as db:
        await db.create_progression(prog_id)
        await db.create_session(
            {
                "id": session_id,
                "progression_id": prog_id,
                "name": f"run-{session_id}",
                "status": "completed",
                "artifacts_path": artifacts_path,
                "source_kind": "live",
            }
        )


@pytest.fixture
def patched_runs_svc(tmp_path: Path, monkeypatch: Any):
    import lionagi.studio.services.sessions as sessions_mod

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(sessions_mod, "_DB", str(db_path))
    monkeypatch.setattr(sessions_mod, "DEFAULT_DB_PATH", db_path)

    import lionagi.studio.services.runs as runs_svc

    return runs_svc, db_path


async def test_happy_path_reads_file_content(patched_runs_svc, tmp_path):
    svc, db_path = patched_runs_svc
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    target = artifact_root / "review.md"
    target.write_text("# Review\n\nLooks good.")

    await seed_session(db_path, session_id="run-1", artifacts_path=str(artifact_root))

    result = await svc.get_run_file("run-1", str(target))
    assert result["content"] == "# Review\n\nLooks good."
    assert result["truncated"] is False
    assert result["size"] == len("# Review\n\nLooks good.")


async def test_relative_path_resolves_under_artifact_root(patched_runs_svc, tmp_path):
    svc, db_path = patched_runs_svc
    artifact_root = tmp_path / "artifacts"
    (artifact_root / "sub").mkdir(parents=True)
    (artifact_root / "sub" / "notes.txt").write_text("hello")

    await seed_session(db_path, session_id="run-2", artifacts_path=str(artifact_root))

    result = await svc.get_run_file("run-2", "sub/notes.txt")
    assert result["content"] == "hello"


async def test_missing_file_returns_404(patched_runs_svc, tmp_path):
    svc, db_path = patched_runs_svc
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    await seed_session(db_path, session_id="run-3", artifacts_path=str(artifact_root))

    with pytest.raises(HTTPException) as exc_info:
        await svc.get_run_file("run-3", str(artifact_root / "nope.md"))
    assert exc_info.value.status_code == 404


async def test_unknown_run_returns_404(patched_runs_svc):
    svc, _db_path = patched_runs_svc
    with pytest.raises(HTTPException) as exc_info:
        await svc.get_run_file("no-such-run", "/tmp/anything.md")
    assert exc_info.value.status_code == 404


async def test_traversal_outside_artifact_root_rejected(patched_runs_svc, tmp_path):
    svc, db_path = patched_runs_svc
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("top secret")
    await seed_session(db_path, session_id="run-4", artifacts_path=str(artifact_root))

    with pytest.raises(HTTPException) as exc_info:
        await svc.get_run_file("run-4", "../secret.txt")
    assert exc_info.value.status_code == 403


async def test_absolute_path_outside_artifact_root_rejected(patched_runs_svc, tmp_path):
    svc, db_path = patched_runs_svc
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("top secret")
    await seed_session(db_path, session_id="run-5", artifacts_path=str(artifact_root))

    with pytest.raises(HTTPException) as exc_info:
        await svc.get_run_file("run-5", str(secret))
    assert exc_info.value.status_code == 403


async def test_symlink_escape_rejected(patched_runs_svc, tmp_path):
    svc, db_path = patched_runs_svc
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("nope")
    link = artifact_root / "escape.txt"
    link.symlink_to(outside)
    await seed_session(db_path, session_id="run-6", artifacts_path=str(artifact_root))

    with pytest.raises(HTTPException) as exc_info:
        await svc.get_run_file("run-6", str(link))
    assert exc_info.value.status_code == 403


async def test_symlinked_directory_escape_rejected(patched_runs_svc, tmp_path):
    svc, db_path = patched_runs_svc
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    outside_dir = tmp_path / "outside_dir"
    outside_dir.mkdir()
    (outside_dir / "leak.txt").write_text("leaked")
    (artifact_root / "linked_dir").symlink_to(outside_dir)
    await seed_session(db_path, session_id="run-7", artifacts_path=str(artifact_root))

    with pytest.raises(HTTPException) as exc_info:
        await svc.get_run_file("run-7", "linked_dir/leak.txt")
    assert exc_info.value.status_code == 403


async def test_large_file_is_truncated(patched_runs_svc, tmp_path, monkeypatch):
    svc, db_path = patched_runs_svc
    monkeypatch.setattr(svc, "_MAX_FILE_READ_BYTES", 10)
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    big = artifact_root / "big.log"
    big.write_text("x" * 100)
    await seed_session(db_path, session_id="run-8", artifacts_path=str(artifact_root))

    result = await svc.get_run_file("run-8", str(big))
    assert result["truncated"] is True
    assert len(result["content"]) == 10
    assert result["size"] == 100
