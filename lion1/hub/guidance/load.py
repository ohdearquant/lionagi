"""Guidance files: the prose ahead of the runtime's own LNDL guidance, one markdown file per role.
`$name` fields are filled by the caller; a field with no value stays as written, so a missing one is
visible in the prompt rather than an exception at build."""

from __future__ import annotations

from pathlib import Path
from string import Template

__all__ = ("HERE", "names", "guidance")

HERE = Path(__file__).resolve().parent


def names() -> list[str]:
    return sorted(p.stem for p in HERE.glob("*.md"))


def guidance(name: str, **fields: object) -> str:
    """The text of `hub/guidance/<name>.md`, its `$fields` filled, trailing newline dropped."""
    path = HERE / f"{name}.md"
    if not path.exists():
        raise KeyError(f"no guidance {name!r}; there is {', '.join(names())}")
    return Template(path.read_text()).safe_substitute({k: str(v) for k, v in fields.items()}).rstrip("\n")
