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

_DANGEROUS_CHARS = frozenset("/\\\x00")
# Characters that have special meaning in glob patterns — reject them in
# definition names so they cannot be weaponised via Path.glob().
_GLOB_METACHARACTERS = frozenset("*?[]{}~")


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
    if not component or component.strip() == "":
        raise HTTPException(status_code=404, detail="invalid path component")
    if component in {".", ".."}:
        raise HTTPException(status_code=404, detail="invalid path component")
    if any(c in component for c in _DANGEROUS_CHARS):
        raise HTTPException(status_code=404, detail="invalid path component")

    candidate = (root / component).resolve()
    root_resolved = root.resolve()
    try:
        candidate.relative_to(root_resolved)
    except ValueError:
        raise HTTPException(status_code=404, detail="path outside root") from None

    return candidate


def public_path(path: Path, *, fallback_name: bool = True) -> str:
    """Return a repo-relative or home-relative path string, never an absolute path.

    Safe for inclusion in API responses — strips the local checkout prefix.
    Falls back to just the filename when the path cannot be made relative.
    """
    resolved = path.resolve()
    # parents from _path_safety.py (apps/studio/server/services/):
    # [0]=services [1]=server [2]=studio [3]=apps [4]=repo_root
    _repo_root = Path(__file__).resolve().parents[4]
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
    if not value or value.strip() == "":
        raise HTTPException(status_code=422, detail=f"invalid {label}: empty or whitespace")
    if value in {".", ".."}:
        raise HTTPException(status_code=422, detail=f"invalid {label}: reserved component")
    if any(c in value for c in _DANGEROUS_CHARS):
        raise HTTPException(
            status_code=422, detail=f"invalid {label}: contains path separator or NUL"
        )
    if any(c in value for c in _GLOB_METACHARACTERS):
        raise HTTPException(status_code=422, detail=f"invalid {label}: contains glob metacharacter")
