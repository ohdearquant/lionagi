from __future__ import annotations

from functools import partial
from typing import Any

import anyio
import yaml
from fastapi import HTTPException
from pydantic import BaseModel

from lionagi._paths import LIONAGI_HOME
from lionagi.libs.frontmatter import parse_frontmatter as _parse_frontmatter
from lionagi.libs.frontmatter import parse_frontmatter_strict as _parse_frontmatter_strict
from lionagi.libs.path_safety import validate_bare_name

from ..registry import studio_route
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


def validate_skill_content(content: str, name: str) -> list[str]:
    """Pre-save checks for a skill's raw file content: safe directory name,
    parseable frontmatter, well-shaped ``allowed-tools``.

    ``parse_frontmatter`` (the tolerant reader used everywhere else) swallows
    a broken YAML block into ``{}`` rather than raising, so a skill saved with
    invalid frontmatter would silently lose its name/description/tool list on
    the very next read with no error anywhere. This is the one place that
    error is allowed to surface, before the content ever reaches disk.
    """
    errors: list[str] = []

    try:
        validate_bare_name(name, "skill name")
    except ValueError as exc:
        errors.append(str(exc))

    try:
        fm, _ = _parse_frontmatter_strict(content)
    except yaml.YAMLError as exc:
        errors.append(f"frontmatter is not valid YAML: {exc}")
    else:
        allowed_tools = fm.get("allowed-tools")
        if allowed_tools is not None and (
            not isinstance(allowed_tools, list)
            or not all(isinstance(t, str) for t in allowed_tools)
        ):
            errors.append("allowed-tools must be a list of tool name strings")

    return errors


class SkillValidateBody(BaseModel):
    content: str


@studio_route("/skills/", method="GET", area="skills", name="list_skills")
async def list_skills_route() -> dict[str, Any]:
    skills = await anyio.to_thread.run_sync(list_skills)
    return {"skills": skills}


@studio_route("/skills/{name}", method="GET", area="skills", name="get_skill")
async def get_skill_route(name: str) -> dict[str, Any]:
    skill = await anyio.to_thread.run_sync(partial(get_skill, name))
    if skill is None:
        raise HTTPException(status_code=404, detail=f"Skill '{name}' not found")
    return skill


@studio_route("/skills/{name}/validate", method="POST", area="skills", name="validate_skill")
async def validate_skill_route(name: str, body: SkillValidateBody) -> dict[str, Any]:
    errors = await anyio.to_thread.run_sync(partial(validate_skill_content, body.content, name))
    return {"ok": not errors, "errors": errors or None}
