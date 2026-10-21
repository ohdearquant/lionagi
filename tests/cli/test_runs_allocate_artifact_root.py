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
import os
from pathlib import Path

import pytest

import lionagi.cli._runs as runs_mod
from lionagi.cli._mcp_resolve import McpResolution
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


def test_inherited_run_id_is_consumed_by_one_allocation(tmp_path, monkeypatch):
    """A one-hop handoff cannot leak into a later descendant allocation."""
    monkeypatch.setattr(runs_mod, "RUNS_ROOT", tmp_path / "runs")
    monkeypatch.setenv("LIONAGI_RUN_ID", "handoff-run")

    handed_off = allocate_run()
    handed_off.write_manifest({"status": "running", "owner": "handoff"})
    descendant = allocate_run(save_dir=tmp_path / "descendant-artifacts")

    assert descendant.run_id != handed_off.run_id
    assert runs_mod.current_run_id() is None
    assert handed_off.read_manifest()["owner"] == "handoff"


def test_explicit_run_id_still_consumes_inherited_handoff(tmp_path, monkeypatch):
    """An explicit child identity must not leave its parent's handoff for a sibling."""
    monkeypatch.setattr(runs_mod, "RUNS_ROOT", tmp_path / "runs")
    monkeypatch.setenv("LIONAGI_RUN_ID", "parent-run")

    explicit_child = allocate_run(run_id="explicit-child")
    next_child = allocate_run()

    assert explicit_child.run_id == "explicit-child"
    assert next_child.run_id not in {"explicit-child", "parent-run"}
    assert runs_mod.current_run_id() is None


def test_allocate_run_writes_running_placeholder_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(runs_mod, "RUNS_ROOT", tmp_path / "runs")

    run = allocate_run(run_id="allocated-run")

    manifest = json.loads(run.manifest_path.read_text())
    assert manifest["run_id"] == "allocated-run"
    assert manifest["status"] == "running"
    assert manifest["started_at"] > 0
    assert manifest["ended_at"] is None


def test_allocate_run_records_resolved_cwd_and_mcp_discovery_provenance(tmp_path, monkeypatch):
    runs_root = tmp_path / "runs"
    launch_dir = tmp_path / "workspace" / "nested"
    launch_dir.mkdir(parents=True)
    config_path = tmp_path / "workspace" / ".mcp.json"
    config_path.write_text('{"mcpServers": {"tools": {}}}')
    monkeypatch.setattr(runs_mod, "RUNS_ROOT", runs_root)
    monkeypatch.chdir(tmp_path)
    resolution = McpResolution(
        servers={"tools": {}},
        reason=None,
        source=config_path,
        searched_from=launch_dir,
        explicit=False,
    )

    run = allocate_run(run_id="provenance-run")
    run = run.with_execution_provenance(
        cwd=Path("workspace") / "nested" / "..",
        mcp_resolution=resolution,
    )

    manifest = run.read_manifest()
    assert manifest["cwd"] == str((tmp_path / "workspace").resolve())
    assert manifest["mcp_config_source"] == str(config_path.resolve())
    assert manifest["mcp_config_searched_from"] == str(launch_dir.resolve())
    assert manifest["mcp_config_explicit"] is False
    assert manifest["mcp_config_reason"] is None


def test_path_resolved_run_preserves_execution_provenance(tmp_path, monkeypatch):
    import lionagi.cli.orchestrate._checkpoint as checkpoint_mod

    runs_root = tmp_path / "runs"
    launch_dir = tmp_path / "workspace"
    launch_dir.mkdir()
    source = launch_dir / ".mcp.json"
    source.write_text('{"mcpServers": {"tools": {}}}')
    monkeypatch.setattr(runs_mod, "RUNS_ROOT", runs_root)
    monkeypatch.setattr(checkpoint_mod, "RUNS_ROOT", runs_root)
    resolution = McpResolution(
        servers={"tools": {}},
        reason=None,
        source=source,
        searched_from=launch_dir,
        explicit=False,
    )

    run = allocate_run(run_id="path-resolved-run")
    run = run.with_execution_provenance(cwd=launch_dir, mcp_resolution=resolution)
    reopened = checkpoint_mod._find_run_dir_by_id(run.run_id)
    assert reopened is not None

    reopened.write_manifest({"status": "completed", "ended_at": 1.0})

    manifest = reopened.read_manifest()
    assert manifest["cwd"] == str(launch_dir.resolve())
    assert manifest["mcp_config_source"] == str(source.resolve())
    assert manifest["mcp_config_searched_from"] == str(launch_dir.resolve())
    assert manifest["mcp_config_explicit"] is False
    assert manifest["mcp_config_reason"] is None


