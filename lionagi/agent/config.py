# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import warnings
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = ("AgentConfig", "HooksMixin", "_wire_secure_guards")


class HooksMixin:
    """Shared hook-registration helpers for agent config/spec dataclasses."""

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

    Shared by AgentConfig.coding() and AgentSpec.coding() so the guard logic
    lives in exactly one place.  ``obj`` must support .pre() (HooksMixin).
    """
    from lionagi.agent.hooks import guard_destructive, guard_paths

    obj.pre("bash", guard_destructive)
    workspace_root = str(Path(cwd) if cwd else Path.cwd())
    path_guard = guard_paths(allowed_paths=[workspace_root])
    obj.pre("reader", path_guard)
    obj.pre("editor", path_guard)


@dataclass
class AgentConfig(HooksMixin):
    """Configuration for a lionagi agent.

    Deprecated: compose an AgentSpec instead.  Retained as a bridge —
    create_agent() converts via AgentSpec.from_legacy().

    Combines model settings, tool selection, hook registration, system prompt,
    and permissions into a single config that create_agent() wires into a Branch.

    Usage::

        # From preset
        config = AgentConfig.coding(model="openai/gpt-4.1")

        # From scratch
        config = AgentConfig(
            name="my-agent",
            model="anthropic/claude-sonnet-4-6",
            tools=["coding"],
            system_prompt="You are a helpful coding assistant.",
        )

        # Add hooks
        config.pre("bash", my_guard_hook)
        config.post("editor", my_format_hook)

        # Create agent
        branch = await create_agent(config)
    """

    name: str = "agent"
    model: str | None = None
    effort: str | None = None
    system_prompt: str = ""
    role: str | None = None
    modes: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    hook_handlers: dict[str, list[Callable]] = field(default_factory=dict)
    permissions: dict[str, Any] = field(default_factory=dict)
    mcp_servers: list[str] | None = None
    mcp_config_path: str | None = None
    max_extensions: int = 20
    yolo: bool = False
    lion_system: bool = True
    cwd: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def build_system_message(self) -> str:
        """Compose the system prompt from role + modes + literal system_prompt.

        ``role`` (a built-in role name or Role) contributes its behavioral body;
        each entry in ``modes`` (a built-in mode name or Mode) contributes its
        behaviors; ``system_prompt`` is appended as extra preamble.  With no
        role or modes set this returns ``system_prompt`` unchanged (backward
        compatible).

        Role + mode composition is delegated to ``Profile`` so the logic lives
        in one place.  When ``role`` is None the mode list is iterated directly
        (Profile requires a role; that case is handled inline).
        """
        from lionagi.casts.pattern import Mode
        from lionagi.casts.profile import Profile

        parts: list[str] = []
        if self.role:
            body = Profile.compose(self.role, modes=self.modes).build_system_message()
            if body:
                parts.append(body)
        else:
            for m in self.modes:
                mode = m if not isinstance(m, str) else Mode.load(m)
                if mode.behaviors:
                    parts.append(mode.behaviors)
        if self.system_prompt:
            parts.append(self.system_prompt)
        return "\n\n".join(parts)

    @classmethod
    def coding(
        cls,
        name: str = "coder",
        model: str | None = None,
        effort: str | None = "high",
        system_prompt: str | None = None,
        cwd: str | None = None,
        secure: bool = True,
        **kwargs: Any,
    ) -> AgentConfig:
        """Preset for a coding agent with CodingToolkit.

        By default (``secure=True``), wires two guards:

        - ``guard_destructive`` as a pre-hook on ``bash`` — blocks destructive
          shell commands (rm -rf, force-push, etc.).
        - ``guard_paths`` as a pre-hook on ``reader`` and ``editor`` — restricts
          file access to the workspace root (``cwd`` if provided, else
          ``Path.cwd()`` at call time).

        Set ``secure=False`` to disable these defaults and manage hooks manually.
        """
        config = cls(
            name=name,
            model=model,
            effort=effort,
            tools=["coding"],
            system_prompt=system_prompt or _CODING_SYSTEM_PROMPT,
            cwd=cwd,
            **kwargs,
        )
        if secure:
            _wire_secure_guards(config, cwd)
        return config

    @classmethod
    def from_yaml(cls, path: str | Path) -> AgentConfig:
        """Load agent config from a YAML file.

        .. deprecated::
            Use ``AgentSpec.from_yaml()`` instead.  AgentConfig YAML round-trip
            is retained for back-compat only.

        YAML format::

            name: coder
            model: openai/gpt-4.1
            effort: high
            tools: [coding]
            system_prompt: |
              You are a coding agent...
            permissions:
              bash.allow: ["git *", "cargo *", "uv *"]
              bash.deny: ["rm -rf *"]
        """
        warnings.warn(
            "AgentConfig.from_yaml() is deprecated. Use AgentSpec.from_yaml() instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        import yaml

        p = Path(path)
        with open(p) as f:
            data = yaml.safe_load(f) or {}

        return cls(
            name=data.get("name", p.stem),
            model=data.get("model"),
            effort=data.get("effort"),
            system_prompt=data.get("system_prompt", ""),
            tools=data.get("tools", []),
            permissions=data.get("permissions", {}),
            yolo=data.get("yolo", False),
            lion_system=data.get("lion_system", True),
            cwd=data.get("cwd"),
            extra={
                k: v
                for k, v in data.items()
                if k
                not in {
                    "name",
                    "model",
                    "effort",
                    "system_prompt",
                    "tools",
                    "permissions",
                    "yolo",
                    "lion_system",
                    "cwd",
                }
            },
        )

    def to_yaml(self, path: str | Path) -> None:
        """Save config to YAML (without hook callables — those are code-only).

        .. deprecated::
            Use ``AgentSpec.to_yaml()`` instead.  AgentConfig YAML round-trip
            is retained for back-compat only.
        """
        warnings.warn(
            "AgentConfig.to_yaml() is deprecated. Use AgentSpec.to_yaml() instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        import yaml

        data = {
            "name": self.name,
            "model": self.model,
            "effort": self.effort,
            "system_prompt": self.system_prompt,
            "tools": self.tools,
            "permissions": self.permissions,
            "yolo": self.yolo,
            "lion_system": self.lion_system,
        }
        if self.cwd:
            data["cwd"] = self.cwd
        if self.extra:
            data.update(self.extra)
        with open(path, "w") as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True)


_CODING_SYSTEM_PROMPT = """\
You are a coding agent operating in a real codebase. You have tools to read and
edit files, search code, and run shell commands:

- **reader**: read files (with line numbers) or list directories.
- **editor**: create files or edit them via exact string replacement.
- **bash**: run shell commands (builds, tests, git, ...).
- **search**: grep code by regex, or find files by name.

Read a file before you edit it (the editor enforces this), and verify your
changes when you can. Beyond that, use your judgment to accomplish the task.
"""
