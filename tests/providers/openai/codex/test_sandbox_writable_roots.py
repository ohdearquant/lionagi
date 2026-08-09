# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import subprocess
from pathlib import Path

from lionagi.providers.openai.codex import CodexCodeRequest, toml_override_value


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _resolve_git_path(cwd: Path, value: str) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (cwd / path).resolve()


def _add_dir_values(args: list[str]) -> list[str]:
    return [args[index + 1] for index, value in enumerate(args) if value == "--add-dir"]


def _git_checkouts(tmp_path: Path) -> tuple[Path, Path]:
    ordinary = tmp_path / "ordinary"
    linked = tmp_path / "linked"
    ordinary.mkdir()
    _git(ordinary, "init", "-q")
    _git(
        ordinary,
        "-c",
        "user.name=Test User",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "--allow-empty",
        "-qm",
        "initial",
    )
    _git(ordinary, "worktree", "add", "--detach", "-q", str(linked))
    return ordinary, linked


def test_linked_worktree_grants_exact_per_worktree_git_directory(tmp_path):
    ordinary, linked = _git_checkouts(tmp_path)
    configured_root = tmp_path / "configured-root"
    configured_root.mkdir()
    git_dir = _resolve_git_path(linked, _git(linked, "rev-parse", "--git-dir"))
    common_dir = _resolve_git_path(linked, _git(linked, "rev-parse", "--git-common-dir"))

    args = CodexCodeRequest(
        prompt="work",
        repo=linked,
        full_auto=True,
        config_overrides={
            "sandbox_workspace_write.writable_roots": [str(configured_root.resolve())]
        },
    ).as_cmd_args()

    assert git_dir != common_dir
    assert _add_dir_values(args) == [str(git_dir)]
    configured_override = (
        "sandbox_workspace_write.writable_roots="
        f"{toml_override_value([str(configured_root.resolve())])}"
    )
    assert configured_override in args


def test_ordinary_checkout_gets_no_automatic_git_directory(tmp_path):
    ordinary, _linked = _git_checkouts(tmp_path)

    args = CodexCodeRequest(prompt="work", repo=ordinary, full_auto=True).as_cmd_args()

    assert _add_dir_values(args) == []
