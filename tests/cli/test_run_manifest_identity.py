# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Run allocation and manifest identity are consistent and contained."""

from __future__ import annotations

import json

import pytest

import lionagi.cli._runs as runs_mod
from lionagi.cli._mcp_resolve import McpResolution


@pytest.fixture
def isolated_runs(tmp_path, monkeypatch):
    root = tmp_path / "runs"
    root.mkdir()
    monkeypatch.setattr(runs_mod, "RUNS_ROOT", root)
    monkeypatch.setattr(runs_mod, "_ALLOCATED_RUN_ID", None)
    monkeypatch.delenv("LIONAGI_RUN_ID", raising=False)
    return root


@pytest.fixture
def forbid_allocation_writes(monkeypatch):
    """Record but do not execute allocator writes, even on the unfixed code."""
    calls = []

    def deny(path):
        calls.append(("mkdir", path))
        return path

    def no_manifest(self, data):
        calls.append(("manifest", self.manifest_path))

    monkeypatch.setattr(runs_mod, "ensure_lionagi_dir", deny)
    monkeypatch.setattr(runs_mod.RunDir, "write_manifest", no_manifest)
    monkeypatch.setattr(runs_mod, "mark_run_allocated", lambda: calls.append(("mark", None)))
    return calls


_INVALID_IDS = [
    pytest.param("", id="empty"),
    pytest.param(".", id="dot"),
    pytest.param("..", id="dot-dot"),
    pytest.param("child/nested", id="slash"),
    pytest.param("run-A/.", id="normalized-trailing-dot"),
    pytest.param("bad\x00id", id="nul"),
]


@pytest.mark.parametrize("bad_id", _INVALID_IDS)
@pytest.mark.parametrize("source", ["explicit", "inherited"])
def test_invalid_run_id_is_refused_before_filesystem_writes(
    isolated_runs, forbid_allocation_writes, monkeypatch, bad_id, source
):
    # A real OS environment cannot store NUL. The fake mapping permits that
    # test without an exec, and stays confined to this monkeypatch scope.
    inherited = bad_id if source == "inherited" else "valid-parent"
    env = {"LIONAGI_RUN_ID": inherited}
    monkeypatch.setattr(runs_mod.os, "environ", env)

    with pytest.raises(ValueError) as caught:
        if source == "explicit":
            runs_mod.allocate_run(run_id=bad_id)
        else:
            runs_mod.allocate_run()

    message = str(caught.value)
    assert repr(bad_id) in message
    assert ("run_id" if source == "explicit" else "LIONAGI_RUN_ID") in message
    assert "LIONAGI_RUN_ID" not in env
    assert forbid_allocation_writes == []
    assert list(isolated_runs.iterdir()) == []


@pytest.mark.parametrize("source", ["explicit", "inherited"])
@pytest.mark.parametrize("target_kind", ["inside-root", "outside-root"])
def test_linked_run_directory_is_refused_before_allocation_writes(
    isolated_runs, forbid_allocation_writes, monkeypatch, tmp_path, source, target_kind
):
    # Both targets are temporary fixtures. The recorder prevents writes
    # through either link, even when the test is run on the old source.
    target = isolated_runs / "other" if target_kind == "inside-root" else tmp_path / "outside"
    target.mkdir()
    (isolated_runs / "linked").symlink_to(target, target_is_directory=True)
    env = {"LIONAGI_RUN_ID": "linked" if source == "inherited" else "parent"}
    monkeypatch.setattr(runs_mod.os, "environ", env)

    with pytest.raises(ValueError) as caught:
        if source == "explicit":
            runs_mod.allocate_run(run_id="linked")
        else:
            runs_mod.allocate_run()

    assert "linked" in str(caught.value)
    assert ("run_id" if source == "explicit" else "LIONAGI_RUN_ID") in str(caught.value)
    assert "LIONAGI_RUN_ID" not in env
    assert forbid_allocation_writes == []


@pytest.mark.parametrize("source", ["explicit", "inherited", "generated"])
def test_valid_allocation_uses_one_id_and_consumes_handoff(isolated_runs, monkeypatch, source):
    if source == "explicit":
        # An ignored invalid inherited ID is consumed, not validated.
        monkeypatch.setenv("LIONAGI_RUN_ID", "invalid/parent")
        run = runs_mod.allocate_run(run_id="explicit-run")
        assert run.run_id == "explicit-run"
    elif source == "inherited":
        monkeypatch.setenv("LIONAGI_RUN_ID", "handoff-run")
        run = runs_mod.allocate_run()
        assert run.run_id == "handoff-run"
    else:
        run = runs_mod.allocate_run()
        assert run.run_id
    assert run.state_root == isolated_runs / run.run_id
    assert run.state_root.resolve().parent == isolated_runs.resolve()
    assert run.read_manifest()["run_id"] == run.run_id
    assert "LIONAGI_RUN_ID" not in runs_mod.os.environ


@pytest.mark.parametrize(
    "other_id", ["different-run", "", None], ids=["different", "empty", "none"]
)
def test_manifest_rejects_different_run_id_without_writing(isolated_runs, other_id):
    run = runs_mod.allocate_run(run_id="actual-run")
    before = run.manifest_path.read_bytes()
    with pytest.raises(ValueError) as caught:
        run.write_manifest({"run_id": other_id, "status": "completed"})
    assert "run_id" in str(caught.value)
    assert run.manifest_path.read_bytes() == before
    assert json.loads(before)["run_id"] == run.run_id


def test_manifest_rejects_different_run_id_before_creating_directory(isolated_runs):
    state_root = isolated_runs / "actual-run"
    run = runs_mod.RunDir(
        run_id="actual-run", state_root=state_root, artifact_root=state_root / "artifacts"
    )

    with pytest.raises(ValueError, match="run_id"):
        run.write_manifest({"run_id": "different-run", "status": "running"})

    assert not state_root.exists()


@pytest.mark.parametrize("include_id", [False, True], ids=["omitted", "matching"])
def test_manifest_accepts_unchanged_identity(isolated_runs, include_id):
    run = runs_mod.allocate_run(run_id="actual-run")
    data = {"status": "completed"}
    if include_id:
        data["run_id"] = run.run_id
    run.write_manifest(data)
    assert run.read_manifest()["run_id"] == run.run_id
    assert run.read_manifest()["status"] == "completed"


def test_execution_provenance_rewrite_keeps_run_identity(isolated_runs, tmp_path):
    run = runs_mod.allocate_run(run_id="actual-run")
    resolution = McpResolution(
        servers=None,
        reason="no_mcp_config_found",
        source=None,
        searched_from=tmp_path,
    )
    rebound = run.with_execution_provenance(cwd=tmp_path, mcp_resolution=resolution)
    assert rebound.run_id == run.run_id
    assert rebound.state_root == run.state_root
    assert rebound.read_manifest()["run_id"] == run.run_id


def test_execution_provenance_refuses_existing_mismatched_identity(isolated_runs, tmp_path):
    run = runs_mod.allocate_run(run_id="actual-run")
    run.manifest_path.write_text(json.dumps({"run_id": "different-run", "status": "running"}))
    before = run.manifest_path.read_bytes()
    resolution = McpResolution(
        servers=None,
        reason="no_mcp_config_found",
        source=None,
        searched_from=tmp_path,
    )

    with pytest.raises(ValueError, match="run_id"):
        run.with_execution_provenance(cwd=tmp_path, mcp_resolution=resolution)

    assert run.manifest_path.read_bytes() == before
