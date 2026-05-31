# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Translate a PermissionPolicy into claude_code endpoint kwargs.

The claude CLI tool names follow PascalCase; permission zones (lowercase) map
to one or more of them via ``_TOOL_MAP``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from lionagi.agent.permissions import PermissionPolicy

__all__ = ("translate_permissions",)

# Zone -> list[claude CLI tool names] (PascalCase as expected by claude_code)
_TOOL_MAP: dict[str, list[str]] = {
    "editor": ["Edit", "MultiEdit", "Write", "NotebookEdit"],
    "bash": ["Bash"],
    "reader": ["Read"],
    "search": ["Grep", "Glob", "WebSearch", "WebFetch"],
    "spawn": ["Task"],
    "context": ["mcp__*"],
}

# Flat list of all known tool names (no wildcards) for deny_all
_ALL_TOOLS: list[str] = [
    tool for zone_tools in _TOOL_MAP.values() for tool in zone_tools if not tool.endswith("*")
]


def translate_permissions(policy: PermissionPolicy) -> dict[str, Any]:
    """Translate *policy* into kwargs suitable for a claude_code endpoint.

    Returns a dict that can be passed as ``**kwargs`` to the endpoint config.
    The returned keys are only those relevant to permission control:
    - ``bypassPermissions`` (bool)
    - ``allowedTools`` (list[str])
    - ``disallowedTools`` (list[str])

    Parameters
    ----------
    policy
        The PermissionPolicy to translate.

    Returns
    -------
    dict[str, Any]
        Endpoint kwargs encoding the permission constraints.
    """
    mode = getattr(policy, "mode", "allow_all")

    if mode == "allow_all":
        return {"bypassPermissions": True}

    if mode == "deny_all":
        return {"disallowedTools": list(_ALL_TOOLS)}

    # read_only and safe are both rules-based — derive from allow/deny patterns
    if mode in ("read_only", "safe", "rules"):
        return _translate_rules(policy)

    # Unknown mode — fall back to allow_all (permissive, warn caller via comment)
    return {"bypassPermissions": True}


def _translate_rules(policy: PermissionPolicy) -> dict[str, Any]:
    """Translate a rules-mode policy to allowed/disallowed tool lists."""
    allowed: list[str] = []
    disallowed: list[str] = []

    # Build allowed tools from policy.allow zones
    allow_rules: dict[str, list[str]] = getattr(policy, "allow", {})
    for zone, patterns in allow_rules.items():
        if zone == "*":
            # Wildcard zone — allow everything
            return {"bypassPermissions": True}
        zone_tools = _TOOL_MAP.get(zone, [])
        if patterns and "*" in patterns:
            allowed.extend(zone_tools)
        elif patterns:
            # Pattern-specific: include tools for the zone that match
            allowed.extend(zone_tools)

    # Build disallowed tools from policy.deny zones
    deny_rules: dict[str, list[str]] = getattr(policy, "deny", {})
    for zone, patterns in deny_rules.items():
        if zone == "*":
            disallowed.extend(_ALL_TOOLS)
            break
        zone_tools = _TOOL_MAP.get(zone, [])
        if patterns and "*" in patterns:
            disallowed.extend(zone_tools)
        elif patterns:
            disallowed.extend(zone_tools)

    # Remove duplicates while preserving order
    seen: set[str] = set()
    unique_allowed: list[str] = []
    for t in allowed:
        if t not in seen:
            seen.add(t)
            unique_allowed.append(t)

    seen = set()
    unique_disallowed: list[str] = []
    for t in disallowed:
        if t not in seen:
            seen.add(t)
            unique_disallowed.append(t)

    result: dict[str, Any] = {}
    if unique_allowed:
        result["allowedTools"] = unique_allowed
    if unique_disallowed:
        result["disallowedTools"] = unique_disallowed
    return result
