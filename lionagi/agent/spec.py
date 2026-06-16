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

    from .permissions import PermissionPolicy

__all__ = ("AgentSpec", "HooksMixin")


class HooksMixin:
    """Shared hook-registration helpers for agent spec dataclasses."""

    hook_handlers: dict[str, list[Callable]]

    def pre(self, tool_name: str, handler: Callable) -> HooksMixin:
        self.hook_handlers.setdefault(f"pre:{tool_name}", []).append(handler)
        return self

    def post(self, tool_name: str, handler: Callable) -> HooksMixin:
        self.hook_handlers.setdefault(f"post:{tool_name}", []).append(handler)
        return self

    def on_error(self, tool_name: str, handler: Callable) -> HooksMixin:
        self.hook_handlers.setdefault(f"error:{tool_name}", []).append(handler)
        return self


def _wire_secure_guards(obj: HooksMixin, cwd: str | None) -> None:
    """Register the standard destructive-command + path-containment guards.

    ``obj`` must support .pre() (HooksMixin).
    """
    from lionagi.agent.hooks import guard_destructive, guard_paths

    obj.pre("bash", guard_destructive)
    workspace_root = str(Path(cwd) if cwd else Path.cwd())
    path_guard = guard_paths(allowed_paths=[workspace_root])
    obj.pre("reader", path_guard)
    obj.pre("editor", path_guard)


@dataclass
class AgentSpec(HooksMixin):
    """Universal runtime agent spec: a Profile (identity) plus runtime concerns —
    the orchestration-facing surface every entry point builds a Branch from."""

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
    cwd: str | None = None
    yolo: bool = False
    mcp_servers: list[str] | None = None
    mcp_config_path: str | None = None

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
    ) -> AgentSpec:
        """Build an AgentSpec from a role name/object + optional overrides.

        ``emits`` overrides *what* capability models are granted: ``None`` uses
        the role's declared contract; a tuple grants exactly those models (plus
        EscalationRequest). Engines pass it for stage-specific event types.
        """
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
        )

    @classmethod
    def coding(
        cls,
        *,
        model: str | None = None,
        effort: str | None = "high",
        system_prompt: str | None = None,
        cwd: str | None = None,
        secure: bool = True,
        **kwargs: Any,
    ) -> AgentSpec:
        """Preset for a coding agent — implementer role + coding tools.

        By default (``secure=True``), wires two guards:

        - ``guard_destructive`` as a pre-hook on ``bash`` — blocks destructive
          shell commands (rm -rf, force-push, etc.).
        - ``guard_paths`` as a pre-hook on ``reader`` and ``editor`` — restricts
          file access to the workspace root (``cwd`` if provided, else
          ``Path.cwd()`` at call time).

        Set ``secure=False`` to disable these defaults and manage hooks manually.
        """
        spec = cls.compose(
            "implementer",
            model=model,
            effort=effort,
            tools=["coding"],
            system_prompt=system_prompt,
            cwd=cwd,
            **kwargs,
        )
        if secure:
            _wire_secure_guards(spec, cwd)
        return spec

    @classmethod
    def from_yaml(cls, path: str | Path) -> AgentSpec:
        """Load an agent spec from a YAML file."""
        import yaml

        p = Path(path)
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
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
        )
        # compose() defaults lion_system to True, so a saved False must be
        # restored explicitly or it would be silently dropped.
        if "lion_system" in data:
            spec.lion_system = bool(data["lion_system"])
        return spec

    def build_system_message(self) -> str:
        """Compose role + modes + RolePolicy block + any extra literal prompt."""
        body = self.profile.build_system_message()
        policy_block = self._render_policy_block()
        parts = [p for p in (body, policy_block, self.extra_prompt) if p]
        return "\n\n".join(parts)

    def emission_operable(self) -> Operable | None:
        """Return the Operable for emission granting, or None.

        ``grant_emissions=False`` disables granting entirely. Otherwise an
        explicit ``emits`` tuple overrides *what* is granted (empty tuple ⇒
        grant nothing, since ``build_emission_operable(())`` is None); ``None``
        falls back to the role's declared contract.
        """
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
        }
        if self.cwd:
            data["cwd"] = self.cwd
        with open(path, "w") as f:
            import yaml

            yaml.dump(data, f, default_flow_style=False, allow_unicode=True)

    def _render_policy_block(self) -> str:
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
        from importlib.resources import as_file, files

        packaged = files("lionagi.casts").joinpath("packs", "default.yaml")
        with as_file(packaged) as p:
            return Pack.from_file(p)
    return None
