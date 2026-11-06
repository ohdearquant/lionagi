# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Integration tests for imported manifest names and stale-session reapers."""

import json
import time
import uuid
from pathlib import Path

import pytest

from lionagi.cli import kill as kill_mod
from lionagi.cli import state as state_mod
from lionagi.state import db as db_mod
from lionagi.state.db import StateDB
from lionagi.state.reasons import RunReasons, SessionReasons
from lionhub.orchestration import job as _util
from lionhub.orchestration import kill as _kill_service
from lionhub.orchestration import runs as _runs

pytestmark = pytest.mark.asyncio


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    monkeypatch.setattr(db_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(
        db_mod, "settings", db_mod.settings.model_copy(update={"LIONAGI_STATE_DB_URL": None})
    )
    monkeypatch.setattr(state_mod, "RUNS_ROOT", runs_root)
    monkeypatch.setattr(_runs, "RUNS_ROOT", runs_root)
    return db_path, runs_root


async def _table_counts(db):
    counts = {}
    for table in ("sessions", "branches", "messages", "progressions", "status_transitions"):
        row = await db.fetch_one(f"SELECT COUNT(*) AS n FROM {table}")
        counts[table] = row["n"]
    return counts


async def test_import_rejects_mismatched_manifest_id_before_any_write(isolated_state, capsys):
    """A run directory and its differently named manifest cannot produce either session."""
    db_path, runs_root = isolated_state
    directory_name = "run-on-disk"
    manifest_id = "different-run-id"
    run_dir = runs_root / directory_name
    run_dir.mkdir()
    (run_dir / "run.json").write_text(
        json.dumps({"run_id": manifest_id, "status": "running", "started_at": time.time() - 7200})
    )
    async with StateDB(db_path) as db:
        before = await _table_counts(db)

    counts = await state_mod._import_runs()
    output = capsys.readouterr().out

    assert counts == {"sessions": 0, "branches": 0, "messages": 0, "skipped": 0, "errors": 1}
    assert directory_name in output
    assert manifest_id in output
    async with StateDB(db_path) as db:
        assert await db.get_session(directory_name) is None
        assert await db.get_session(manifest_id) is None
        assert await _table_counts(db) == before


@pytest.mark.parametrize("missing_id", [False, True], ids=["matching", "legacy-missing-id"])
async def test_import_accepts_matching_or_missing_manifest_id(isolated_state, missing_id):
    db_path, runs_root = isolated_state
    run_id = "valid-run"
    run_dir = runs_root / run_id
    run_dir.mkdir()
    manifest = {"status": "running", "started_at": time.time() - 7200}
    if not missing_id:
        manifest["run_id"] = run_id
    (run_dir / "run.json").write_text(json.dumps(manifest))

    counts = await state_mod._import_runs()

    assert counts == {"sessions": 1, "branches": 0, "messages": 0, "skipped": 0, "errors": 0}
    async with StateDB(db_path) as db:
        row = await db.get_session(run_id)
    assert row is not None and row["source_kind"] == "imported_fs"


@pytest.mark.parametrize("manifest_id", [None, "", 12], ids=["null", "empty", "number"])
async def test_import_rejects_present_nonmatching_manifest_id(isolated_state, manifest_id, capsys):
    db_path, runs_root = isolated_state
    run_dir = runs_root / "run-on-disk"
    run_dir.mkdir()
    (run_dir / "run.json").write_text(json.dumps({"run_id": manifest_id, "status": "running"}))

    counts = await state_mod._import_runs()

    assert counts["errors"] == 1
    assert counts["sessions"] == 0
    output = capsys.readouterr().out
    assert "run-on-disk" in output
    assert repr(manifest_id) in output
    async with StateDB(db_path) as db:
        assert await db.get_session("run-on-disk") is None


@pytest.mark.parametrize("reaper", ["sweep", "doctor"])
@pytest.mark.parametrize("kind", ["traversal", "linked"])
async def test_reaper_never_reads_manifest_outside_named_directory(
    isolated_state, tmp_path, monkeypatch, capsys, reaper, kind
):
    """The unsafe row takes today's stale transition; the live control stays running."""
    db_path, runs_root = isolated_state
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_manifest = outside / "run.json"
    live_manifest = {"status": "running", "pid": 4242, "pid_create_time": 1005.0}
    outside_manifest.write_text(json.dumps(live_manifest))
    if kind == "linked":
        (runs_root / "linked").symlink_to(outside, target_is_directory=True)
    unsafe_id = "../outside" if kind == "traversal" else "linked"

    control_id = "live-control"
    control = runs_root / control_id
    control.mkdir()
    (control / "run.json").write_text(json.dumps(live_manifest))
    probes = []

    def alive(pid):
        probes.append(("alive", pid))
        return pid == 4242

    def identity(pid, *_args, **_kwargs):
        probes.append(("identity", pid))
        return "ours" if pid == 4242 else "not_ours"

    def no_signal(*_args, **_kwargs):
        raise AssertionError("a stale reaper must not signal a process")

    monkeypatch.setattr(kill_mod, "_pid_alive", alive)
    monkeypatch.setattr(_kill_service, "_pid_alive", alive)
    monkeypatch.setattr(_util, "pid_alive", alive)
    monkeypatch.setattr(kill_mod, "check_pid_identity", identity)
    monkeypatch.setattr(_kill_service, "check_pid_identity", identity)
    monkeypatch.setattr(_kill_service, "_terminate_pid", no_signal)
    monkeypatch.setattr(_kill_service.os, "kill", no_signal)

    # The live-looking control proves the positive fake-identity path is reachable.
    assert _kill_service._imported_degraded_process_identity(control_id) == ("ours", None)
    assert ("identity", 4242) in probes
    probes.clear()

    read_text = Path.read_text
    target_reads = []
    outside_target = outside_manifest.resolve()

    def record_read(path, *args, **kwargs):
        if path.name == "run.json" and path.resolve() == outside_target:
            target_reads.append(path)
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", record_read)
    async with StateDB(db_path) as db:
        for sid in (unsafe_id, control_id):
            progression = str(uuid.uuid4())
            await db.create_progression(progression)
            await db.create_session(
                {
                    "id": sid,
                    "progression_id": progression,
                    "status": "running",
                    "source_kind": "imported_fs",
                    "started_at": time.time() - 48 * 3600,
                    "node_metadata": None,
                }
            )

    if reaper == "sweep":
        assert await kill_mod._do_kill_all_stale(threshold_seconds=3600) == 0
        assert "skipped_live_pid=1" in capsys.readouterr().out
        status, reason = "cancelled", RunReasons.CANCELLED_STALE_AUTO
    else:
        assert await state_mod._doctor(stale_hours=24, dry_run=False) == {
            "running": 2,
            "swept": 1,
            "skipped": 1,
        }
        status, reason = "aborted", SessionReasons.HEALTH_STALE_NO_HEARTBEAT

    assert target_reads == []
    assert ("identity", 4242) in probes
    async with StateDB(db_path) as db:
        assert (await db.get_session(unsafe_id))["status"] == status
        assert (await db.get_session(control_id))["status"] == "running"
        transition = await db.fetch_one(
            "SELECT reason_code FROM status_transitions "
            "WHERE entity_id = ? AND previous_status = 'running' AND status = ?",
            (unsafe_id, status),
        )
    assert transition is not None and transition["reason_code"] == reason
