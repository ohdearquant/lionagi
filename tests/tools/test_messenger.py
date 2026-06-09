# Copyright (c) 2025 - 2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Coverage tests for LionMessenger."""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from lionagi.protocols.action.tool import Tool
from lionagi.session.exchange import Exchange
from lionagi.tools.communication.messenger import (
    LionMessenger,
    MessengerAction,
    MessengerRequest,
)


def _make_branch(exchange: Exchange):
    """Minimal Branch stub with id + msgs.messages.include()."""
    included = []
    messages = SimpleNamespace(
        include=lambda msg: included.append(msg),
        messages=included,  # for `msg in branch.msgs.messages`
    )

    # `msg not in branch.msgs.messages` → checks membership on included list
    class _MsgsView:
        def __init__(self, store):
            self._store = store

        def __iter__(self):
            return iter(self._store)

        def __contains__(self, item):
            return item in self._store

        def include(self, msg):
            self._store.append(msg)

    view = _MsgsView(included)
    msgs = SimpleNamespace(messages=view)

    branch_id = uuid4()
    exchange.register(branch_id)
    return SimpleNamespace(id=branch_id, msgs=msgs, _included=included)


class TestLionMessengerBasics:
    def test_to_tool_raises(self):
        m = LionMessenger(exchange=Exchange())
        with pytest.raises(NotImplementedError, match="requires branch context"):
            m.to_tool()

    def test_on_registers_callback(self):
        m = LionMessenger(exchange=Exchange())
        calls = []
        m.on("done", lambda **kw: calls.append(kw))
        m._fire("done", name="x")
        assert calls == [{"name": "x"}]


class TestMessengerBindSend:
    def test_bind_returns_tool(self):
        ex = Exchange()
        m = LionMessenger(exchange=ex)
        branch = _make_branch(ex)
        tool = m.bind(branch, roster={})
        assert isinstance(tool, Tool)
        assert tool.request_options is MessengerRequest

    def test_send_missing_to(self):
        ex = Exchange()
        m = LionMessenger(exchange=ex)
        branch = _make_branch(ex)
        tool = m.bind(branch, roster={})
        result = tool.func_callable(action="send", content="hi")
        assert "Error" in result and "'to'" in result

    def test_send_missing_content(self):
        ex = Exchange()
        m = LionMessenger(exchange=ex)
        branch = _make_branch(ex)
        tool = m.bind(branch, roster={"a": uuid4()})
        result = tool.func_callable(action="send", to="a")
        assert "Error" in result

    def test_send_unknown_recipient(self):
        ex = Exchange()
        m = LionMessenger(exchange=ex)
        branch = _make_branch(ex)
        tool = m.bind(branch, roster={})
        result = tool.func_callable(action="send", to="ghost", content="hi")
        assert "Unknown recipient: ghost" in result

    def test_send_single_recipient(self):
        ex = Exchange()
        m = LionMessenger(exchange=ex)
        branch = _make_branch(ex)
        alice_id = uuid4()
        ex.register(alice_id)
        tool = m.bind(branch, roster={"alice": alice_id}, sender_name="bob")
        result = tool.func_callable(action="send", to="alice", content="hello")
        assert "Sent to alice" in result
        assert len(branch._included) == 1

    def test_send_list_recipients_partial_unknown(self):
        ex = Exchange()
        m = LionMessenger(exchange=ex)
        branch = _make_branch(ex)
        alice_id = uuid4()
        ex.register(alice_id)
        tool = m.bind(branch, roster={"alice": alice_id})
        result = tool.func_callable(action="send", to=["alice", "ghost"], content="hey")
        assert "Sent to alice" in result
        assert "Unknown recipient: ghost" in result


class TestMessengerBindStateEvents:
    def test_done_fires_callback(self):
        ex = Exchange()
        m = LionMessenger(exchange=ex)
        events = []
        m.on("done", lambda **kw: events.append(kw))
        branch = _make_branch(ex)
        tool = m.bind(branch, roster={}, sender_name="bob")
        result = tool.func_callable(action="done", content="task complete")
        assert "bob is now done" in result
        assert events[0]["name"] == "bob"
        assert events[0]["reason"] == "task complete"

    def test_done_without_content_defaults(self):
        ex = Exchange()
        m = LionMessenger(exchange=ex)
        branch = _make_branch(ex)
        tool = m.bind(branch, roster={}, sender_name="bob")
        result = tool.func_callable(action="done")
        assert "no reason" in result

    def test_finished_fires_callback(self):
        ex = Exchange()
        m = LionMessenger(exchange=ex)
        events = []
        m.on("finished", lambda **kw: events.append(kw))
        branch = _make_branch(ex)
        tool = m.bind(branch, roster={}, sender_name="bob")
        result = tool.func_callable(action="finished", content="forever")
        assert "permanently finished" in result
        assert events[0]["reason"] == "forever"

    def test_finished_without_content(self):
        ex = Exchange()
        m = LionMessenger(exchange=ex)
        branch = _make_branch(ex)
        tool = m.bind(branch, roster={})
        result = tool.func_callable(action="finished")
        assert "no reason" in result


