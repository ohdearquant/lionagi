# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from lionagi.ln.types import Enum, ModelConfig, Params
from lionagi.protocols._concepts import Composable

__all__ = (
    "Pattern",
    "PatternKind",
    "Mode",
    "Role",
    "list_roles",
    "list_modes",
)

_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


class PatternKind(Enum):
    OTHER = "other"
    ROLE = "role"
    MODE = "mode"


@dataclass(init=False, frozen=True, slots=True)
class Pattern(Params, Composable):
    """Abstract, composable atom of agent configuration.

    A frozen value object with a name and description. Concrete patterns
    subclass it (Role, Mode) and override ``kind``.
    """

    _config = ModelConfig(
        none_as_sentinel=True,
        empty_as_sentinel=True,
    )

    name: str
    description: str

    @property
    def kind(self) -> PatternKind:
        return PatternKind.OTHER


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split YAML frontmatter from markdown body. Returns (meta, body)."""
    import yaml

    fm_match = _FRONTMATTER_RE.match(text)
    if not fm_match:
        raise ValueError("Missing YAML frontmatter.")
    meta = yaml.safe_load(fm_match.group(1)) or {}
    body = text[fm_match.end() :]
    return meta, body


def _read_pattern_md(*parts: str) -> str:
    """Read a packaged pattern markdown file under casts/roles/ (wheel-safe)."""
    from importlib.resources import files

    return files("lionagi.casts").joinpath("roles", *parts).read_text(encoding="utf-8")


@dataclass(init=False, frozen=True, slots=True)
class Mode(Pattern):
    """Cognitive overlay — shapes *how* an agent reasons."""

    behaviors: str = ""
    conflicts_with: frozenset = field(default_factory=frozenset)

    @property
    def kind(self) -> PatternKind:
        return PatternKind.MODE

    @classmethod
    def from_md(cls, s: str, /) -> Mode:
        meta, body = _parse_frontmatter(s)
        return cls(
            name=meta["name"],
            description=meta.get("description", ""),
            behaviors=body.strip(),
            conflicts_with=frozenset(meta.get("conflicts_with") or ()),
        )

    @classmethod
    def from_file(cls, path: Path, /) -> Mode:
        return cls.from_md(path.read_text(encoding="utf-8"))

    @classmethod
    def load(cls, name: str, /) -> Mode:
        """Load a built-in mode by name from the packaged roles/modes/."""
        return cls.from_md(_read_pattern_md("modes", f"{name}.md"))


@dataclass(init=False, frozen=True, slots=True)
class Role(Pattern):
    """Behavioral pattern — what an agent does and the discipline it follows.

    The markdown body (mission, principles, anti-patterns, artifacts) composes
    into the system prompt. The frontmatter ``description`` is the dense,
    orchestrator-facing selection signal and is not part of the prompt body.
    """

    body: str = ""

    @property
    def kind(self) -> PatternKind:
        return PatternKind.ROLE

    @classmethod
    def from_md(cls, s: str, /) -> Role:
        meta, body = _parse_frontmatter(s)
        return cls(
            name=meta["name"],
            description=meta.get("description", ""),
            body=body.strip(),
        )

    @classmethod
    def from_file(cls, path: str | Path, /) -> Role:
        return cls.from_md(Path(path).read_text(encoding="utf-8"))

    @classmethod
    def load(cls, name: str, /) -> Role:
        """Load a built-in role by name from the packaged roles/."""
        return cls.from_md(_read_pattern_md(f"{name}.md"))


def list_roles() -> list[str]:
    """Return sorted names of all available roles (packaged + user-local).

    Uses file stems so names round-trip through Role.load(name).
    Excludes TEMPLATE; the modes/ subdirectory is skipped automatically
    because it has no .md extension.
    """
    from importlib.resources import files

    pkg = files("lionagi.casts").joinpath("roles")
    names: set[str] = set()
    for item in pkg.iterdir():
        n = item.name
        if n.endswith(".md") and n != "TEMPLATE.md":
            names.add(n[:-3])
    user_dir = Path.home() / ".lionagi" / "roles"
    if user_dir.is_dir():
        for p in user_dir.glob("*.md"):
            names.add(p.stem)
    return sorted(names)


def list_modes() -> list[str]:
    """Return sorted names of all available modes (packaged + user-local).

    Uses file stems so names round-trip through Mode.load(name).
    """
    from importlib.resources import files

    pkg = files("lionagi.casts").joinpath("roles", "modes")
    names: set[str] = set()
    for item in pkg.iterdir():
        n = item.name
        if n.endswith(".md"):
            names.add(n[:-3])
    user_dir = Path.home() / ".lionagi" / "modes"
    if user_dir.is_dir():
        for p in user_dir.glob("*.md"):
            names.add(p.stem)
    return sorted(names)
