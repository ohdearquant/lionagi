# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Regression: every production call site that can bring a `.lionagi`
directory into existence must invalidate `find_lionagi_dirs()`'s
process-lifetime cache, not just `li hooks import`.

`find_lionagi_dirs()` is memoized per `(cwd, home)` for the life of the
process (see `lionagi/_paths.py`). A prior fix invalidated that cache after
`li hooks import` creates a project-local `.lionagi/`, but other production
paths create the same discovered directory -- notably `li agent`'s
last-branch pointer and the plugin settings writers -- without clearing the
cache. A long-lived process (an embedding host, or any code path that calls
`find_lionagi_dirs()` before and after one of these writers runs) kept
seeing the pre-creation `[]` result even after the directory had been
created in-process.

Both writers below now create their directory via the shared
`lionagi._paths.ensure_lionagi_dir` helper, which invalidates the cache as
part of directory creation.
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
