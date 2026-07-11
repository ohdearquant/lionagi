# Copyright (c) 2025 - 2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Coverage tests for LionMessenger."""

import logging
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


def _make_branch(exchange: Exchange, branch_id=None):
    included = []
    view = _MsgsView(included)
    msgs = SimpleNamespace(messages=view)

    branch_id = branch_id or uuid4()
    exchange.register(branch_id)
    return SimpleNamespace(id=branch_id, msgs=msgs, _included=included)


class TestMessengerActionEnum:
    def test_enum_values(self):
        assert MessengerAction.send == "send"
        assert MessengerAction.receive == "receive"
        assert MessengerAction.done == "done"
        assert MessengerAction.finished == "finished"
        assert MessengerAction.wakeup == "wakeup"
        assert MessengerAction.help == "help"


class TestMessengerRequestModel:
    def test_minimal_request(self):
        req = MessengerRequest(action="done")
        assert req.action == MessengerAction.done
        assert req.to is None
        assert req.content is None

    def test_send_request(self):
        req = MessengerRequest(action="send", to="alice", content="hi")
        assert req.to == "alice"
        assert req.content == "hi"

    def test_send_request_list_to(self):
        req = MessengerRequest(action="send", to=["alice", "bob"], content="hi")
        assert req.to == ["alice", "bob"]


class TestLionMessengerBasics:
    def test_is_system_tool(self):
        m = LionMessenger(exchange=Exchange())
        assert m.is_lion_system_tool is True
        assert m.system_tool_name == "messenger"

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

    def test_fire_no_callback_noop(self):
        m = LionMessenger(exchange=Exchange())
        result = m._fire("done", name="x")
        assert result is None
        assert "done" not in m._callbacks

    def test_fire_no_callback_logs_debug(self, caplog):
        """A mis-wired coordinator (no .on() registered) must be discoverable
        during bring-up: the no-op is debug-logged, not fully silent."""
        m = LionMessenger(exchange=Exchange())
        with caplog.at_level(logging.DEBUG, logger="lionagi.tools.communication.messenger"):
            m._fire("help", name="x", reason="stuck", urgency="fyi")
        assert any(
            "no callback registered" in r.message and "help" in r.message for r in caplog.records
        )


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

    def test_default_sender_name_from_branch_id(self):
        ex = Exchange()
        m = LionMessenger(exchange=ex)
        branch = _make_branch(ex)
        tool = m.bind(branch, roster={})
        result = tool.func_callable(action="done", content="ok")
        # sender_name defaults to str(branch.id)[:8]
        assert str(branch.id)[:8] in result

    def test_done_callback_raising_propagates_to_caller(self):
        """Unlike 'help', 'done' is not a fire-and-continue channel: a
        raising state-recording callback must surface to the messenger
        caller, not be swallowed behind a success string — otherwise the
        caller believes the state update landed when it didn't."""
        ex = Exchange()
        m = LionMessenger(exchange=ex)

        def _boom(**kw):
            raise RuntimeError("state store unavailable")

        m.on("done", _boom)
        branch = _make_branch(ex)
        tool = m.bind(branch, roster={}, sender_name="bob")
        with pytest.raises(RuntimeError, match="state store unavailable"):
            tool.func_callable(action="done", content="task complete")

    def test_finished_callback_raising_propagates_to_caller(self):
        ex = Exchange()
        m = LionMessenger(exchange=ex)

        def _boom(**kw):
            raise RuntimeError("state store unavailable")

        m.on("finished", _boom)
        branch = _make_branch(ex)
        tool = m.bind(branch, roster={}, sender_name="bob")
        with pytest.raises(RuntimeError, match="state store unavailable"):
            tool.func_callable(action="finished", content="forever")

    def test_wakeup_callback_raising_propagates_to_caller(self):
        ex = Exchange()
        m = LionMessenger(exchange=ex)

        def _boom(**kw):
            raise RuntimeError("state store unavailable")

        m.on("wakeup", _boom)
        alice_id = uuid4()
        ex.register(alice_id)
        branch = _make_branch(ex)
        tool = m.bind(branch, roster={"alice": alice_id}, sender_name="bob")
        with pytest.raises(RuntimeError, match="state store unavailable"):
            tool.func_callable(action="wakeup", to="alice", content="wake up")


