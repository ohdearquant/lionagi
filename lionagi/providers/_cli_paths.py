# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Fail-closed path-containment helpers for agentic-CLI provider models."""

from __future__ import annotations

from pathlib import Path


def check_path_safe(value: str, field_name: str, *, strip_at: bool = False) -> str:
    """Reject absolute paths and directory-traversal sequences."""
    entry = value.lstrip("@") if strip_at else value
    p = Path(entry)

    if p.is_absolute():
        raise ValueError(
            f"{field_name} entry {value!r} is an absolute path — "
            "only relative paths inside the repository are allowed. "
            "Absolute paths can grant CLI access to arbitrary files."
        )
    if ".." in p.parts:
        raise ValueError(
            f"{field_name} entry {value!r} contains directory traversal ('..') — "
            "only paths that remain inside the repository are allowed."
        )
    return value


def check_add_dir_entry_safe(value: str, field_name: str) -> str:
    """Validate a read-grant dir path. Allows absolute, rejects traversal."""
    p = Path(value)
    if ".." in p.parts:
        raise ValueError(
            f"{field_name} entry {value!r} contains directory traversal ('..') — "
            "use an explicit absolute path to grant access to directories outside "
            "the repository instead of relative traversal sequences."
        )
    return value


def check_add_dir_entries_safe(values: list[str], field_name: str) -> list[str]:
    """Apply check_add_dir_entry_safe to every item."""
    for v in values:
        check_add_dir_entry_safe(v, field_name)
    return values


def check_paths_safe(
    values: list[str],
    field_name: str,
    *,
    strip_at: bool = False,
) -> list[str]:
    """Apply check_path_safe to every item."""
    for v in values:
        check_path_safe(v, field_name, strip_at=strip_at)
    return values


def contain_path_in_repo(
    value: str | Path,
    repo: Path,
    field_name: str,
    *,
    strip_at: bool = False,
) -> None:
    """Resolve against repo and reject symlink-escape attempts."""
    entry = str(value).lstrip("@") if strip_at else str(value)
    resolved = (repo / entry).resolve()
    try:
        resolved.relative_to(repo)
    except ValueError as exc:
        raise ValueError(
            f"{field_name} entry {value!r} resolves to {resolved} which is "
            f"outside the repository root {repo} (possible symlink escape). "
            "Only paths that remain inside the repository are allowed."
        ) from exc


def contain_paths_in_repo(
    values: list[str | Path],
    repo: Path,
    field_name: str,
    *,
    strip_at: bool = False,
) -> None:
    """Apply contain_path_in_repo to every item."""
    for v in values:
        contain_path_in_repo(v, repo, field_name, strip_at=strip_at)
