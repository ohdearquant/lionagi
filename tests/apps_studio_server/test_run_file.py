# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for the read-only run-artifact file endpoint (get_run_file)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

aiosqlite = pytest.importorskip("aiosqlite", reason="aiosqlite not installed")
fastapi = pytest.importorskip("fastapi", reason="studio extra not installed")

from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from lionagi.libs.path_safety import resolve_workspace_path  # noqa: E402
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


# ---------------------------------------------------------------------------
# Bounded read: the cap must be enforced by the read itself, not by slicing
# an already-materialized full read (Path.read_bytes() then [:cap] would
# allocate the whole file before the cap applies).
# ---------------------------------------------------------------------------


async def test_content_read_never_calls_path_read_bytes(patched_runs_svc, tmp_path, monkeypatch):
    svc, db_path = patched_runs_svc
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    target = artifact_root / "small.txt"
    target.write_text("hello world")
    await seed_session(db_path, session_id="run-9", artifacts_path=str(artifact_root))

    def _boom(self, *a, **k):
        raise AssertionError("Path.read_bytes must not be used for run-file content reads")

    monkeypatch.setattr(Path, "read_bytes", _boom)

    result = await svc.get_run_file("run-9", str(target))
    assert result["content"] == "hello world"


async def test_bounded_read_requests_at_most_cap_plus_one_byte(
    patched_runs_svc, tmp_path, monkeypatch
):
    """A large file must never be fully read — only cap+1 bytes are requested,
    via a single os.read() call on the no-follow descriptor."""
    svc, db_path = patched_runs_svc
    monkeypatch.setattr(svc, "_MAX_FILE_READ_BYTES", 1024)
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    huge = artifact_root / "huge.log"
    # Real file well over the cap — proves the cap is enforced by the read
    # size, not by slicing a full read after the fact.
    huge.write_bytes(b"y" * (5 * 1024 * 1024))
    await seed_session(db_path, session_id="run-10", artifacts_path=str(artifact_root))

    real_read = os.read
    requested_sizes: list[int] = []

    def _tracking_read(fd, n):
        requested_sizes.append(n)
        return real_read(fd, n)

    monkeypatch.setattr(os, "read", _tracking_read)

    result = await svc.get_run_file("run-10", str(huge))

    assert requested_sizes == [1025]  # cap + 1, exactly one call
    assert result["truncated"] is True
    assert len(result["content"]) == 1024
    assert result["size"] == 5 * 1024 * 1024


# ---------------------------------------------------------------------------
# Symlink swap after validation (TOCTOU): resolve_workspace_path validates a
# path that was a regular file at check time; a later open-by-path would
# follow whatever occupies that name at open time. The no-follow descriptor
# walk must refuse the swapped target instead.
# ---------------------------------------------------------------------------


async def test_open_helper_refuses_target_swapped_to_symlink_after_validation(tmp_path):
    import lionagi.studio.services.runs as runs_svc

    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("top secret")
    target = artifact_root / "review.md"
    target.write_text("looks good")

    root = artifact_root.resolve()
    # Validate while `target` is still a regular file inside root.
    resolved = resolve_workspace_path(str(target), root)

    # Simulate the race: swap the validated path for a symlink pointing
    # outside root before the read-side open happens.
    target.unlink()
    target.symlink_to(outside)

    with pytest.raises((OSError, PermissionError)):
        runs_svc._open_regular_file_no_follow(root, resolved)


async def test_open_helper_refuses_intermediate_dir_swapped_to_symlink(tmp_path):
    import lionagi.studio.services.runs as runs_svc

    artifact_root = tmp_path / "artifacts"
    (artifact_root / "sub").mkdir(parents=True)
    outside_dir = tmp_path / "outside_dir"
    outside_dir.mkdir()
    (outside_dir / "leak.txt").write_text("leaked")
    target = artifact_root / "sub" / "notes.txt"
    target.write_text("hello")

    root = artifact_root.resolve()
    resolved = resolve_workspace_path("sub/notes.txt", root)

    # Swap the intermediate directory for a symlink after validation.
    import shutil

    shutil.rmtree(artifact_root / "sub")
    (artifact_root / "sub").symlink_to(outside_dir)

    with pytest.raises((OSError, PermissionError)):
        runs_svc._open_regular_file_no_follow(root, resolved)


# ---------------------------------------------------------------------------
# Endpoint-level coverage: exercise GET /api/runs/{run_id}/file through the
# actual FastAPI route (TestClient), not just the service function directly.
# ---------------------------------------------------------------------------


