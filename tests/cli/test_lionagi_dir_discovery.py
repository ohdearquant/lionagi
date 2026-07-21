# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Regression: `find_lionagi_dirs()` always reflects current `.lionagi/`
topology and current git context, with no cache to invalidate.

A prior design cached the git-root lookup keyed by `(cwd, git-redirect env)`.
Each review round found another env var (`GIT_DIR`, `GIT_WORK_TREE`,
`GIT_COMMON_DIR`, then `GIT_CEILING_DIRECTORIES`,
`GIT_DISCOVERY_ACROSS_FILESYSTEM`, `GIT_IMPLICIT_WORK_TREE`) that can change
`git rev-parse --show-toplevel`'s answer for a fixed cwd, so the invalidation
domain could not be enumerated safely. `lionagi/_paths.py` now calls
`git rev-parse` directly on every `find_lionagi_dirs()` call; the subprocess
costs single-digit milliseconds and callers only invoke it a handful of times
per process.
"""

from __future__ import annotations

import subprocess

import lionagi._paths as paths


def test_save_last_branch_pointer_visible_to_discovery(monkeypatch, tmp_path):
    """`save_last_branch_pointer()` creates `~/.lionagi` on first use; a
    prior empty-result call must not shadow it."""
    import lionagi.cli._runs as runs_mod

    lionagi_home = tmp_path / ".lionagi"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runs_mod, "LIONAGI_HOME", lionagi_home)
    monkeypatch.setattr(runs_mod, "_LAST_BRANCH_POINTER", lionagi_home / "last_branch.json")

    assert paths.find_lionagi_dirs() == []
    assert not lionagi_home.is_dir()

    runs_mod.save_last_branch_pointer("run-x", "branch-x")

    assert lionagi_home.is_dir()
    assert paths.find_lionagi_dirs() == [lionagi_home]


def test_run_dir_ensure_state_dirs_visible_to_discovery(monkeypatch, tmp_path):
    """`RunDir.ensure_state_dirs()` (the first directory write of a normal
    `li agent` invocation, via `allocate_run()`) creates `~/.lionagi/runs/{run_id}/`
    before `save_last_branch_pointer()` ever runs; discovery must see it too."""
    import lionagi.cli._runs as runs_mod

    lionagi_home = tmp_path / ".lionagi"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)

    assert paths.find_lionagi_dirs() == []
    assert not lionagi_home.is_dir()

    run = runs_mod.RunDir(
        run_id="r1",
        state_root=lionagi_home / "runs" / "r1",
        artifact_root=lionagi_home / "runs" / "r1" / "artifacts",
    )
    run.ensure_state_dirs()

    assert lionagi_home.is_dir()
    assert paths.find_lionagi_dirs() == [lionagi_home]


def test_write_user_settings_visible_to_discovery(monkeypatch, tmp_path):
    """`write_user_settings()` creates `~/.lionagi` on first use."""
    from lionagi.plugins._user_settings import write_user_settings

    lionagi_home = tmp_path / ".lionagi"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)

    assert paths.find_lionagi_dirs() == []
    assert not lionagi_home.is_dir()

    write_user_settings({"plugins": {}})

    assert lionagi_home.is_dir()
    assert paths.find_lionagi_dirs() == [lionagi_home]


def test_locked_user_settings_visible_to_discovery(monkeypatch, tmp_path):
    """`locked_user_settings()` (the read-modify-write path used by GC,
    trust, enable/disable) also creates `~/.lionagi` on first use."""
    from lionagi.plugins._user_settings import locked_user_settings

    lionagi_home = tmp_path / ".lionagi"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)

    assert paths.find_lionagi_dirs() == []
    assert not lionagi_home.is_dir()

    with locked_user_settings() as settings:
        settings["plugins"] = {}

    assert lionagi_home.is_dir()
    assert paths.find_lionagi_dirs() == [lionagi_home]


async def test_stream_persist_default_dir_visible_to_discovery(monkeypatch, tmp_path):
    """`Branch.run(..., stream_persist=True)` writes its snapshot and buffer
    under the default `LIONAGI_HOME / "logs" / "runs"` directory through the
    generic `acreate_path()` path utility (`lionagi/ln/_utils.py`), not
    through `ensure_lionagi_dir()`. Discovery must still see it."""
    import types
    from unittest.mock import AsyncMock

    from lionagi.operations.run.run import RunParam, run
    from lionagi.service.imodel import iModel
    from lionagi.service.types.stream_chunk import StreamChunk
    from lionagi.session.branch import Branch

    lionagi_home = tmp_path / ".lionagi"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)

    default_persist_dir = lionagi_home / "logs" / "runs"

    model = iModel(provider="openai", model="gpt-4.1-mini", api_key="test_key")
    model.endpoint = types.SimpleNamespace(
        is_cli=True,
        session_id=None,
        to_dict=lambda: {"type": "fake_cli", "session_id": None},
    )
    model.streaming_process_func = None

    async def create_event(**kw):
        return object()

    model.create_event = create_event
    model.executor = types.SimpleNamespace(append=AsyncMock(), config={})

    async def stream(api_call=None):
        yield StreamChunk(type="text", content="done")

    model.stream = stream

    branch = Branch()
    branch.chat_model = model

    assert paths.find_lionagi_dirs() == []
    assert not lionagi_home.is_dir()

    param = RunParam(stream_persist=True, persist_dir=default_persist_dir)
    async for _ in run(branch, "persist-me", param):
        pass

    assert default_persist_dir.is_dir()
    assert paths.find_lionagi_dirs() == [lionagi_home]


def test_find_lionagi_dirs_follows_git_redirect_env_every_call(monkeypatch, tmp_path):
    """A process can point git at a different worktree without changing cwd by
    setting GIT_DIR/GIT_WORK_TREE. With no cache, every call re-resolves the
    git root, so redirecting mid-process is picked up immediately."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    # Two independently initialized repos, each with its own `.lionagi/`.
    repo_a = tmp_path / "repo_a"
    repo_b = tmp_path / "repo_b"
    for repo in (repo_a, repo_b):
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        (repo / ".lionagi").mkdir()

    # Stay in a neutral cwd for every call so cwd never distinguishes the two.
    neutral = tmp_path / "neutral"
    neutral.mkdir()
    monkeypatch.chdir(neutral)

    monkeypatch.setenv("GIT_DIR", str(repo_a / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(repo_a))
    first = paths.find_lionagi_dirs()
    assert (repo_a / ".lionagi") in first

    # Redirect git at repo_b, same cwd. A cached lookup keyed on cwd alone
    # would still return repo_a's root here; the uncached lookup returns
    # repo_b's on this very call.
    monkeypatch.setenv("GIT_DIR", str(repo_b / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(repo_b))
    second = paths.find_lionagi_dirs()
    assert (repo_b / ".lionagi") in second
    assert (repo_a / ".lionagi") not in second
