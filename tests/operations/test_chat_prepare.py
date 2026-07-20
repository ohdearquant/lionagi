# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for lionagi/operations/chat/_prepare.py::_prepare_run_kwargs."""

import pytest

from lionagi._errors import EmptyOutgoingContentError
from lionagi.operations.chat._prepare import _prepare_run_kwargs
from lionagi.operations.types import ChatParam
from lionagi.protocols.messages.assistant_response import AssistantResponseContent
from lionagi.protocols.messages.instruction import InstructionContent
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


# ---------------------------------------------------------------------------
# Fail-loud guard (issue #2308): a non-empty instruction must never silently
# vanish from the outgoing message list.
# ---------------------------------------------------------------------------


def test_prepare_run_kwargs_raises_when_real_instruction_renders_empty(monkeypatch):
    """If the current turn carries real instruction text but its rendered
    form comes out empty (an assembly bug of any kind — this guard doesn't
    care which), the call must fail loudly instead of silently sending the
    model scaffolding-only content and completing with a useless reply."""
    branch = Branch()
    param = ChatParam()

    monkeypatch.setattr(InstructionContent, "rendered", property(lambda self: ""))

    with pytest.raises(EmptyOutgoingContentError, match="instruction_len=") as excinfo:
        _prepare_run_kwargs(branch, "a real, non-empty prompt", param)

    # The prompt text itself must never appear in the exception message -
    # it gets serialized into persisted failure signals (RunFailed) and
    # must not carry caller-supplied content.
    assert "a real, non-empty prompt" not in str(excinfo.value)


def test_prepare_run_kwargs_guard_checks_current_turn_not_earlier_history(monkeypatch):
    """Multi-item case: history contains an earlier Instruction + a real,
    non-empty-rendering AssistantResponse, so the loop building `chat_msgs`
    passes through at least one non-empty render before it ever reaches the
    current turn's entry. Only InstructionContent is patched to render
    empty, so the earlier AssistantResponse entry still renders fine — the
    guard's input (the *current turn's* render) is therefore distinguishable
    from 'whatever the loop last computed while iterating history'. The
    guard must still fire because the current turn (always the last entry)
    is an Instruction and renders empty."""
    branch = Branch()
    param = ChatParam()

    branch.msgs.add_message(instruction="initial question")
    branch.msgs.add_message(assistant_response="a real, non-empty answer")

    monkeypatch.setattr(InstructionContent, "rendered", property(lambda self: ""))

    with pytest.raises(EmptyOutgoingContentError, match="instruction_len="):
        _prepare_run_kwargs(branch, "a real, non-empty prompt", param)


def test_prepare_run_kwargs_guard_ignores_earlier_history_render_state(monkeypatch):
    """Companion case: an earlier historical AssistantResponse renders empty,
    but the current turn's own Instruction is unpatched and renders
    normally. The guard must NOT fire — it must track the current turn's
    render specifically, not get confused by an unrelated empty render
    earlier in the same loop (the loop's ambient last-value would coincide
    here too, but only because the current turn happens to be last; this
    pins that the guard's read is not sensitive to what came before it)."""
    branch = Branch()
    param = ChatParam()

    branch.msgs.add_message(instruction="initial question")
    branch.msgs.add_message(assistant_response="a stale, now-empty-rendering answer")

    monkeypatch.setattr(AssistantResponseContent, "rendered", property(lambda self: ""))

    _ins, kw = _prepare_run_kwargs(branch, "a real, current prompt", param)
    messages = kw["messages"]
    assert any("a real, current prompt" in m["content"] for m in messages)


def test_prepare_run_kwargs_allows_empty_render_when_no_instruction_text(monkeypatch):
    """The guard is scoped to *real* instruction text — a turn with no
    instruction at all (e.g. a bare action-response continuation) that
    happens to render empty must not be treated as the bug this guards."""
    branch = Branch()
    param = ChatParam()

    monkeypatch.setattr(InstructionContent, "rendered", property(lambda self: ""))

    # No instruction text/plain_content/images supplied -> guard must not fire.
    _ins, kw = _prepare_run_kwargs(branch, None, param)
    assert kw["messages"] == []
