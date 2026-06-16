# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from lionagi.casts.pattern import Mode, Role

if TYPE_CHECKING:
    from lionagi.ln.types import Operable

__all__ = ("Profile",)


@dataclass(frozen=True, slots=True)
class Profile:
    """Named identity composition: one Role + ordered cognitive Modes (pure
    config; validates mode conflicts at construction)."""

    name: str
    role: Role
    modes: tuple[Mode, ...] = ()

    def __post_init__(self) -> None:
        seen: dict[str, Mode] = {}
        for m in self.modes:
            for other in seen.values():
                if m.name in other.conflicts_with or other.name in m.conflicts_with:
                    raise ValueError(f"Mode conflict: {m.name!r} vs {other.name!r}")
            seen[m.name] = m

    def emission_operable(self) -> Operable | None:
        """Delegate to the role's emission contract."""
        return self.role.emission_operable()

    def build_system_message(self) -> str:
        """Compose role body + each mode's behaviors, joined by blank lines."""
        parts = [self.role.body] if self.role.body else []
        parts += [m.behaviors for m in self.modes if m.behaviors]
        return "\n\n".join(parts)

    @classmethod
    def compose(
        cls,
        role: str | Role,
        *,
        modes: list[str | Mode] | None = None,
        name: str | None = None,
    ) -> Profile:
        """Build a Profile from role/mode names or objects."""
        r = role if not isinstance(role, str) else Role.load(role)
        ms = tuple(m if not isinstance(m, str) else Mode.load(m) for m in (modes or []))
        return cls(name=name or r.name, role=r, modes=ms)

    @classmethod
    def from_yaml(cls, path: str | Path) -> Profile:
        """Load a Profile from a YAML file with {name, role, modes: [...]}."""
        import yaml

        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return cls.compose(
            role=data["role"],
            modes=data.get("modes") or [],
            name=data.get("name"),
        )

    def to_yaml(self, path: str | Path) -> None:
        """Save to YAML ({name, role, modes}) using canonical names — symmetric
        with from_yaml."""
        import yaml

        data = {
            "name": self.name,
            "role": self.role.name,
            "modes": [m.name for m in self.modes],
        }
        Path(path).write_text(
            yaml.dump(data, default_flow_style=False, allow_unicode=True),
            encoding="utf-8",
        )
