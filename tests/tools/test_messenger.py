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


class TestMessengerActionEnum:
    def test_enum_values(self):
        assert MessengerAction.send == "send"
        assert MessengerAction.done == "done"
        assert MessengerAction.finished == "finished"
        assert MessengerAction.wakeup == "wakeup"


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
