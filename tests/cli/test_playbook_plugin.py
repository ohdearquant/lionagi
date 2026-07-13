# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for the plugin-playbook consumer wiring in lionagi.cli.orchestrate.

An active (trusted + enabled + version-compatible) plugin's declared
playbooks join the search only after a project/global miss, namespaced as
``<plugin>/<name>``. A bare name resolves only when unambiguous. A user's own
local file always wins over a same-named plugin playbook.
"""

from __future__ import annotations

from lionagi.cli.orchestrate import _resolve_playbook_path, list_playbooks
from lionagi.plugins.discovery import discover_plugins
from lionagi.plugins.trust import trust_plugin

MANIFEST = """\
name: {name}
version: "0.1.0"
lionagi: ">=0.0,<100.0"

capabilities:
  playbooks: [playbooks/{playbook}.playbook.yaml]
"""


def _trust(dir_name: str) -> None:
    d = next(x for x in discover_plugins() if x.dir_name == dir_name)
    trust_plugin(d)


def _write_plugin_playbook(write_plugin, dir_name: str, name: str, playbook: str, body: str):
    write_plugin(
        dir_name,
        MANIFEST.format(name=name, playbook=playbook),
        files={f"playbooks/{playbook}.playbook.yaml": body},
    )
    _trust(dir_name)


def test_untrusted_plugin_playbook_is_not_available(write_plugin):
    write_plugin(
        "wr",
        MANIFEST.format(name="web-research", playbook="deep-research"),
        files={"playbooks/deep-research.playbook.yaml": "prompt: research it\n"},
    )
    # never trusted

    assert "web-research/deep-research" not in list_playbooks()
    path, err = _resolve_playbook_path("web-research/deep-research")
    assert path is None
    assert "web-research" in err


def test_active_plugin_playbook_resolves_by_namespaced_token(write_plugin):
    _write_plugin_playbook(
        write_plugin, "wr", "web-research", "deep-research", "prompt: research it\n"
    )

    assert "web-research/deep-research" in list_playbooks()
    path, err = _resolve_playbook_path("web-research/deep-research")
    assert err is None
    assert path.read_text() == "prompt: research it\n"


def test_unambiguous_bare_name_resolves_to_plugin(write_plugin):
    _write_plugin_playbook(
        write_plugin, "wr", "web-research", "deep-research", "prompt: research it\n"
    )

    path, err = _resolve_playbook_path("deep-research")
    assert err is None
    assert path.read_text() == "prompt: research it\n"


def test_ambiguous_bare_name_across_two_plugins_does_not_resolve(write_plugin):
    _write_plugin_playbook(write_plugin, "wr1", "plugin-one", "research", "prompt: a\n")
    _write_plugin_playbook(write_plugin, "wr2", "plugin-two", "research", "prompt: b\n")

    path, err = _resolve_playbook_path("research")
    assert path is None
    assert "not found" in err

    # Both remain reachable via their explicit namespaced token.
    p1, err1 = _resolve_playbook_path("plugin-one/research")
    assert err1 is None
    assert p1.read_text() == "prompt: a\n"
    p2, err2 = _resolve_playbook_path("plugin-two/research")
    assert err2 is None
    assert p2.read_text() == "prompt: b\n"


def test_local_project_playbook_shadows_plugin_bare_name(write_plugin, plugin_home):
    _write_plugin_playbook(
        write_plugin, "wr", "web-research", "deep-research", "prompt: from-plugin\n"
    )
    playbooks_dir = plugin_home / ".lionagi" / "playbooks"
    playbooks_dir.mkdir(parents=True)
    (playbooks_dir / "deep-research.playbook.yaml").write_text("prompt: from-local\n")

    path, err = _resolve_playbook_path("deep-research")
    assert err is None
    assert path.read_text() == "prompt: from-local\n"

    # The plugin's version stays reachable via its namespaced token.
    plugin_path, plugin_err = _resolve_playbook_path("web-research/deep-research")
    assert plugin_err is None
    assert plugin_path.read_text() == "prompt: from-plugin\n"


def test_local_shadow_logs_a_warning(write_plugin, plugin_home, caplog):
    _write_plugin_playbook(
        write_plugin, "wr", "web-research", "deep-research", "prompt: from-plugin\n"
    )
    playbooks_dir = plugin_home / ".lionagi" / "playbooks"
    playbooks_dir.mkdir(parents=True)
    (playbooks_dir / "deep-research.playbook.yaml").write_text("prompt: from-local\n")

    with caplog.at_level("WARNING", logger="lionagi.cli.warn"):
        _resolve_playbook_path("deep-research")

    assert any("web-research" in rec.message for rec in caplog.records)


def test_disabled_plugin_playbook_is_unreachable(write_plugin):
    from lionagi.plugins._user_settings import read_user_settings, write_user_settings
    from lionagi.plugins.registry import PluginRegistry

    _write_plugin_playbook(
        write_plugin, "wr", "web-research", "deep-research", "prompt: research it\n"
    )
    settings = read_user_settings()
    settings.setdefault("plugins", {})["web-research"] = {"enabled": False}
    write_user_settings(settings)
    PluginRegistry.reset()

    assert "web-research/deep-research" not in list_playbooks()
    path, err = _resolve_playbook_path("web-research/deep-research")
    assert path is None
    assert "web-research" in err


def test_untrusted_plugin_token_gives_named_loud_error(write_plugin):
    """An absent/untrusted plugin in a `<plugin>/<name>` token must surface a
    named error (matching the registry's own rejection wording), not a bare
    'not found' with no indication of *why*."""
    path, err = _resolve_playbook_path("nonexistent-plugin/some-playbook")
    assert path is None
    assert "nonexistent-plugin" in err
    assert "not active" in err
