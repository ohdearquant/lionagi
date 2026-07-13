# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for the plugin-agent-profile consumer wiring in lionagi.cli._providers.

An active (trusted + enabled + version-compatible) plugin's declared agent
profiles join the search only after a project/global miss, namespaced as
``<plugin>/<name>``. A bare name resolves only when unambiguous. A user's own
local file always wins over a same-named plugin profile.
"""

from __future__ import annotations

import pytest

from lionagi.cli._providers import list_agents, load_agent_profile
from lionagi.plugins.discovery import discover_plugins
from lionagi.plugins.trust import trust_plugin

MANIFEST = """\
name: {name}
version: "0.1.0"
lionagi: ">=0.0,<100.0"

capabilities:
  agents: [agents/{agent}.md]
"""


def _trust(dir_name: str) -> None:
    d = next(x for x in discover_plugins() if x.dir_name == dir_name)
    trust_plugin(d)


def _write_plugin_profile(write_plugin, dir_name: str, name: str, agent: str, body: str):
    write_plugin(
        dir_name,
        MANIFEST.format(name=name, agent=agent),
        files={f"agents/{agent}.md": body},
    )
    _trust(dir_name)


def test_untrusted_plugin_profile_is_not_available(write_plugin):
    write_plugin(
        "wr",
        MANIFEST.format(name="web-research", agent="researcher"),
        files={"agents/researcher.md": "---\nmodel: x\n---\n\nbody\n"},
    )
    # never trusted

    assert "web-research/researcher" not in list_agents()
    with pytest.raises(FileNotFoundError):
        load_agent_profile("web-research/researcher")


def test_active_plugin_profile_resolves_by_namespaced_token(write_plugin):
    _write_plugin_profile(
        write_plugin,
        "wr",
        "web-research",
        "researcher",
        "---\nmodel: codex/gpt-5.5\n---\n\nresearch body\n",
    )

    assert "web-research/researcher" in list_agents()
    profile = load_agent_profile("web-research/researcher")
    assert profile.model == "codex/gpt-5.5"
    assert "research body" in profile.system_prompt


def test_unambiguous_bare_name_resolves_to_plugin(write_plugin):
    _write_plugin_profile(
        write_plugin, "wr", "web-research", "researcher", "---\nmodel: codex/gpt-5.5\n---\n\nbody\n"
    )

    profile = load_agent_profile("researcher")
    assert profile.model == "codex/gpt-5.5"


def test_ambiguous_bare_name_across_two_plugins_does_not_resolve(write_plugin):
    _write_plugin_profile(
        write_plugin, "wr1", "plugin-one", "researcher", "---\nmodel: a\n---\n\nbody\n"
    )
    _write_plugin_profile(
        write_plugin, "wr2", "plugin-two", "researcher", "---\nmodel: b\n---\n\nbody\n"
    )

    with pytest.raises(FileNotFoundError):
        load_agent_profile("researcher")
    # Both remain reachable via their explicit namespaced token.
    assert load_agent_profile("plugin-one/researcher").model == "a"
    assert load_agent_profile("plugin-two/researcher").model == "b"


def test_local_project_profile_shadows_plugin_bare_name(write_plugin, plugin_home):
    _write_plugin_profile(
        write_plugin, "wr", "web-research", "researcher", "---\nmodel: from-plugin\n---\n\nbody\n"
    )
    agents_dir = plugin_home / ".lionagi" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "researcher.md").write_text("---\nmodel: from-local\n---\n\nlocal body\n")

    profile = load_agent_profile("researcher")
    assert profile.model == "from-local"
    # The plugin's version stays reachable via its namespaced token.
    assert load_agent_profile("web-research/researcher").model == "from-plugin"


def test_local_shadow_logs_a_warning(write_plugin, plugin_home, caplog):
    _write_plugin_profile(
        write_plugin, "wr", "web-research", "researcher", "---\nmodel: from-plugin\n---\n\nbody\n"
    )
    agents_dir = plugin_home / ".lionagi" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "researcher.md").write_text("---\nmodel: from-local\n---\n\nlocal body\n")

    with caplog.at_level("WARNING", logger="lionagi.cli.warn"):
        load_agent_profile("researcher")

    assert any("web-research" in rec.message for rec in caplog.records)


def test_disabled_plugin_profile_is_unreachable(write_plugin):
    from lionagi.plugins._user_settings import read_user_settings, write_user_settings
    from lionagi.plugins.registry import PluginRegistry

    _write_plugin_profile(
        write_plugin, "wr", "web-research", "researcher", "---\nmodel: codex/gpt-5.5\n---\n\nbody\n"
    )
    settings = read_user_settings()
    settings.setdefault("plugins", {})["web-research"] = {"enabled": False}
    write_user_settings(settings)
    PluginRegistry.reset()

    assert "web-research/researcher" not in list_agents()
    with pytest.raises(FileNotFoundError):
        load_agent_profile("web-research/researcher")
