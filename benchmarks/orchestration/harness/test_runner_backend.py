"""Tests for runner.py's ADR-0089 backend selector on run_once().

No LLM calls: ``_run_once_inprocess`` is monkeypatched, isolating the
selector's provision/teardown wiring from orchestration behavior.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness import runner  # noqa: E402
from harness.config import OrchestrationConfig  # noqa: E402
from harness.task import RunResult, Task  # noqa: E402


def _init_git_repo(path: Path) -> None:
    for cmd in (
        ["git", "init"],
        ["git", "config", "user.email", "test@test.com"],
        ["git", "config", "user.name", "Test"],
    ):
        subprocess.run(cmd, cwd=str(path), capture_output=True, check=True)  # noqa: S603  # fixed git argv, no shell interpolation
    (path / "README.md").write_text("initial\n")
    subprocess.run(["git", "add", "."], cwd=str(path), capture_output=True, check=True)  # noqa: S603, S607
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(path), capture_output=True, check=True)  # noqa: S603, S607


@pytest.fixture
def git_repo_cwd(tmp_path, monkeypatch):
    _init_git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _task() -> Task:
    return Task(id="t1", prompt="review", context={"file": "x.py"})


def _config() -> OrchestrationConfig:
    return OrchestrationConfig(name="c1")


async def test_run_once_default_backend_is_unchanged_inprocess(monkeypatch):
    calls = []

    async def fake_inprocess(task, config, trial):
        calls.append((task.id, config.name, trial))
        return RunResult(
            task_id=task.id, config_key=config.key(), trial=trial, outputs=["ok"], wall_seconds=0.1
        )

    monkeypatch.setattr(runner, "_run_once_inprocess", fake_inprocess)
    result = await runner.run_once(_task(), _config(), 0)

    assert calls == [("t1", "c1", 0)]
    assert result.backend == "inprocess"


async def test_run_once_local_worktree_backend_provisions_and_tears_down(git_repo_cwd, monkeypatch):
    async def fake_inprocess(task, config, trial):
        return RunResult(
            task_id=task.id, config_key=config.key(), trial=trial, outputs=["ok"], wall_seconds=0.1
        )

    monkeypatch.setattr(runner, "_run_once_inprocess", fake_inprocess)
    result = await runner.run_once(_task(), _config(), 0, backend="local_worktree")

    assert result.backend == "local_worktree"
    # the worktree created during provision must be cleaned up by teardown
    assert list((git_repo_cwd / ".worktrees").glob("*")) == []


async def test_run_once_teardown_failure_does_not_mask_trial_result(git_repo_cwd, monkeypatch):
    async def fake_inprocess(task, config, trial):
        return RunResult(
            task_id=task.id, config_key=config.key(), trial=trial, outputs=["ok"], wall_seconds=0.1
        )

    async def failing_teardown(self, handle):
        raise RuntimeError("boom")

    monkeypatch.setattr(runner, "_run_once_inprocess", fake_inprocess)
    from lionagi.tools.sandbox_backend import LocalWorktreeBackend

    monkeypatch.setattr(LocalWorktreeBackend, "teardown", failing_teardown)
    result = await runner.run_once(_task(), _config(), 0, backend="local_worktree")

    assert result.outputs == ["ok"]
    assert result.backend == "local_worktree"


async def test_run_once_unknown_backend_raises():
    with pytest.raises(ValueError):
        await runner.run_once(_task(), _config(), 0, backend="docker")
