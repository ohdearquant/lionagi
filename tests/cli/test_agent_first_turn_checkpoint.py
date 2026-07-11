# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Regression tests for the first-turn checkpoint: a branch killed (e.g.
SIGTERM) before its first turn completes still leaves a resumable snapshot.

`lionagi.operations.run.run.run()` writes the branch's JSON snapshot to
`snapshot_dir` *before* the model stream starts (see `_write_branch_snapshot`),
not only when the turn finishes cleanly. These tests pin the two things a
resumer actually depends on:

  * `find_branch()` locates a pre-turn checkpoint under `<run>/branches/`
    exactly like it would a post-turn one.
  * `li agent -r <id>` (via `_run_agent`'s resume branch) loads such a
    checkpoint and proceeds without crashing.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def _write_pre_turn_snapshot(branches_dir: Path) -> str:
    """Simulate the early snapshot `run()` writes before streaming starts:
    a branch with only the instruction message recorded, no assistant
    response — the state that exists the instant a long CLI turn is killed.
    """
    from lionagi import Branch

    b = Branch()
    b.msgs.add_message(instruction="do the long-running thing")
    branch_id = str(b.id)
    branches_dir.mkdir(parents=True, exist_ok=True)
    (branches_dir / f"{branch_id}.json").write_text(json.dumps(b.to_dict()))
    return branch_id


def test_find_branch_locates_pre_turn_checkpoint(tmp_path, monkeypatch):
    """find_branch() must locate a checkpoint written before the turn ever
    completed — the same lookup a completed-turn snapshot uses.
    """
    import lionagi.cli._runs as runs_mod

    run_dir = tmp_path / "runs" / "r1"
    branches_dir = run_dir / "branches"
    monkeypatch.setattr(runs_mod, "RUNS_ROOT", tmp_path / "runs")

    branch_id = _write_pre_turn_snapshot(branches_dir)

    found_run_id, found_path = runs_mod.find_branch(branch_id)
    assert found_run_id == "r1"
    assert found_path == branches_dir / f"{branch_id}.json"

    # And it must be valid, parseable JSON — not a torn write.
    data = json.loads(found_path.read_text())
    assert data["id"] == branch_id


def test_find_branch_prefix_match_on_pre_turn_checkpoint(tmp_path, monkeypatch):
    """A truncated id (as a user might paste) still prefix-matches a
    pre-turn checkpoint."""
    import lionagi.cli._runs as runs_mod

    run_dir = tmp_path / "runs" / "r1"
    branches_dir = run_dir / "branches"
    monkeypatch.setattr(runs_mod, "RUNS_ROOT", tmp_path / "runs")

    branch_id = _write_pre_turn_snapshot(branches_dir)

    found_run_id, found_path = runs_mod.find_branch(branch_id[:8])
    assert found_run_id == "r1"
    assert found_path.name == f"{branch_id}.json"


def _wire_agent_stubs(monkeypatch, tmp_path: Path, operate_return=None):
    """Monkeypatch all external I/O in _run_agent so tests run without real I/O.

    Mirrors the helper in test_agent_resume_model_override.py.
    """
    import lionagi.cli.agent as agent_mod
    from lionagi import Branch
    from lionagi.service.manager import iModelManager

    async def fake_operate(self, instruction=None, **kw):
        return operate_return

    monkeypatch.setattr(Branch, "operate", fake_operate)
    monkeypatch.setattr(iModelManager, "shutdown", AsyncMock())

    monkeypatch.setattr(agent_mod, "resolve_persisted_effort", lambda *a, **kw: None)

    async def fake_setup(*a, **kw):
        return None

    async def fake_teardown(
        ctx,
        *,
        status="completed",
        exception=None,
        cwd=None,
        engine_session_uid=None,
        defer_terminal=False,
    ):
        return status

    monkeypatch.setattr(agent_mod, "setup_agent_persist", fake_setup)
    monkeypatch.setattr(agent_mod, "teardown_agent_persist", fake_teardown)
    monkeypatch.setattr(agent_mod, "save_last_branch_pointer", lambda *a, **kw: None)
    monkeypatch.setattr(
        agent_mod,
        "_provenance",
        SimpleNamespace(
            resolve_model_spec=lambda p, m: f"{p}/{m}",
            agent_definition_hash=lambda n: "abc",
        ),
    )
    monkeypatch.setattr(agent_mod, "resolve_artifact_contract", lambda **_: None)
    monkeypatch.setattr(
        agent_mod,
        "allocate_run",
        lambda: SimpleNamespace(
            run_id="r",
            artifact_root=tmp_path / "artifacts",
            stream_dir=tmp_path / "stream",
            branches_dir=tmp_path / "branches",
        ),
    )


@pytest.mark.asyncio
async def test_resume_on_pre_turn_checkpoint_does_not_crash(tmp_path, monkeypatch):
    """`li agent -r <id>` against a checkpoint from an interrupted first turn
    (instruction recorded, no assistant response) must load the branch and
    run the next turn — not raise FileNotFoundError or crash on the
    incomplete history.
    """
    branches_dir = tmp_path / "src_branches"
    branch_id = _write_pre_turn_snapshot(branches_dir)
    branch_path = branches_dir / f"{branch_id}.json"

    import lionagi.cli.agent as agent_mod

    monkeypatch.setattr(agent_mod, "find_branch", lambda bid: ("run-x", branch_path))
    _wire_agent_stubs(monkeypatch, tmp_path, operate_return="continuing the long-running thing")

    from lionagi.cli.agent import _run_agent

    result, provider, resumed_branch_id, terminal_status, _sid = await _run_agent(
        None,
        "continue and conclude the task",
        resume=branch_id,
    )

    assert terminal_status == "completed"
    assert result == "continuing the long-running thing"
    assert resumed_branch_id == branch_id
    # No model override was given — the branch's own persisted provider/model wins.
    assert provider == "openai"
