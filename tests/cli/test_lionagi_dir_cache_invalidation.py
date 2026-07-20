# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Regression: `find_lionagi_dirs()` must always reflect current `.lionagi/`
topology, regardless of which code path created or removed a directory.

The original design memoized the whole discovery result per `(cwd, home)`
for the life of the process, and relied on every production creator calling
`ensure_lionagi_dir()` to invalidate that cache. That kept regressing: a
prior fix invalidated the cache after `li hooks import` creates a
project-local `.lionagi/`, then further writers (the last-branch pointer,
the plugin settings writers) needed the same treatment, and then stream
persistence (`Branch.run(..., stream_persist=True)`) was found writing
`.lionagi/logs/runs/` through the generic `acreate_path()` utility, which
has no idea it just created lionagi topology and cannot call the helper.

`lionagi/_paths.py` now only caches the expensive part of discovery -- the
`git rev-parse --show-toplevel` subprocess call, keyed by cwd -- and
re-evaluates the cheap `Path.is_dir()` existence checks on every call. A
caller always sees current topology with no invalidation required from any
writer; `ensure_lionagi_dir()` remains the recommended creation boundary,
but a writer that bypasses it (like `acreate_path()`) no longer produces
stale discovery results.
"""

from __future__ import annotations

import lionagi._paths as paths


def test_save_last_branch_pointer_invalidates_lionagi_dir_cache(monkeypatch, tmp_path):
    """`save_last_branch_pointer()` creates `~/.lionagi` on first use; a
    caller that primed the discovery cache before that must see it
    afterward, not the stale empty result."""
    import lionagi.cli._runs as runs_mod

    lionagi_home = tmp_path / ".lionagi"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runs_mod, "LIONAGI_HOME", lionagi_home)
    monkeypatch.setattr(runs_mod, "_LAST_BRANCH_POINTER", lionagi_home / "last_branch.json")

    paths.clear_lionagi_dirs_cache()
    try:
        # Prime the cache for this (cwd, home) before `.lionagi/` exists.
        assert paths.find_lionagi_dirs() == []
        assert not lionagi_home.is_dir()

        runs_mod.save_last_branch_pointer("run-x", "branch-x")

        assert lionagi_home.is_dir()
        assert paths.find_lionagi_dirs() == [lionagi_home]
    finally:
        paths.clear_lionagi_dirs_cache()


def test_run_dir_ensure_state_dirs_invalidates_lionagi_dir_cache(monkeypatch, tmp_path):
    """`RunDir.ensure_state_dirs()` (the first directory write of a normal
    `li agent` invocation, via `allocate_run()`) must also invalidate the
    cache -- it can create `~/.lionagi/runs/{run_id}/` before
    `save_last_branch_pointer()` ever runs."""
    import lionagi.cli._runs as runs_mod

    lionagi_home = tmp_path / ".lionagi"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)

    paths.clear_lionagi_dirs_cache()
    try:
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
    finally:
        paths.clear_lionagi_dirs_cache()


def test_write_user_settings_invalidates_lionagi_dir_cache(monkeypatch, tmp_path):
    """`write_user_settings()` creates `~/.lionagi` on first use; the
    discovery cache must reflect that within the same process."""
    from lionagi.plugins._user_settings import write_user_settings

    lionagi_home = tmp_path / ".lionagi"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)

    paths.clear_lionagi_dirs_cache()
    try:
        assert paths.find_lionagi_dirs() == []
        assert not lionagi_home.is_dir()

        write_user_settings({"plugins": {}})

        assert lionagi_home.is_dir()
        assert paths.find_lionagi_dirs() == [lionagi_home]
    finally:
        paths.clear_lionagi_dirs_cache()


def test_locked_user_settings_invalidates_lionagi_dir_cache(monkeypatch, tmp_path):
    """`locked_user_settings()` (the read-modify-write path used by GC,
    trust, enable/disable) also creates `~/.lionagi` on first use."""
    from lionagi.plugins._user_settings import locked_user_settings

    lionagi_home = tmp_path / ".lionagi"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)

    paths.clear_lionagi_dirs_cache()
    try:
        assert paths.find_lionagi_dirs() == []
        assert not lionagi_home.is_dir()

        with locked_user_settings() as settings:
            settings["plugins"] = {}

        assert lionagi_home.is_dir()
        assert paths.find_lionagi_dirs() == [lionagi_home]
    finally:
        paths.clear_lionagi_dirs_cache()


async def test_stream_persist_default_dir_visible_to_discovery(monkeypatch, tmp_path):
    """`Branch.run(..., stream_persist=True)` writes its snapshot and buffer
    under the default `LIONAGI_HOME / "logs" / "runs"` directory through the
    generic `acreate_path()` path utility (`lionagi/ln/_utils.py`), not
    through `ensure_lionagi_dir()`. A caller that primed discovery before
    that write must still see the directory afterward -- not because the
    writer invalidated anything, but because discovery re-checks existence
    on every call.
    """
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

    paths.clear_lionagi_dirs_cache()
    try:
        # Prime the cache for this (cwd, home) before `.lionagi/` exists.
        assert paths.find_lionagi_dirs() == []
        assert not lionagi_home.is_dir()

        param = RunParam(stream_persist=True, persist_dir=default_persist_dir)
        async for _ in run(branch, "persist-me", param):
            pass

        assert default_persist_dir.is_dir()
        assert paths.find_lionagi_dirs() == [lionagi_home]
    finally:
        paths.clear_lionagi_dirs_cache()


def test_git_root_lookup_not_rerun_for_repeated_calls_at_same_cwd(monkeypatch, tmp_path):
    """The git-root subprocess call is the only expensive part of discovery;
    repeated `find_lionagi_dirs()` calls at the same cwd must not re-invoke
    `git rev-parse --show-toplevel`, proving the remaining cache still does
    its job now that `.lionagi/` existence is no longer memoized."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)

    real_run = paths.subprocess.run
    calls = []

    def counting_run(*args, **kwargs):
        calls.append(args)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(paths.subprocess, "run", counting_run)

    paths.clear_lionagi_dirs_cache()
    try:
        paths.find_lionagi_dirs()
        paths.find_lionagi_dirs()
        paths.find_lionagi_dirs()
        assert len(calls) == 1
    finally:
        paths.clear_lionagi_dirs_cache()


def test_git_root_cache_keyed_by_git_context_not_cwd_alone(monkeypatch, tmp_path):
    """A process can point git at a different worktree without changing cwd by
    setting GIT_DIR/GIT_WORK_TREE. The git-root cache must key on that context,
    not cwd alone, or the first worktree's `.lionagi/` root is handed to every
    later discovery call until an unrelated caller clears the cache."""
    import subprocess

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

    paths.clear_lionagi_dirs_cache()
    try:
        monkeypatch.setenv("GIT_DIR", str(repo_a / ".git"))
        monkeypatch.setenv("GIT_WORK_TREE", str(repo_a))
        first = paths.find_lionagi_dirs()
        assert (repo_a / ".lionagi") in first

        # Redirect git at repo_b, same cwd, no cache clear. Keying on cwd alone
        # would still return repo_a's root here; keying on git context returns
        # repo_b's.
        monkeypatch.setenv("GIT_DIR", str(repo_b / ".git"))
        monkeypatch.setenv("GIT_WORK_TREE", str(repo_b))
        second = paths.find_lionagi_dirs()
        assert (repo_b / ".lionagi") in second
        assert (repo_a / ".lionagi") not in second
    finally:
        paths.clear_lionagi_dirs_cache()
