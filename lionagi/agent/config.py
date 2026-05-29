# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class AgentConfig:
    """Configuration for a lionagi agent.

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

    def pre(self, tool_name: str, handler: Callable) -> AgentConfig:
        self.hook_handlers.setdefault(f"pre:{tool_name}", []).append(handler)
        return self

    def post(self, tool_name: str, handler: Callable) -> AgentConfig:
        self.hook_handlers.setdefault(f"post:{tool_name}", []).append(handler)
        return self

    def on_error(self, tool_name: str, handler: Callable) -> AgentConfig:
        self.hook_handlers.setdefault(f"error:{tool_name}", []).append(handler)
        return self

    def build_system_message(self) -> str:
        """Compose the system prompt from role + modes + literal system_prompt.

        ``role`` (a built-in role name or Role) contributes its behavioral body;
        each entry in ``modes`` (a built-in mode name or Mode) contributes its
        behaviors; ``system_prompt`` is appended as extra preamble. With no role
        or modes set, this returns ``system_prompt`` unchanged (backward
        compatible).
        """
        from lionagi.casts.pattern import Mode, Role

        parts: list[str] = []
        if self.role:
            role = self.role if not isinstance(self.role, str) else Role.load(self.role)
            if role.body:
                parts.append(role.body)
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
        **kwargs: Any,
    ) -> AgentConfig:
        """Preset for a coding agent with CodingToolkit."""
        return cls(
            name=name,
            model=model,
            effort=effort,
            tools=["coding"],
            system_prompt=system_prompt or _CODING_SYSTEM_PROMPT,
            cwd=cwd,
            **kwargs,
        )

    @classmethod
    def from_yaml(cls, path: str | Path) -> AgentConfig:
        """Load agent config from a YAML file.

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
        """Save config to YAML (without hook callables — those are code-only)."""
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
You are a coding agent with tools for reading, editing, and searching code, \
running shell commands, managing your conversation context, and delegating \
tasks to sub-agents.

## Tools available
- **reader**: Read files (with line numbers) or list directories. Always read a file before editing it.
- **editor**: Write new files or edit existing ones via exact string replacement.
- **bash**: Run shell commands (builds, tests, git, etc.).
- **search**: Search code with grep (regex) or find files by name.
- **context**: Check your context usage and evict old tool outputs when running low.
- **subagent**: Delegate a scoped task to a sub-agent with its own context.

## Workflow
1. Understand the task. Ask clarifying questions if needed.
2. Search/read relevant code to build understanding.
3. Plan your changes before editing.
4. Make targeted edits — prefer edit (string replacement) over full file writes.
5. Verify changes: run tests, check builds, review diffs.
6. If context gets large, use context to evict old search/bash results.

## Efficiency
- You have up to 20 tool-use rounds, but stop as soon as the task is done. \
Don't use all 20 rounds just because they're available.
- Batch related reads together when possible.
- If you need more rounds to finish, say so in your final answer — the user \
can continue the conversation.

## Rules
- Always read a file before editing it (the editor enforces this).
- Prefer small, targeted edits over full file rewrites.
- Run tests after making changes.
- Don't make changes beyond what's asked.
- If unsure, read more code before acting.
"""
