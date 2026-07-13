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
