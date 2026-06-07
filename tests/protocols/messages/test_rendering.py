# Copyright (c) 2025 - 2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for custom render/parser protocols (Phase 1 — #1092).

Covers:
- StructureFormat enum
- CustomRenderer / CustomParser protocols (structural typing)
- validate_image_url security guards
- InstructionContent.with_updates / .render / .create additions
- ActionRequestContent.render_compact / .render additions
- ActionResponseContent.render_summary / .error / .success additions
- prepare_messages_for_chat pipeline basics
"""

from typing import Any

import pytest
from pydantic import BaseModel

from lionagi.protocols.messages import (
    ActionRequest,
    ActionRequestContent,
    ActionResponse,
    ActionResponseContent,
    AssistantResponse,
    AssistantResponseContent,
    Instruction,
    InstructionContent,
    prepare_messages_for_chat,
)
from lionagi.protocols.messages.rendering import CustomParser, CustomRenderer, StructureFormat
from lionagi.protocols.messages.validators import validate_image_url


@pytest.fixture(autouse=True)
def _allow_public_image_hosts(monkeypatch):
    """Stub is_ssrf_safe -> True so example.com URLs validate without live DNS.

    SSRF rejection is covered deterministically (IP literals) in
    test_instruction_url_security; here we only test scheme/format/structure.
    """
    monkeypatch.setattr("lionagi.protocols.messages.validators.is_ssrf_safe", lambda host: True)


# ---------------------------------------------------------------------------
# StructureFormat
# ---------------------------------------------------------------------------


class TestStructureFormat:
    def test_enum_values(self):
        assert StructureFormat.JSON.value == "json"
        assert StructureFormat.CUSTOM.value == "custom"
        assert StructureFormat.LNDL.value == "lndl"

    def test_three_members(self):
        assert len(StructureFormat) == 3


# ---------------------------------------------------------------------------
# CustomRenderer protocol
# ---------------------------------------------------------------------------


class TestCustomRendererProtocol:
    def test_callable_that_returns_str_satisfies_protocol(self):
        """Any callable(model, **kwargs) -> str satisfies CustomRenderer."""

        def my_renderer(model: type[BaseModel], **kwargs: Any) -> str:
            return f"rendered:{model.__name__}"

        assert isinstance(my_renderer, CustomRenderer)

    def test_class_with_call_satisfies_protocol(self):
        class UpperCaseRenderer:
            def __call__(self, model: type[BaseModel], **kwargs: Any) -> str:
                return model.__name__.upper()

        assert isinstance(UpperCaseRenderer(), CustomRenderer)

    def test_non_callable_does_not_satisfy(self):
        """Non-callables don't satisfy CustomRenderer."""
        assert not isinstance("not a callable", CustomRenderer)
        assert not isinstance(42, CustomRenderer)
        assert not isinstance(None, CustomRenderer)


# ---------------------------------------------------------------------------
# CustomParser protocol
# ---------------------------------------------------------------------------


class TestCustomParserProtocol:
    def test_callable_satisfies_protocol(self):
        def my_parser(text: str, target_keys: list[str], **kwargs: Any) -> dict[str, Any]:
            return {k: text for k in target_keys}

        assert isinstance(my_parser, CustomParser)

    def test_class_with_call_satisfies_protocol(self):
        class UpperCaseParser:
            def __call__(self, text: str, target_keys: list[str], **kwargs: Any) -> dict[str, Any]:
                return {k: text.upper() for k in target_keys}

        assert isinstance(UpperCaseParser(), CustomParser)


# ---------------------------------------------------------------------------
# validate_image_url
# ---------------------------------------------------------------------------


