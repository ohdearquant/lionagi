# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"Plugin profile lookup and catalog contracts."

from __future__ import annotations

import pytest

from lionhub.orchestration.spawn import build_agent_profile_catalog, list_agents, load_agent_profile
from tests._cli_logging_test_support import _restore_cli_logging as _restore_cli_logging
from tests._spawn_plugins_test_support import MANIFEST as MANIFEST
from tests._spawn_plugins_test_support import _trust as _trust
from tests._spawn_plugins_test_support import _write_plugin_profile as _write_plugin_profile


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


def test_agent_profile_catalog_indexes_resolved_configuration(write_plugin):
    _write_plugin_profile(
        write_plugin,
        "wr",
        "web-research",
        "researcher",
        """\
---
model: codex/gpt-5.5
effort: high
role: researcher
pack: research-pack
---

research body
""",
    )

    catalog = build_agent_profile_catalog()

    assert catalog["web-research/researcher"] == {
        "bypass": False,
        "effort": "high",
        "fast_mode": False,
        "khive_injection": None,
        "lion_system": True,
        "model": "codex/gpt-5.5",
        "pack": "research-pack",
        "resume_on_timeout": False,
        "role": "researcher",
        "timeout": None,
        "yolo": False,
    }


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
