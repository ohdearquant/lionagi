# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Regression tests: allocate_run must create the artifact root it hands out.

A session's artifacts_path is persisted at allocation time (cli/agent.py passes
str(run.artifact_root) straight into setup_agent_persist), but allocate_run used
to call only ensure_state_dirs() -- never ensure_artifact_root(). Every fresh
run's recorded artifacts_path pointed at a directory that didn't exist yet,
which the Studio lifecycle reaper's phantom classifier reads as evidence of a
crashed/lost session (see tests/apps_studio_server/test_admin.py's
_classify_phantom coverage).
"""

from __future__ import annotations

import json

import lionagi.cli._runs as runs_mod
from lionagi.cli._runs import allocate_run


def test_allocate_run_creates_artifact_root_default(tmp_path, monkeypatch):
    """Default (no --save) form: artifact_root lives under state_root/artifacts."""
    monkeypatch.setattr(runs_mod, "RUNS_ROOT", tmp_path / "runs")

    run = allocate_run()

    assert run.artifact_root.is_dir()
    assert run.artifact_root == run.state_root / "artifacts"


def test_allocate_run_creates_artifact_root_save_dir(tmp_path, monkeypatch):
    """Explicit --save dir form: the caller-supplied directory is also created."""
    monkeypatch.setattr(runs_mod, "RUNS_ROOT", tmp_path / "runs")
    save_dir = tmp_path / "explicit-save" / "nested"

    run = allocate_run(save_dir=str(save_dir))

    assert run.artifact_root == save_dir.resolve()
    assert run.artifact_root.is_dir()


def test_allocate_run_artifact_root_creation_is_idempotent(tmp_path, monkeypatch):
    """Re-allocating the same run_id (subprocess handoff) doesn't error on an
    already-existing artifact root."""
    monkeypatch.setattr(runs_mod, "RUNS_ROOT", tmp_path / "runs")

    first = allocate_run(run_id="fixed-run-id")
    second = allocate_run(run_id="fixed-run-id")

    assert first.artifact_root == second.artifact_root
    assert second.artifact_root.is_dir()


def test_allocate_run_writes_running_placeholder_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(runs_mod, "RUNS_ROOT", tmp_path / "runs")

    run = allocate_run(run_id="allocated-run")

    manifest = json.loads(run.manifest_path.read_text())
    assert manifest["run_id"] == "allocated-run"
    assert manifest["status"] == "running"
    assert manifest["started_at"] > 0
    assert manifest["ended_at"] is None


def test_write_manifest_replaces_rather_than_merges(tmp_path, monkeypatch):
    """write_manifest is a pure replacement write: a field from an earlier
    write that the current write doesn't repeat must not survive."""
    monkeypatch.setattr(runs_mod, "RUNS_ROOT", tmp_path / "runs")
    run = allocate_run(run_id="replace-run")

    run.write_manifest({"status": "running", "stale_only_field": "should-not-survive"})
    run.write_manifest({"status": "completed", "ended_at": 1.0})

    manifest = json.loads(run.manifest_path.read_text())
    assert manifest["status"] == "completed"
    assert manifest["ended_at"] == 1.0
    assert "stale_only_field" not in manifest


def test_write_manifest_never_reads_a_corrupt_pre_existing_file(tmp_path, monkeypatch):
    """write_manifest must not read the on-disk file before writing, so a
    corrupt/non-JSON run.json (partial write, disk issue) never blocks a
    fresh write from succeeding."""
    monkeypatch.setattr(runs_mod, "RUNS_ROOT", tmp_path / "runs")
    run = allocate_run(run_id="corrupt-run")
    run.manifest_path.write_text("{bad-json")

    run.write_manifest({"status": "completed", "ended_at": 2.0})

    manifest = json.loads(run.manifest_path.read_text())
    assert manifest["status"] == "completed"
    assert manifest["ended_at"] == 2.0


def test_write_notify_outcome_replaces_and_is_atomic(tmp_path, monkeypatch):
    """write_notify_outcome is a separate file from the manifest, pure
    replacement semantics, written via tmp + os.replace (no partial file
    left behind on success)."""
    monkeypatch.setattr(runs_mod, "RUNS_ROOT", tmp_path / "runs")
    run = allocate_run(run_id="outcome-run")

    run.write_notify_outcome({"ok": False, "exit_code": 1, "stderr_path": "boom"})
    run.write_notify_outcome({"ok": True, "exit_code": 0, "stderr_path": None})

    outcome = json.loads(run.notify_outcome_path.read_text())
    assert outcome == {"ok": True, "exit_code": 0, "stderr_path": None}
    assert not run.notify_outcome_path.with_suffix(".json.tmp").exists()

    manifest = json.loads(run.manifest_path.read_text())
    assert "notify_outcome" not in manifest


def test_write_manifest_is_atomic_no_reader_sees_a_partial_file(tmp_path, monkeypatch):
    """A concurrent reader of run.json must observe either the previous
    manifest or the new one, never a truncated file.

    A plain truncate-and-write leaves the file empty (or half-written) for the
    duration of the write, so any reader that lands in that window gets a
    JSONDecodeError. Writing to a temp file and renaming it into place makes
    the swap a single filesystem operation.
    """
    import threading

    monkeypatch.setattr(runs_mod, "RUNS_ROOT", tmp_path / "runs")
    run = allocate_run(run_id="atomic-manifest-run")

    # A payload large enough that a non-atomic write spends real time in the
    # partially-written state.
    big_value = "x" * 200_000
    run.write_manifest({"status": "running", "blob": big_value})

    errors: list[Exception] = []
    stop = threading.Event()

    def _reader() -> None:
        while not stop.is_set():
            try:
                json.loads(run.manifest_path.read_text())
            except FileNotFoundError:
                continue
            except Exception as exc:  # noqa: BLE001 -- the property under test
                errors.append(exc)
                return

    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()
    try:
        for i in range(60):
            run.write_manifest({"status": "running", "iteration": i, "blob": big_value})
    finally:
        stop.set()
        reader.join(timeout=5)

    assert not errors, f"reader observed a partial manifest: {errors[0]!r}"
    assert json.loads(run.manifest_path.read_text())["iteration"] == 59
    # The temp file is never left behind.
    assert not list(run.state_root.glob("*.tmp"))
