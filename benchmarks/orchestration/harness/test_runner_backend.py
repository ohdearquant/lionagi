"""Tests for runner.py's ADR-0089 backend selector on run_once().

The default (``backend=None``) path is covered with ``_run_once_inprocess``
monkeypatched — no LLM calls, isolating the selector from orchestration
behavior. The backend-selected path is covered two ways: (1) spy tests that
replace ``LocalWorktreeBackend.run_cell`` to prove ``run_once`` genuinely
calls it and propagates its result (rather than the trial silently keeping
``_run_once_inprocess`` unchanged next to an unused handle), and (2) one real
end-to-end test that runs the actual subprocess entrypoint with a
deliberately-invalid model spec, which fails deterministically before any
network call — proving the seam's subprocess/pickle/env plumbing genuinely
works, not just that a stub was invoked.
"""

from __future__ import annotations

import pickle
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


async def test_run_once_local_worktree_backend_routes_through_run_cell(git_repo_cwd, monkeypatch):
    """The seam must not sit unused next to the handle: run_cell must actually
    be invoked, with a real prompt-cell, and its result is what comes back."""
    from lionagi.tools.sandbox_backend import CellResult, LocalWorktreeBackend

    calls = []
    canned = RunResult(
        task_id="t1", config_key="c1", trial=0, outputs=["ok-from-cell"], wall_seconds=0.1
    )

    async def fake_run_cell(self, handle, cell, on_event=None):
        calls.append(cell)
        return CellResult(exit_code=0, stdout="", artifacts={"out.pkl": pickle.dumps(canned)})

    monkeypatch.setattr(LocalWorktreeBackend, "run_cell", fake_run_cell)
    result = await runner.run_once(_task(), _config(), 0, backend="local_worktree")

    assert len(calls) == 1
    assert calls[0].kind == "prompt_cell"
    assert "_cell_entry.py" in calls[0].entrypoint
    assert calls[0].artifact_manifest == ["out.pkl"]
    assert result.outputs == ["ok-from-cell"]
    assert result.backend == "local_worktree"
    # the worktree created during provision must be cleaned up by teardown
    assert list((git_repo_cwd / ".worktrees").glob("*")) == []


async def test_run_once_local_worktree_backend_cell_failure_becomes_error_result(
    git_repo_cwd, monkeypatch
):
    from lionagi.tools.sandbox_backend import CellResult, LocalWorktreeBackend

    async def failing_run_cell(self, handle, cell, on_event=None):
        return CellResult(exit_code=1, stdout="", stderr="boom inside the cell")

    monkeypatch.setattr(LocalWorktreeBackend, "run_cell", failing_run_cell)
    result = await runner.run_once(_task(), _config(), 0, backend="local_worktree")

    assert result.outputs == []
    assert result.error is not None
    assert "boom inside the cell" in result.error
    assert result.backend == "local_worktree"


async def test_run_once_teardown_failure_does_not_mask_trial_result(git_repo_cwd, monkeypatch):
    from lionagi.tools.sandbox_backend import CellResult, LocalWorktreeBackend

    canned = RunResult(task_id="t1", config_key="c1", trial=0, outputs=["ok"], wall_seconds=0.1)

    async def fake_run_cell(self, handle, cell, on_event=None):
        return CellResult(exit_code=0, stdout="", artifacts={"out.pkl": pickle.dumps(canned)})

    async def failing_teardown(self, handle):
        raise RuntimeError("boom")

    monkeypatch.setattr(LocalWorktreeBackend, "run_cell", fake_run_cell)
    monkeypatch.setattr(LocalWorktreeBackend, "teardown", failing_teardown)
    result = await runner.run_once(_task(), _config(), 0, backend="local_worktree")

    assert result.outputs == ["ok"]
    assert result.backend == "local_worktree"


async def test_run_once_unknown_backend_raises():
    with pytest.raises(ValueError):
        await runner.run_once(_task(), _config(), 0, backend="docker")


async def test_run_once_daytona_backend_fails_fast_not_silently_inprocess():
    """run_once's trial is always a prompt-cell; Daytona cannot host one
    host-side (ADR-0089 §3) — this must raise clearly, never silently keep
    running the trial in-process as if the backend had been honored."""
    with pytest.raises(ValueError, match="prompt-cell"):
        await runner.run_once(_task(), _config(), 0, backend="daytona")


async def test_run_once_local_worktree_backend_real_subprocess_end_to_end(git_repo_cwd):
    """No monkeypatching of run_cell: the real subprocess entrypoint runs
    inside the real provisioned worktree. An invalid model spec fails
    deterministically at header construction (no network reached), so this
    stays fast and offline while proving the seam's actual plumbing —
    pickling, sys.path bootstrap, the minimal env, cwd/path resolution —
    genuinely executes the trial rather than a parallel unused path."""
    config = OrchestrationConfig(
        name="c1", pattern="single", model="totally-bogus-provider/nonexistent-model"
    )
    result = await runner.run_once(_task(), config, 0, backend="local_worktree")

    assert result.backend == "local_worktree"
    assert result.config_key == config.key()
    assert result.error is not None
    assert "API key is required for authentication" in result.error
    assert list((git_repo_cwd / ".worktrees").glob("*")) == []
