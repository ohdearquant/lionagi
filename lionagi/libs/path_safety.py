# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Path safety primitives for workspace containment, traversal prevention, and symlink escape detection."""

from __future__ import annotations

import re
from pathlib import Path

DENIED_NAMES: frozenset[str] = frozenset(
    {".env", ".netrc", "id_rsa", "id_ed25519", "id_ecdsa", ".htpasswd"}
)

_DENIED_NAMES_CASEFOLD: frozenset[str] = frozenset(name.casefold() for name in DENIED_NAMES)


def is_protected_name(name: str) -> bool:
    """True if name matches a protected basename, case-insensitively.

    Filesystems (notably default macOS/Windows volumes) are case-insensitive,
    so a case-sensitive membership test against DENIED_NAMES can be bypassed
    with a spelling like ".ENV" that still resolves to the same file as
    ".env". This is the one primitive both resolve_workspace_path and the
    deny-only hook floor use for the protected-basename check.
    """
    return name.casefold() in _DENIED_NAMES_CASEFOLD


DANGEROUS_CHARS: frozenset[str] = frozenset("/\\\x00")

GLOB_CHARS: frozenset[str] = frozenset("*?[]{}~")

_BARE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def has_traversal(p: Path) -> bool:
    """True if path contains '..' components."""
    return ".." in p.parts


def resolve_workspace_path(path: str | Path, workspace_root: Path) -> Path:
    """Resolve a tool-supplied path against workspace root with full safety checks.

    Checks: expanduser, symlink detection pre-resolve, containment, denied names.
    Raises PermissionError on any violation. Validation happens at check time only:
    a concurrent filesystem mutation between this check and a later I/O call on the
    same path (e.g. swapping a regular file for a symlink) is out of scope — callers
    that need a stronger guarantee against a racing filesystem must perform the
    final I/O through a root-anchored, no-follow file descriptor instead.
    """
    raw = Path(path).expanduser()
    candidate = raw if raw.is_absolute() else workspace_root / raw
    if candidate.is_symlink():
        raise PermissionError(f"Refusing to access symlink: {path!r}")
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(workspace_root)
    except ValueError as e:
        raise PermissionError(f"Path escapes workspace root: {path!r}") from e
    if is_protected_name(resolved.name):
        raise PermissionError(f"Refusing to access protected path: {resolved.name!r}")
    return resolved


def check_path_safe(
    value: str,
    field_name: str,
    *,
    reject_absolute: bool = True,
    strip_at: bool = False,
) -> str:
    """Validate a path string: reject traversal, NUL bytes, and optionally absolute paths.

    Also rejects Windows-style drive-letter paths (e.g. C:foo) when reject_absolute is True.
    Raises ValueError on any violation.
    """
    entry = value.lstrip("@") if strip_at else value
    if "\x00" in entry:
        raise ValueError(f"{field_name} entry {value!r} contains NUL bytes")
    p = Path(entry)
    if reject_absolute and p.is_absolute():
        raise ValueError(
            f"{field_name} entry {value!r} is an absolute path — "
            "only repo-relative paths inside the repository are allowed. "
            "Absolute paths can grant CLI access to arbitrary files."
        )
    if reject_absolute and len(entry) >= 2 and entry[1] == ":":
        raise ValueError(
            f"{field_name} entry {value!r} is a Windows drive-letter path — "
            "only repo-relative paths inside the repository are allowed."
        )
    if has_traversal(p):
        raise ValueError(
            f"{field_name} entry {value!r} contains directory traversal ('..') — "
            "only paths that remain inside the repository are allowed."
        )
    return value


def check_paths_safe(
    values: list[str],
    field_name: str,
    *,
    reject_absolute: bool = True,
    strip_at: bool = False,
) -> list[str]:
    """Batch version of check_path_safe."""
    for v in values:
        check_path_safe(v, field_name, reject_absolute=reject_absolute, strip_at=strip_at)
    return values


