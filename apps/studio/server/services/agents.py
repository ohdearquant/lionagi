from __future__ import annotations

from typing import Any

import yaml

from lionagi.cli._runs import LIONAGI_HOME

_AGENTS_ROOT = LIONAGI_HOME / "agents"


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Parse YAML frontmatter + markdown body. Returns (frontmatter_dict, body_text)."""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    try:
        fm = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        fm = {}
    return fm if isinstance(fm, dict) else {}, parts[2].strip()


def list_agents() -> list[dict[str, Any]]:
    if not _AGENTS_ROOT.exists():
        return []
    out = []
    for path in sorted(_AGENTS_ROOT.glob("*.md")):
        try:
            text = path.read_text()
        except OSError:
            continue
        fm, _ = _parse_frontmatter(text)
        entry: dict[str, Any] = {"name": path.stem, "path": str(path), **fm}
        if path.is_symlink():
            try:
                entry["symlink_target"] = str(path.resolve())
            except OSError:
                pass
        out.append(entry)
    return out


def get_agent(name: str) -> dict[str, Any] | None:
    stem = name.removesuffix(".md")
    path = _AGENTS_ROOT / f"{stem}.md"
    if not path.exists():
        return None
    try:
        text = path.read_text()
    except OSError:
        return None
    fm, body = _parse_frontmatter(text)
    result: dict[str, Any] = {
        "name": stem,
        "path": str(path),
        "frontmatter": fm,
        "body": body,
        "raw": text,
    }
    if path.is_symlink():
        try:
            result["symlink_target"] = str(path.resolve())
        except OSError:
            pass
    return result
