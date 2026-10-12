# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""`li skill` — CC-compatible skill reader (~/.lionagi/skills/<NAME>/SKILL.md)."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from lionagi.libs.path_safety import validate_path_component

from ._logging import log_error


def _skills_root() -> Path:
    return Path("~/.lionagi/skills").expanduser()


# Pointer files git writes (.git gitfiles, commondir) hold one short line; a skill
# lookup must not pay for whatever else happens to be sitting at that path.
_POINTER_READ_LIMIT = 4096


def _first_line(path: Path) -> str:
    """First line of a pointer file, stripped, bounded, "" when unreadable.

    Regular files only, decided from the OPEN FILE rather than from a prior stat: a FIFO at a
    pointer path would otherwise block this open until someone writes to it, and a stat-then-open
    pair can be raced. ``O_NONBLOCK`` makes the FIFO open return at once so the shape test can run;
    it does not change how a regular file reads.
    """
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        return ""
    handle = None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return ""
        handle = os.fdopen(fd, "r", encoding="utf-8", errors="replace")
        return handle.readline(_POINTER_READ_LIMIT).strip()
    except OSError:
        return ""
    finally:
        if handle is not None:
            handle.close()
        else:
            os.close(fd)


def _is_git_dir(git: Path) -> bool:
    """git's own shape test for a repository directory: HEAD here, with objects/
    and refs/ either here or, for a linked worktree's private gitdir, in the
    common directory its ``commondir`` file names. That gitdir has no objects/
    of its own, and older git gives it no refs/ either, so requiring either
    one here rejects linked worktrees."""
    if not (git.is_dir() and (git / "HEAD").is_file()):
        return False
    if (git / "objects").is_dir() and (git / "refs").is_dir():
        return True
    common = _first_line(git / "commondir")
    if not common:
        return False
    shared = (git / common).resolve()
    return (shared / "objects").is_dir() and (shared / "refs").is_dir()


def _is_repository(directory: Path) -> bool:
    """A working tree root: ``.git`` is a repository directory, or a gitfile whose
    ``gitdir:`` line points at one (linked worktree). Any other ``.git`` entry is not."""
    git = directory / ".git"
    if git.is_file():
        line = _first_line(git)
        if not line.startswith("gitdir:"):
            return False
        return _is_git_dir((directory / line[len("gitdir:") :].strip()).resolve())
    return _is_git_dir(git)


def _containment_anchor(resolved_root: Path) -> Path:
    """Directory a resolved SKILL.md must stay under: the repository the skills
    root resolves into, or the root itself when it is not inside one."""
    return _nearest_repository(resolved_root) or resolved_root


def _nearest_repository(directory: Path) -> Path | None:
    """Return the nearest repository containing a resolved path, if any."""
    for ancestor in (directory, *directory.parents):
        if _is_repository(ancestor):
            return ancestor
    return None


def resolve_skill_path(name: str) -> tuple[Path | None, str | None]:
    """Resolve a skill NAME to (Path, None) or (None, error).

    The resolved SKILL.md must stay under the skills root. If that root is a
    symlink into a repository, skills may also live elsewhere in that repository.
    """
    if not name or not isinstance(name, str):
        return None, "skill name must be a non-empty string"
    try:
        validate_path_component(name, label="skill NAME")
    except ValueError:
        return None, f"skill NAME must be a bare identifier, got {name!r}."
    skills_root = _skills_root()
    candidate = skills_root / name / "SKILL.md"
    if not candidate.is_file():
        suggestions = list_skill_names()
        hint = (
            f" Available: {', '.join(suggestions[:10])}"
            if suggestions
            else " No skills installed at ~/.lionagi/skills/"
        )
        return None, f"skill not found: {candidate}.{hint}"
    # Symlink containment: blocks a SKILL.md symlinked to an arbitrary file.
    # A symlinked skills root may point into a repository whose skill
    # directories link to sibling trees. Ordinary roots stay bounded to
    # themselves, even when a repository contains them.
    try:
        resolved_root = skills_root.resolve(strict=True)
        resolved_candidate = candidate.resolve(strict=True)
        anchor = resolved_root
        if skills_root.is_symlink():
            anchor = _containment_anchor(resolved_root)
            if anchor != resolved_root:
                if resolved_candidate.name != "SKILL.md":
                    raise ValueError(resolved_candidate)
                if _nearest_repository(resolved_candidate) != anchor:
                    raise ValueError(resolved_candidate)
        resolved_candidate.relative_to(anchor)
    except (OSError, RuntimeError, ValueError):
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
