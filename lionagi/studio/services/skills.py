from __future__ import annotations

from typing import Any

from lionagi._paths import LIONAGI_HOME
from lionagi.libs.frontmatter import parse_frontmatter as _parse_frontmatter

from ._path_safety import public_path, safe_path_join

SKILLS_ROOT = LIONAGI_HOME / "skills"


def _find_skill_md(skill_dir: Any) -> Any | None:
    """Return the primary .md file for a skill directory — SKILL.md, then {dir_name}.md, then any .md."""
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
            continue

        if entry.is_dir():
            path = _find_skill_md(entry)
            if path is None:
                continue
        elif entry.suffix == ".md":
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
    safe_path_join(SKILLS_ROOT, name)

    skill_dir = SKILLS_ROOT / name
    if skill_dir.is_dir():
        path = _find_skill_md(skill_dir)
        if path is None:
            return None
    else:
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
