# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""ADR-0023 loader tests."""

from __future__ import annotations

import pytest

from lionagi.hooks import (
    DEFAULT_HOOKS,
    HookBus,
    HookPoint,
    build_session_bus,
    load_hooks_for_agent,
    register_handler,
    resolve_handler,
)

# ── Registry ──────────────────────────────────────────────────────────────────


def test_builtins_resolvable_by_name():
    """ADR-0023 §"Built-in handlers" — names must be agent-YAML addressable."""
    for name in (
        "persist_session_start",
        "persist_session_end",
        "persist_branch_provenance",
        "persist_message",
        "log_api_metrics",
        "log_tool_use",
    ):
        assert callable(resolve_handler(name)), f"{name!r} not registered"


def test_resolve_unknown_handler_raises_descriptively():
    with pytest.raises(KeyError, match="not_a_real_handler"):
        resolve_handler("not_a_real_handler")


def test_register_handler_makes_it_addressable():
    async def my_custom(**kw):
        pass

    register_handler("my_custom_handler_for_test", my_custom)
    try:
        assert resolve_handler("my_custom_handler_for_test") is my_custom
    finally:
        # Cleanup: re-registration with the same name acts as remove +
        # add, so register a sentinel to "free" the slot for the next
        # test run (avoids cross-test pollution).
        from lionagi.hooks.loader import _REGISTRY

        _REGISTRY.pop("my_custom_handler_for_test", None)


# ── load_hooks_for_agent ─────────────────────────────────────────────────────


def test_load_hooks_resolves_string_names_to_callables():
    resolved = load_hooks_for_agent(
        {
            "session.start": ["persist_session_start"],
            "api.post_call": ["log_api_metrics"],
        }
    )
    assert set(resolved) == {HookPoint.SESSION_START, HookPoint.API_POST_CALL}
    assert len(resolved[HookPoint.SESSION_START]) == 1
    assert len(resolved[HookPoint.API_POST_CALL]) == 1


def test_load_hooks_rejects_unknown_hook_point():
    with pytest.raises(ValueError, match="Unknown hook point"):
        load_hooks_for_agent({"session.bogus": ["persist_session_start"]})


def test_load_hooks_rejects_non_list_value():
    with pytest.raises(ValueError, match="must be a list"):
        load_hooks_for_agent({"session.start": "persist_session_start"})


def test_load_hooks_empty_or_none_returns_empty_map():
    assert load_hooks_for_agent(None) == {}
    assert load_hooks_for_agent({}) == {}


# ── build_session_bus override semantics ─────────────────────────────────────


def test_build_session_bus_uses_defaults_when_no_profile():
    bus = build_session_bus(None)
    for point, handlers in DEFAULT_HOOKS.items():
        assert bus.handlers_for(point) == handlers


def test_build_session_bus_profile_overrides_defaults():
    """Profile listing a hook point REPLACES the default for that point."""
    bus = build_session_bus(
        {"session.start": ["log_api_metrics"]}  # bogus mapping; just testing override
    )
    # session.start now has only log_api_metrics, NOT persist_session_start.
    handlers = bus.handlers_for(HookPoint.SESSION_START)
    assert len(handlers) == 1
    # Other defaults are untouched.
    assert bus.handlers_for(HookPoint.SESSION_END) == DEFAULT_HOOKS[HookPoint.SESSION_END]
    assert bus.handlers_for(HookPoint.MESSAGE_ADD) == DEFAULT_HOOKS[HookPoint.MESSAGE_ADD]


def test_build_session_bus_empty_list_disables_default():
    """``message.add: []`` is the documented way to turn off persistence."""
    bus = build_session_bus({"message.add": []})
    assert bus.handlers_for(HookPoint.MESSAGE_ADD) == []
    # Defaults at other points still present.
    assert bus.handlers_for(HookPoint.SESSION_START) != []


def test_build_session_bus_adds_handlers_for_non_default_points():
    """Profile may register handlers on points that have no default."""
    bus = build_session_bus(
        {"api.post_call": ["log_api_metrics"], "tool.pre": ["log_tool_use"]}
    )
    assert len(bus.handlers_for(HookPoint.API_POST_CALL)) == 1
    assert len(bus.handlers_for(HookPoint.TOOL_PRE)) == 1


def test_default_hooks_only_cover_session_lifecycle_and_persistence():
    """ADR-0023 §"Default hooks" — pinned set."""
    assert set(DEFAULT_HOOKS) == {
        HookPoint.SESSION_START,
        HookPoint.SESSION_END,
        HookPoint.MESSAGE_ADD,
        HookPoint.BRANCH_CREATE,
    }


def test_build_session_bus_returns_fresh_instance_each_call():
    """Each session gets its own bus — no shared state."""
    a = build_session_bus(None)
    b = build_session_bus(None)
    assert a is not b
    assert isinstance(a, HookBus)
    assert isinstance(b, HookBus)
