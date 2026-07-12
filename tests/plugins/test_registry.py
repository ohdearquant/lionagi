# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for PluginRegistry: lifecycle state, collisions, enable/disable, stage-2 activation."""

from __future__ import annotations

import pytest

from lionagi.plugins._user_settings import read_user_settings, write_user_settings
from lionagi.plugins.discovery import discover_plugins
from lionagi.plugins.registry import PluginActivationError, PluginRegistry, PluginState
from lionagi.plugins.trust import trust_plugin

TOOL_MANIFEST = """\
name: {name}
version: "0.1.0"
lionagi: "{spec}"

capabilities:
  tools:
    - name: {tool_name}
      target: tools/t.py:t
  agents: [agents/{agent_name}.md]
"""


def _write_tool_plugin(
    write_plugin,
    dir_name: str,
    *,
    name: str | None = None,
    spec: str = ">=0.0,<100.0",
    tool_name: str = "tool1",
    agent_name: str = "a",
    tool_body: str = "def t():\n    return 1\n",
):
    return write_plugin(
        dir_name,
        TOOL_MANIFEST.format(
            name=name or dir_name, spec=spec, tool_name=tool_name, agent_name=agent_name
        ),
        files={"tools/t.py": tool_body, f"agents/{agent_name}.md": "x\n"},
    )


def _trust_by_dir_name(dir_name: str) -> None:
    d = next(x for x in discover_plugins() if x.dir_name == dir_name)
    trust_plugin(d)


class TestLifecycleStates:
    def test_untrusted_plugin_is_untrusted(self, write_plugin):
        _write_tool_plugin(write_plugin, "p1")

        record = PluginRegistry.get("p1")
        assert record is not None
        assert record.state is PluginState.UNTRUSTED

    def test_trusted_enabled_compatible_is_active(self, write_plugin):
        _write_tool_plugin(write_plugin, "p1")
        _trust_by_dir_name("p1")

        PluginRegistry.reset()
        record = PluginRegistry.get("p1")
        assert record.state is PluginState.ACTIVE

    def test_incompatible_version_range(self, write_plugin):
        _write_tool_plugin(write_plugin, "p1", spec=">=999.0")
        _trust_by_dir_name("p1")

        PluginRegistry.reset()
        record = PluginRegistry.get("p1")
        assert record.state is PluginState.INCOMPATIBLE

    def test_invalid_manifest_still_listed(self, write_plugin):
        write_plugin("broken", "name: 123\nversion: '1.0'\nlionagi: '>=0.0'\n")

        records = PluginRegistry.list_plugins()
        assert any(r.state is PluginState.INVALID for r in records)

    def test_disable_flips_state_without_touching_bundle(self, write_plugin):
        bundle = _write_tool_plugin(write_plugin, "p1")
        _trust_by_dir_name("p1")
        manifest_before = (bundle / "plugin.yaml").read_text()

        settings = read_user_settings()
        settings.setdefault("plugins", {})["p1"] = {"enabled": False}
        write_user_settings(settings)

        PluginRegistry.reset()
        record = PluginRegistry.get("p1")
        assert record.state is PluginState.DISABLED
        assert (bundle / "plugin.yaml").read_text() == manifest_before


