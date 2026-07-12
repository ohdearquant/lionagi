# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for `li plugin` (list/info/trust/enable/disable)."""

from __future__ import annotations

from pathlib import Path

import pytest

from lionagi.cli.main import main as cli_main
from lionagi.plugins._user_settings import read_user_settings
from lionagi.plugins.registry import PluginRegistry

pytestmark = pytest.mark.usefixtures("plugin_home")

MANIFEST = """\
name: web-research
version: "0.3.0"
description: Web research toolkit
lionagi: ">=0.0,<100.0"

capabilities:
  tools:
    - name: web_search
      target: tools/search.py:web_search
  agents: [agents/researcher.md]
"""


@pytest.fixture
def web_research_bundle(write_plugin):
    return write_plugin(
        "web-research",
        MANIFEST,
        files={
            "tools/search.py": "def web_search(q):\n    return q\n",
            "agents/researcher.md": "---\nmodel: codex/gpt-5.5\n---\n\nresearch body\n",
        },
    )


def test_list_shows_untrusted_plugin(capsys, web_research_bundle):
    code = cli_main(["plugin", "list"])
    assert code == 0
    out = capsys.readouterr().out
    assert "web-research" in out
    assert "untrusted" in out


def test_list_empty(capsys, plugin_home: Path):
    code = cli_main(["plugin", "list"])
    assert code == 0
    out = capsys.readouterr().out
    assert "no plugins found" in out


def test_info_shows_manifest_contents(capsys, web_research_bundle):
    code = cli_main(["plugin", "info", "web-research"])
    assert code == 0
    out = capsys.readouterr().out
    assert "web_search" in out
    assert "tools/search.py:web_search" in out
    assert "researcher.md" in out


def test_info_unknown_plugin_errors(capsys, plugin_home: Path):
    code = cli_main(["plugin", "info", "nonexistent"])
    assert code == 1


def test_trust_with_yes_records_and_activates(capsys, web_research_bundle):
    code = cli_main(["plugin", "trust", "web-research", "--yes"])
    assert code == 0
    out = capsys.readouterr().out
    assert "web_search" in out  # full disclosure shown before recording
    assert "trusted" in out

    trusted = read_trusted_plugins_helper()
    assert "web-research" in trusted

    PluginRegistry.reset()
    record = PluginRegistry.get("web-research")
    assert record.state.value == "active"


def test_trust_prompt_declined(monkeypatch, capsys, web_research_bundle):
    monkeypatch.setattr("builtins.input", lambda _: "n")
    code = cli_main(["plugin", "trust", "web-research"])
    assert code == 1
    out = capsys.readouterr().out
    assert "not trusted" in out


def test_enable_disable_round_trip(capsys, web_research_bundle):
    code = cli_main(["plugin", "trust", "web-research", "--yes"])
    assert code == 0
    capsys.readouterr()

    code = cli_main(["plugin", "disable", "web-research"])
    assert code == 0
    PluginRegistry.reset()
    assert PluginRegistry.get("web-research").state.value == "disabled"

    code = cli_main(["plugin", "enable", "web-research"])
    assert code == 0
    PluginRegistry.reset()
    assert PluginRegistry.get("web-research").state.value == "active"


def test_disable_does_not_mutate_bundle(web_research_bundle):
    manifest_before = (web_research_bundle / "plugin.yaml").read_text()
    code = cli_main(["plugin", "disable", "web-research"])
    assert code == 0
    assert (web_research_bundle / "plugin.yaml").read_text() == manifest_before


def read_trusted_plugins_helper() -> dict:
    settings = read_user_settings()
    return settings.get("trusted_plugins", {})
