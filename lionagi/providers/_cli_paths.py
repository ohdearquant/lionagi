# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Fail-closed path-containment helpers for agentic-CLI provider models."""

from __future__ import annotations

from pathlib import Path

from lionagi.libs.path_safety import (
    check_add_dir_safe,
    check_add_dirs_safe,
    check_path_safe,
    check_paths_safe,
    contain_path_in_root,
    contain_paths_in_root,
)

__all__ = [
    "check_path_safe",
    "check_paths_safe",
    "check_add_dir_entry_safe",
    "check_add_dir_entries_safe",
    "contain_path_in_repo",
    "contain_paths_in_repo",
]


def check_add_dir_entry_safe(value: str, field_name: str) -> str:
    """Validate a read-grant dir path. Allows absolute, rejects traversal."""
    return check_add_dir_safe(value, field_name)


def check_add_dir_entries_safe(values: list[str], field_name: str) -> list[str]:
    """Apply check_add_dir_entry_safe to every item."""
    return check_add_dirs_safe(values, field_name)


def contain_path_in_repo(
    value: str | Path,
    repo: Path,
    field_name: str,
    *,
    strip_at: bool = False,
) -> None:
    """Resolve against repo and reject symlink-escape attempts."""
    contain_path_in_root(value, repo, field_name, strip_at=strip_at)


def contain_paths_in_repo(
    values: list[str | Path],
    repo: Path,
    field_name: str,
    *,
    strip_at: bool = False,
) -> None:
    """Apply contain_path_in_repo to every item."""
    contain_paths_in_root(values, repo, field_name, strip_at=strip_at)
