# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for plugin discovery: scanning bundles, tolerating broken ones, path-traversal checks."""

from __future__ import annotations

from pathlib import Path

from lionagi.plugins.discovery import discover_plugins

VALID_MANIFEST = """\
name: {name}
version: "0.1.0"
lionagi: ">=0.0,<100.0"

capabilities:
  tools:
    - name: t1
      target: tools/t.py:t
  agents: [agents/a.md]
"""


def test_bundle_dir_without_manifest_is_ignored(plugin_home: Path):
    (plugin_home / ".lionagi" / "plugins" / "wip").mkdir(parents=True)

    discovered = discover_plugins()

    assert discovered == []


def test_valid_bundle_discovered(write_plugin):
    write_plugin(
        "web-research",
        VALID_MANIFEST.format(name="web-research"),
        files={"tools/t.py": "def t():\n    return 1\n", "agents/a.md": "x\n"},
    )

    discovered = discover_plugins()

    assert len(discovered) == 1
    d = discovered[0]
    assert d.manifest is not None
    assert d.manifest.name == "web-research"
    assert d.error is None
    assert "tools/t.py" in d.declared_files
    assert "agents/a.md" in d.declared_files


def test_malformed_manifest_excluded_not_fatal(write_plugin):
    write_plugin("broken", "name: 123\nversion: '1.0'\nlionagi: '>=0.0'\n")
    write_plugin(
        "good",
        VALID_MANIFEST.format(name="good"),
        files={"tools/t.py": "def t():\n    return 1\n", "agents/a.md": "x\n"},
    )

    discovered = discover_plugins()

    by_dir = {d.dir_name: d for d in discovered}
    assert by_dir["broken"].manifest is None
    assert by_dir["broken"].error is not None
    assert by_dir["good"].manifest is not None
    assert by_dir["good"].error is None


def test_traversal_in_declared_capability_path_is_excluded(write_plugin):
    write_plugin(
        "escape-artist",
        """\
name: escape-artist
version: "0.1.0"
lionagi: ">=0.0,<100.0"

capabilities:
  agents: ["../../../../etc/passwd"]
""",
    )

    discovered = discover_plugins()

    assert len(discovered) == 1
    assert discovered[0].manifest is None
    assert "traversal" in discovered[0].error.lower()


def test_absolute_declared_path_is_excluded(write_plugin):
    write_plugin(
        "absolute-path",
        """\
name: absolute-path
version: "0.1.0"
lionagi: ">=0.0,<100.0"

capabilities:
  packs: ["/etc/passwd"]
""",
    )

    discovered = discover_plugins()

    assert len(discovered) == 1
    assert discovered[0].manifest is None
    assert "absolute" in discovered[0].error.lower()


def test_colon_in_declared_capability_path_is_excluded(write_plugin):
    """A bundle-relative filename has no legitimate reason to contain ':' — it's reserved
    as the tool-target/callable separator. Refused for every capability kind, not just
    tool targets, so a colon-bearing filename can never even be declared."""
    write_plugin(
        "colon-name",
        """\
name: colon-name
version: "0.1.0"
lionagi: ">=0.0,<100.0"

capabilities:
  agents: ["agents/a:b.md"]
""",
        files={"agents/a:b.md": "x\n"},
    )

    discovered = discover_plugins()

    assert len(discovered) == 1
    assert discovered[0].manifest is None
    assert "must not contain ':'" in discovered[0].error


def test_symlink_escape_is_excluded(write_plugin, plugin_home: Path):
    bundle = write_plugin(
        "symlink-escape",
        """\
name: symlink-escape
version: "0.1.0"
lionagi: ">=0.0,<100.0"

capabilities:
  packs: ["escape.yaml"]
""",
    )
    outside = plugin_home / "outside.yaml"
    outside.write_text("secret: data\n")
    (bundle / "escape.yaml").symlink_to(outside)

    discovered = discover_plugins()

    d = discovered[0]
    # The symlink itself resolves outside the bundle -> excluded.
    assert d.manifest is None
    assert "outside" in d.error.lower() or "escape" in d.error.lower()
