# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for plugin trust: content-pinned hashing, user-level (never project-level) records."""

from __future__ import annotations

from pathlib import Path

from lionagi.plugins.discovery import discover_plugins
from lionagi.plugins.trust import (
    TrustState,
    build_trust_disclosure,
    read_trusted_plugins,
    trust_plugin,
    trust_state,
)

MANIFEST = """\
name: web-research
version: "0.1.0"
lionagi: ">=0.0,<100.0"

capabilities:
  tools:
    - name: t1
      target: tools/t.py:t
  agents: [agents/a.md]
"""


def _discover_one(write_plugin):
    write_plugin(
        "web-research",
        MANIFEST,
        files={"tools/t.py": "def t():\n    return 1\n", "agents/a.md": "x\n"},
    )
    return discover_plugins()[0]


def test_untrusted_by_default(write_plugin):
    d = _discover_one(write_plugin)
    assert trust_state(d) is TrustState.UNTRUSTED


def test_trust_then_trusted(write_plugin):
    d = _discover_one(write_plugin)
    trust_plugin(d)
    assert trust_state(d) is TrustState.TRUSTED


def test_trust_writes_user_level_settings_only(write_plugin, plugin_home: Path):
    d = _discover_one(write_plugin)
    trust_plugin(d)

    user_settings = plugin_home / ".lionagi" / "settings.yaml"
    assert user_settings.is_file()
    trusted = read_trusted_plugins()
    assert "web-research" in trusted
    assert "manifest" in trusted["web-research"]
    assert trusted["web-research"]["targets"]["tools/t.py"]
    assert trusted["web-research"]["targets"]["agents/a.md"]


def test_editing_declared_file_reverts_to_changed(write_plugin):
    d = _discover_one(write_plugin)
    trust_plugin(d)
    assert trust_state(d) is TrustState.TRUSTED

    (d.bundle_dir / "tools" / "t.py").write_text("def t():\n    return 2\n")
    # Re-discover: declared_files/manifest are unchanged but file content differs.
    d2 = discover_plugins()[0]
    assert trust_state(d2) is TrustState.CHANGED


def test_editing_agent_profile_reverts_to_changed(write_plugin):
    """Prompt-bearing files are pinned too, not just executables (D5's stated rationale)."""
    d = _discover_one(write_plugin)
    trust_plugin(d)

    (d.bundle_dir / "agents" / "a.md").write_text("attacker-controlled instructions\n")
    d2 = discover_plugins()[0]
    assert trust_state(d2) is TrustState.CHANGED


def test_editing_manifest_reverts_to_changed(write_plugin):
    d = _discover_one(write_plugin)
    trust_plugin(d)

    (d.manifest_path).write_text(MANIFEST + "description: added later\n")
    d2 = discover_plugins()[0]
    assert trust_state(d2) is TrustState.CHANGED


def test_disclosure_shows_full_argv_and_targets(write_plugin):
    write_plugin(
        "hooked",
        """\
name: hooked
version: "0.1.0"
lionagi: ">=0.0,<100.0"

capabilities:
  hooks_external:
    PreToolUse:
      - matcher: "web_search"
        hooks:
          - type: command
            command: ["hooks/rate_guard", "--strict", "--limit=5"]
""",
        files={"hooks/rate_guard": "#!/bin/sh\necho ok\n"},
    )
    d = discover_plugins()[0]

    disclosure = build_trust_disclosure(d)

    assert disclosure["hooks_external"][0]["argv"] == ["hooks/rate_guard", "--strict", "--limit=5"]


def test_hash_is_stable_across_yaml_formatting_changes(write_plugin):
    """The canonical-JSON manifest hash should be stable to whitespace/comment-only edits."""
    d = _discover_one(write_plugin)
    trust_plugin(d)

    (d.manifest_path).write_text("# just a comment\n\n" + MANIFEST)
    d2 = discover_plugins()[0]
    assert trust_state(d2) is TrustState.TRUSTED
