# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from dataclasses import dataclass, field
from textwrap import shorten
from typing import Any


@dataclass
class CLISession:
    """Provider-agnostic accumulator for a CLI agent session (chunks, tool views, summary stats)."""

    session_id: str | None = None
    model: str | None = None

    # chronological log (StreamChunk instances)
    chunks: list = field(default_factory=list)

    # materialised views
    thinking_log: list[str] = field(default_factory=list)
    messages: list[dict[str, Any]] = field(default_factory=list)
    tool_uses: list[dict[str, Any]] = field(default_factory=list)
    tool_results: list[dict[str, Any]] = field(default_factory=list)

    # final summary
    result: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    total_cost_usd: float | None = None
    num_turns: int | None = None
    duration_ms: int | None = None
    duration_api_ms: int | None = None
    is_error: bool = False
    summary: dict | None = None

    def populate_summary(self) -> None:
        self.summary = _extract_summary(self)


def _extract_summary(session: CLISession) -> dict[str, Any]:
    tool_counts: dict[str, int] = {}
    tool_details: list[dict[str, Any]] = []
    file_operations: dict[str, list[str]] = {"reads": [], "writes": [], "edits": []}
    key_actions: list[str] = []

    for tool_use in session.tool_uses:
        tool_name = tool_use.get("name", "unknown")
        tool_input = tool_use.get("input", {})
        tool_id = tool_use.get("id", "")

        tool_counts[tool_name] = tool_counts.get(tool_name, 0) + 1
        tool_details.append({"tool": tool_name, "id": tool_id, "input": tool_input})

        if tool_name in ("Read", "read", "read_file"):
            file_path = tool_input.get("file_path", tool_input.get("path", "unknown"))
            file_operations["reads"].append(file_path)
            key_actions.append(f"Read {file_path}")

        elif tool_name in ("Write", "write", "write_file", "create_file"):
            file_path = tool_input.get("file_path", tool_input.get("path", "unknown"))
            file_operations["writes"].append(file_path)
            key_actions.append(f"Wrote {file_path}")

        elif tool_name in ("Edit", "edit", "edit_file", "patch", "MultiEdit"):
            file_path = tool_input.get("file_path", tool_input.get("path", "unknown"))
            file_operations["edits"].append(file_path)
            key_actions.append(f"Edited {file_path}")

        elif tool_name in ("Bash", "bash", "shell", "terminal", "run_shell_command"):
            command = tool_input.get("command", tool_input.get("cmd", ""))
            key_actions.append(f"Ran: {shorten(command, 53)}")

        elif tool_name in ("Glob", "glob"):
            key_actions.append(f"Searched files: {tool_input.get('pattern', '')}")

        elif tool_name in ("Grep", "grep"):
            key_actions.append(f"Searched content: {tool_input.get('pattern', '')}")

        elif tool_name in ("Task", "task", "Agent"):
            key_actions.append(f"Spawned agent: {tool_input.get('description', '')}")

        elif tool_name == "TodoWrite":
            key_actions.append(f"Created {len(tool_input.get('todos', []))} todos")

        elif tool_name.startswith("mcp__") or tool_name.startswith("mcp_"):
            key_actions.append(f"MCP {tool_name.replace('mcp__', '').replace('mcp_', '')}")

        else:
            key_actions.append(f"Used {tool_name}")

    key_actions = list(dict.fromkeys(key_actions)) if key_actions else ["No specific actions"]

    for op_type in file_operations:
        file_operations[op_type] = list(dict.fromkeys(file_operations[op_type]))

    result_summary = (session.result[:200] + "...") if len(session.result) > 200 else session.result

    return {
        "tool_counts": tool_counts,
        "tool_details": tool_details,
        "file_operations": file_operations,
        "key_actions": key_actions,
        "total_tool_calls": sum(tool_counts.values()),
        "result_summary": result_summary,
        "usage_stats": {
            "total_cost_usd": session.total_cost_usd,
            "num_turns": session.num_turns,
            "duration_ms": session.duration_ms,
            "duration_api_ms": session.duration_api_ms,
            **session.usage,
        },
    }
