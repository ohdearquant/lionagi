# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Permission adapter for the claude_code provider.

Other providers (codex, openai) are follow-up adapters — out of scope for v1.

NOTE (MIN-1 — advisory): The ``safe`` preset carries ``escalate={"bash": ["*"]}``
semantics, meaning bash *could* be approved interactively.  This adapter has no
escalation channel so bash → deny in practice.  The RolePolicy block injected into
the system message mentions escalation; enforcement cannot honour it.  This is
documented as a known partial-parity limitation (ADR-0073 §5.2) and is advisory only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lionagi.agent.permissions import PermissionPolicy

__all__ = ("translate_permissions",)

# Lionagi zone → real claude CLI tool names (PascalCase, verified against
# providers/anthropic/claude_code/models.py and
# tests/service/connections/providers/test_cli_cancellation.py).
# Each zone maps to ONE OR MORE real tool names — the adapter emits lists.
#
# Canonical claude tool vocabulary: Bash, Read, Edit, Write, Glob, Grep,
# WebSearch, WebFetch, Task, NotebookEdit; MCP tools are mcp__<server>__<tool>.
#
# NOTE (MIN-2 — known/follow-up): For the API/native-tool path, lionagi
# registers tools via ActionManager (register_tools) using zone names.  The
# _TOOL_MAP below is only relevant to the claude_code CLI adapter; it has no
# observable effect on the native-tool path.
_TOOL_MAP: dict[str, list[str]] = {
    "editor": ["Edit", "Write", "NotebookEdit"],
    "bash": ["Bash"],
    "reader": ["Read"],
    "search": ["Grep", "Glob", "WebSearch", "WebFetch"],
    "context": ["mcp__*"],
}

# Flat list of ALL real tool names covered by the map (used by deny_all).
_ALL_CC_TOOLS: list[str] = [t for tools in _TOOL_MAP.values() for t in tools]


def translate_permissions(policy: PermissionPolicy) -> dict:
    """Translate a PermissionPolicy to claude_code endpoint kwargs.

    Maps:
      allow_all  → {"permission_mode": "bypassPermissions"}
      read_only  → deny editor + bash; allow reader + search + context
      safe       → deny bash; allow editor + reader + search + context
      deny_all   → deny ALL real claude tool names (fail-closed)
      rules      → maps allow/deny zone patterns to claude_code allow/deny lists

    Tool names emitted here are the PascalCase names the claude CLI recognises
    (e.g. "Bash", "Edit", "Write", "Read", "Glob", "Grep", "WebSearch",
    "WebFetch", "NotebookEdit", "mcp__*").  Using lowercase or invented names
    causes the CLI to silently ignore the restriction (fail-open).
    """
    if policy.mode == "allow_all":
        return {"permission_mode": "bypassPermissions"}

    if policy.mode == "deny_all":
        # Deny every real claude tool — fail-closed.
        return {
            "permission_mode": "default",
            "disallowed_tools": list(_ALL_CC_TOOLS),
        }

    # rules mode — used by read_only and safe presets as well as custom policies
    allowed_tools: list[str] = []
    denied_tools: list[str] = []

    # Build allow list: zones that have explicit allow rules with a wildcard
    for lion_name, cc_names in _TOOL_MAP.items():
        patterns = policy.allow.get(lion_name, [])
        if "*" in patterns or any(p.strip() == "*" for p in patterns):
            allowed_tools.extend(cc_names)

    # Build deny list: zones that have explicit deny rules
    for lion_name, cc_names in _TOOL_MAP.items():
        if lion_name in policy.deny:
            denied_tools.extend(cc_names)

    result: dict = {"permission_mode": "default"}
    if allowed_tools:
        result["allowed_tools"] = allowed_tools
    if denied_tools:
        result["disallowed_tools"] = denied_tools
    return result
