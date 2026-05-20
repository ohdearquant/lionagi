# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Load agent profiles from .lionagi/agents/

Directory layout (preferred — supports supplementary references):

    .lionagi/agents/<name>/
        <name>.md            # Main profile (frontmatter + system prompt)
        patterns/            # Optional: supplementary reference files
        refs/                # Optional: anything else the agent reads on demand

Flat layout (legacy, still resolved for backward compat):

    .lionagi/agents/<name>.md

Profile format (YAML frontmatter + markdown body):

    ---
    model: claude_code/opus
    effort: high
    yolo: true
    ---

    You are an implementer. Write production code, not stubs...

Frontmatter fields (all optional, CLI flags override):
  model:       provider/model spec
  effort:      reasoning effort level
  yolo:        auto-approve tool calls
  fast_mode:   route via OpenAI priority tier (codex only)
  lion_system: prepend LION_SYSTEM_MESSAGE (default: true)
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class AgentProfile:
    name: str
    system_prompt: str = ""
    model: str | None = None
    effort: str | None = None
    yolo: bool = False
    fast_mode: bool = False
    lion_system: bool = True
    extra: dict = field(default_factory=dict)


def _find_lionagi_dirs() -> list[Path]:
    """Find .lionagi/ directories — project-local first, then global ~/.lionagi/.

    Returns all found directories in priority order (project-local wins).
    """
    dirs: list[Path] = []

    # 1. Git root
    try:
        root = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if root.returncode == 0:
            candidate = Path(root.stdout.strip()) / ".lionagi"
            if candidate.is_dir():
                dirs.append(candidate)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # 2. Walk up from cwd
    cwd = Path.cwd()
    for parent in [cwd, *cwd.parents]:
        candidate = parent / ".lionagi"
        if candidate.is_dir() and candidate not in dirs:
            dirs.append(candidate)

    # 3. Global ~/.lionagi/ (always check)
    home_candidate = Path.home() / ".lionagi"
    if home_candidate.is_dir() and home_candidate not in dirs:
        dirs.append(home_candidate)

    return dirs


def _find_lionagi_dir() -> Path | None:
    """Find first .lionagi/ directory (backward compat)."""
    dirs = _find_lionagi_dirs()
    return dirs[0] if dirs else None


def _resolve_profile_path(agents_dir: Path, name: str) -> Path | None:
    """Return the profile file for NAME in AGENTS_DIR, or None.

    Resolution order:
      1. <agents_dir>/<name>/<name>.md   (directory layout, preferred)
      2. <agents_dir>/<name>.md          (flat legacy layout)
    """
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
    """Load an agent profile by name.

    Searches project-local .lionagi/agents/ first, then ~/.lionagi/agents/.
    Resolves directory layout (<name>/<name>.md) before flat (<name>.md).
    Raises FileNotFoundError if not found in any location.
    """
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
    """Parse YAML frontmatter + markdown body."""
    frontmatter = {}
    body = text

    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            fm_text = parts[1].strip()
            body = parts[2].strip()
            for line in fm_text.splitlines():
                line = line.strip()
                if ":" in line:
                    key, _, val = line.partition(":")
                    val = val.strip()
                    if val.lower() in ("true", "false"):
                        frontmatter[key.strip()] = val.lower() == "true"
                    else:
                        frontmatter[key.strip()] = val

    lion_system = bool(frontmatter.get("lion_system", True))
    if lion_system:
        from lionagi.session.prompts import LION_SYSTEM_MESSAGE

        body = LION_SYSTEM_MESSAGE.strip() + "\n\n" + body

    return AgentProfile(
        name=name,
        system_prompt=body,
        model=frontmatter.get("model"),
        effort=frontmatter.get("effort"),
        yolo=bool(frontmatter.get("yolo", False)),
        fast_mode=bool(frontmatter.get("fast_mode", False)),
        lion_system=lion_system,
        extra={
            k: v
            for k, v in frontmatter.items()
            if k not in ("model", "effort", "yolo", "fast_mode", "lion_system")
        },
    )
