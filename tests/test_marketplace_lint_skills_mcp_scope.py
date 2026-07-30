"""Pins the marketplace/scripts/lint_skills.py rule that rejects a bare, unscoped
`mcp__<server>__` reference inside a marketplace skill.

A plugin-provided MCP server's tools are namespaced
`mcp__plugin_<plugin-name>_<server-name>__<tool>`, never the bare `mcp__<server>__`
form. A skill that writes the bare form names a tool that does not exist for a user
who installed the bundle as a plugin, even though the file reads correctly and
lints clean otherwise. This test breaks the subject on purpose (a bare reference)
and restores it (the correctly scoped reference), because a clean run cannot tell
a rule that passed from a rule that never looked.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent
_SCRIPTS_DIR = str(_REPO_ROOT / "marketplace" / "scripts")


def _scan_file():
    if _SCRIPTS_DIR not in sys.path:
        sys.path.insert(0, _SCRIPTS_DIR)
    from lint_skills import scan_file

    return scan_file


def test_bare_unscoped_mcp_tool_name_is_flagged(tmp_path: Path) -> None:
    scan_file = _scan_file()
    subject = tmp_path / "skill.md"
    subject.write_text("Call `mcp__lion__request` with ops.\n")
    findings = scan_file(subject)
    assert len(findings) == 1
    assert "unscoped MCP tool name" in findings[0]


def test_plugin_scoped_mcp_tool_name_is_not_flagged(tmp_path: Path) -> None:
    """The restore side of the same probe: the correctly scoped form must pass."""
    scan_file = _scan_file()
    subject = tmp_path / "skill.md"
    subject.write_text("Call `mcp__plugin_orchestrate_lion__request` with ops.\n")
    findings = scan_file(subject)
    assert findings == []


def test_other_plugin_scoped_servers_are_not_flagged(tmp_path: Path) -> None:
    """The rule must not fire on tools belonging to some other installed plugin."""
    scan_file = _scan_file()
    subject = tmp_path / "skill.md"
    subject.write_text(
        "Fetch docs via `mcp__plugin_context7_context7__query-docs`.\n"
        "Browse with `mcp__plugin_playwright_playwright__browser_click`.\n"
    )
    findings = scan_file(subject)
    assert findings == []


def test_bare_khive_reference_is_also_flagged(tmp_path: Path) -> None:
    """The rule is general — any bare mcp__<server>__ is wrong in a shipped skill,
    not only mcp__lion__. khive does not ship with this bundle at all (D-h)."""
    scan_file = _scan_file()
    subject = tmp_path / "skill.md"
    subject.write_text("Recall prior art with `mcp__khive__request`.\n")
    findings = scan_file(subject)
    assert any("unscoped MCP tool name" in f for f in findings)


def test_shipped_marketplace_skills_have_no_bare_mcp_tool_name() -> None:
    """The rule holds against the bundle actually being shipped."""
    scan_file = _scan_file()
    marketplace_root = _REPO_ROOT / "marketplace"
    md_files = sorted(marketplace_root.rglob("*.md"))
    assert md_files, "no .md files found under marketplace/ — the check below would be vacuous"
    findings = [f for path in md_files for f in scan_file(path) if "unscoped MCP" in f]
    assert findings == []
