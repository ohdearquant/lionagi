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
# Parse-site evidence (models.py:619-657):
#   editor zone: Edit, MultiEdit (multi-file edit — privilege-escalation risk)
#   spawn  zone: Task            (spawns sub-agents — privilege-escalation risk)
#   bash   zone: Bash
#   reader zone: Read
#   search zone: Grep, Glob, WebSearch, WebFetch
#   context zone: mcp__* (MCP server tools)
#   write  zone: Write, NotebookEdit
#
# NOTE (MIN-2 — known/follow-up): For the API/native-tool path, lionagi
# registers tools via ActionManager (register_tools) using zone names.  The
# _TOOL_MAP below is only relevant to the claude_code CLI adapter; it has no
# observable effect on the native-tool path.
_TOOL_MAP: dict[str, list[str]] = {
    "editor": ["Edit", "MultiEdit", "Write", "NotebookEdit"],
    "bash": ["Bash"],
    "reader": ["Read"],
    "search": ["Grep", "Glob", "WebSearch", "WebFetch"],
    # spawn: sub-agent spawning — intentionally separate so it can be denied
    # independently of other tool zones (e.g. safe preset denies bash+spawn).
    "spawn": ["Task"],
    "context": ["mcp__*"],
}

# Flat list of ALL real tool names covered by the map — used as the
# deny_all denylist.  deny_all is fail-closed BY ENUMERATION: it names every
# tool this adapter knows about.  The guarantee is "all tools listed here are
# blocked"; it cannot guarantee tools added to the claude CLI after this code
# was written are also blocked (that would require an empty allowlist, but
# the arg-builder in models.py:511 drops empty lists as falsy, so an empty
# allowed_tools is silently omitted and would provide no additional coverage).
# Conclusion: keep _TOOL_MAP current as new claude tool names are discovered.
_ALL_CC_TOOLS: list[str] = [t for tools in _TOOL_MAP.values() for t in tools]


def translate_permissions(policy: PermissionPolicy) -> dict:
    """Translate a PermissionPolicy to claude_code endpoint kwargs.

    Maps:
      allow_all  → {"permission_mode": "bypassPermissions"}
      read_only  → deny editor + bash; allow reader + search + context
      safe       → deny bash; allow editor + reader + search + context
      deny_all   → deny all tools enumerated in _TOOL_MAP (fail-closed for known vocabulary)
      rules      → maps allow/deny zone patterns to claude_code allow/deny lists

    Tool names emitted here are the PascalCase names the claude CLI recognises
    (e.g. "Bash", "Edit", "Write", "Read", "Glob", "Grep", "WebSearch",
    "WebFetch", "NotebookEdit", "mcp__*").  Using lowercase or invented names
    causes the CLI to silently ignore the restriction (fail-open).
    """
    if policy.mode == "allow_all":
        return {"permission_mode": "bypassPermissions"}

    if policy.mode == "deny_all":
        # Deny every tool known to this adapter (see _ALL_CC_TOOLS / _TOOL_MAP).
        # This is fail-closed for the enumerated vocabulary.  Note: an empty
        # allowed_tools list cannot be used as an additional safety net because
        # _build_declarative_args (models.py:511) drops empty lists as falsy —
        # it would emit no --allowedTools flag at all, giving zero extra coverage.
        # The correct response to a newly discovered claude tool not yet in
        # _TOOL_MAP is to add it here; do not rely on a "deny-by-default" catch-all.
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