def _make_client(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    import lionagi.studio.services.sessions as sessions_mod

    monkeypatch.setattr(sessions_mod, "_DB", str(db_path))
    monkeypatch.setattr(sessions_mod, "DEFAULT_DB_PATH", db_path)

    from lionagi.studio.app import app

    return TestClient(app, base_url="http://127.0.0.1:8765")


async def _seed(db_path: Path, *, session_id: str, artifacts_path: str) -> None:
    await seed_session(db_path, session_id=session_id, artifacts_path=artifacts_path)


def test_route_happy_path(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    client = _make_client(db_path, monkeypatch)
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    target = artifact_root / "review.md"
    target.write_text("# Review\n\nLooks good.")
    _run_async(_seed(db_path, session_id="route-1", artifacts_path=str(artifact_root)))

    resp = client.get("/api/runs/route-1/file", params={"path": str(target)})
    assert resp.status_code == 200
    body = resp.json()
    assert body["content"] == "# Review\n\nLooks good."
    assert body["truncated"] is False


def test_route_traversal_rejected_without_leaking_resolved_path(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    client = _make_client(db_path, monkeypatch)
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("top secret")
    _run_async(_seed(db_path, session_id="route-2", artifacts_path=str(artifact_root)))

    resp = client.get("/api/runs/route-2/file", params={"path": "../secret.txt"})
    assert resp.status_code == 403
    assert str(secret.resolve()) not in resp.text
    assert "top secret" not in resp.text


def test_route_absolute_escape_rejected(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    client = _make_client(db_path, monkeypatch)
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("top secret")
    _run_async(_seed(db_path, session_id="route-3", artifacts_path=str(artifact_root)))

    resp = client.get("/api/runs/route-3/file", params={"path": str(secret)})
    assert resp.status_code == 403
    assert "top secret" not in resp.text


def test_route_final_symlink_escape_rejected(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    client = _make_client(db_path, monkeypatch)
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("nope")
    link = artifact_root / "escape.txt"
    link.symlink_to(outside)
    _run_async(_seed(db_path, session_id="route-4", artifacts_path=str(artifact_root)))

    resp = client.get("/api/runs/route-4/file", params={"path": str(link)})
    assert resp.status_code == 403
    assert "nope" not in resp.text


def test_route_intermediate_symlink_dir_escape_rejected(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    client = _make_client(db_path, monkeypatch)
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    outside_dir = tmp_path / "outside_dir"
    outside_dir.mkdir()
    (outside_dir / "leak.txt").write_text("leaked")
    (artifact_root / "linked_dir").symlink_to(outside_dir)
    _run_async(_seed(db_path, session_id="route-5", artifacts_path=str(artifact_root)))

    resp = client.get("/api/runs/route-5/file", params={"path": "linked_dir/leak.txt"})
    assert resp.status_code == 403
    assert "leaked" not in resp.text


def test_route_protected_basename_rejected(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    client = _make_client(db_path, monkeypatch)
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    dotenv = artifact_root / ".env"
    dotenv.write_text("SECRET=abc123")
    _run_async(_seed(db_path, session_id="route-6", artifacts_path=str(artifact_root)))

    resp = client.get("/api/runs/route-6/file", params={"path": str(dotenv)})
    assert resp.status_code == 403
    assert "abc123" not in resp.text


def test_route_non_utf8_returns_415(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    client = _make_client(db_path, monkeypatch)
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    binary = artifact_root / "data.bin"
    binary.write_bytes(b"\xff\xfe\x00\x01binary")
    _run_async(_seed(db_path, session_id="route-7", artifacts_path=str(artifact_root)))

    resp = client.get("/api/runs/route-7/file", params={"path": str(binary)})
    assert resp.status_code == 415


def test_route_bounded_read_and_truncation(tmp_path, monkeypatch):
    import lionagi.studio.services.runs as runs_svc

    db_path = tmp_path / "state.db"
    client = _make_client(db_path, monkeypatch)
    monkeypatch.setattr(runs_svc, "_MAX_FILE_READ_BYTES", 16)
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    big = artifact_root / "big.log"
    big.write_text("z" * 1000)
    _run_async(_seed(db_path, session_id="route-8", artifacts_path=str(artifact_root)))

    resp = client.get("/api/runs/route-8/file", params={"path": str(big)})
    assert resp.status_code == 200
    body = resp.json()
    assert body["truncated"] is True
    assert len(body["content"]) == 16
    assert body["size"] == 1000


def test_route_missing_file_returns_404_without_leaking_resolved_path(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    client = _make_client(db_path, monkeypatch)
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    _run_async(_seed(db_path, session_id="route-9", artifacts_path=str(artifact_root)))

    # Relative, caller-supplied path — the server-resolved absolute path
    # must not appear in the 404 body even though it never existed.
    resp = client.get("/api/runs/route-9/file", params={"path": "nope.md"})
    assert resp.status_code == 404
    assert str(artifact_root.resolve()) not in resp.text


def _run_async(coro):
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
