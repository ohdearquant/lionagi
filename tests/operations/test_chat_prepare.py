# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for lionagi/operations/chat/_prepare.py::_prepare_run_kwargs."""

import re

import pytest
from pydantic import BaseModel

from lionagi._errors import EmptyOutgoingContentError
from lionagi.operations.chat._prepare import _prepare_run_kwargs
from lionagi.operations.types import ChatParam
from lionagi.protocols.messages.assistant_response import AssistantResponseContent
from lionagi.protocols.messages.instruction import InstructionContent
from lionagi.providers.openai.codex import CodexCodeRequest
from lionagi.session.branch import Branch
from lionagi.tools.context.context import ContextTool

# _prepare_run_kwargs merges consecutive AssistantResponse messages


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


async def test_prepare_run_kwargs_accepts_compaction_of_earliest_instruction():
    branch = Branch(system="System policy")
    branch.msgs.add_message(instruction="old task")
    branch.msgs.add_message(assistant_response="old analysis")
    branch.msgs.add_message(instruction="current task")
    branch.msgs.add_message(assistant_response="current analysis")

    context_tool = ContextTool().bind(branch).func_callable
    compacted = await context_tool(
        action="compact",
        start=1,
        end=3,
        mode="all",
        summary="The earlier task and response were superseded.",
    )

    assert compacted["success"] is True
    _ins, kwargs = _prepare_run_kwargs(branch, "follow up", ChatParam())
    assert "old task" in kwargs["messages"][0]["content"]
    assert "CONTEXT COMPACTION" in kwargs["messages"][1]["content"]
    assert "current task" in kwargs["messages"][2]["content"]
    prompt = CodexCodeRequest.model_validate({"messages": kwargs["messages"]}).prompt
    positions = [
        prompt.index("old task"),
        prompt.index("CONTEXT COMPACTION"),
        prompt.index("current task"),
        prompt.index("follow up"),
    ]
    assert positions == sorted(positions)


async def test_prepare_run_kwargs_accepts_default_compaction_starting_on_an_instruction():
    """The default mode keeps instructions; its summary must not land ahead of one."""
    from lionagi.protocols.messages import ActionRequest, ActionResponse

    branch = Branch(system="System policy")
    branch.msgs.add_message(instruction="read the file")
    branch.msgs.add_message(assistant_response="reading it")
    request = ActionRequest(content={"function": "read_file", "arguments": {"path": "f.py"}})
    branch.msgs.messages.include(request)
    branch.msgs.progression.include(request.id)
    response = ActionResponse(content={"function": "read_file", "arguments": {}, "output": "X"})
    branch.msgs.messages.include(response)
    branch.msgs.progression.include(response.id)

    context_tool = ContextTool().bind(branch).func_callable
    compacted = await context_tool(
        action="compact", start=1, summary="f.py read; nothing to change."
    )

    assert compacted["success"] is True
    _ins, kwargs = _prepare_run_kwargs(branch, "follow up", ChatParam())
    assert "read the file" in kwargs["messages"][0]["content"]


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


def test_prepare_run_kwargs_drops_whitespace_only_history_message():
    """A reply of only whitespace (a model cut at max_tokens mid-run of newlines) is not
    sent back: providers that refuse whitespace-only text would reject every later call."""
    branch = Branch()
    param = ChatParam()

    branch.msgs.add_message(instruction="initial question")
    branch.msgs.add_message(assistant_response="\n" * 500)

    _ins, kw = _prepare_run_kwargs(branch, "follow up", param)

    assert all(m["content"].strip() for m in kw["messages"])
    assert not any(m["role"] == "assistant" for m in kw["messages"])
    assert "follow up" in kw["messages"][-1]["content"]


def test_prepare_run_kwargs_raises_when_real_instruction_renders_whitespace_only(monkeypatch):
    """The empty-render guard also covers a current turn that renders only whitespace."""
    branch = Branch()
    param = ChatParam()

    monkeypatch.setattr(InstructionContent, "rendered", property(lambda self: " \n\t\n"))

    with pytest.raises(EmptyOutgoingContentError, match="instruction_len="):
        _prepare_run_kwargs(branch, "a real, non-empty prompt", param)


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


