# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from lionagi.casts.pack import Pack
from lionagi.casts.profile import Profile

if TYPE_CHECKING:
    from lionagi.ln.types import Operable

    from .config import AgentConfig
    from .permissions import PermissionPolicy

__all__ = ("AgentSpec",)


@dataclass
class AgentSpec:
    """Universal runtime agent spec: Profile (identity) + runtime concerns.

    This is the orchestration-facing composition surface. Every entry point
    (cli, programmatic create_agent, flow wiring) composes to AgentSpec and
    builds a Branch from it.
    """

    profile: Profile
    model: str | None = None
    effort: str | None = None
    tools: tuple[str, ...] = ()
    permissions: PermissionPolicy | None = None
    grant_capabilities: bool = True
    pack: str | Pack | None = "default"
    lion_system: bool = True
    # Bridge field: captures AgentConfig.system_prompt that has no AgentSpec
    # equivalent. Appended after role+modes in build_system_message().
    extra_prompt: str | None = None

    @classmethod
    def compose(
        cls,
        role: Any,
        *,
        modes: list[Any] | None = None,
        model: str | None = None,
        effort: str | None = None,
        tools: tuple[str, ...] | list[str] = (),
        permissions: Any = None,
        pack: str | Pack | None = "default",
        grant_capabilities: bool = True,
    ) -> AgentSpec:
        """Build an AgentSpec from a role name/object + optional overrides."""
        prof = Profile.compose(role, modes=modes)
        perm = _resolve_permissions(permissions)
        return cls(
            profile=prof,
            model=model,
            effort=effort,
            tools=tuple(tools),
            permissions=perm,
            pack=pack,
            grant_capabilities=grant_capabilities,
        )

    def build_system_message(self) -> str:
        """Compose role + modes + RolePolicy block (authority / boundaries /
        escalations) + any extra literal prompt."""
        body = self.profile.build_system_message()
        policy_block = self._render_policy_block()
        parts = [p for p in (body, policy_block, self.extra_prompt) if p]
        return "\n\n".join(parts)

    def capability_operable(self) -> Operable | None:
        """Return the role's Operable for capability granting, or None when
        grant_capabilities is False."""
        if not self.grant_capabilities:
            return None
        from lionagi.casts.capabilities import capability_operable

        return capability_operable(self.profile.role.name)

    def _render_policy_block(self) -> str:
        """Render the RolePolicy as operational guidance in the system message."""
        if self.pack is None:
            return ""
        pack = _load_pack(self.pack)
        if pack is None:
            return ""
        policy = pack.policy(self.profile.role.name)
        if policy is None:
            return ""

        lines: list[str] = []
        if policy.authority:
            lines.append("## Authority")
            lines.extend(f"- {a}" for a in policy.authority)
        if policy.boundaries:
            if lines:
                lines.append("")
            lines.append("## Operational Boundaries")
            lines.extend(f"- {b}" for b in policy.boundaries)
        if policy.escalations:
            if lines:
                lines.append("")
            lines.append("## Escalation Conditions")
            lines.append(
                "When any of these conditions occur, STOP and emit an"
                " `escalation_request` capability with the reason:"
            )
            lines.extend(f"- {e}" for e in policy.escalations)
        return "\n".join(lines)

    @classmethod
    def from_config(cls, config: AgentConfig) -> AgentSpec:
        """Bridge an AgentConfig into an AgentSpec.

        Contract conflict resolution: AgentConfig.system_prompt has no direct
        AgentSpec equivalent. It is preserved in extra_prompt and appended to
        the composed system message by build_system_message().
        """
        from .permissions import PermissionPolicy

        perm: PermissionPolicy | None = None
        if config.permissions:
            if isinstance(config.permissions, PermissionPolicy):
                perm = config.permissions
            elif isinstance(config.permissions, dict):
                perm = PermissionPolicy.from_dict(config.permissions)

        role = config.role or "implementer"
        prof = Profile.compose(role=role, modes=config.modes or [])

        return cls(
            profile=prof,
            model=config.model,
            effort=config.effort,
            tools=tuple(config.tools or []),
            permissions=perm,
            grant_capabilities=True,
            pack="default",
            lion_system=config.lion_system,
            extra_prompt=config.system_prompt or None,
        )


def _resolve_permissions(
    permissions: Any,
) -> PermissionPolicy | None:
    """Normalise a permissions argument to a PermissionPolicy or None."""
    from .permissions import PermissionPolicy

    if permissions is None:
        return None
    if isinstance(permissions, PermissionPolicy):
        return permissions
    if isinstance(permissions, dict):
        return PermissionPolicy.from_dict(permissions)
    if isinstance(permissions, str):
        preset = permissions.lower()
        presets = {
            "safe": PermissionPolicy.safe,
            "read_only": PermissionPolicy.read_only,
            "allow_all": PermissionPolicy.allow_all,
            "deny_all": PermissionPolicy.deny_all,
        }
        factory = presets.get(preset)
        if factory is None:
            raise ValueError(
                f"Unknown permissions preset {permissions!r}. Valid: {sorted(presets)}"
            )
        return factory()
    raise TypeError(f"Cannot resolve permissions from {type(permissions)!r}")


def _load_pack(pack: str | Pack) -> Pack | None:
    """Load a Pack by name or return it directly if already a Pack."""
    if isinstance(pack, Pack):
        return pack
    if pack == "default":
        from importlib.resources import as_file, files

        packaged = files("lionagi.casts").joinpath("packs", "default.yaml")
        with as_file(packaged) as p:
            return Pack.from_file(p)
    return None
