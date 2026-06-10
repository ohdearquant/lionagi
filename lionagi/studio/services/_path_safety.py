"""Studio path safety — HTTP error wrappers around libs.path_safety primitives."""

from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException

from lionagi.libs.path_safety import safe_join, validate_name


def safe_path_join(root: Path, component: str) -> Path:
    try:
        return safe_join(root, component)
    except ValueError:
        raise HTTPException(status_code=404, detail="invalid path component") from None


def public_path(path: Path, *, fallback_name: bool = True) -> str:
    """Repo-relative or home-relative path string, never absolute."""
    resolved = path.resolve()
    _repo_root = Path(__file__).resolve().parents[3]
    for root in [_repo_root, Path.home()]:
        try:
            return resolved.relative_to(root.resolve()).as_posix()
        except ValueError:
            continue
    return resolved.name if fallback_name else ""


def validate_name_component(value: str, label: str = "name") -> None:
    try:
        validate_name(value, label)
    except ValueError as exc:
        detail = str(exc)
        raise HTTPException(status_code=422, detail=f"invalid {label}: {detail}") from exc
