# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for the plugin-tool consumer wiring in ActionManager (ADR-0088 D3).

On a tool-name-resolution miss, `ActionManager.match_tool` consults the
plugin registry for a matching declared tool before raising "not
registered" — only trusted, enabled, still-TRUSTED, ACTIVE plugins are
consulted, activation goes through `PluginRegistry.activate_target` (never a
direct plugin-module import), and two enabled plugins declaring the same
bare tool name is a hard error (ADR-0088 D6) surfaced as
`PluginToolCollisionError`.
"""

from __future__ import annotations

import pytest

from lionagi.plugins._user_settings import read_user_settings, write_user_settings
from lionagi.plugins.discovery import discover_plugins
from lionagi.plugins.registry import (
    PluginActivationError,
    PluginRegistry,
    PluginToolCollisionError,
)
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
    d = next(x for x in discover_plugins() if x.dir_name == dir_name)
    trust_plugin(d)


def _write_tool_plugin(
    write_plugin,
    dir_name: str,
    *,
    name: str | None = None,
    tool_name: str = "greet",
    func_name: str = "do_greet",
    return_value: str = "hi from plugin",
):
    # The plugin's own Python function name (func_name) is deliberately kept
    # different from the manifest's declared tool `name` — the consumer must
    # advertise the requested name, not whatever the plugin author called
    # the underlying callable.
    body = f"def {func_name}():\n    return {return_value!r}\n"
    return write_plugin(
        dir_name,
        MANIFEST.format(name=name or dir_name, tool_name=tool_name, func_name=func_name),
        files={"tools/impl.py": body},
    )


class TestToolConsumerHit:
    def test_active_plugin_tool_resolves_on_miss(self, write_plugin):
        _write_tool_plugin(write_plugin, "p1", name="greeter", tool_name="greet")
        _trust("p1")
        PluginRegistry.reset()

        manager = ActionManager()
        result = manager.match_tool({"function": "greet", "arguments": {}})

        # Advertised name matches what was requested ("greet"), not the
        # plugin's own python function name ("do_greet").
        assert result.func_tool.function == "greet"

    @pytest.mark.asyncio
    async def test_resolved_plugin_tool_actually_invokes(self, write_plugin):
        _write_tool_plugin(write_plugin, "p1", name="greeter", tool_name="greet")
        _trust("p1")
        PluginRegistry.reset()

        manager = ActionManager()
        function_calling = await manager.invoke({"function": "greet", "arguments": {}})
        assert function_calling.execution.response == "hi from plugin"

    def test_plugin_tool_is_not_added_to_the_static_registry(self, write_plugin):
        """Resolution stays live: a match doesn't get memoized into
        `self.registry`, so a later disable/edit is picked up immediately
        (see the exclusion tests below)."""
        _write_tool_plugin(write_plugin, "p1", name="greeter", tool_name="greet")
        _trust("p1")
        PluginRegistry.reset()

        manager = ActionManager()
        manager.match_tool({"function": "greet", "arguments": {}})
        assert "greet" not in manager.registry


class TestToolConsumerMiss:
    def test_no_plugin_declares_it_same_error_as_today(self, write_plugin):
        manager = ActionManager()
        with pytest.raises(ValueError, match="Function ghost is not registered"):
            manager.match_tool({"function": "ghost", "arguments": {}})

    def test_untrusted_plugin_tool_is_excluded(self, write_plugin):
        _write_tool_plugin(write_plugin, "p1", tool_name="greet")
        # never trusted
        PluginRegistry.reset()

        manager = ActionManager()
        with pytest.raises(ValueError, match="Function greet is not registered"):
            manager.match_tool({"function": "greet", "arguments": {}})

    def test_disabled_plugin_tool_is_excluded(self, write_plugin):
        _write_tool_plugin(write_plugin, "p1", name="greeter", tool_name="greet")
        _trust("p1")
        settings = read_user_settings()
        settings.setdefault("plugins", {})["greeter"] = {"enabled": False}
        write_user_settings(settings)
        PluginRegistry.reset()

        manager = ActionManager()
        with pytest.raises(ValueError, match="Function greet is not registered"):
            manager.match_tool({"function": "greet", "arguments": {}})

    def test_changed_plugin_tool_is_excluded(self, write_plugin):
        bundle = _write_tool_plugin(write_plugin, "p1", name="greeter", tool_name="greet")
        _trust("p1")
        (bundle / "tools" / "impl.py").write_text("def do_greet():\n    return 'tampered'\n")
        PluginRegistry.reset()

        manager = ActionManager()
        with pytest.raises(ValueError, match="Function greet is not registered"):
            manager.match_tool({"function": "greet", "arguments": {}})

    def test_incompatible_plugin_tool_is_excluded(self, write_plugin):
        _write_tool_plugin(write_plugin, "p1", name="greeter", tool_name="greet")
        _trust("p1")
        # Overwrite with an incompatible lionagi range after trusting.
        bundle = next(x for x in discover_plugins() if x.dir_name == "p1").bundle_dir
        (bundle / "plugin.yaml").write_text(
            MANIFEST.format(name="greeter", tool_name="greet", func_name="do_greet").replace(
                '">=0.0,<100.0"', '">=999.0"'
            )
        )
        PluginRegistry.reset()

        manager = ActionManager()
        with pytest.raises(ValueError, match="Function greet is not registered"):
            manager.match_tool({"function": "greet", "arguments": {}})


class TestToolConsumerCollision:
    def test_two_enabled_plugins_same_tool_name_hard_errors(self, write_plugin):
        _write_tool_plugin(write_plugin, "p1", name="plugin-one", tool_name="greet", func_name="a")
        _write_tool_plugin(write_plugin, "p2", name="plugin-two", tool_name="greet", func_name="b")
        _trust("p1")
        _trust("p2")
        PluginRegistry.reset()

        manager = ActionManager()
        with pytest.raises(PluginToolCollisionError) as excinfo:
            manager.match_tool({"function": "greet", "arguments": {}})
        assert "greet" in str(excinfo.value)
        assert "plugin-one" in str(excinfo.value)
        assert "plugin-two" in str(excinfo.value)

    def test_disabling_one_plugin_resolves_the_collision(self, write_plugin):
        _write_tool_plugin(write_plugin, "p1", name="plugin-one", tool_name="greet", func_name="a")
        _write_tool_plugin(write_plugin, "p2", name="plugin-two", tool_name="greet", func_name="b")
        _trust("p1")
        _trust("p2")

        settings = read_user_settings()
        settings.setdefault("plugins", {})["plugin-two"] = {"enabled": False}
        write_user_settings(settings)
        PluginRegistry.reset()

        manager = ActionManager()
        result = manager.match_tool({"function": "greet", "arguments": {}})
        assert result.func_tool.function == "greet"


class TestLiveRescanWithoutReset:
    """The cached snapshot is only a candidate index -- enabled/collision/target
    must be re-derived live on every call, not frozen at the last reset()."""

    def test_disabling_after_first_resolve_excludes_it_live(self, write_plugin):
        _write_tool_plugin(write_plugin, "p1", name="greeter", tool_name="greet")
        _trust("p1")
        PluginRegistry.reset()

        manager = ActionManager()
        first = manager.match_tool({"function": "greet", "arguments": {}})
        assert first.func_tool.function == "greet"

        settings = read_user_settings()
        settings.setdefault("plugins", {})["greeter"] = {"enabled": False}
        write_user_settings(settings)
        # Deliberately no PluginRegistry.reset(): the disable must take
        # effect on the very next call regardless.

        with pytest.raises(ValueError, match="Function greet is not registered"):
            manager.match_tool({"function": "greet", "arguments": {}})

    def test_disabling_after_first_resolve_also_blocks_direct_activation(self, write_plugin):
        _write_tool_plugin(write_plugin, "p1", name="greeter", tool_name="greet")
        _trust("p1")
        PluginRegistry.reset()

        target = PluginRegistry.resolve_tool_target("greet")
        assert target is not None
        PluginRegistry.activate_target(target.plugin_name, target.target)

        settings = read_user_settings()
        settings.setdefault("plugins", {})["greeter"] = {"enabled": False}
        write_user_settings(settings)

        with pytest.raises(PluginActivationError):
            PluginRegistry.activate_target(target.plugin_name, target.target)

    def test_disabling_one_of_two_colliding_plugins_resolves_live(self, write_plugin):
        _write_tool_plugin(write_plugin, "p1", name="plugin-one", tool_name="greet", func_name="a")
        _write_tool_plugin(write_plugin, "p2", name="plugin-two", tool_name="greet", func_name="b")
        _trust("p1")
        _trust("p2")
        PluginRegistry.reset()

        manager = ActionManager()
        with pytest.raises(PluginToolCollisionError):
            manager.match_tool({"function": "greet", "arguments": {}})

        settings = read_user_settings()
        settings.setdefault("plugins", {})["plugin-two"] = {"enabled": False}
        write_user_settings(settings)
        # No reset(): the collision must resolve on the very next call.

        result = manager.match_tool({"function": "greet", "arguments": {}})
        assert result.func_tool.function == "greet"

    def test_retrusted_target_change_resolves_to_new_target_live(self, write_plugin):
        bundle = _write_tool_plugin(write_plugin, "p1", name="greeter", tool_name="greet")
        _trust("p1")
        PluginRegistry.reset()

        first = PluginRegistry.resolve_tool_target("greet")
        assert first is not None
        assert first.target == "tools/impl.py:do_greet"

        (bundle / "tools" / "new.py").write_text("def replacement():\n    return 'new behavior'\n")
        (bundle / "plugin.yaml").write_text(
            MANIFEST.format(name="greeter", tool_name="greet", func_name="replacement").replace(
                "tools/impl.py", "tools/new.py"
            )
        )
        _trust("p1")
        # No reset(): the re-trusted target change must be visible on the
        # very next call, not the target string captured by the first lookup.

        second = PluginRegistry.resolve_tool_target("greet")
        assert second is not None
        assert second.target == "tools/new.py:replacement"

        fn = PluginRegistry.activate_target(second.plugin_name, second.target)
        assert fn() == "new behavior"


class TestLocalRegistrationTakesPriority:
    def test_locally_registered_tool_wins_over_a_colliding_plugin(self, write_plugin):
        """A registry hit still wins over a same-named plugin declaration --
        the plugin registry is consulted only to emit a named collision
        diagnostic (ADR-0088 D6), never to override the registered tool."""

        def greet():
            return "local"

        manager = ActionManager()
        manager.register_tool(greet)

        _write_tool_plugin(write_plugin, "p1", name="greeter", tool_name="greet")
        _trust("p1")
        PluginRegistry.reset()

        result = manager.match_tool({"function": "greet", "arguments": {}})
        assert result.func_tool.func_callable is greet


class TestBuiltinToolCollisionDiagnostic:
    """ADR-0088 D6: a plugin tool must never silently replace a name already
    present in the manager's registry -- the registered tool wins, and the
    plugin's declaration is rejected with a diagnostic naming which plugin
    and which tool name lost."""

    def test_shadow_is_logged_naming_plugin_and_tool(self, write_plugin, caplog):
        import logging

        def greet():
            return "local"

        manager = ActionManager()
        manager.register_tool(greet)

        _write_tool_plugin(write_plugin, "p1", name="greeter", tool_name="greet")
        _trust("p1")
        PluginRegistry.reset()

        with caplog.at_level(logging.WARNING, logger="lionagi.protocols.action.manager"):
            result = manager.match_tool({"function": "greet", "arguments": {}})

        # The registered tool still wins...
        assert result.func_tool.func_callable is greet
        # ...but the collision was surfaced, naming the plugin and the tool.
        assert "greeter" in caplog.text
        assert "greet" in caplog.text

    def test_no_diagnostic_when_no_plugin_declares_the_same_name(self, write_plugin, caplog):
        """A non-colliding local tool call must not spuriously warn."""
        import logging

        def unrelated():
            return "local"

        manager = ActionManager()
        manager.register_tool(unrelated)

        _write_tool_plugin(write_plugin, "p1", name="greeter", tool_name="greet")
        _trust("p1")
        PluginRegistry.reset()

        with caplog.at_level(logging.WARNING, logger="lionagi.protocols.action.manager"):
            result = manager.match_tool({"function": "unrelated", "arguments": {}})

        assert result.func_tool.func_callable is unrelated
        assert caplog.text == ""

    def test_no_diagnostic_when_no_plugins_are_installed(self, write_plugin, caplog):
        """The common case (no plugins at all) must not pay for or log a
        plugin-collision check on every registered-tool call."""
        import logging

        def greet():
            return "local"

        manager = ActionManager()
        manager.register_tool(greet)
        PluginRegistry.reset()

        with caplog.at_level(logging.WARNING, logger="lionagi.protocols.action.manager"):
            result = manager.match_tool({"function": "greet", "arguments": {}})

        assert result.func_tool.func_callable is greet
        assert caplog.text == ""
