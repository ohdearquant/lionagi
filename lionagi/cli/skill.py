# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""`li skill` — CC-compatible skill reader (~/.lionagi/skills/<NAME>/SKILL.md)."""

from __future__ import annotations

from pathlib import Path

from lionagi.libs.path_safety import validate_path_component

from ._logging import log_error


def _skills_root() -> Path:
    return Path("~/.lionagi/skills").expanduser()


def resolve_skill_path(name: str) -> tuple[Path | None, str | None]:
    """Resolve a skill NAME to (Path, None) or (None, error); blocks symlink escapes outside skills root."""
    if not name or not isinstance(name, str):
        return None, "skill name must be a non-empty string"
    try:
        validate_path_component(name, label="skill NAME")
    except ValueError:
        return None, f"skill NAME must be a bare identifier, got {name!r}."
    candidate = _skills_root() / name / "SKILL.md"
    if not candidate.is_file():
        suggestions = list_skill_names()
        hint = (
            f" Available: {', '.join(suggestions[:10])}"
            if suggestions
            else " No skills installed at ~/.lionagi/skills/"
        )
        return None, f"skill not found: {candidate}.{hint}"
    # Symlink containment: blocks a SKILL.md symlinked to an arbitrary file.
    # See docs/internals/cli.md.
    try:
        resolved_root = _skills_root().resolve(strict=True)
        resolved_candidate = candidate.resolve(strict=True)
        resolved_candidate.relative_to(resolved_root)
    except (OSError, ValueError):
        return (
            None,
            f"skill {name!r} resolves outside skills root (symlink escape blocked)",
        )
    return candidate, None


def list_skill_names() -> list[str]:
    """Return sorted list of skill names present in ~/.lionagi/skills/."""
    root = _skills_root()
    if not root.is_dir():
        return []
    names: list[str] = []
    for child in root.iterdir():
        if child.is_dir() and (child / "SKILL.md").is_file():
            names.append(child.name)
    return sorted(names)


def strip_frontmatter(text: str) -> str:
    text = text.lstrip()
    if not text.startswith("---"):
        return text
    from lionagi.libs.frontmatter import _FM_SPLIT

    parts = _FM_SPLIT.split(text, maxsplit=2)
    if len(parts) < 3:
        return text
    return parts[2].lstrip("\n")


def read_skill_body(name: str) -> tuple[str | None, str | None]:
    """Load and return the body of a skill (post-frontmatter)."""
    path, err = resolve_skill_path(name)
    if err is not None:
        return None, err
    text = path.read_text()
    return strip_frontmatter(text), None


def run_skill(argv: list[str]) -> int:
    """Handle `li skill NAME|list|show NAME` invocation."""
    if not argv:
        print("Usage: li skill <name>  |  li skill list  |  li skill show <name>")
        return 1
    head = argv[0]
    if head == "list":
        names = list_skill_names()
        if not names:
            print(f"(no skills in {_skills_root()})")
            return 0
        for n in names:
            print(n)
        return 0
    if head == "show":
        if len(argv) < 2:
            log_error("li skill show requires a NAME")
            return 1
        path, err = resolve_skill_path(argv[1])
        if err is not None:
            log_error(err)
            return 1
        print(path.read_text(), end="")
        return 0
    if head.startswith("-"):
        log_error("li skill NAME must come before flags")
        return 1
    body, err = read_skill_body(head)
    if err is not None:
        log_error(err)
        return 1
    # `end=""` — the body already ends with its own newline convention.
    print(body, end="")
    return 0
