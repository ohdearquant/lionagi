# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""A path-resolution failure must not authorize a stale terminal transition."""

import json
import time
import uuid
from pathlib import Path

import pytest

from lionagi.state import db as db_mod
from lionagi.state.db import StateDB
from lionhub.orchestration import job as _util
from lionhub.orchestration import kill as _kill_service
from lionhub.orchestration import runs as _runs
from lionhub.serve.cli import kill as kill_mod
from lionhub.serve.cli import state as state_mod

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("reaper", ["sweep", "doctor"])
@pytest.mark.parametrize("fault", ["directory", "manifest"])
async def test_resolution_failure_propagates_without_terminal_transition(
    tmp_path, monkeypatch, reaper, fault
):
    db_path = tmp_path / "state.db"
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    monkeypatch.setattr(db_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(
        db_mod, "settings", db_mod.settings.model_copy(update={"LIONAGI_STATE_DB_URL": None})
    )
    monkeypatch.setattr(_runs, "RUNS_ROOT", runs_root)
    sid, control_id = "faulted-run", "live-control"
    manifest = {"status": "running", "pid": 4242, "pid_create_time": 1005.0}
    for name in (sid, control_id):
        run_dir = runs_root / name
        run_dir.mkdir()
        (run_dir / "run.json").write_text(json.dumps(manifest))

    def alive(pid):
        assert pid == 4242
        return True

    def identity(pid, *_args, **_kwargs):
        assert pid == 4242
        return "ours"

    def no_signal(*_args, **_kwargs):
        raise AssertionError("a stale reaper must not signal a process")

    monkeypatch.setattr(kill_mod, "_pid_alive", alive)
    monkeypatch.setattr(_kill_service, "_pid_alive", alive)
    monkeypatch.setattr(_util, "pid_alive", alive)
    monkeypatch.setattr(kill_mod, "check_pid_identity", identity)
    monkeypatch.setattr(_kill_service, "check_pid_identity", identity)
    monkeypatch.setattr(_kill_service, "_terminate_pid", no_signal)
    monkeypatch.setattr(_kill_service.os, "kill", no_signal)

    async with StateDB(db_path) as db:
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
        before = await db.fetch_all(
            "SELECT status, reason_code FROM status_transitions WHERE entity_id = ?", (sid,)
        )

    # The same liveness fixtures must classify an unaffected manifest as live.
    assert _kill_service._imported_degraded_process_identity(control_id) == ("ours", None)

    real_resolve = Path.resolve
    target_dir = runs_root.resolve() / sid
    target_manifest = target_dir / "run.json"
    faulted = []

    def resolve_with_fault(path, *args, **kwargs):
        # An inaccessible parent prevents resolving the manifest path.
        targeted = path == target_manifest or (fault == "directory" and path == target_dir)
        if targeted:
            faulted.append(path)
            raise PermissionError("fixture cannot resolve path")
        return real_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve_with_fault)
    assert _kill_service._imported_degraded_process_identity(control_id) == ("ours", None)

    with pytest.raises(PermissionError, match="fixture cannot resolve path"):
        if reaper == "sweep":
            await kill_mod._do_kill_all_stale(threshold_seconds=3600)
        else:
            await state_mod._doctor(stale_hours=24, dry_run=False)

    assert faulted
    async with StateDB(db_path) as db:
        assert (await db.get_session(sid))["status"] == "running"
        after = await db.fetch_all(
            "SELECT status, reason_code FROM status_transitions WHERE entity_id = ?", (sid,)
        )
    assert after == before
