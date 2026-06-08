# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Path safety primitives for workspace containment, traversal prevention, and symlink escape detection."""

from __future__ import annotations

from pathlib import Path

DENIED_NAMES: frozenset[str] = frozenset(
    {".env", ".netrc", "id_rsa", "id_ed25519", "id_ecdsa", ".htpasswd"}
)

DANGEROUS_CHARS: frozenset[str] = frozenset("/\\\x00")

GLOB_CHARS: frozenset[str] = frozenset("*?[]{}~")


def has_traversal(p: Path) -> bool:
    """True if path contains '..' components."""
    return ".." in p.parts


def resolve_workspace_path(path: str | Path, workspace_root: Path) -> Path:
    """Resolve a tool-supplied path against workspace root with full safety checks.

    Checks: expanduser, symlink detection pre-resolve, containment, denied names.
    Raises PermissionError on any violation.
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
    if resolved.name in DENIED_NAMES:
        raise PermissionError(f"Refusing to access protected path: {resolved.name!r}")
    return resolved


def check_path_safe(
    value: str,
    field_name: str,
    *,
    reject_absolute: bool = True,
    strip_at: bool = False,
) -> str:
    """Validate a path string: reject traversal, optionally reject absolute paths.

    Raises ValueError on any violation.
    """
    entry = value.lstrip("@") if strip_at else value
    p = Path(entry)
    if reject_absolute and p.is_absolute():
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
    reject_absolute: bool = True,
    strip_at: bool = False,
) -> list[str]:
    """Batch version of check_path_safe."""
    for v in values:
        check_path_safe(v, field_name, reject_absolute=reject_absolute, strip_at=strip_at)
    return values


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
    if ".." in p.parts:
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
