# Copyright (c) 2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Equivalence and invalidation coverage for the per-message render cache."""

import orjson
import pytest

from lionagi.operations.chat._prepare import _prepare_run_kwargs
from lionagi.operations.types import ChatParam
from lionagi.session.branch import Branch


def _build_history(size: int) -> Branch:
    branch = Branch(system="Follow the system policy.")
    branch.msgs.add_message(
        instruction="Describe the first artifact.",
        context=[{"source": "fixture", "values": [1, 2]}],
        images=["data:image/png;base64,aGVsbG8="],
    )
    branch.msgs.add_message(assistant_response="The first artifact is recorded.")
    request = branch.msgs.add_message(
        action_function="lookup", action_arguments={"record_id": "r-1"}
    )
    branch.msgs.add_message(action_request=request, action_output={"status": "found"})

    for index in range(size - len(branch.msgs.messages)):
        if index % 2 == 0:
            branch.msgs.add_message(instruction=f"Historic instruction {index}.")
        else:
            branch.msgs.add_message(assistant_response=f"Historic answer {index}.")

    assert len(branch.msgs.messages) == size
    return branch


def _chat_param(branch: Branch) -> ChatParam:
    return ChatParam(sender="user", recipient=branch.id, imodel=branch.chat_model, imodel_kw={})


@pytest.mark.parametrize("size", [10, 100])
def test_cached_preparation_is_byte_identical_to_uncached_history(size: int):
    branch = _build_history(size)
    param = _chat_param(branch)

    _, uncached = _prepare_run_kwargs(
        branch, "Current instruction.", param, _use_render_cache=False
    )
    _, cached = _prepare_run_kwargs(branch, "Current instruction.", param)
    _, warm_cached = _prepare_run_kwargs(branch, "Current instruction.", param)

    assert orjson.dumps(cached) == orjson.dumps(uncached)
    assert orjson.dumps(warm_cached) == orjson.dumps(uncached)

    uncached_messages = branch.msgs.to_chat_msgs(_use_render_cache=False)
    cached_messages = branch.msgs.to_chat_msgs()
    assert orjson.dumps(cached_messages) == orjson.dumps(uncached_messages)


def test_historic_in_place_mutation_invalidates_only_that_message_rendering():
    branch = _build_history(10)
    param = _chat_param(branch)
    historic_instructions = list(branch.msgs.instructions)
    changed = historic_instructions[2]
    unchanged = historic_instructions[3]

    _prepare_run_kwargs(branch, "Current instruction.", param)
    changed_before = changed._render_cache["prepared_instruction"]
    unchanged_before = unchanged._render_cache["prepared_instruction"]

    changed.content.instruction = "Edited historic instruction."
    _, prepared = _prepare_run_kwargs(branch, "Current instruction.", param)

    assert "Edited historic instruction." in orjson.dumps(prepared).decode()
    assert changed._render_cache["prepared_instruction"] is not changed_before
    assert unchanged._render_cache["prepared_instruction"] is unchanged_before


def test_content_object_replacement_invalidates_rendering():
    branch = Branch()
    message = branch.msgs.add_message(instruction="Original text.")

    assert "Original text." in branch.msgs.to_chat_msgs()[0]["content"]

    # update() swaps in a brand-new content object at revision zero; the
    # cache must key on the content object itself, not its address, so the
    # replacement can never be confused with a prior same-address content.
    message.update(instruction="Replaced text.")
    entry = message._render_cache["chat"]
    assert entry[0] is not message.content

    rendered = branch.msgs.to_chat_msgs()[0]["content"]
    assert "Replaced text." in rendered
    assert "Original text." not in rendered
    assert message._render_cache["chat"][0] is message.content