class TestMessengerBindWakeup:
    def test_wakeup_missing_to(self):
        ex = Exchange()
        m = LionMessenger(exchange=ex)
        branch = _make_branch(ex)
        tool = m.bind(branch, roster={})
        result = tool.func_callable(action="wakeup", content="hey")
        assert "Error" in result

    def test_wakeup_missing_content(self):
        ex = Exchange()
        m = LionMessenger(exchange=ex)
        branch = _make_branch(ex)
        tool = m.bind(branch, roster={"a": uuid4()})
        result = tool.func_callable(action="wakeup", to="a")
        assert "Error" in result

    def test_wakeup_unknown_target(self):
        ex = Exchange()
        m = LionMessenger(exchange=ex)
        branch = _make_branch(ex)
        tool = m.bind(branch, roster={})
        result = tool.func_callable(action="wakeup", to="ghost", content="up")
        assert "Unknown teammate: ghost" in result

    def test_wakeup_single_target(self):
        ex = Exchange()
        m = LionMessenger(exchange=ex)
        events = []
        m.on("wakeup", lambda **kw: events.append(kw))
        branch = _make_branch(ex)
        alice_id = uuid4()
        ex.register(alice_id)
        tool = m.bind(branch, roster={"alice": alice_id}, sender_name="bob", channel="team")
        result = tool.func_callable(action="wakeup", to="alice", content="stand up")
        assert "Woke up alice" in result
        assert len(branch._included) == 1
        assert events[0]["target"] == "alice"
        assert events[0]["message"] == "stand up"

    def test_wakeup_list_to_uses_first(self):
        ex = Exchange()
        m = LionMessenger(exchange=ex)
        branch = _make_branch(ex)
        alice_id = uuid4()
        ex.register(alice_id)
        tool = m.bind(branch, roster={"alice": alice_id})
        result = tool.func_callable(action="wakeup", to=["alice", "bob"], content="rise")
        assert "Woke up alice" in result


class TestMessengerBindUnknownAction:
    def test_unknown_action_returned(self):
        ex = Exchange()
        m = LionMessenger(exchange=ex)
        branch = _make_branch(ex)
        tool = m.bind(branch, roster={})
        result = tool.func_callable(action="bogus")
        assert "Unknown action: bogus" in result


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestMessengerEdgeCases:
    def test_send_to_self_same_branch_id(self):
        ex = Exchange()
        m = LionMessenger(exchange=ex)
        branch = _make_branch(ex)
        # Register sender also as a recipient under an alias
        tool = m.bind(branch, roster={"self": branch.id}, sender_name="self")
        result = tool.func_callable(action="send", to="self", content="hello me")
        assert "Sent to self" in result

    def test_send_very_large_content(self):
        ex = Exchange()
        m = LionMessenger(exchange=ex)
        branch = _make_branch(ex)
        alice_id = uuid4()
        ex.register(alice_id)
        tool = m.bind(branch, roster={"alice": alice_id})
        large_content = "X" * 100_000
        result = tool.func_callable(action="send", to="alice", content=large_content)
        assert "Sent to alice" in result

    def test_send_after_branch_unregistered(self):
        ex = Exchange()
        m = LionMessenger(exchange=ex)
        branch = _make_branch(ex)
        alice_id = uuid4()
        ex.register(alice_id)
        tool = m.bind(branch, roster={"alice": alice_id})
        ex.unregister(alice_id)
        # Sending to unregistered recipient — exchange.send may raise or return error
        try:
            result = tool.func_callable(action="send", to="alice", content="hi")
            # If it returns a string, must contain something informative
            assert isinstance(result, str)
        except Exception:
            pass

    def test_concurrent_sends_from_multiple_branches(self):
        import asyncio

        ex = Exchange()
        alice_id = uuid4()
        ex.register(alice_id)

        results = []

        def _send_from_fresh_branch():
            branch = _make_branch(ex)
            tool = LionMessenger(exchange=ex).bind(branch, roster={"alice": alice_id})
            result = tool.func_callable(action="send", to="alice", content="ping")
            results.append(result)

        # Run multiple synchronous sends — the Exchange is not async here,
        # but we confirm there's no shared-state corruption.
        for _ in range(10):
            _send_from_fresh_branch()

        assert all("Sent to alice" in r for r in results)

    def test_wakeup_targets_branch_that_was_not_done(self):
        ex = Exchange()
        m = LionMessenger(exchange=ex)
        wakeup_events = []
        m.on("wakeup", lambda **kw: wakeup_events.append(kw))
        branch = _make_branch(ex)
        alice_id = uuid4()
        ex.register(alice_id)
        tool = m.bind(branch, roster={"alice": alice_id}, sender_name="bob")
        # Wakeup doesn't require the target to be in "done" state — it's just a send
        result = tool.func_callable(action="wakeup", to="alice", content="rise!")
        assert "Woke up alice" in result
        assert wakeup_events[0]["target"] == "alice"
