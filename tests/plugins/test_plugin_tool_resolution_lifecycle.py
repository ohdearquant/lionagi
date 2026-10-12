# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Characterizes the lifecycle of a plugin-declared tool resolved by
``ActionManager``: each resolution builds a fresh ``Tool`` wrapper, and each
resolution revalidates trust against freshly scanned bundle content.

Pins today's behavior at ``ActionManager._resolve_plugin_tool`` and
``PluginRegistry.resolve_tool_target``/``activate_target``: the wrapper is new
on every call (so a hook attached to a resolved ``Tool`` does not survive to
the next call), while the activated callable behind it may be the cached
stage-2 result; a content or trust change after a successful resolution
refuses the very next call without any ``PluginRegistry.reset()``.
"""

from __future__ import annotations

import sys

import pytest

from lionagi.plugins._user_settings import read_user_settings, write_user_settings
from lionagi.plugins.discovery import discover_plugins
from lionagi.plugins.registry import PluginActivationError, PluginRegistry
from lionagi.plugins.trust import trust_plugin
from lionagi.protocols.action.manager import ActionManager

MANIFEST = """\
name: {name}
version: "0.1.0"
lionagi: ">=0.0,<100.0"

capabilities:
  tools:
    - name: {tool_name}
      target: tools/impl.py:{func_name}