def contain_and_resolve(path: str | Path, root: Path) -> Path:
    """Resolve path under root; raise ValueError if it escapes."""
    root_resolved = root.resolve()
    resolved = (root / path).resolve()
    try:
        resolved.relative_to(root_resolved)
    except ValueError:
        raise ValueError(
            f"Workspace path escapes repository bounds. "
            f"Repository: {root_resolved}, Workspace: {resolved}"
        ) from None
    return resolved


def contain_path_in_root(
    value: str | Path,
    root: Path,
    field_name: str,
    *,
    strip_at: bool = False,
) -> str:
    """Resolve path against root, raise ValueError if it escapes."""
    entry = str(value).lstrip("@") if strip_at else str(value)
    resolved = (root / entry).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(
            f"{field_name} entry {value!r} resolves to {resolved} which is "
            f"outside the repository root {root} (possible symlink escape). "
            "Only paths that remain inside the repository are allowed."
        ) from exc
    return str(value)


def contain_paths_in_root(
    values: list[str | Path],
    root: Path,
    field_name: str,
    *,
    strip_at: bool = False,
) -> list[str | Path]:
    """Batch containment check."""
    for v in values:
        contain_path_in_root(v, root, field_name, strip_at=strip_at)
    return values


def check_add_dir_safe(value: str, field_name: str) -> str:
    """Validate add_dir entry: allow absolute, reject traversal only (read-grant semantics)."""
    p = Path(value)
    if has_traversal(p):
        raise ValueError(
            f"{field_name} entry {value!r} contains directory traversal ('..') — "
            "use an explicit absolute path to grant access to directories outside "
            "the repository instead of relative traversal sequences."
        )
    return value


def check_add_dirs_safe(values: list[str], field_name: str) -> list[str]:
    """Batch version of check_add_dir_safe."""
    for v in values:
        check_add_dir_safe(v, field_name)
    return values


def safe_join(root: Path, component: str) -> Path:
    """Join component to root with traversal and symlink escape checks. Raises ValueError on violation."""
    if not component or component.strip() == "":
        raise ValueError("invalid path component: empty or whitespace")
    if component in {".", ".."}:
        raise ValueError("invalid path component: reserved component")
    if any(c in component for c in DANGEROUS_CHARS):
        raise ValueError("invalid path component: contains path separator or NUL")

    candidate = (root / component).resolve()
    root_resolved = root.resolve()
    try:
        candidate.relative_to(root_resolved)
    except ValueError:
        raise ValueError("path outside root") from None

    return candidate


def validate_name(value: str, label: str = "name") -> str:
    """Validate a name component (no traversal, no dangerous/glob chars). Raises ValueError."""
    if not value or value.strip() == "":
        raise ValueError(f"invalid {label}: empty or whitespace")
    if value in {".", ".."}:
        raise ValueError(f"invalid {label}: reserved component")
    if any(c in value for c in DANGEROUS_CHARS):
        raise ValueError(f"invalid {label}: contains path separator or NUL")
    if any(c in value for c in GLOB_CHARS):
        raise ValueError(f"invalid {label}: contains glob metacharacter")
    return value


def validate_bare_name(name: str, label: str = "name") -> str:
    """Validate bare identifier [A-Za-z0-9_-]; rejects empty, separators, dots, globs. Raises ValueError."""
    if not name or not _BARE_NAME_RE.match(name):
        raise ValueError(
            f"invalid {label} {name!r}: must be a bare identifier "
            "(ASCII letters, digits, underscores, hyphens only — no path "
            "separators, '.', '..', leading dots, or glob characters)"
        )
    return name


def validate_path_component(component: str, label: str = "component") -> str:
    """Validate a path segment; rejects empty, separators, NUL, dots, and leading dots. Raises ValueError."""
    if not component or not isinstance(component, str):
        raise ValueError(f"invalid {label}: empty or not a string")
    if "/" in component or "\\" in component or "\x00" in component:
        raise ValueError(f"invalid {label} {component!r}: contains path separator or NUL")
    if component in (".", ".."):
        raise ValueError(f"invalid {label} {component!r}: reserved component")
    if component.startswith("."):
        raise ValueError(f"invalid {label} {component!r}: leading dot not allowed")
    return component