# The premise the guard rests on: the current turn is the LAST assembled entry.
#
# The guard reads the current turn's render by index, which is only the current
# turn's render because every assembly branch appends the current turn last. The
# tests above cannot see a violation of that premise: they patch renders and
# observe the guard, and both the by-index read and a bare after-the-loop read
# agree as long as the premise holds. So a future edit that appends anything
# after the current turn would leave those tests green while the guard silently
# started inspecting the wrong entry — and in the direction that matters, a
# genuinely dropped instruction going unnoticed.
#
# These pin the premise itself, one per assembly branch, through the public
# return: with every entry rendering normally, the last outgoing message is the
# current turn's.


def test_current_turn_is_last_message_without_system():
    """Plain-append branch: no system message, so the current turn is appended
    directly onto whatever history produced."""
    branch = Branch()
    param = ChatParam()

    branch.msgs.add_message(instruction="an earlier question")
    branch.msgs.add_message(assistant_response="an earlier answer")

    _ins, kw = _prepare_run_kwargs(branch, "the current prompt", param)

    assert "the current prompt" in kw["messages"][-1]["content"]


def test_current_turn_is_last_message_with_system():
    """Guidance-merge branch: a system message rewrites the first entry's
    guidance and then appends the current turn, which must still end up last."""
    branch = Branch(system="you are a helpful assistant")
    param = ChatParam()

    branch.msgs.add_message(instruction="an earlier question")
    branch.msgs.add_message(assistant_response="an earlier answer")

    _ins, kw = _prepare_run_kwargs(branch, "the current prompt", param)

    assert "the current prompt" in kw["messages"][-1]["content"]


def test_current_turn_is_last_message_with_trailing_action_context():
    """Action-context branch: trailing action responses are folded into the
    current turn's prompt context rather than appended as their own entry, so
    the current turn must still be last and must carry the action output."""
    from lionagi.protocols.messages import ActionRequest

    branch = Branch()
    param = ChatParam()

    branch.msgs.add_message(instruction="an earlier question")
    request = ActionRequest(
        content={"function": "lookup", "arguments": {"key": "k"}},
        sender="x",
        recipient="user",
    )
    branch.msgs.add_message(action_request=request)
    branch.msgs.add_message(
        action_request=request,
        action_output={"value": "a distinctive action result"},
    )

    _ins, kw = _prepare_run_kwargs(branch, "the current prompt", param)

    last = kw["messages"][-1]["content"]
    assert "the current prompt" in last
    assert "a distinctive action result" in last


# The prompt-caching option (ADR-0127 D3): the system text becomes its own block


class _Endpoint:
    def __init__(self, places_prompt_cache):
        self.places_prompt_cache = places_prompt_cache


class _Model:
    def __init__(self, places_prompt_cache):
        self.endpoint = _Endpoint(places_prompt_cache)


def test_a_caching_endpoint_takes_the_system_text_as_a_stable_block():
    branch = Branch(system="you are a careful editor")
    branch.msgs.add_message(instruction="an earlier question")
    branch.msgs.add_message(assistant_response="an earlier answer")

    _ins, kw = _prepare_run_kwargs(branch, "the current prompt", ChatParam(), imodel=_Model(True))

    assert kw["system_blocks"] == [branch.msgs.system.rendered]
    assert all(
        "you are a careful editor" not in str(message["content"]) for message in kw["messages"]
    )
    assert "an earlier question" in str(kw["messages"][0]["content"])
    assert "the current prompt" in str(kw["messages"][-1]["content"])


@pytest.mark.parametrize("imodel", [None, _Model(False)], ids=["no-model", "not-declared"])
def test_without_the_placement_the_system_text_is_folded_as_before(imodel):
    branch = Branch(system="you are a careful editor")
    branch.msgs.add_message(instruction="an earlier question")
    branch.msgs.add_message(assistant_response="an earlier answer")

    _ins, kw = _prepare_run_kwargs(branch, "the current prompt", ChatParam(), imodel=imodel)
    _ins, plain = _prepare_run_kwargs(branch, "the current prompt", ChatParam())

    assert "system_blocks" not in kw
    assert "you are a careful editor" in str(kw["messages"][0]["content"])
    assert kw == plain


def test_a_caching_endpoint_without_a_system_message_gets_no_stable_block():
    branch = Branch()

    _ins, kw = _prepare_run_kwargs(branch, "hello", ChatParam(), imodel=_Model(True))

    assert "system_blocks" not in kw


# The prompt-caching option (ADR-0127 D2): the tool set renders once, in the system block


