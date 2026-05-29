# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

__all__ = ("Pack", "RolePolicy")


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
class Pack:
    """A named set of per-role operational overlays.

    ``default`` ships with lionagi; users supply their own pack files to
    override or extend it.
    """

    name: str
    policies: dict[str, RolePolicy] = field(default_factory=dict)

    def policy(self, role: str, /) -> RolePolicy | None:
        return self.policies.get(role)

    @classmethod
    def from_file(cls, path: str | Path, /) -> Pack:
        import yaml

        path = Path(path)
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        policies = {
            role: RolePolicy(
                authority=tuple(spec.get("authority") or ()),
                boundaries=tuple(spec.get("boundaries") or ()),
                escalations=tuple(spec.get("escalations") or ()),
            )
            for role, spec in (data.get("roles") or {}).items()
        }
        return cls(name=data.get("name", path.stem), policies=policies)
