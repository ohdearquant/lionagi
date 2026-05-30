# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Permission adapter for the claude_code provider.

Other providers (codex, openai) are follow-up adapters — out of scope for v1.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lionagi.agent.permissions import PermissionPolicy

__all__ = ("translate_permissions",)

# Tool name → claude_code permission tool type string
_TOOL_MAP: dict[str, str] = {
    "editor": "edit",
    "bash": "bash",
    "reader": "read",
    "search": "search",
    "context": "mcp",
}


def translate_permissions(policy: PermissionPolicy) -> dict:
    """Translate a PermissionPolicy to claude_code endpoint kwargs.

    Maps:
      allow_all  → {"permission_mode": "bypassPermissions"}
      read_only  → deny-list covering editor + bash tools
      deny_all   → {"permission_mode": "default", "disallowed_tools": [all tools]}
      rules      → maps allow/deny fnmatch patterns to claude_code allow/deny lists
    """
    if policy.mode == "allow_all":
        return {"permission_mode": "bypassPermissions"}

    if policy.mode == "deny_all":
        # Deny every known tool class
        return {
            "permission_mode": "default",
            "disallowed_tools": list(_TOOL_MAP.values()),
        }

    # rules mode — used by read_only and safe presets as well as custom policies
    allowed_tools: list[str] = []
    denied_tools: list[str] = []

    # Build allow list: tool names that have explicit allow rules with a wildcard
    for lion_name, cc_name in _TOOL_MAP.items():
        patterns = policy.allow.get(lion_name, [])
        if "*" in patterns or any(p.strip() == "*" for p in patterns):
            allowed_tools.append(cc_name)

    # Build deny list: tool names that have explicit deny rules
    for lion_name, cc_name in _TOOL_MAP.items():
        if lion_name in policy.deny:
            denied_tools.append(cc_name)

    result: dict = {"permission_mode": "default"}
    if allowed_tools:
        result["allowed_tools"] = allowed_tools
    if denied_tools:
        result["disallowed_tools"] = denied_tools
    return result
