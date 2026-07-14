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

from lionagi.cli._providers import build_agent_profile_catalog, list_agents, load_agent_profile
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


def test_resolve_worker_spec_resolves_plugin_namespaced_role(write_plugin):
    """A `<plugin>/<name>` role assigned by the orchestrator's planner must resolve to the
    plugin's declared profile, not silently fall back to a bare model spec.
    """
    from lionagi.cli.orchestrate._orchestration import resolve_worker_spec

    _write_plugin_profile(
        write_plugin,
        "wr",
        "web-research",
        "researcher",
        "---\nmodel: codex/gpt-5.5\n---\n\nresearch body\n",
    )

    model, profile = resolve_worker_spec("web-research/researcher")

    assert model == "codex/gpt-5.5"
    assert profile is not None
    assert profile.name == "web-research/researcher"


def test_resolve_worker_spec_falls_back_to_raw_model_spec_for_unknown_slash_token(write_plugin):
    """A literal `provider/model` token (no matching plugin profile) still resolves as a raw
    model spec, unaffected by the plugin-namespaced-role fix.
    """
    from lionagi.cli.orchestrate._orchestration import resolve_worker_spec

    model, profile = resolve_worker_spec("openai/gpt-4.1")

    assert model == "openai/gpt-4.1"
    assert profile is None


def test_resolve_worker_spec_falls_back_to_raw_model_spec_for_nonexistent_bare_token(write_plugin):
    """A `<namespace>/<name>` token with no dots (a valid profile-name shape) that matches no
    real plugin or local profile misses via `FileNotFoundError`, not `ValueError` — a distinct
    fallback branch from the dotted-model-version case below and must be exercised separately.
    """
    from lionagi.cli.orchestrate._orchestration import resolve_worker_spec

    model, profile = resolve_worker_spec("unknown/role")

    assert model == "unknown/role"
    assert profile is None


def test_resolve_worker_spec_falls_back_to_raw_model_spec_for_codex_token(write_plugin):
    """`codex/gpt-5.5` (a dotted model version, matching no plugin profile) is a real-world
    raw-spec token used by the orchestrator's own worker defaults and must resolve as such.
    """
    from lionagi.cli.orchestrate._orchestration import resolve_worker_spec

    model, profile = resolve_worker_spec("codex/gpt-5.5")

    assert model == "codex/gpt-5.5"
    assert profile is None


def test_resolve_worker_spec_falls_back_for_dotted_model_version(write_plugin):
    """A dotted model version (invalid as a bare profile-name component) must still fall back
    to a raw model spec instead of raising.
    """
    from lionagi.cli.orchestrate._orchestration import resolve_worker_spec

    _write_plugin_profile(
        write_plugin,
        "wr",
        "web-research",
        "researcher",
        "---\nmodel: codex/gpt-5.5\n---\n\nresearch body\n",
    )

    model, profile = resolve_worker_spec("anthropic/claude-3.7")

    assert model == "anthropic/claude-3.7"
    assert profile is None


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
