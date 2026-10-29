# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from lionagi.casts.pack import Pack
from lionagi.casts.profile import Profile

if TYPE_CHECKING:
    from lionagi.ln.types import Operable
    from lionagi.tools._seatbelt import SeatbeltPolicy

    from .permissions import PermissionPolicy

__all__ = ("AgentSpec", "HooksMixin")

_CODING_EFFORT_DEFAULT = object()


class HooksMixin:
    """Shared hook-registration helpers for agent spec dataclasses."""

    hook_handlers: dict[str, list[Callable]]

    def pre(self, tool_name: str, handler: Callable) -> HooksMixin:
        self.hook_handlers.setdefault(f"pre:{tool_name}", []).append(handler)
        return self

    def security_pre(self, tool_name: str, handler: Callable) -> HooksMixin:
        self.hook_handlers.setdefault(f"security_pre:{tool_name}", []).append(handler)
        return self

    def post(self, tool_name: str, handler: Callable) -> HooksMixin:
        self.hook_handlers.setdefault(f"post:{tool_name}", []).append(handler)
        return self

    def on_error(self, tool_name: str, handler: Callable) -> HooksMixin:
        self.hook_handlers.setdefault(f"error:{tool_name}", []).append(handler)
        return self


def _wire_secure_guards(obj: HooksMixin, cwd: str | None) -> None:
    """Register the standard destructive-command + path-containment guards.

    Uses the security_pre bucket (not user pre) so these guards participate
    in the security -> user -> security recheck (ADR-0086 delta row 1).
    """
    from lionagi.agent.hooks import guard_destructive, guard_paths

    obj.security_pre("bash", guard_destructive)
    workspace_root = str(Path(cwd) if cwd else Path.cwd())
    path_guard = guard_paths(allowed_paths=[workspace_root])
    obj.security_pre("reader", path_guard)
    obj.security_pre("editor", path_guard)