def test_a_manifest_that_cannot_be_read_stops_the_write_instead_of_dropping_provenance(
    tmp_path, monkeypatch
):
    if os.geteuid() == 0:
        pytest.skip("root reads a file whose mode denies it, so the case cannot be set up")
    import lionagi.cli.orchestrate._checkpoint as checkpoint_mod

    runs_root = tmp_path / "runs"
    launch_dir = tmp_path / "workspace"
    launch_dir.mkdir()
    monkeypatch.setattr(runs_mod, "RUNS_ROOT", runs_root)
    monkeypatch.setattr(checkpoint_mod, "RUNS_ROOT", runs_root)
    resolution = McpResolution(
        servers=None, reason="no_mcp_config_found", source=None, searched_from=launch_dir
    )
    run = allocate_run(run_id="unreadable-run")
    run = run.with_execution_provenance(cwd=launch_dir, mcp_resolution=resolution)
    reopened = checkpoint_mod._find_run_dir_by_id(run.run_id)
    assert reopened is not None and reopened.cwd is None
    before = run.manifest_path.read_bytes()

    run.manifest_path.chmod(0)
    try:
        with pytest.raises(PermissionError):
            reopened.write_manifest({"status": "completed", "ended_at": 1.0})
    finally:
        run.manifest_path.chmod(0o644)

    assert run.manifest_path.read_bytes() == before


def test_manifest_provenance_survives_whole_file_live_persist_replacement(tmp_path, monkeypatch):
    monkeypatch.setattr(runs_mod, "RUNS_ROOT", tmp_path / "runs")
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    resolution = McpResolution(
        servers=None,
        reason="no_mcp_config_found",
        source=None,
        searched_from=cwd,
    )
    run = allocate_run(run_id="replace-provenance")
    run = run.with_execution_provenance(cwd=cwd, mcp_resolution=resolution)

    run.write_manifest({"status": "completed", "ended_at": 1.0})

    manifest = run.read_manifest()
    assert manifest["cwd"] == str(cwd.resolve())
    assert manifest["mcp_config_source"] is None
    assert manifest["mcp_config_searched_from"] == str(cwd.resolve())
    assert manifest["mcp_config_explicit"] is False
    assert manifest["mcp_config_reason"] == "no_mcp_config_found"


def test_allocate_run_records_explicit_mcp_disable_reason(tmp_path, monkeypatch):
    monkeypatch.setattr(runs_mod, "RUNS_ROOT", tmp_path / "runs")
    resolution = McpResolution(
        servers=None,
        reason=None,
        source=None,
        searched_from=tmp_path,
    )

    run = allocate_run(run_id="mcp-disabled")
    run = run.with_execution_provenance(
        cwd=tmp_path,
        mcp_resolution=resolution,
        mcp_disabled=True,
    )

    manifest = run.read_manifest()
    assert manifest["mcp_config_source"] is None
    assert manifest["mcp_config_reason"] == "mcp_disabled_by_caller"


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


def test_binding_provenance_holds_the_manifest_lock_across_its_read_and_rewrite(
    tmp_path, monkeypatch
):
    """Another invocation sharing the run id writes run.json under its lock; a
    rebind that read outside the lock could overwrite that write with stale data."""
    fcntl = pytest.importorskip("fcntl")
    monkeypatch.setattr(runs_mod, "RUNS_ROOT", tmp_path / "runs")
    run = allocate_run(run_id="rebind-under-lock")
    resolution = McpResolution(
        servers=None, reason="no_mcp_config_found", source=None, searched_from=tmp_path
    )
    held_at_read = []
    actual_read = runs_mod.RunDir.read_manifest

    def observe_read(self):
        fd = os.open(self.state_root / ".run.json.lock", os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            held_at_read.append(True)
        else:
            held_at_read.append(False)
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
        return actual_read(self)

    monkeypatch.setattr(runs_mod.RunDir, "read_manifest", observe_read)
    rebound = run.with_execution_provenance(cwd=tmp_path, mcp_resolution=resolution)

    # The rebind reads the file, and its write reads it again for the degradation time to keep.
    assert held_at_read == [True, True]
    assert json.loads(rebound.manifest_path.read_text())["mcp_config_reason"] == (
        "no_mcp_config_found"
    )


def test_a_manifest_that_cannot_be_decoded_stops_the_write_instead_of_dropping_provenance(
    tmp_path, monkeypatch
):
    import lionagi.cli.orchestrate._checkpoint as checkpoint_mod

    runs_root = tmp_path / "runs"
    launch_dir = tmp_path / "workspace"
    launch_dir.mkdir()
    monkeypatch.setattr(runs_mod, "RUNS_ROOT", runs_root)
    monkeypatch.setattr(checkpoint_mod, "RUNS_ROOT", runs_root)
    resolution = McpResolution(
        servers=None, reason="no_mcp_config_found", source=None, searched_from=launch_dir
    )
    run = allocate_run(run_id="undecodable-run")
    run = run.with_execution_provenance(cwd=launch_dir, mcp_resolution=resolution)
    reopened = checkpoint_mod._find_run_dir_by_id(run.run_id)
    assert reopened is not None and reopened.cwd is None
    before = run.manifest_path.read_bytes()
    run.manifest_path.write_bytes(before + b"\xff")
    undecodable = run.manifest_path.read_bytes()

    with pytest.raises(UnicodeError):
        reopened.write_manifest({"status": "completed", "ended_at": 1.0})

    assert run.manifest_path.read_bytes() == undecodable