def lookup_order(order_id: str) -> str:
    """Find an order by its id."""
    return order_id


UNREGISTERED = {
    "type": "function",
    "function": {
        "name": "unregistered_lookup",
        "description": "A schema the Branch never registered.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}


class _Verdict(BaseModel):
    approved: bool


def _branch_with_a_tool(system="you are a careful editor"):
    branch = Branch(system=system) if system else Branch()
    branch.register_tools(lookup_order)
    return branch


def _tool_section(branch):
    from lionagi.libs.schema.minimal_yaml import minimal_yaml

    return minimal_yaml({"Tools": branch.acts.schema_list}).strip()


def test_a_caching_endpoint_takes_the_tool_set_after_the_system_text():
    branch = _branch_with_a_tool()

    _ins, kw = _prepare_run_kwargs(branch, "the current prompt", ChatParam(), imodel=_Model(True))

    assert kw["system_blocks"] == [branch.msgs.system.rendered, _tool_section(branch)]
    assert "Find an order by its id." in kw["system_blocks"][1]
    assert all("Find an order by its id." not in str(m["content"]) for m in kw["messages"])


def test_a_tool_set_without_a_system_message_is_the_only_stable_block():
    branch = _branch_with_a_tool(system=None)

    _ins, kw = _prepare_run_kwargs(branch, "hello", ChatParam(), imodel=_Model(True))

    assert kw["system_blocks"] == [_tool_section(branch)]


def test_an_instruction_names_its_registered_tools_and_keeps_any_other_schema_whole():
    branch = _branch_with_a_tool()
    schemas = [*branch.acts.schema_list, UNREGISTERED]

    _ins, kw = _prepare_run_kwargs(
        branch, "the current prompt", ChatParam(tool_schemas=schemas), imodel=_Model(True)
    )

    turn = str(kw["messages"][-1]["content"])
    assert "- lookup_order" in turn
    assert "Find an order by its id." not in turn
    assert "A schema the Branch never registered." in turn


def _two_turns(imodel, **param):
    branch = _branch_with_a_tool()
    first_param = ChatParam(tool_schemas=branch.acts.schema_list, **param)
    ins, first = _prepare_run_kwargs(branch, "the first prompt", first_param, imodel=imodel)
    branch.msgs.add_message(instruction=ins)
    branch.msgs.add_message(assistant_response="an answer")
    _ins, second = _prepare_run_kwargs(branch, "the second prompt", ChatParam(), imodel=imodel)
    return first, second


def test_an_instruction_renders_the_same_bytes_current_and_in_history():
    first, second = _two_turns(_Model(True), response_format=_Verdict)

    assert second["messages"][: len(first["messages"])] == first["messages"]
    assert "- lookup_order" in str(first["messages"][0]["content"])
    assert "approved" in str(first["messages"][0]["content"])
    assert first["system_blocks"] == second["system_blocks"]


def test_without_the_placement_history_drops_the_tools_and_the_format_as_before():
    first, second = _two_turns(None, response_format=_Verdict)

    assert "Find an order by its id." in str(first["messages"][0]["content"])
    assert "approved" in str(first["messages"][0]["content"])
    assert "lookup_order" not in str(second["messages"][0]["content"])
    assert "approved" not in str(second["messages"][0]["content"])


# The prompt-caching option (ADR-0127 Amendment 2): a turn's context blocks stay with that turn


def _turn(branch, prompt, *, blocks=None, imodel=None):
    ins, kw = _prepare_run_kwargs(
        branch, prompt, ChatParam(), imodel=imodel or _Model(True), context_blocks=blocks
    )
    branch.msgs.add_message(instruction=ins)
    branch.msgs.add_message(assistant_response=f"answer to {prompt}")
    return ins, kw


def test_a_caching_endpoint_sends_first_turn_context_after_the_task_on_every_request():
    branch = Branch(system="you are a careful editor")

    ins, first = _turn(branch, "the task", blocks=["RECALLED"])
    _ins, second = _prepare_run_kwargs(branch, "next", ChatParam(), imodel=_Model(True))

    task, context = first["messages"][0]["content"]
    assert "the task" in task["text"] and "RECALLED" not in task["text"]
    assert context == {"type": "text", "text": "RECALLED"}
    assert second["messages"][0] == first["messages"][0]
    assert ins.metadata["context_blocks"] == ["RECALLED"]


def test_a_later_turns_context_stays_at_its_turn_and_leaves_the_task_unchanged():
    branch = Branch(system="you are a careful editor")

    _turn(branch, "the task", blocks=["FIRST"])
    _ins, second = _turn(branch, "a later prompt", blocks=["LATER"])
    _ins, third = _prepare_run_kwargs(branch, "next", ChatParam(), imodel=_Model(True))

    assert second["messages"][-1]["content"][-1] == {"type": "text", "text": "LATER"}
    assert third["messages"][: len(second["messages"])] == second["messages"]
    assert "LATER" not in str(third["messages"][0]["content"])


def test_a_repeated_gather_for_one_instruction_keeps_the_first_blocks():
    branch = Branch(system="you are a careful editor")
    ins, _kw = _prepare_run_kwargs(
        branch, "the task", ChatParam(), imodel=_Model(True), context_blocks=["FIRST"]
    )

    _ins, again = _prepare_run_kwargs(
        branch, None, ChatParam(), ins=ins, imodel=_Model(True), context_blocks=["SECOND"]
    )

    assert ins.metadata["context_blocks"] == ["FIRST"]
    assert "SECOND" not in str(again["messages"])


def test_kept_context_survives_a_serialized_round_trip():
    branch = Branch(system="you are a careful editor")
    ins, _kw = _turn(branch, "the task", blocks=["RECALLED"])

    restored = type(ins).from_dict(ins.to_dict())

    assert restored.metadata["context_blocks"] == ["RECALLED"]


def test_a_cloned_branch_sends_the_kept_context_at_its_turn():
    branch = Branch(system="you are a careful editor")
    _ins, first = _turn(branch, "the task", blocks=["RECALLED"])

    _ins, cloned = _prepare_run_kwargs(branch.clone(), "next", ChatParam(), imodel=_Model(True))

    assert cloned["messages"][0] == first["messages"][0]


def test_without_the_placement_context_blocks_are_shown_once_and_not_kept():
    branch = Branch(system="you are a careful editor")

    ins, first = _turn(branch, "the task", blocks=["RECALLED"], imodel=_Model(False))
    _ins, second = _prepare_run_kwargs(branch, "next", ChatParam(), imodel=_Model(False))

    assert "RECALLED" in first["messages"][0]["content"]
    assert "RECALLED" not in str(second["messages"])
    assert "context_blocks" not in ins.metadata


def test_uncached_blocks_only_with_native_empty_current_are_sent():
    from lionagi.protocols.messages import Instruction

    ins = Instruction(content=InstructionContent(), sender="user")
    original = ins.to_dict()

    _, request = _prepare_run_kwargs(
        Branch(), None, ChatParam(), ins=ins, imodel=_Model(False), context_blocks=["BLOCK"]
    )

    assert len(request["messages"]) == 1
    assert request["messages"][0]["role"] == "user"
    assert str(request["messages"]).count("BLOCK") == 1
    assert ins.to_dict() == original


def test_uncached_blocks_only_request_without_system_is_sent(monkeypatch):
    from lionagi.protocols.messages import Instruction

    branch = Branch()
    ins = Instruction(content=InstructionContent(), sender="user")
    render = InstructionContent.rendered.fget
    monkeypatch.setattr(
        InstructionContent,
        "rendered",
        property(
            lambda content: (
                "" if not content.instruction and not content.guidance else render(content)
            )
        ),
    )

    _, request = _prepare_run_kwargs(
        branch,
        None,
        ChatParam(),
        ins=ins,
        imodel=_Model(False),
        context_blocks=["BLOCK-ONE", "BLOCK-TWO"],
    )

    assert request["messages"] == [{"role": "user", "content": "BLOCK-ONE\nBLOCK-TWO"}]
    assert "context_blocks" not in ins.metadata
    assert ins.content.plain_content is None


def test_uncached_blocks_with_system_and_empty_plain_current_are_sent_once():
    from lionagi.protocols.messages import Instruction

    branch = Branch(system="SYSTEM-POLICY")
    ins = Instruction(content=InstructionContent(plain_content=""), sender="user")
    original = ins.to_dict()

    _, request = _prepare_run_kwargs(
        branch, None, ChatParam(), ins=ins, imodel=_Model(False), context_blocks=["BLOCK"]
    )

    assert len(request["messages"]) == 1
    assert request["messages"][0]["role"] == "user"
    assert str(request["messages"]).count("BLOCK") == 1
    assert "SYSTEM-POLICY" in str(request["messages"])
    assert ins.to_dict() == original


def test_uncached_blocks_with_empty_current_stay_on_prior_instruction(monkeypatch):
    from lionagi.protocols.messages import Instruction

    branch = Branch()
    branch.msgs.add_message(instruction="PREVIOUS-INSTRUCTION")
    branch.msgs.add_message(assistant_response="PREVIOUS-RESPONSE")
    ins = Instruction(content=InstructionContent(), sender="user")
    render = InstructionContent.rendered.fget
    monkeypatch.setattr(
        InstructionContent,
        "rendered",
        property(
            lambda content: (
                "" if not content.instruction and not content.guidance else render(content)
            )
        ),
    )

    _, request = _prepare_run_kwargs(
        branch, None, ChatParam(), ins=ins, imodel=_Model(False), context_blocks=["BLOCK"]
    )

    assert len(request["messages"]) == 2
    assert "PREVIOUS-INSTRUCTION" in request["messages"][0]["content"]
    assert "BLOCK" in request["messages"][0]["content"]
    assert str(request["messages"]).count("BLOCK") == 1
    assert request["messages"][1]["role"] == "assistant"
    assert "context_blocks" not in ins.metadata


def test_uncached_blocks_move_past_an_empty_system_plain_instruction():
    from lionagi.protocols.messages import Instruction

    branch = Branch(system="SYSTEM-POLICY")
    earlier = Instruction(content={"plain_content": " \t "}, sender="user")
    branch.msgs.add_message(instruction=earlier)
    branch.msgs.add_message(assistant_response="PREVIOUS-RESPONSE")

    _, request = _prepare_run_kwargs(
        branch, "CURRENT-TASK", ChatParam(), imodel=_Model(False), context_blocks=["BLOCK"]
    )

    assert len(request["messages"]) == 2
    assert request["messages"][0]["role"] == "assistant"
    assert request["messages"][1]["role"] == "user"
    assert "CURRENT-TASK" in request["messages"][1]["content"]
    assert "BLOCK" in request["messages"][1]["content"]
    assert str(request["messages"]).count("BLOCK") == 1


@pytest.mark.parametrize("with_system", [False, True], ids=["no-system", "system"])
@pytest.mark.parametrize("with_history", [False, True], ids=["no-history", "history"])
def test_uncached_context_blocks_are_sent_once_in_first_instruction(with_system, with_history):
    branch = Branch(system="SYSTEM-POLICY") if with_system else Branch()
    if with_history:
        branch.msgs.add_message(instruction="EARLIER-INSTRUCTION")
        branch.msgs.add_message(assistant_response="EARLIER-RESPONSE")

    ins, request = _prepare_run_kwargs(
        branch,
        "CURRENT-INSTRUCTION",
        ChatParam(),
        imodel=_Model(False),
        context_blocks=["CONTEXT-BLOCK-1", "CONTEXT-BLOCK-2"],
    )

    messages = request["messages"]
    assert messages[0]["role"] == "user"
    first = str(messages[0]["content"])
    assert "CONTEXT-BLOCK-1" in first
    assert "CONTEXT-BLOCK-2" in first
    assert str(messages).count("CONTEXT-BLOCK-1") == 1
    assert str(messages).count("CONTEXT-BLOCK-2") == 1
    assert "CURRENT-INSTRUCTION" in str(messages[-1]["content"])
    if with_history:
        assert "EARLIER-INSTRUCTION" in first
        assert "EARLIER-RESPONSE" in str(messages)
    if with_system:
        assert "SYSTEM-POLICY" in first
    assert "context_blocks" not in ins.metadata


@pytest.mark.parametrize("with_system", [False, True], ids=["no-system", "system"])
def test_uncached_context_blocks_do_not_leak_into_later_turns(with_system):
    branch = Branch(system="SYSTEM-POLICY") if with_system else Branch()
    first_ins, first = _prepare_run_kwargs(
        branch, "FIRST-TASK", ChatParam(), imodel=_Model(False), context_blocks=["FIRST-BLOCK"]
    )
    branch.msgs.add_message(instruction=first_ins)
    branch.msgs.add_message(assistant_response="FIRST-ANSWER")

    second_ins, second = _prepare_run_kwargs(
        branch, "SECOND-TASK", ChatParam(), imodel=_Model(False), context_blocks=["SECOND-BLOCK"]
    )
    branch.msgs.add_message(instruction=second_ins)
    branch.msgs.add_message(assistant_response="SECOND-ANSWER")
    _, third = _prepare_run_kwargs(branch, "THIRD-TASK", ChatParam(), imodel=_Model(False))

    assert "FIRST-BLOCK" in str(first["messages"][0]["content"])
    assert "FIRST-BLOCK" not in str(second["messages"])
    assert "SECOND-BLOCK" in str(second["messages"][0]["content"])
    assert "FIRST-BLOCK" not in str(third["messages"])
    assert "SECOND-BLOCK" not in str(third["messages"])
    assert "context_blocks" not in first_ins.metadata
    assert "context_blocks" not in second_ins.metadata
    assert "FIRST-TASK" in str(third["messages"])
    assert "SECOND-TASK" in str(third["messages"])


@pytest.mark.parametrize("with_system", [False, True], ids=["no-system", "system"])
@pytest.mark.parametrize("with_history", [False, True], ids=["current", "historical"])
@pytest.mark.parametrize("with_image", [False, True], ids=["text", "image"])
def test_uncached_context_blocks_with_plain_content_preserve_rendering(
    with_system, with_history, with_image
):
    from lionagi.protocols.messages import Instruction

    branch = Branch(system="SYSTEM-POLICY") if with_system else Branch()
    content = {"plain_content": "PLAIN-TEXT"}
    if with_image:
        content.update(images=["data:image/png;base64,YWJj"], image_detail="high")
    ins = Instruction(content=content, sender="user", recipient="assistant")
    original = ins.to_dict()
    if with_history:
        branch.msgs.add_message(instruction=ins)
        branch.msgs.add_message(assistant_response="EARLIER-RESPONSE")
        _, request = _prepare_run_kwargs(
            branch, "CURRENT-TASK", ChatParam(), imodel=_Model(False), context_blocks=["BLOCK"]
        )
        assert "CURRENT-TASK" in str(request["messages"][-1]["content"])
    else:
        _, request = _prepare_run_kwargs(
            branch, None, ChatParam(), ins=ins, imodel=_Model(False), context_blocks=["BLOCK"]
        )

    first = str(request["messages"][0]["content"])
    assert "PLAIN-TEXT" in first
    assert "BLOCK" in first
    assert str(request["messages"]).count("BLOCK") == 1
    if with_image:
        assert "data:image/png;base64,YWJj" in first
        assert "high" in first
    assert ins.to_dict() == original


@pytest.mark.parametrize("with_system", [False, True], ids=["no-system", "system"])
def test_uncached_blocks_do_not_mask_empty_current_instruction(monkeypatch, with_system):
    branch = Branch(system="SYSTEM-POLICY") if with_system else Branch()
    branch.msgs.add_message(instruction="EARLIER-TASK")
    branch.msgs.add_message(assistant_response="EARLIER-RESPONSE")
    monkeypatch.setattr(InstructionContent, "rendered", property(lambda self: ""))

    with pytest.raises(EmptyOutgoingContentError, match="instruction_len="):
        _prepare_run_kwargs(
            branch, "CURRENT-TASK", ChatParam(), imodel=_Model(False), context_blocks=["BLOCK"]
        )


def test_uncached_blocks_follow_history_that_starts_with_an_assistant_message():
    branch = Branch()
    branch.msgs.add_message(assistant_response="PREVIOUS-ANSWER")

    _, request = _prepare_run_kwargs(
        branch, "CURRENT-TASK", ChatParam(), imodel=_Model(False), context_blocks=["BLOCK"]
    )

    messages = request["messages"]
    assert messages[0]["role"] == "assistant"
    assert "PREVIOUS-ANSWER" in str(messages[0]["content"])
    instructions = [message for message in messages if message["role"] == "user"]
    assert len(instructions) == 1
    assert "CURRENT-TASK" in str(instructions[0]["content"])
    assert "BLOCK" in str(instructions[0]["content"])
    assert "BLOCK" not in str(messages[0]["content"])


def test_uncached_blocks_preserve_structured_historical_guidance():
    from lionagi.protocols.messages import Instruction

    branch = Branch()
    historical = Instruction(
        content={"instruction": "EARLIER-TASK", "guidance": {"rule": "KEEP-GUIDANCE"}},
        sender="user",
        recipient="assistant",
    )
    original = historical.to_dict()
    branch.msgs.add_message(instruction=historical)
    branch.msgs.add_message(assistant_response="EARLIER-ANSWER")

    _, request = _prepare_run_kwargs(
        branch,
        "CURRENT-TASK",
        ChatParam(),
        imodel=_Model(False),
        context_blocks=["BLOCK-ONE", "BLOCK-TWO"],
    )

    first = str(request["messages"][0]["content"])
    assert "EARLIER-TASK" in first
    assert "KEEP-GUIDANCE" in first
    assert re.search(r"BLOCK-ONE\s*\n\s*BLOCK-TWO\s*\n\s*rule:\s*KEEP-GUIDANCE", first)
    assert historical.to_dict() == original


def test_a_history_instruction_names_a_tool_registered_after_it_was_sent():
    branch = Branch(system="you are a careful editor")
    late = {
        "type": "function",
        "function": {
            "name": "lookup_order",
            "description": "Find an order by its id.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    }
    # Not the first instruction: the first is rebuilt every call, a later one renders through
    # the message's render cache, which this pins.
    branch.msgs.add_message(instruction="the opening question")
    branch.msgs.add_message(assistant_response="the opening answer")
    branch.msgs.add_message(instruction="an earlier question", tool_schemas=[late])
    branch.msgs.add_message(assistant_response="an earlier answer")

    _ins, before = _prepare_run_kwargs(branch, "now", ChatParam(), imodel=_Model(True))
    branch.register_tools(lookup_order)
    _ins, after = _prepare_run_kwargs(branch, "now", ChatParam(), imodel=_Model(True))
    _ins, uncached = _prepare_run_kwargs(
        branch, "now", ChatParam(), imodel=_Model(True), _use_render_cache=False
    )

    assert "Find an order by its id." in str(before["messages"][2]["content"])
    assert "Find an order by its id." not in str(after["messages"][2]["content"])
    assert "- lookup_order" in str(after["messages"][2]["content"])
    assert after["messages"] == uncached["messages"]


@pytest.mark.asyncio
async def test_bound_projects_both_result_collection_seams_with_one_source_session(monkeypatch):
    import json

    from lionagi.operations.chat import _prepare
    from lionagi.protocols.messages import ActionResponse
    from lionagi.service.imodel import iModel

    model = iModel(
        provider="anthropic",
        model="claude-haiku-5-5",
        api_key="test-not-a-key",
        max_tokens=64,
        tool_result_max_chars=1024,
    )
    branch = Branch(system="Keep evidence.", chat_model=model, parse_model=model)
    await branch.msgs.a_add_message(instruction="first task")
    outputs = []
    for number in range(2):
        text = "head " * 250 + f"HIDDEN_MIDDLE_{number}" + "tail " * 20000
        result = ActionResponse(content={"function": "reader", "arguments": {}, "output": text})
        await branch.msgs._a_add_built_response(result)
        outputs.append(result)
        if number == 0:
            # A historical instruction is sent only after an assistant turn; adjacent
            # instructions are deliberately coalesced by the existing compiler.
            await branch.msgs.a_add_message(assistant_response="continue with the evidence")
            await branch.msgs.a_add_message(instruction="historical collection boundary")
    original = [result.to_dict() for result in outputs]
    progression = list(branch.progression)
    record_size = len(branch.msgs.messages)
    sessions = []
    real = _prepare._collect_action_dicts

    def observed(messages, projection=None):
        sessions.append(projection)
        return real(messages, projection)

    monkeypatch.setattr(_prepare, "_collect_action_dicts", observed)
    try:
        _instruction, request = _prepare_run_kwargs(
            branch, "current task", ChatParam(), imodel=model
        )
        payload, _headers = model.endpoint.create_payload(request=request)
        text = json.dumps(payload, ensure_ascii=False)
        assert len(sessions) == 2 and sessions[0] is not None and sessions[0] is sessions[1]
        assert text.count("chars omitted from") == 2
        assert all(f"HIDDEN_MIDDLE_{number}" not in text for number in range(2))
        assert [result.to_dict() for result in outputs] == original
        assert list(branch.progression) == progression
        assert len(branch.msgs.messages) == record_size
    finally:
        await branch.mdls.shutdown()
