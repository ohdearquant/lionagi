# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Load agent profiles from .lionagi/agents/ (directory or flat layout, YAML frontmatter)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from lionagi._paths import find_lionagi_dirs as _find_lionagi_dirs
from lionagi.libs.frontmatter import parse_frontmatter as _parse_frontmatter
from lionagi.libs.path_safety import validate_bare_name


def _validate_bare_name(name: str) -> None:
    validate_bare_name(name, label="agent profile name")


def build_deadline_preamble(timeout_seconds: int) -> str:
    """Build a [DEADLINE] preamble injected as the first user message when --timeout is set."""
    import time as _time
    from datetime import datetime, timezone

    minutes = max(1, int(timeout_seconds / 60))
    deadline_ts = _time.time() + timeout_seconds
    deadline_iso = datetime.fromtimestamp(deadline_ts, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    return (
        f"[DEADLINE]\n"
        f"You have {minutes} minute{'s' if minutes != 1 else ''} "
        f"(until {deadline_iso}) to complete this task.\n"
        f"Pace your reasoning accordingly. Prefer decisive verdicts over exhaustive\n"
        f"deliberation. If you're more than 60% through your time budget and\n"
        f"still in research mode, switch to writing the deliverable.\n\n"
        f"You can check the current time with: `date -Iseconds`\n"
        f"[/DEADLINE]\n"
    )


@dataclass
class AgentProfile:
    name: str
    system_prompt: str = ""
    raw_body: str = ""
    """Body as written in the file, before LION_SYSTEM_MESSAGE is prepended; use this when composing into AgentSpec.extra_prompt to avoid double-prepend."""
    model: str | None = None
    effort: str | None = None
    yolo: bool = False
    fast_mode: bool = False
    lion_system: bool = True
    artifact_defaults: dict | None = None
    extra: dict = field(default_factory=dict)


def _find_lionagi_dir() -> Path | None:
    """Find first .lionagi/ directory (backward compat)."""
    dirs = _find_lionagi_dirs()
    return dirs[0] if dirs else None


def _resolve_profile_path(agents_dir: Path, name: str) -> Path | None:
    """Return profile path for NAME: directory layout (<name>/<name>.md) before flat (<name>.md)."""
    dir_candidate = agents_dir / name / f"{name}.md"
    if dir_candidate.is_file():
        return dir_candidate
    flat_candidate = agents_dir / f"{name}.md"
    if flat_candidate.is_file():
        return flat_candidate
    return None


def list_agents() -> list[str]:
    """List available agent profile names (merged across all .lionagi/ dirs).

    Discovers both directory (<name>/<name>.md) and flat (<name>.md) layouts.
    """
    seen: set[str] = set()
    for d in _find_lionagi_dirs():
        agents_dir = d / "agents"
        if not agents_dir.is_dir():
            continue
        # Directory layout
        for child in agents_dir.iterdir():
            if child.is_dir() and (child / f"{child.name}.md").is_file():
                seen.add(child.name)
        # Flat legacy layout
        for p in agents_dir.glob("*.md"):
            if p.is_file():
                seen.add(p.stem)
    return sorted(seen)


def load_agent_profile(name: str) -> AgentProfile:
    """Load a named agent profile, searching project-local then global ~/.lionagi/agents/."""
    _validate_bare_name(name)
    dirs = _find_lionagi_dirs()
    if not dirs:
        raise FileNotFoundError(
            "No .lionagi/ directory found. Create .lionagi/agents/ in your repo "
            "or ~/.lionagi/agents/ globally."
        )

    for d in dirs:
        path = _resolve_profile_path(d / "agents", name)
        if path is not None:
            text = path.read_text()
            return _parse_profile(name, text)

    available = list_agents()
    msg = f"Agent profile '{name}' not found"
    if available:
        msg += f"\nAvailable: {', '.join(available)}"
    raise FileNotFoundError(msg)


def _parse_profile(name: str, text: str) -> AgentProfile:
    frontmatter, body = _parse_frontmatter(text)

    lion_system = bool(frontmatter.get("lion_system", True))
    raw_body = body  # always the body as written, before any expansion
    if lion_system:
        from lionagi.session.prompts import LION_SYSTEM_MESSAGE

        expanded = LION_SYSTEM_MESSAGE.strip() + "\n\n" + body
    else:
        expanded = body

    return AgentProfile(
        name=name,
        system_prompt=expanded,
        raw_body=raw_body,
        model=frontmatter.get("model"),
        effort=frontmatter.get("effort"),
        yolo=bool(frontmatter.get("yolo", False)),
        fast_mode=bool(frontmatter.get("fast_mode", False)),
        lion_system=lion_system,
        artifact_defaults=frontmatter.get("artifact_defaults"),
        extra={
            k: v
            for k, v in frontmatter.items()
            if k
            not in (
                "model",
                "effort",
                "yolo",
                "fast_mode",
                "lion_system",
                "artifact_defaults",
            )
        },
    )