@dataclass
class AgentSpec(HooksMixin):
    """Universal runtime spec: Profile (identity) plus model, tools, hooks, and permissions."""

    profile: Profile
    model: str | None = None
    effort: str | None = None
    tools: tuple[str, ...] = ()
    permissions: PermissionPolicy | None = None
    grant_emissions: bool = True
    emits: tuple | None = None
    pack: str | Pack | None = "default"
    lion_system: bool = True
    extra_prompt: str | None = None
    hook_handlers: dict[str, list[Callable]] = field(default_factory=dict)
    external_hooks: list[dict[str, Any]] = field(default_factory=list)
    cwd: str | None = None
    yolo: bool = False
    mcp_servers: list[str] | None = None
    mcp_config_path: str | None = None
    context_management: bool = True
    # The coding tools to bind when `tools` names "coding"; None binds the default set.
    coding_tools: tuple[str, ...] | None = None
    # When set, the coding editor and bash run under this macOS Seatbelt policy.
    seatbelt: SeatbeltPolicy | None = None
    khive_injection: Any = None
    # Codex only: turn the signed-in account's apps and plugins off for this
    # agent's endpoint by default (a call can still lift it; see create_agent).
    # An agent of another provider is built unchanged.
    contain_account_integrations: bool = False
    # Turn the endpoint's prompt_caching option on (ADR-0127) for the model
    # create_agent builds. A provider that does not declare prefix caching sends
    # its requests unchanged, as the option does everywhere.
    prompt_caching: bool = False
    command_env_allowlist: tuple[str, ...] = ()
    # The output-token limit set on the Anthropic model create_agent builds, in the
    # request field its endpoint names ``max_tokens``; any other provider refuses it.
    # None sets nothing, so the provider's own default applies (Anthropic sends 8192).
    max_tokens: int | None = None

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "effort" and hasattr(self, "_effort_origin"):
            object.__setattr__(self, "_effort_origin", "explicit")
        if name == "model" and hasattr(self, "_model_origin"):
            object.__setattr__(self, "_model_origin", "explicit")
        object.__setattr__(self, name, value)

    def __post_init__(self) -> None:
        # Every way of building a spec (constructor, compose, from_yaml) passes here, so a
        # limit the provider would reject or a bool that reads as 1 stops at the spec.
        value = self.max_tokens
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value < 1
        ):
            raise ValueError(f"max_tokens must be a positive integer, got {value!r}")

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
        grant_emissions: bool = True,
        emits: tuple | None = None,
        system_prompt: str | None = None,
        cwd: str | None = None,
        yolo: bool = False,
        khive_injection: Any = None,
        contain_account_integrations: bool = False,
        prompt_caching: bool = False,
        command_env_allowlist: tuple[str, ...] = (),
        max_tokens: int | None = None,
    ) -> AgentSpec:
        """Build an AgentSpec from a role name/object; ``emits=None`` uses role's declared contract."""
        from lionagi._command_env import normalize_command_env_allowlist

        prof = Profile.compose(role, modes=modes)
        perm = _resolve_permissions(permissions)
        return cls(
            profile=prof,
            model=model,
            effort=effort,
            tools=tuple(tools),
            permissions=perm,
            pack=pack,
            grant_emissions=grant_emissions,
            emits=emits,
            extra_prompt=system_prompt or None,
            cwd=cwd,
            yolo=yolo,
            khive_injection=khive_injection,
            contain_account_integrations=contain_account_integrations,
            prompt_caching=prompt_caching,
            command_env_allowlist=normalize_command_env_allowlist(command_env_allowlist),
            max_tokens=max_tokens,
        )

    @classmethod
    def coding(
        cls,
        *,
        role: str = "implementer",
        model: str | None = None,
        effort: str | None | object = _CODING_EFFORT_DEFAULT,
        system_prompt: str | None = None,
        cwd: str | None = None,
        secure: bool = True,
        context_management: bool = True,
        **kwargs: Any,
    ) -> AgentSpec:
        """Preset for a coding agent; ``secure=True`` wires guard_destructive + guard_paths.

        ``context_management=False`` drops the context tool and its system-prompt one-liner.
        """
        default_effort = effort is _CODING_EFFORT_DEFAULT
        spec = cls.compose(
            role,
            model=model,
            effort="high" if default_effort else effort,
            tools=["coding"],
            system_prompt=system_prompt,
            cwd=cwd,
            **kwargs,
        )
        if default_effort:
            object.__setattr__(spec, "_effort_origin", "default")
        spec.context_management = context_management
        if secure:
            _wire_secure_guards(spec, cwd)
        return spec

    @classmethod
    def from_yaml(cls, path: str | Path) -> AgentSpec:
        """Load an agent spec from YAML; its effort is a profile default."""
        import yaml

        p = Path(path)
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        flags = {}
        for key in ("contain_account_integrations", "prompt_caching"):
            value = data.get(key, False)
            if not isinstance(value, bool):
                # bool("false") is True, and a key with no value reads as None; either is a
                # mistake to report, not to read.
                got = "no value" if value is None else type(value).__name__
                raise ValueError(f"{key} must be true or false, got {got}")
            flags[key] = value
        max_tokens = data.get("max_tokens")
        spec = cls.compose(
            role=data.get("role", "implementer"),
            modes=data.get("modes"),
            model=data.get("model"),
            effort=data.get("effort"),
            tools=data.get("tools", []),
            permissions=data.get("permissions"),
            pack=data.get("pack", "default"),
            system_prompt=data.get("system_prompt"),
            cwd=data.get("cwd"),
            yolo=data.get("yolo", False),
            command_env_allowlist=data.get("command_env_allowlist", ()),
            max_tokens=max_tokens,
            **flags,
        )
        # compose() defaults lion_system to True, so a saved False must be
        # restored explicitly or it would be silently dropped.
        if "lion_system" in data:
            spec.lion_system = bool(data["lion_system"])
        if "context_management" in data:
            spec.context_management = bool(data["context_management"])
        if "khive_injection" in data:
            spec.khive_injection = data["khive_injection"]
        object.__setattr__(spec, "_effort_origin", "profile")
        object.__setattr__(spec, "_model_origin", "profile")
        return spec

    def build_system_message(self) -> str:
        """Compose role + modes + RolePolicy block + any extra literal prompt."""
        body = self.profile.build_system_message()
        policy_block = self._render_policy_block()
        parts = [p for p in (body, policy_block, self.extra_prompt) if p]
        return "\n\n".join(parts)

    def emission_operable(self) -> Operable | None:
        """Return Operable for capability granting, or None when grant_emissions=False."""
        if not self.grant_emissions:
            return None
        if self.emits is not None:
            from lionagi.casts import build_emission_operable

            return build_emission_operable(
                tuple(self.emits), name=f"{self.profile.role.name}_emissions"
            )
        return self.profile.emission_operable()

    def to_yaml(self, path: str | Path) -> None:
        """Save spec to YAML (without hook callables — those are code-only)."""
        from lionagi._command_env import normalize_command_env_allowlist

        data = {
            "role": self.profile.role.name,
            "modes": [m.name for m in self.profile.modes],
            "model": self.model,
            "effort": self.effort,
            "tools": list(self.tools),
            "pack": self.pack if isinstance(self.pack, str) else None,
            "system_prompt": self.extra_prompt,
            "yolo": self.yolo,
            "lion_system": self.lion_system,
            "context_management": self.context_management,
            "command_env_allowlist": list(
                normalize_command_env_allowlist(self.command_env_allowlist)
            ),
        }
        if self.cwd:
            data["cwd"] = self.cwd
        if self.contain_account_integrations:
            data["contain_account_integrations"] = True
        if self.prompt_caching:
            data["prompt_caching"] = True
        if self.max_tokens is not None:
            data["max_tokens"] = self.max_tokens
        if isinstance(self.khive_injection, (bool, dict)):
            data["khive_injection"] = self.khive_injection
        with open(path, "w") as f:
            import yaml

            yaml.dump(data, f, default_flow_style=False, allow_unicode=True)

    def _render_policy_block(self) -> str:
        if self.pack is None:
            return ""
        pack = _load_pack(self.pack)
        if pack is None:
            return ""
        role_name = self.profile.role.name
        policy = pack.policy(role_name)
        if policy is None:
            # Missing pack entry = privilege gap, fails loud (not a silent
            # no-op). An explicit empty entry (`{}`) opts into unconstrained.
            raise ValueError(
                f"Role {role_name!r} has no policy entry in pack {pack.name!r}. "
                "Add an entry under `roles:` in the pack yaml — an empty entry "
                "(e.g. `{}`) is the explicit way to run this role unconstrained; "
                "a missing entry is treated as a configuration error, not intent."
            )

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
                " `escalation_request` with the reason:"
            )
            lines.extend(f"- {e}" for e in policy.escalations)
        return "\n".join(lines)


def _resolve_permissions(permissions: Any) -> PermissionPolicy | None:
    from .permissions import PermissionPolicy

    if permissions is None:
        return None
    if isinstance(permissions, PermissionPolicy):
        return permissions
    if isinstance(permissions, dict):
        return PermissionPolicy.from_dict(permissions)
    if isinstance(permissions, str):
        presets = {
            "safe": PermissionPolicy.safe,
            "read_only": PermissionPolicy.read_only,
            "allow_all": PermissionPolicy.allow_all,
            "deny_all": PermissionPolicy.deny_all,
        }
        factory = presets.get(permissions.lower())
        if factory is None:
            raise ValueError(
                f"Unknown permissions preset {permissions!r}. Valid: {sorted(presets)}"
            )
        return factory()
    raise TypeError(f"Cannot resolve permissions from {type(permissions)!r}")


def _load_pack(pack: str | Pack) -> Pack | None:
    if isinstance(pack, Pack):
        return pack
    if pack == "default":
        from lionagi.casts.catalog import _load_packaged_pack

        return _load_packaged_pack(raise_on_error=True)
    return None