class TestValidateImageUrl:
    def test_valid_https_url(self):
        validate_image_url("https://example.com/image.png")  # no raise

    def test_valid_http_url(self):
        validate_image_url("http://example.com/image.jpg")  # no raise

    def test_rejects_empty_string(self):
        with pytest.raises(ValueError, match="non-empty string"):
            validate_image_url("")

    def test_rejects_none(self):
        with pytest.raises((ValueError, AttributeError)):
            validate_image_url(None)  # type: ignore[arg-type]

    def test_rejects_file_scheme(self):
        with pytest.raises(ValueError, match="http"):
            validate_image_url("file:///etc/passwd")

    def test_rejects_javascript_scheme(self):
        with pytest.raises(ValueError, match="http"):
            validate_image_url("javascript:alert('xss')")

    def test_rejects_data_scheme(self):
        with pytest.raises(ValueError, match="http"):
            validate_image_url("data:image/png;base64,abc123")

    def test_rejects_null_byte(self):
        with pytest.raises(ValueError, match="null byte"):
            validate_image_url("https://example.com/img\x00.png")

    def test_rejects_percent_encoded_null(self):
        with pytest.raises(ValueError, match="null byte"):
            validate_image_url("https://example.com/img%00.png")

    def test_rejects_missing_domain(self):
        with pytest.raises(ValueError, match="missing domain"):
            validate_image_url("https://")


# ---------------------------------------------------------------------------
# InstructionContent additions
# ---------------------------------------------------------------------------


class TestInstructionContentAdditions:
    def test_instruction_field(self):
        c = InstructionContent(instruction="hello world")
        assert c.instruction == "hello world"

    def test_structure_stored(self):
        c = InstructionContent(instruction="x", structure="json")
        assert c.structure is not None

    def test_role_property(self):
        from lionagi.protocols.messages.message import MessageRole

        c = InstructionContent(instruction="x")
        assert c.role == MessageRole.USER

    def test_with_updates_changes_instruction(self):
        c = InstructionContent(instruction="original")
        c2 = c.with_updates(instruction="updated")
        assert c2.instruction == "updated"
        assert c.instruction == "original"  # original unchanged

    def test_with_updates_prompt_context(self):
        c = InstructionContent(instruction="x")
        c2 = c.with_updates(prompt_context=["ctx item"])
        assert "ctx item" in c2.prompt_context

    def test_rendered_property(self):
        c = InstructionContent(instruction="hello")
        result = c.rendered
        assert "hello" in result


# ---------------------------------------------------------------------------
# ActionRequestContent additions
# ---------------------------------------------------------------------------


class TestActionRequestContentAdditions:
    def test_render_delegates_to_rendered(self):
        c = ActionRequestContent(function="my_fn", arguments={"a": 1})
        assert c.render() == c.rendered

    def test_render_compact_no_args(self):
        c = ActionRequestContent(function="search")
        assert c.render_compact() == "search()"

    def test_render_compact_with_args(self):
        c = ActionRequestContent(function="read", arguments={"path": "/tmp/x"})
        result = c.render_compact()
        assert result.startswith("read(")
        assert "path=" in result

    def test_role_property(self):
        from lionagi.protocols.messages.message import MessageRole

        c = ActionRequestContent(function="f")
        assert c.role == MessageRole.ACTION


# ---------------------------------------------------------------------------
# ActionResponseContent additions
# ---------------------------------------------------------------------------


class TestActionResponseContentAdditions:
    def test_render_delegates_to_rendered(self):
        c = ActionResponseContent(function="f", output="result text")
        assert c.render() == c.rendered

    def test_success_when_no_error(self):
        c = ActionResponseContent(function="f", output="ok")
        assert c.success is True

    def test_success_false_when_error(self):
        c = ActionResponseContent(function="f", error="something went wrong")
        assert c.success is False

    def test_render_summary_success_string(self):
        c = ActionResponseContent(function="f", output="hello")
        assert c.render_summary() == "hello"

    def test_render_summary_success_none(self):
        c = ActionResponseContent(function="f", output=None)
        assert c.render_summary() == "ok"

    def test_render_summary_error(self):
        c = ActionResponseContent(function="f", error="timeout")
        assert "error" in c.render_summary()
        assert "timeout" in c.render_summary()

    def test_render_summary_dict_output(self):
        c = ActionResponseContent(function="f", output={"key": "val"})
        summary = c.render_summary()
        assert "key" in summary

    def test_result_alias(self):
        c = ActionResponseContent(function="f", output="data")
        assert c.result == "data"

    def test_request_id_alias(self):
        c = ActionResponseContent(function="f", action_request_id="req-123")
        assert c.request_id == "req-123"

    def test_role_property(self):
        from lionagi.protocols.messages.message import MessageRole

        c = ActionResponseContent(function="f")
        assert c.role == MessageRole.ACTION


