# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Shared path-containment helpers for agentic-CLI provider models.

Agentic-CLI providers (Codex, Claude Code, Gemini, Pi) accept path-grant
fields (directories, config files, image paths, etc.) that are forwarded
verbatim to subprocess argv.  Without validation an untrusted value such as
``../../etc/passwd`` or ``/etc/shadow`` would let the spawned CLI read
arbitrary host files.

These helpers implement fail-closed containment: every path is validated
*before* argv is constructed.

Two-layer model (mirrors Pi's original design):
  1. ``check_path_safe`` — lexical check (no absolute paths, no ``..``
     components).  Fast, no filesystem access.  Apply in field validators.
  2. ``contain_path_in_repo`` — resolves symlinks against a repo root and
     rejects any path whose real location escapes the root.  Apply in model
     validators after the repo root is known.
"""

from __future__ import annotations

from pathlib import Path


def check_path_safe(value: str, field_name: str, *, strip_at: bool = False) -> str:
    """Lexically reject absolute paths and directory-traversal sequences.

    Parameters
    ----------
    value:
        The raw path string to validate.
    field_name:
        Name of the originating field, used in error messages.
    strip_at:
        When True, strip a leading ``@`` before analysing the path (Pi-style
        file references).  The original string is returned unchanged.

    Returns
    -------
    str
        The original *value* if it passes validation.

    Raises
    ------
    ValueError
        If the path is absolute or contains ``..`` components.
    """
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


def check_paths_safe(
    values: list[str],
    field_name: str,
    *,
    strip_at: bool = False,
) -> list[str]:
    """Apply :func:`check_path_safe` to every item in a list.

    Parameters
    ----------
    values:
        List of raw path strings.
    field_name:
        Name of the originating field, used in error messages.
    strip_at:
        Forwarded to :func:`check_path_safe`.

    Returns
    -------
    list[str]
        The original list if all entries pass.

    Raises
    ------
    ValueError
        On the first entry that fails.
    """
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
    """Resolve a path against *repo* and reject symlink-escape attempts.

    The lexical validator (:func:`check_path_safe`) rejects absolute paths
    and ``..`` components, but a repo-local symlink (``repo/link -> /outside``)
    is lexically clean yet lets the CLI read outside the repo.  Resolving
    against the real filesystem catches these cases.

    Parameters
    ----------
    value:
        The raw path string.  May contain a leading ``@`` (stripped when
        *strip_at* is True).
    repo:
        Resolved repository root (call ``repo.resolve()`` before passing).
    field_name:
        Name of the originating field, used in error messages.
    strip_at:
        Strip a leading ``@`` before resolving the path.

    Raises
    ------
    ValueError
        If the resolved path falls outside *repo*.
    """
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
    """Apply :func:`contain_path_in_repo` to every item in a list.

    Parameters
    ----------
    values:
        List of raw path strings.
    repo:
        Resolved repository root.
    field_name:
        Name of the originating field.
    strip_at:
        Forwarded to :func:`contain_path_in_repo`.

    Raises
    ------
    ValueError
        On the first entry that fails.
    """
    for v in values:
        contain_path_in_repo(v, repo, field_name, strip_at=strip_at)
