# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""ADR-0023 HookBus tests."""

from __future__ import annotations

import pytest

from lionagi.hooks import HookBus, HookPoint, StopHook, hook

# ── Basic dispatch ────────────────────────────────────────────────────────────


async def test_emit_calls_registered_handlers_in_order():
    bus = HookBus()
    calls: list[str] = []

    async def h1(**kw):
        calls.append("h1")

    async def h2(**kw):
        calls.append("h2")

    bus.on(HookPoint.SESSION_START, h1)
    bus.on(HookPoint.SESSION_START, h2)
    await bus.emit(HookPoint.SESSION_START, session_id="s")

    assert calls == ["h1", "h2"]


async def test_emit_with_no_handlers_is_silent():
    bus = HookBus()
    # Should not raise.
    await bus.emit(HookPoint.SESSION_END, session_id="s", status="completed")


async def test_off_removes_handler():
    bus = HookBus()
    calls: list[str] = []

    async def h(**kw):
        calls.append("fired")

    bus.on(HookPoint.MESSAGE_ADD, h)
    bus.off(HookPoint.MESSAGE_ADD, h)
    await bus.emit(HookPoint.MESSAGE_ADD, message={}, session_id="s")
    assert calls == []


async def test_off_unregistered_handler_is_noop():
    bus = HookBus()

    async def h(**kw):
        pass

    # Should not raise even though h was never registered.
    bus.off(HookPoint.MESSAGE_ADD, h)


# ── Sync handlers accepted ────────────────────────────────────────────────────


async def test_sync_handler_runs_without_await():
    bus = HookBus()
    calls: list[int] = []

    def sync_handler(**kw):
        calls.append(1)

    bus.on(HookPoint.SESSION_END, sync_handler)
    await bus.emit(HookPoint.SESSION_END, session_id="s", status="completed")
    assert calls == [1]


# ── Isolation: handler errors do NOT abort ───────────────────────────────────


async def test_handler_exception_is_logged_and_swallowed():
    bus = HookBus()
    fired_after: list[str] = []

    async def boom(**kw):
        raise RuntimeError("explode")

    async def after(**kw):
        fired_after.append("after")

    bus.on(HookPoint.MESSAGE_ADD, boom)
    bus.on(HookPoint.MESSAGE_ADD, after)
    # No exception should propagate.
    await bus.emit(HookPoint.MESSAGE_ADD, message={}, session_id="s")
    # Subsequent handlers still ran.
    assert fired_after == ["after"]


async def test_sync_handler_exception_is_also_swallowed():
    bus = HookBus()
    fired_after: list[str] = []

    def boom(**kw):
        raise RuntimeError("sync boom")

    async def after(**kw):
        fired_after.append("after")

    bus.on(HookPoint.MESSAGE_ADD, boom)
    bus.on(HookPoint.MESSAGE_ADD, after)
    await bus.emit(HookPoint.MESSAGE_ADD, message={}, session_id="s")
    assert fired_after == ["after"]


# ── StopHook short-circuits remaining handlers ───────────────────────────────


async def test_stop_hook_aborts_siblings_but_not_operation():
    bus = HookBus()
    calls: list[str] = []

    async def stopper(**kw):
        calls.append("stopper")
        raise StopHook

    async def never(**kw):  # pragma: no cover
        calls.append("never")

    bus.on(HookPoint.MESSAGE_ADD, stopper)
    bus.on(HookPoint.MESSAGE_ADD, never)
    await bus.emit(HookPoint.MESSAGE_ADD, message={}, session_id="s")
    assert calls == ["stopper"]


# ── Read introspection ──────────────────────────────────────────────────────


async def test_handlers_for_returns_copy_not_internal_list():
    bus = HookBus()

    async def h(**kw):
        pass

    bus.on(HookPoint.SESSION_START, h)
    snapshot = bus.handlers_for(HookPoint.SESSION_START)
    snapshot.clear()  # Should not affect the bus.

    assert bus.handlers_for(HookPoint.SESSION_START) == [h]


# ── @hook decorator ──────────────────────────────────────────────────────────


def test_hook_decorator_tags_function_with_point():
    @hook(HookPoint.API_POST_CALL)
    async def my_handler(**kw):
        pass

    assert my_handler.__lionagi_hook_point__ is HookPoint.API_POST_CALL


def test_hook_decorator_accepts_string_point():
    @hook("api.pre_call")
    async def my_handler(**kw):
        pass

    assert my_handler.__lionagi_hook_point__ is HookPoint.API_PRE_CALL


def test_hook_decorator_rejects_unknown_point():
    with pytest.raises(ValueError):

        @hook("not.a.real.point")
        async def _handler(**kw):
            pass


# ── HookPoint vocabulary pinned ──────────────────────────────────────────────


def test_hook_point_vocabulary():
    """Pin the 11-event vocabulary so a removal is visible in this test."""
    values = {p.value for p in HookPoint}
    assert values == {
        "session.start",
        "session.end",
        "branch.create",
        "api.pre_call",
        "api.post_call",
        "api.stream_chunk",
        "tool.pre",
        "tool.post",
        "tool.error",
        "message.add",
        "artifact.created",
    }