# ---------------------------------------------------------------------------
# AssistantResponseContent additions
# ---------------------------------------------------------------------------


class TestAssistantResponseContentAdditions:
    def test_render_delegates_to_rendered(self):
        c = AssistantResponseContent(assistant_response="hello")
        assert c.render() == "hello"

    def test_role_property(self):
        from lionagi.protocols.messages.message import MessageRole

        c = AssistantResponseContent(assistant_response="hi")
        assert c.role == MessageRole.ASSISTANT

    def test_response_alias(self):
        c = AssistantResponseContent(assistant_response="foo")
        assert c.response == "foo"


# ---------------------------------------------------------------------------
# prepare_messages_for_chat basics
# ---------------------------------------------------------------------------


class TestPrepareMessagesForChat:
    def _make_instruction_msg(self, text: str) -> "Instruction":
        return Instruction(content=InstructionContent(instruction=text))

    def _make_assistant_msg(self, text: str) -> "AssistantResponse":
        return AssistantResponse(content=AssistantResponseContent(assistant_response=text))

    def test_empty_pile_no_new_instruction_returns_empty(self):
        from lionagi.protocols.generic.pile import Pile

        pile: Pile = Pile()
        result = prepare_messages_for_chat(pile)
        assert result == []

    def test_single_instruction_in_pile(self):
        from lionagi.protocols.generic.pile import Pile

        msg = self._make_instruction_msg("do something")
        pile: Pile = Pile(collections=[msg])
        result = prepare_messages_for_chat(pile)
        assert len(result) == 1
        assert isinstance(result[0], dict)
        assert result[0]["role"] == "user"
        assert "do something" in result[0]["content"]

    def test_new_instruction_only(self):
        from lionagi.protocols.generic.pile import Pile

        pile: Pile = Pile()
        new_msg = InstructionContent(instruction="brand new")
        result = prepare_messages_for_chat(pile, new_instruction=new_msg)
        assert len(result) == 1
        assert "brand new" in result[0]["content"]

    def test_to_chat_false_returns_content_objects(self):
        from lionagi.protocols.generic.pile import Pile

        msg = self._make_instruction_msg("test")
        pile: Pile = Pile(collections=[msg])
        result = prepare_messages_for_chat(pile, to_chat=False)
        assert len(result) == 1
        assert isinstance(result[0], InstructionContent)

    def test_system_message_embedded_into_first_instruction(self):
        """System content should be prepended to the first instruction text."""
        from lionagi.protocols.generic.pile import Pile
        from lionagi.protocols.messages import System, SystemContent

        sys_msg = System(content=SystemContent(system_message="You are helpful."))
        instr_msg = self._make_instruction_msg("Answer this.")
        pile: Pile = Pile(collections=[sys_msg, instr_msg])
        result = prepare_messages_for_chat(pile)
        # First result must be user turn with both system text and instruction
        combined_content = result[0]["content"]
        assert "You are helpful." in combined_content
        assert "Answer this." in combined_content

    def test_consecutive_assistant_responses_merged(self):
        """Consecutive AssistantResponse messages should be merged into one."""
        from lionagi.protocols.generic.pile import Pile

        instr = self._make_instruction_msg("ask")
        asst1 = self._make_assistant_msg("part one")
        asst2 = self._make_assistant_msg("part two")
        pile: Pile = Pile(collections=[instr, asst1, asst2])
        result = prepare_messages_for_chat(pile)
        # Should have instruction + one merged assistant
        assert len(result) == 2
        asst_entry = next(r for r in result if r["role"] == "assistant")
        assert "part one" in asst_entry["content"]
        assert "part two" in asst_entry["content"]

    def test_system_prefix_prepended(self):
        from lionagi.protocols.generic.pile import Pile

        msg = self._make_instruction_msg("main text")
        pile: Pile = Pile(collections=[msg])
        result = prepare_messages_for_chat(pile, system_prefix="## PREFIX")
        assert "## PREFIX" in result[0]["content"]
        assert "main text" in result[0]["content"]
