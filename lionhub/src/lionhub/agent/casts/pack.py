# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

__all__ = ("Pack", "RolePolicy", "RoleConfig")


@dataclass(frozen=True, slots=True)
class RolePolicy:
    """Runtime operational envelope for one role: decision-rights, boundaries, and escalations."""

    authority: tuple[str, ...] = ()
    boundaries: tuple[str, ...] = ()
    escalations: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RoleConfig:
    """Per-role runtime tuning: model, effort, permitted modes, and active-roster membership."""

    model: str | None = None
    effort: str | None = None
    default_modes: tuple[str, ...] = ()
    modes_allow: tuple[str, ...] = ()
    active: bool = True


@dataclass(frozen=True, slots=True)
class Pack:
    """Named set of per-role policy and config overlays; supply a custom pack to override the built-in default."""

    name: str
    policies: dict[str, RolePolicy] = field(default_factory=dict)
    configs: dict[str, RoleConfig] = field(default_factory=dict)

    def policy(self, role: str, /) -> RolePolicy | None:
        return self.policies.get(role)

    def config(self, role: str, /) -> RoleConfig | None:
        return self.configs.get(role)

    @classmethod
    def from_file(cls, path: str | Path, /) -> Pack:
        import yaml

        path = Path(path)
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        roles = data.get("roles") or {}
        policies = {
            role: RolePolicy(
                authority=tuple(spec.get("authority") or ()),
                boundaries=tuple(spec.get("boundaries") or ()),
                escalations=tuple(spec.get("escalations") or ()),
            )
            for role, spec in roles.items()
        }
        configs = {
            role: RoleConfig(
                model=spec.get("model"),
                effort=spec.get("effort"),
                default_modes=tuple(spec.get("default_modes") or ()),
                modes_allow=tuple(spec.get("modes_allow") or ()),
                active=bool(spec.get("active", True)),
            )
            for role, spec in roles.items()
        }
        return cls(name=data.get("name", path.stem), policies=policies, configs=configs)