class TestMessengerBindHelp:
    def test_help_missing_content(self):
        ex = Exchange()
        m = LionMessenger(exchange=ex)
        branch = _make_branch(ex)
        tool = m.bind(branch, roster={})
        result = tool.func_callable(action="help")
        assert "Error" in result and "'content'" in result

    def test_help_fires_callback_with_default_fyi_urgency(self):
        ex = Exchange()
        m = LionMessenger(exchange=ex)
        events = []
        m.on("help", lambda **kw: events.append(kw))
        branch = _make_branch(ex)
        tool = m.bind(branch, roster={}, sender_name="bob")
        result = tool.func_callable(action="help", content="not sure who to ask")
        assert "help signal" in result
        assert "fyi" in result
        assert events[0]["name"] == "bob"
        assert events[0]["reason"] == "not sure who to ask"
        assert events[0]["urgency"] == "fyi"

    def test_help_fires_callback_with_blocked_urgency(self):
        ex = Exchange()
        m = LionMessenger(exchange=ex)
        events = []
        m.on("help", lambda **kw: events.append(kw))
        branch = _make_branch(ex)
        tool = m.bind(branch, roster={}, sender_name="bob")
        result = tool.func_callable(action="help", content="cannot proceed", urgency="blocked")
        assert "blocked" in result
        assert events[0]["urgency"] == "blocked"

    def test_help_returns_immediately_never_blocks_on_missing_callback(self):
        """Fire-and-continue: no callback registered -> still a normal,
        immediate return (never a hang, never an exception)."""
        ex = Exchange()
        m = LionMessenger(exchange=ex)
        branch = _make_branch(ex)
        tool = m.bind(branch, roster={}, sender_name="bob")
        result = tool.func_callable(action="help", content="stuck")
        assert "sent a help signal" in result

    def test_help_returns_immediately_even_if_callback_raises(self, caplog):
        """A raising coordinator callback must not propagate into the
        worker's tool-call turn — the help channel is fire-and-continue.
        The worker still gets its acknowledgment string; the failure is
        logged, not swallowed silently."""
        ex = Exchange()
        m = LionMessenger(exchange=ex)

        def _boom(**kw):
            raise RuntimeError("coordinator exploded")

        m.on("help", _boom)
        branch = _make_branch(ex)
        tool = m.bind(branch, roster={}, sender_name="bob")
        with caplog.at_level(logging.WARNING, logger="lionagi.tools.communication.messenger"):
            result = tool.func_callable(action="help", content="stuck")
        assert "sent a help signal" in result
        assert any("callback for event='help' raised" in r.message for r in caplog.records)


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


class TestMessengerBindReceive:
    def test_receive_empty_inbox(self):
        ex = Exchange()
        m = LionMessenger(exchange=ex)
        branch = _make_branch(ex)
        tool = m.bind(branch, roster={})
        result = tool.func_callable(action="receive")
        assert result == "No new messages."

    def test_receive_single_sender(self):
        ex = Exchange()
        m = LionMessenger(exchange=ex)
        bob = _make_branch(ex)
        alice = _make_branch(ex)

        alice_tool = m.bind(alice, roster={"bob": bob.id}, sender_name="alice")
        alice_tool.func_callable(action="send", to="bob", content="hi bob")

        import asyncio

        asyncio.run(ex.collect(alice.id))

        bob_tool = m.bind(bob, roster={"alice": alice.id}, sender_name="bob")
        result = bob_tool.func_callable(action="receive")
        assert result == "[alice] hi bob"

        # second call drains an empty inbox
        result2 = bob_tool.func_callable(action="receive")
        assert result2 == "No new messages."

    def test_receive_multi_sender_drains_all(self):
        ex = Exchange()
        m = LionMessenger(exchange=ex)
        bob = _make_branch(ex)
        alice = _make_branch(ex)
        carol = _make_branch(ex)

        alice_tool = m.bind(alice, roster={"bob": bob.id}, sender_name="alice")
        carol_tool = m.bind(carol, roster={"bob": bob.id}, sender_name="carol")
        alice_tool.func_callable(action="send", to="bob", content="from alice")
        carol_tool.func_callable(action="send", to="bob", content="from carol")

        import asyncio

        asyncio.run(ex.collect(alice.id))
        asyncio.run(ex.collect(carol.id))

        bob_tool = m.bind(bob, roster={"alice": alice.id, "carol": carol.id}, sender_name="bob")
        result = bob_tool.func_callable(action="receive")
        lines = result.splitlines()
        assert "[alice] from alice" in lines
        assert "[carol] from carol" in lines
        assert len(lines) == 2

        # fully drained
        assert bob_tool.func_callable(action="receive") == "No new messages."

    def test_receive_unknown_sender_falls_back_to_uuid_prefix(self):
        ex = Exchange()
        m = LionMessenger(exchange=ex)
        bob = _make_branch(ex)
        ghost = _make_branch(ex)

        ghost_tool = m.bind(ghost, roster={"bob": bob.id}, sender_name="ghost")
        ghost_tool.func_callable(action="send", to="bob", content="mystery")

        import asyncio

        asyncio.run(ex.collect(ghost.id))

        # bob's roster doesn't include ghost's name -> fallback to uuid prefix
        bob_tool = m.bind(bob, roster={}, sender_name="bob")
        result = bob_tool.func_callable(action="receive")
        assert result == f"[{str(ghost.id)[:8]}] mystery"


class TestMessengerBindUnknownAction:
    def test_unknown_action_returned(self):
        ex = Exchange()
        m = LionMessenger(exchange=ex)
        branch = _make_branch(ex)
        tool = m.bind(branch, roster={})
        result = tool.func_callable(action="bogus")
        assert "Unknown action: bogus" in result
