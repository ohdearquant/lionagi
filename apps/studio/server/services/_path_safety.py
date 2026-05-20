"""Path safety utilities for the Lion Studio server.

All route path components (topic, run_id, agent_name, etc.) that are joined to
filesystem roots MUST be validated through ``safe_path_join`` before any
filesystem operation.  This prevents path traversal including URL-encoded
variants such as ``%2e%2e`` which the ASGI layer decodes before the route
parameter is populated.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException

_DANGEROUS_CHARS = frozenset("/\\\x00")


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
