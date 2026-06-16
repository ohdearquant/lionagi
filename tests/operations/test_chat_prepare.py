# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for lionagi/operations/chat/_prepare.py::_prepare_run_kwargs."""

from lionagi.operations.chat._prepare import _prepare_run_kwargs
from lionagi.operations.types import ChatParam
from lionagi.session.branch import Branch

# ---------------------------------------------------------------------------
# _prepare_run_kwargs merges consecutive AssistantResponse messages
# ---------------------------------------------------------------------------


def test_prepare_run_kwargs_collapses_consecutive_assistant_messages():
    """Two consecutive AssistantResponse messages are merged into one."""
    branch = Branch()
    param = ChatParam()

    branch.msgs.add_message(instruction="initial question")
    branch.msgs.add_message(assistant_response="first answer")
    branch.msgs.add_message(assistant_response="second answer")

    _ins, kw = _prepare_run_kwargs(branch, "follow up", param)

    # The kw["messages"] are rendered chat dicts
    messages = kw["messages"]
    assistant_messages = [m for m in messages if m.get("role") == "assistant"]

    # Consecutive assistant messages collapsed into a single turn
    assert len(assistant_messages) == 1
    assert "first answer" in assistant_messages[0]["content"]
    assert "second answer" in assistant_messages[0]["content"]


def test_prepare_run_kwargs_returns_instruction_object():
    """The first return value is an Instruction instance."""
    from lionagi.protocols.messages import Instruction

    branch = Branch()
    param = ChatParam()

    ins, _kw = _prepare_run_kwargs(branch, "hello", param)

    assert isinstance(ins, Instruction)


def test_prepare_run_kwargs_kw_contains_messages_key():
    """The second return value is a dict with a 'messages' key."""
    branch = Branch()
    param = ChatParam()

    _ins, kw = _prepare_run_kwargs(branch, "hello", param)

    assert "messages" in kw
    assert isinstance(kw["messages"], list)
