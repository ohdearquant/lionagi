"""Path safety utilities for the Lion Studio server.

All route path components (topic, run_id, agent_name, etc.) that are joined to
filesystem roots MUST be validated through ``safe_path_join`` before any
filesystem operation.  This prevents path traversal including URL-encoded
variants such as ``%2e%2e`` which the ASGI layer decodes before the route
parameter is populated.

``validate_name_component`` is a stricter guard for definition names (and
``kind`` values) that additionally rejects glob metacharacters that could be
fed into ``Path.glob()`` calls.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException

from lionagi.libs.path_safety import safe_join, validate_name


def safe_path_join(root: Path, component: str) -> Path:
    """Return ``root / component`` after asserting it stays inside *root*.

    Raises ``HTTPException(404)`` for any component that:
    - is empty
    - is ``"."`` or ``".."``
    - contains a forward-slash, backslash, or NUL character
    - contains only whitespace
    - resolves to a path outside *root* (covers symlink-based escapes and any
      multi-segment smuggling that slips past the simpler character checks)
    """
    try:
        return safe_join(root, component)
    except ValueError:
        raise HTTPException(status_code=404, detail="invalid path component") from None


def public_path(path: Path, *, fallback_name: bool = True) -> str:
    """Return a repo-relative or home-relative path string, never an absolute path.

    Safe for inclusion in API responses — strips the local checkout prefix.
    Falls back to just the filename when the path cannot be made relative.
    """
    resolved = path.resolve()
    # parents from _path_safety.py (lionagi/studio/services/):
    # [0]=services [1]=studio [2]=lionagi [3]=repo_root
    _repo_root = Path(__file__).resolve().parents[3]
    roots = [_repo_root, Path.home()]
    for root in roots:
        try:
            rel = resolved.relative_to(root.resolve())
        except ValueError:
            continue
        return rel.as_posix()
    return resolved.name if fallback_name else ""


def validate_name_component(value: str, label: str = "name") -> None:
    """Raise ``HTTPException(422)`` if *value* is not safe as a single path
    component for a definition name or ``kind`` value.

    Extends ``safe_path_join`` checks with rejection of glob metacharacters
    so that caller-supplied values cannot be passed to ``Path.glob()``.

    Raises:
        HTTPException(422): for any unsafe value.
    """
    try:
        validate_name(value, label)
    except ValueError as exc:
        detail = str(exc)
        if "empty" in detail or "whitespace" in detail:
            raise HTTPException(
                status_code=422, detail=f"invalid {label}: empty or whitespace"
            ) from exc
        if "reserved" in detail:
            raise HTTPException(
                status_code=422, detail=f"invalid {label}: reserved component"
            ) from exc
        if "separator" in detail or "NUL" in detail:
            raise HTTPException(
                status_code=422, detail=f"invalid {label}: contains path separator or NUL"
            ) from exc
        if "glob" in detail:
            raise HTTPException(
                status_code=422, detail=f"invalid {label}: contains glob metacharacter"
            ) from exc
        raise HTTPException(status_code=422, detail=detail) from exc
