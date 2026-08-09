# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Caller-visible coverage for runs that continue without lifecycle persistence."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from lionagi import Branch, Session
from lionagi.cli import _runs
from lionagi.cli.orchestrate import _checkpoint, _orchestration
from lionagi.cli.orchestrate._checkpoint import FlowResumeError
from lionagi.mcp import config, jobs


def _env(run: _runs.RunDir) -> _orchestration.OrchestrationEnv:
    branch = Branch(name="orchestrator")
    return _orchestration.OrchestrationEnv(
        run=run,
        session=Session(default_branch=branch),
        orc_branch=branch,
        builder=MagicMock(),
        orc_profile=None,
        orc_profile_name=None,
        default_model_spec="codex",
        bare=False,
        effort=None,
        theme=None,
        yolo=False,
        bypass=False,
        verbose=False,
        fast=False,
        cwd=None,
    )


def _run_dir(root: Path, run_id: str) -> _runs.RunDir:
    run = _runs.RunDir(
        run_id=run_id,
        state_root=root / run_id,
        artifact_root=root / run_id / "artifacts",
    )
    run.ensure_state_dirs()
    run.ensure_artifact_root()
    return run


def _write_terminal_job(run_id: str) -> None:
    jobs._write_job(
        {
            "run_id": run_id,
            "pid": None,
            "kind": "flow",
            "status": "completed",
            "spawn_state": "started",
            "submitted_at": "2026-08-08T00:00:00+00:00",
            "finished_at": "2026-08-08T00:01:00+00:00",
            "log": None,
        }
    )


async def _seed_runs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[str, str]:
    runs_root = tmp_path / "runs"
    open_shared_db = _orchestration._open_shared_db
    monkeypatch.setattr(config, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(config, "RUNS_DIR", runs_root)
    monkeypatch.setattr(_checkpoint, "RUNS_ROOT", runs_root)
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", tmp_path / "state.db")
    monkeypatch.setattr(jobs, "_read_lifecycle", lambda run_id: None)

    degraded_id = "20260808T000002-000002"
    healthy_id = "20260808T000001-000001"
    degraded = _run_dir(runs_root, degraded_id)
    healthy = _run_dir(runs_root, healthy_id)

    async def unavailable(*args, **kwargs):
        raise OSError()

    monkeypatch.setattr(_orchestration, "_open_shared_db", unavailable)
    await _orchestration.start_live_persist(_env(degraded), invocation_kind="flow")

    monkeypatch.setattr(_orchestration, "_open_shared_db", open_shared_db)
    healthy_env = _env(healthy)
    await _orchestration.start_live_persist(healthy_env, invocation_kind="flow")
    assert healthy_env._live_persist is not None
    await _orchestration.stop_live_persist(healthy_env)

    _write_terminal_job(degraded_id)
    _write_terminal_job(healthy_id)
    return degraded_id, healthy_id


async def test_status_reports_persistence_reason_and_healthy_control(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    degraded_id, healthy_id = await _seed_runs(monkeypatch, tmp_path)

    degraded = jobs.status(degraded_id)
    healthy = jobs.status(healthy_id)

    assert degraded["persistence_degraded_reason"] == "OSError()"
    assert degraded["persistence_degraded_reason"]
    assert healthy["persistence_degraded_reason"] is None


async def test_agent_degradation_survives_terminal_manifest_rewrite(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run = _run_dir(tmp_path / "runs", "20260808T000003-000003")
    run_manifest = {
        "branch_id": "agent-branch",
        "status": "running",
        "started_at": 1.0,
        "ended_at": None,
    }
    run.write_manifest(run_manifest)

    async def unavailable(*args, **kwargs):
        raise OSError()

    monkeypatch.setattr(_runs, "_open_shared_db", unavailable)
    live = await _runs.setup_agent_persist(Branch(), run_id=run.run_id, run_manifest=run_manifest)

    assert live is None
    run_manifest.update(status="completed", ended_at=2.0)
    run.write_manifest(run_manifest)
    assert run.read_manifest()["persistence_degraded_reason"] == "OSError()"


async def test_listing_distinguishes_degraded_run_from_healthy_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    degraded_id, healthy_id = await _seed_runs(monkeypatch, tmp_path)

    listed = {row["run_id"]: row for row in jobs.list_jobs()}

    assert listed[degraded_id]["persistence_degraded_reason"] == "OSError()"
    assert listed[healthy_id]["persistence_degraded_reason"] is None


async def test_resume_without_checkpoint_reports_persistence_reason(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    degraded_id, _ = await _seed_runs(monkeypatch, tmp_path)

    with pytest.raises(FlowResumeError, match=r"persistence.*OSError\(\)"):
        await _checkpoint.resolve_checkpoint_target(degraded_id)


async def test_resume_with_unreadable_degradation_state_keeps_generic_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runs_root = tmp_path / "runs"
    run_id = "20260808T000004-000004"
    run = _run_dir(runs_root, run_id)
    run.manifest_path.write_bytes(b"\xff")
    monkeypatch.setattr(_checkpoint, "RUNS_ROOT", runs_root)

    class EmptyStateDB:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, exc_type, exc, traceback):
            return None

    async def unresolved(db, target):
        return None

    monkeypatch.setattr("lionagi.state.db.StateDB", EmptyStateDB)
    monkeypatch.setattr("lionagi.cli.status._resolve_any_target", unresolved)

    with pytest.raises(FlowResumeError, match=r"No run, session, invocation, or play found"):
        await _checkpoint.resolve_checkpoint_target(run_id)