class TestCollisions:
    def test_duplicate_plugin_name_across_dirs_is_collision(self, write_plugin):
        _write_tool_plugin(
            write_plugin, "dir-one", name="same-name", tool_name="tool1", agent_name="a1"
        )
        _write_tool_plugin(
            write_plugin, "dir-two", name="same-name", tool_name="tool2", agent_name="a2"
        )

        records = [r for r in PluginRegistry.list_plugins() if r.name == "same-name"]
        assert len(records) == 2
        assert all(r.state is PluginState.COLLISION for r in records)
        assert all("same-name" in (r.error or "") for r in records)

    def test_two_active_plugins_same_tool_name_is_collision(self, write_plugin):
        # Distinct agent names so only the tool-name surface collides.
        _write_tool_plugin(write_plugin, "p1", tool_name="shared_tool", agent_name="a1")
        _write_tool_plugin(write_plugin, "p2", tool_name="shared_tool", agent_name="a2")
        _trust_by_dir_name("p1")
        _trust_by_dir_name("p2")

        PluginRegistry.reset()
        r1 = PluginRegistry.get("p1")
        r2 = PluginRegistry.get("p2")
        assert r1.state is PluginState.COLLISION
        assert r2.state is PluginState.COLLISION
        assert "shared_tool" in r1.error
        assert "tools" in r1.error

    def test_two_active_plugins_same_agent_name_is_not_a_collision(self, write_plugin):
        """Agent profiles are namespaced (<plugin>/<name>) — same local name across two
        plugins is not a hard error, only the bare name becomes ambiguous (resolver's job)."""
        _write_tool_plugin(write_plugin, "p1", tool_name="tool1", agent_name="researcher")
        _write_tool_plugin(write_plugin, "p2", tool_name="tool2", agent_name="researcher")
        _trust_by_dir_name("p1")
        _trust_by_dir_name("p2")

        PluginRegistry.reset()
        r1 = PluginRegistry.get("p1")
        r2 = PluginRegistry.get("p2")
        assert r1.state is PluginState.ACTIVE
        assert r2.state is PluginState.ACTIVE
        # Both remain independently reachable via their namespaced token.
        files = PluginRegistry.active_agent_profile_files()
        assert "p1/researcher" in files
        assert "p2/researcher" in files

    def test_disabling_one_resolves_the_collision(self, write_plugin):
        _write_tool_plugin(write_plugin, "p1", tool_name="shared_tool", agent_name="a1")
        _write_tool_plugin(write_plugin, "p2", tool_name="shared_tool", agent_name="a2")
        _trust_by_dir_name("p1")
        _trust_by_dir_name("p2")

        settings = read_user_settings()
        settings.setdefault("plugins", {})["p2"] = {"enabled": False}
        write_user_settings(settings)

        PluginRegistry.reset()
        r1 = PluginRegistry.get("p1")
        r2 = PluginRegistry.get("p2")
        assert r1.state is PluginState.ACTIVE
        assert r2.state is PluginState.DISABLED

    def test_single_active_plugin_never_collides_with_itself(self, write_plugin):
        _write_tool_plugin(write_plugin, "p1")
        _trust_by_dir_name("p1")

        PluginRegistry.reset()
        assert PluginRegistry.get("p1").state is PluginState.ACTIVE


class TestActivateTarget:
    def test_activates_lazily_and_caches(self, write_plugin):
        _write_tool_plugin(write_plugin, "p1", tool_body="def t():\n    return 42\n")
        _trust_by_dir_name("p1")
        PluginRegistry.reset()

        fn = PluginRegistry.activate_target("p1", "tools/t.py:t")
        assert fn() == 42
        # Cached: same object on second call.
        assert PluginRegistry.activate_target("p1", "tools/t.py:t") is fn

    def test_missing_target_file_raises_named_diagnostic(self, write_plugin):
        _write_tool_plugin(write_plugin, "p1")
        _trust_by_dir_name("p1")
        PluginRegistry.reset()

        with pytest.raises(PluginActivationError) as excinfo:
            PluginRegistry.activate_target("p1", "tools/missing.py:nope")
        assert "p1" in str(excinfo.value)

    def test_raising_module_is_reported_once_and_cached(self, write_plugin):
        _write_tool_plugin(write_plugin, "p1", tool_body="raise RuntimeError('boom')\n")
        _trust_by_dir_name("p1")
        PluginRegistry.reset()

        with pytest.raises(PluginActivationError):
            PluginRegistry.activate_target("p1", "tools/t.py:t")
        # Second call hits the cached failure, not a fresh import (which would
        # raise a bare RuntimeError instead of PluginActivationError if the
        # cache weren't consulted first).
        with pytest.raises(PluginActivationError):
            PluginRegistry.activate_target("p1", "tools/t.py:t")

    def test_untrusted_plugin_cannot_activate(self, write_plugin):
        _write_tool_plugin(write_plugin, "p1")
        # never trusted
        with pytest.raises(PluginActivationError):
            PluginRegistry.activate_target("p1", "tools/t.py:t")
