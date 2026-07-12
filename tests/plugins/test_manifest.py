# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for the plugin manifest schema: declarative, pure-data, strict validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from lionagi.plugins.manifest import ManifestError, PluginManifest, parse_manifest

VALID_MANIFEST = """\
name: web-research
version: "0.3.0"
description: Web research toolkit
lionagi: ">=0.20,<1.0"

capabilities:
  tools:
    - name: web_search
      target: tools/search.py:web_search
  hooks_external:
    PreToolUse:
      - matcher: "web_search"
        hooks:
          - type: command
            command: ["hooks/rate_guard"]
  agents: [agents/researcher.md]
  playbooks: [playbooks/deep-research.playbook.yaml]
  providers:
    - module: providers/searchapi.py
  packs: [packs/research.yaml]
"""


def test_valid_manifest_parses(tmp_path: Path):
    path = tmp_path / "plugin.yaml"
    path.write_text(VALID_MANIFEST)

    manifest = parse_manifest(path)

    assert manifest.name == "web-research"
    assert manifest.version == "0.3.0"
    assert manifest.lionagi == ">=0.20,<1.0"
    assert [t.name for t in manifest.capabilities.tools] == ["web_search"]
    assert manifest.capabilities.tools[0].target == "tools/search.py:web_search"
    assert manifest.capabilities.agents == ["agents/researcher.md"]
    assert manifest.capabilities.playbooks == ["playbooks/deep-research.playbook.yaml"]
    assert manifest.capabilities.providers[0].module == "providers/searchapi.py"
    assert manifest.capabilities.packs == ["packs/research.yaml"]
    hooks = manifest.capabilities.hooks_external["PreToolUse"]
    assert hooks[0].matcher == "web_search"
    assert hooks[0].hooks[0].command == ["hooks/rate_guard"]


def test_manifest_parsing_imports_nothing(tmp_path: Path):
    """Parsing must never import the target/module strings it declares."""
    path = tmp_path / "plugin.yaml"
    path.write_text(VALID_MANIFEST)

    # The declared target/module files don't even exist on disk; if parsing
    # tried to import them it would raise here.
    manifest = parse_manifest(path)
    assert manifest.capabilities.tools[0].target == "tools/search.py:web_search"


def test_unknown_top_level_key_is_load_time_error(tmp_path: Path):
    path = tmp_path / "plugin.yaml"
    path.write_text("name: x\nversion: '1.0'\nlionagi: '>=0.0'\nplaybook: playbooks/oops.yaml\n")

    with pytest.raises(ManifestError) as excinfo:
        parse_manifest(path)
    assert str(path) in str(excinfo.value)


def test_unknown_capability_key_is_load_time_error(tmp_path: Path):
    path = tmp_path / "plugin.yaml"
    path.write_text("name: x\nversion: '1.0'\nlionagi: '>=0.0'\ncapabilities:\n  tols: []\n")

    with pytest.raises(ManifestError):
        parse_manifest(path)


def test_vendor_prefixed_keys_are_ignored(tmp_path: Path):
    path = tmp_path / "plugin.yaml"
    path.write_text("name: x\nversion: '1.0'\nlionagi: '>=0.0'\nx-provenance: some vendor note\n")

    manifest = parse_manifest(path)
    assert manifest.name == "x"


@pytest.mark.parametrize(
    "bad_name",
    ["Web-Research", "web_research", "-web", "a" * 33, "", "web research"],
)
def test_invalid_plugin_name_rejected(tmp_path: Path, bad_name: str):
    path = tmp_path / "plugin.yaml"
    path.write_text(f"name: {bad_name!r}\nversion: '1.0'\nlionagi: '>=0.0'\n")

    with pytest.raises(ManifestError):
        parse_manifest(path)


def test_malformed_version_specifier_rejected(tmp_path: Path):
    path = tmp_path / "plugin.yaml"
    path.write_text("name: x\nversion: '1.0'\nlionagi: 'not a specifier!!'\n")

    with pytest.raises(ManifestError):
        parse_manifest(path)


def test_is_compatible_true_within_range():
    manifest = PluginManifest(name="x", version="1.0", lionagi=">=0.20,<1.0")
    assert manifest.is_compatible("0.28.0") is True


def test_is_compatible_false_outside_range():
    manifest = PluginManifest(name="x", version="1.0", lionagi=">=0.20,<0.25")
    assert manifest.is_compatible("0.28.0") is False


def test_missing_manifest_file_raises_manifest_error(tmp_path: Path):
    with pytest.raises(ManifestError):
        parse_manifest(tmp_path / "does-not-exist.yaml")


def test_non_mapping_manifest_raises(tmp_path: Path):
    path = tmp_path / "plugin.yaml"
    path.write_text("- just\n- a\n- list\n")

    with pytest.raises(ManifestError):
        parse_manifest(path)


def test_invalid_yaml_raises_manifest_error(tmp_path: Path):
    path = tmp_path / "plugin.yaml"
    path.write_text("name: [unterminated\n")

    with pytest.raises(ManifestError):
        parse_manifest(path)
