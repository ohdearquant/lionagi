from __future__ import annotations

from typing import Any

import yaml

from lionagi.cli._runs import LIONAGI_HOME

from ._path_safety import public_path, safe_path_join

SKILLS_ROOT = LIONAGI_HOME / "skills"


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


def _find_skill_md(skill_dir: Any) -> Any | None:
    """Return the primary .md file for a skill directory.

    Checks for ``SKILL.md`` first (the canonical name used by all current
    skills), then falls back to a file named ``{dir_name}.md`` to handle
    the alternative layout described in the spec.
    """
    canonical = skill_dir / "SKILL.md"
    if canonical.exists():
        return canonical
    alt = skill_dir / f"{skill_dir.name}.md"
    if alt.exists():
        return alt
    # Last resort: any .md file in the directory
    mds = list(skill_dir.glob("*.md"))
    return mds[0] if mds else None


def list_skills() -> list[dict[str, Any]]:
    if not SKILLS_ROOT.exists():
        return []
    out = []
    for entry in sorted(SKILLS_ROOT.iterdir()):
        if entry.name.startswith("_"):
            # Skip hidden/archive directories (e.g. _archive)
            continue

        if entry.is_dir():
            path = _find_skill_md(entry)
            if path is None:
                continue
        elif entry.suffix == ".md":
            # Direct .md file at skills root
            path = entry
        else:
            continue

        try:
            text = path.read_text()
        except OSError:
            continue

        fm, _ = _parse_frontmatter(text)
        allowed_tools = fm.get("allowed-tools")
        if not isinstance(allowed_tools, list):
            allowed_tools = [allowed_tools] if allowed_tools else []

        out.append(
            {
                "name": fm.get("name") or entry.stem,
                "description": str(fm.get("description") or "").strip(),
                "path": public_path(path),
                "allowed_tools": allowed_tools,
            }
        )
    return out


def get_skill(name: str) -> dict[str, Any] | None:
    # Validate path component — raises HTTPException(404) if unsafe
    safe_path_join(SKILLS_ROOT, name)

    skill_dir = SKILLS_ROOT / name
    if skill_dir.is_dir():
        path = _find_skill_md(skill_dir)
        if path is None:
            return None
    else:
        # Try as a direct .md file
        stem = name.removesuffix(".md")
        path = SKILLS_ROOT / f"{stem}.md"
        if not path.exists():
            return None

    try:
        text = path.read_text()
    except OSError:
        return None

    fm, body = _parse_frontmatter(text)
    allowed_tools = fm.get("allowed-tools")
    if not isinstance(allowed_tools, list):
        allowed_tools = [allowed_tools] if allowed_tools else []

    return {
        "name": fm.get("name") or name.removesuffix(".md"),
        "description": str(fm.get("description") or "").strip(),
        "path": public_path(path),
        "allowed_tools": allowed_tools,
        "content": body,
    }