"""


def _trust(dir_name: str) -> None:
    plugin = next(x for x in discover_plugins() if x.dir_name == dir_name)
    trust_plugin(plugin)


@pytest.fixture(autouse=True)
def _drop_executed_bundle_modules():
    """Stage-2 activation execs a plugin module into ``sys.modules`` and leaves it
    there; drop any the test created so they do not outlive it."""
    before = set(sys.modules)
    yield
    for name in set(sys.modules) - before:
        if name.startswith("_lionagi_plugin_"):
            sys.modules.pop(name, None)


def _write_tool_plugin(write_plugin, dir_name: str, *, name: str, tool_name: str = "greet"):
    return write_plugin(
        dir_name,
        MANIFEST.format(name=name, tool_name=tool_name, func_name="do_greet"),
        files={"tools/impl.py": "def do_greet():\n    return 'hi from plugin'\n"},
    )


class TestPluginToolIsBuiltFreshPerCall:
    def test_each_resolution_returns_a_distinct_tool_wrapper(self, write_plugin):
        _write_tool_plugin(write_plugin, "p1", name="greeter")
        _trust("p1")
        PluginRegistry.reset()

        manager = ActionManager()
        first = manager.match_tool({"function": "greet", "arguments": {}})
        second = manager.match_tool({"function": "greet", "arguments": {}})

        assert first.func_tool is not second.func_tool

    def test_the_activated_callable_is_the_cached_stage_two_result(self, write_plugin):
        """The freshness is of the ``Tool`` wrapper, not of the callable: the
        stage-2 activation cache returns the same object across resolutions."""
        _write_tool_plugin(write_plugin, "p1", name="greeter")
        _trust("p1")
        PluginRegistry.reset()

        manager = ActionManager()
        first = manager.match_tool({"function": "greet", "arguments": {}})
        second = manager.match_tool({"function": "greet", "arguments": {}})

        assert first.func_tool.func_callable is second.func_tool.func_callable

    async def test_a_preprocessor_attached_to_a_resolved_tool_never_runs(self, write_plugin):
        """A hook attached to a plugin tool at resolution time does not see the
        next call, because that call resolves a new ``Tool``."""
        _write_tool_plugin(write_plugin, "p1", name="greeter")
        _trust("p1")
        PluginRegistry.reset()

        manager = ActionManager()
        resolved = manager.match_tool({"function": "greet", "arguments": {}})

        seen: list[dict] = []

        def spy(args):
            seen.append(args)
            return args

        resolved.func_tool.preprocessor = spy

        function_calling = await manager.invoke({"function": "greet", "arguments": {}})

        assert seen == []
        assert function_calling.execution.response == "hi from plugin"

    def test_stage_two_activation_runs_once_per_resolution(self, write_plugin, monkeypatch):
        """No memoization between calls: each resolution re-enters
        ``PluginRegistry.activate_target`` rather than reusing a cached Tool."""
        import lionagi.plugins.registry as registry_mod

        _write_tool_plugin(write_plugin, "p1", name="greeter")
        _trust("p1")
        PluginRegistry.reset()

        calls: list[str] = []
        original = registry_mod.PluginRegistry.activate_target.__func__

        def counting_activate_target(cls, plugin_name, target):
            calls.append(plugin_name)
            return original(cls, plugin_name, target)

        monkeypatch.setattr(
            registry_mod.PluginRegistry,
            "activate_target",
            classmethod(counting_activate_target),
        )

        manager = ActionManager()
        manager.match_tool({"function": "greet", "arguments": {}})
        manager.match_tool({"function": "greet", "arguments": {}})

        assert calls == ["greeter", "greeter"]

    def test_resolution_never_writes_into_the_static_registry(self, write_plugin):
        _write_tool_plugin(write_plugin, "p1", name="greeter")
        _trust("p1")
        PluginRegistry.reset()

        manager = ActionManager()
        manager.match_tool({"function": "greet", "arguments": {}})

        assert "greet" not in manager.registry


class TestPluginActivationRevalidatesEachCall:
    def test_content_change_after_a_resolution_refuses_the_next_call(self, write_plugin):
        bundle = _write_tool_plugin(write_plugin, "p1", name="greeter")
        _trust("p1")
        PluginRegistry.reset()

        manager = ActionManager()
        assert (
            manager.match_tool({"function": "greet", "arguments": {}}).func_tool.function == "greet"
        )

        (bundle / "tools" / "impl.py").write_text("def do_greet():\n    return 'tampered'\n")

        with pytest.raises(ValueError, match="Function greet is not registered"):
            manager.match_tool({"function": "greet", "arguments": {}})

    def test_manifest_change_after_a_resolution_refuses_the_next_call(self, write_plugin):
        bundle = _write_tool_plugin(write_plugin, "p1", name="greeter")
        _trust("p1")
        PluginRegistry.reset()

        manager = ActionManager()
        assert (
            manager.match_tool({"function": "greet", "arguments": {}}).func_tool.function == "greet"
        )

        (bundle / "plugin.yaml").write_text(
            MANIFEST.format(name="greeter", tool_name="greet", func_name="do_greet")
            + "description: changed after trust\n"
        )

        with pytest.raises(ValueError, match="Function greet is not registered"):
            manager.match_tool({"function": "greet", "arguments": {}})

    def test_trust_record_removal_after_a_resolution_refuses_the_next_call(self, write_plugin):
        _write_tool_plugin(write_plugin, "p1", name="greeter")
        _trust("p1")
        PluginRegistry.reset()

        manager = ActionManager()
        assert (
            manager.match_tool({"function": "greet", "arguments": {}}).func_tool.function == "greet"
        )

        settings = read_user_settings()
        settings["trusted_plugins"].pop("greeter")
        write_user_settings(settings)

        with pytest.raises(ValueError, match="Function greet is not registered"):
            manager.match_tool({"function": "greet", "arguments": {}})

    def test_activate_target_rechecks_trust_on_content_change(self, write_plugin):
        bundle = _write_tool_plugin(write_plugin, "p1", name="greeter")
        _trust("p1")
        PluginRegistry.reset()

        target = PluginRegistry.resolve_tool_target("greet")
        assert target is not None
        assert (
            PluginRegistry.activate_target(target.plugin_name, target.target)() == "hi from plugin"
        )

        (bundle / "tools" / "impl.py").write_text("def do_greet():\n    return 'tampered'\n")

        with pytest.raises(PluginActivationError, match="no longer trusted"):
            PluginRegistry.activate_target(target.plugin_name, target.target)

    def test_activate_target_refuses_after_the_trust_record_is_removed(self, write_plugin):
        _write_tool_plugin(write_plugin, "p1", name="greeter")
        _trust("p1")
        PluginRegistry.reset()

        target = PluginRegistry.resolve_tool_target("greet")
        assert target is not None
        PluginRegistry.activate_target(target.plugin_name, target.target)

        settings = read_user_settings()
        settings["trusted_plugins"].pop("greeter")
        write_user_settings(settings)

        with pytest.raises(PluginActivationError, match="no longer trusted"):
            PluginRegistry.activate_target(target.plugin_name, target.target)
