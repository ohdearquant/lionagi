# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

__all__ = ("Pack", "RolePolicy", "RoleConfig")


@dataclass(frozen=True, slots=True)
class RolePolicy:
    """Runtime-facing operational policy for one role.

    Not part of the prompt body — describes the role's operational envelope
    (decision-rights, hand-off boundaries, escalation conditions) for a future
    orchestrator / operation node to consume. Kept separate from the behavioral
    Role so the prompt stays dense and the policy stays pluggable.

    Escalations are prose conditions ("when to hand off"); they carry no routing
    target yet — capability-based routing is added once that system exists.
    """

    authority: tuple[str, ...] = ()
    boundaries: tuple[str, ...] = ()
    escalations: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RoleConfig:
    """Per-role runtime configuration (ADR-0074).

    The pack is the per-role configuration layer: where ``RolePolicy`` is the
    prompt-side operational envelope, ``RoleConfig`` is the runtime tuning a
    casts role needs to actually run — which model/effort to use, which
    cognitive modes overlay by default, which are permitted, and whether the
    role is part of the active roster an orchestrator selects from.

    ``model``/``effort`` default to ``None`` (fall through to the caller's
    default) — a shipped pack should NOT hardcode a provider. ``default_modes``
    overlay onto the role body; ``modes_allow`` (empty = unrestricted) bounds a
    per-task mode override.
    """

    model: str | None = None
    effort: str | None = None
    default_modes: tuple[str, ...] = ()
    modes_allow: tuple[str, ...] = ()
    active: bool = True


@dataclass(frozen=True, slots=True)
class Pack:
    """A named set of per-role overlays — policy + runtime configuration.

    ``default`` ships with lionagi; users supply their own pack files to
    override or extend it.
    """

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