def test_render_cache_is_message_identity_scoped_across_branches():
    first = Branch()
    second = Branch()
    first_message = first.msgs.add_message(instruction="Same text.")
    second_message = second.msgs.add_message(instruction="Same text.")

    assert first.msgs.to_chat_msgs() == second.msgs.to_chat_msgs()
    second_before = second_message._render_cache["chat"]

    first_message.content.instruction = "First branch changed."
    assert "First branch changed." in first.msgs.to_chat_msgs()[0]["content"]
    assert second.msgs.to_chat_msgs()[0]["content"] != first.msgs.to_chat_msgs()[0]["content"]
    assert second_message._render_cache["chat"] is second_before


def test_response_format_structure_derives_from_tracked_copy():
    schema = {"answer": {"type": "string", "description": "before"}}
    branch = Branch()
    message = branch.msgs.add_message(instruction="historic", response_format=schema)
    branch.msgs.to_chat_msgs()

    # Mutating the caller's original dict is invisible: the structure was
    # built from the tracked copy, so cached and uncached renderings agree.
    schema["answer"]["description"] = "external-alias-edit"
    cached = branch.msgs.to_chat_msgs()
    uncached = branch.msgs.to_chat_msgs(_use_render_cache=False)
    assert orjson.dumps(cached) == orjson.dumps(uncached)
    assert "external-alias-edit" not in cached[0]["content"]

    # Mutating the tracked field advances the revision and stays in parity.
    revision = message.content._render_revision
    message.content.response_format["answer"]["description"] = "tracked-edit"
    assert message.content._render_revision > revision
    assert orjson.dumps(branch.msgs.to_chat_msgs()) == orjson.dumps(
        branch.msgs.to_chat_msgs(_use_render_cache=False)
    )


def test_message_deepcopy_pickle_and_prepare_roundtrip():
    import copy
    import pickle

    from lionagi.protocols.generic.pile import Pile
    from lionagi.protocols.messages.instruction import Instruction, InstructionContent
    from lionagi.protocols.messages.prepare import prepare_messages_for_chat

    content = InstructionContent(
        instruction="copy me",
        prompt_context=[{"nested": [1]}],
        tool_schemas=[{"name": "lookup"}],
        images=["data:image/png;base64,aGVsbG8="],
    )
    message = Instruction(content=content)

    duplicate = copy.deepcopy(message)
    assert duplicate.rendered == message.rendered

    restored = pickle.loads(pickle.dumps(message))
    assert restored.rendered == message.rendered

    assert prepare_messages_for_chat(Pile(), new_instruction=message) is not None

    # The copy tracks revisions independently of the original.
    revision = duplicate.content._render_revision
    duplicate.content.prompt_context.append({"more": [2]})
    assert duplicate.content._render_revision > revision
    assert "more" not in str(message.content.prompt_context)


@pytest.mark.parametrize("via", ["deepcopy", "pickle"])
def test_copied_dict_response_format_stays_wired_to_structure(via: str):
    import copy
    import pickle

    from lionagi.protocols.messages.instruction import Instruction, InstructionContent

    message = Instruction(
        content=InstructionContent(
            instruction="copy",
            response_format={"answer": {"description": "before"}},
        )
    )
    if via == "deepcopy":
        clone = copy.deepcopy(message)
    else:
        clone = pickle.loads(pickle.dumps(message))

    # The restored private structure must read the restored public field, so a
    # public mutation on the copy changes the rendered schema.
    assert clone.content._structure_instance.base_dict is clone.content.response_format
    revision = clone.content._render_revision
    clone.content.response_format["answer"]["description"] = "after"
    assert clone.content._render_revision > revision
    assert "after" in clone.rendered
    assert "before" not in clone.rendered
    assert "before" in message.rendered

    from lionagi.protocols.messages.manager import MessageManager

    manager = MessageManager(messages=[clone])
    cached = manager.to_chat_msgs()
    uncached = manager.to_chat_msgs(_use_render_cache=False)
    assert orjson.dumps(cached) == orjson.dumps(uncached)
    assert "after" in orjson.dumps(cached).decode()
