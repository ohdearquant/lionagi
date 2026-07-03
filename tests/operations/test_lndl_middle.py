# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for lionagi.operations.lndl_middle — the ADR-0087 §1-2 LNDL seam
Middle: round-outcome classification, the ActionCall -> ActionRequest bridge,
prompt injection, and repair semantics."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import BaseModel

from lionagi.hooks.bus import HookBus, HookPoint
from lionagi.lndl import Continue, Retry, Success, get_lndl_system_prompt
from lionagi.operations.lndl_middle.lndl_middle import (
    _classify_round,
    _run_round_chat,
    build_lndl_middle,
    lndl_middle,
)
from lionagi.operations.types import ChatParam, RunParam
from lionagi.testing import TestBranch


class AnswerModel(BaseModel):
    answer: str


class TwoFieldModel(BaseModel):
    answer: str
    detail: str


def lookup(query: str) -> str:
    """Return a canned lookup result for a query."""
    return f"result for {query}"


# ---------------------------------------------------------------------------
# _classify_round — pure-function unit tests (no branch, no async)
# ---------------------------------------------------------------------------


class TestClassifyRound:
    def test_no_lndl_blocks_returns_continue(self):
        outcome, pending, assembled = _classify_round("plain thinking, no fences", AnswerModel, {})
        assert isinstance(outcome, Continue)
        assert pending == []
        assert assembled is None

    def test_continue_round_with_lact_returns_pending_call(self):
        text = '```lndl\n<lact q a>lookup(query="x")</lact>\n```'
        outcome, pending, assembled = _classify_round(text, AnswerModel, {})
        assert isinstance(outcome, Continue)
        assert len(pending) == 1
        assert pending[0].name == "a"
        assert pending[0].function == "lookup"
        assert assembled is None

    def test_success_round_no_actions(self):
        text = "```lndl\n<lvar a>hi</lvar>\nOUT{answer: [a]}\n```"
        outcome, pending, assembled = _classify_round(text, AnswerModel, {})
        assert isinstance(outcome, Success)
        assert pending == []
        assert assembled == {"answer": "hi"}

    def test_parse_error_returns_retry(self):
        text = "```lndl\n<lvar a>unclosed\n```"
        outcome, pending, assembled = _classify_round(text, AnswerModel, {})
        assert isinstance(outcome, Retry)
        assert "Unclosed lvar tag" in outcome.error
        assert pending == []
        assert assembled is None

    def test_missing_lvar_returns_retry(self):
        text = "```lndl\nOUT{answer: [missing_alias]}\n```"
        outcome, _pending, _assembled = _classify_round(text, AnswerModel, {})
        assert isinstance(outcome, Retry)
        assert "undeclared alias" in outcome.error

    def test_missing_field_returns_retry(self):
        text = "```lndl\n<lvar a>hi</lvar>\nOUT{answer: [a]}\n```"
        outcome, _pending, _assembled = _classify_round(text, TwoFieldModel, {})
        assert isinstance(outcome, Retry)
        assert "missing required field" in outcome.error

    def test_invalid_constructor_in_success_round_returns_retry(self):
        text = "```lndl\n<lact a>not_a_valid_call!!!</lact>\nOUT{answer: [a]}\n```"
        outcome, _pending, _assembled = _classify_round(text, AnswerModel, {})
        assert isinstance(outcome, Retry)

    def test_invalid_constructor_in_continue_round_returns_retry(self):
        """A malformed lact declared with no OUT{} this round (a Continue
        shape) must still classify as Retry, not raise past _classify_round —
        this guards the fix where build_action_call runs inside the same
        try/except as lex/parse/assemble for the no-out_block branch."""
        text = "```lndl\n<lact a>not_a_valid_call!!!</lact>\n```"
        outcome, pending, assembled = _classify_round(text, AnswerModel, {})
        assert isinstance(outcome, Retry)
        assert pending == []
        assert assembled is None

    def test_cross_round_alias_resolves_via_action_results(self):
        text = "```lndl\nOUT{answer: [a]}\n```"
        outcome, _pending, assembled = _classify_round(
            text, AnswerModel, {"a": "from an earlier round"}
        )
        assert isinstance(outcome, Success)
        assert assembled == {"answer": "from an earlier round"}


# ---------------------------------------------------------------------------
# _run_round_chat — CLI vs API dispatch
# ---------------------------------------------------------------------------


class TestRunRoundChatDispatch:
    """Unit-level dispatch checks use a bare SimpleNamespace stand-in for the
    branch, not a real TestBranch: ScriptedEndpoint hardcodes
    ``is_cli: ClassVar[bool] = True`` (it simulates CLI-style streaming), so
    every real TestBranch is always CLI-routed regardless of ChatParam type —
    a SimpleNamespace gives precise, confound-free control over is_cli for
    exercising both branches of the dispatch condition."""

    @pytest.mark.asyncio
    async def test_dispatches_to_run_and_collect_for_cli_model(self):
        branch = SimpleNamespace(chat_model=SimpleNamespace(is_cli=True))
        fake = AsyncMock(return_value="cli text")
        with patch("lionagi.operations.run.run.run_and_collect", new=fake):
            result = await _run_round_chat(branch, "hi", ChatParam())
        assert result == "cli text"
        fake.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_dispatches_to_run_and_collect_for_run_param(self):
        branch = SimpleNamespace(chat_model=SimpleNamespace(is_cli=False))
        fake = AsyncMock(return_value="cli text")
        with patch("lionagi.operations.run.run.run_and_collect", new=fake):
            result = await _run_round_chat(branch, "hi", RunParam())
        assert result == "cli text"
        fake.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_dispatches_to_communicate_for_api_model(self):
        branch = SimpleNamespace(chat_model=SimpleNamespace(is_cli=False))
        fake = AsyncMock(return_value="api text")
        with patch("lionagi.operations.communicate.communicate.communicate", new=fake):
            result = await _run_round_chat(branch, "hi", ChatParam())
        assert result == "api text"
        fake.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_real_scripted_branch_round_trips_via_run_and_collect(self):
        """End-to-end sanity check with a real TestBranch (necessarily
        CLI-routed, see class docstring) — confirms the dispatch wiring
        actually reaches a real inner-chat implementation, not just mocks."""
        branch = TestBranch.from_text("```lndl\n<lvar a>hi</lvar>\nOUT{answer: [a]}\n```")
        result = await _run_round_chat(branch, "hi", ChatParam())
        assert "OUT{" in result


# ---------------------------------------------------------------------------
# Full Middle loop — round-outcome scenarios, end to end
# ---------------------------------------------------------------------------


class TestSuccessScenarios:
    @pytest.mark.asyncio
    async def test_single_round_success_no_actions(self):
        branch = TestBranch.from_text("```lndl\n<lvar a>hi</lvar>\nOUT{answer: [a]}\n```")
        chat_param = ChatParam(response_format=AnswerModel)
        result = await lndl_middle(branch, "say hi", chat_param)
        assert isinstance(result, AnswerModel)
        assert result.answer == "hi"

    @pytest.mark.asyncio
    async def test_no_response_format_returns_raw_dict(self):
        branch = TestBranch.from_text("```lndl\n<lvar a>hi</lvar>\nOUT{answer: [a]}\n```")
        chat_param = ChatParam(response_format=None)
        result = await lndl_middle(branch, "say hi", chat_param)
        assert result == {"answer": "hi"}

    @pytest.mark.asyncio
    async def test_operate_integration_end_to_end(self):
        """The Middle plugs into branch.operate() as the ADR describes."""
        branch = TestBranch.from_text("```lndl\n<lvar a>hi</lvar>\nOUT{answer: [a]}\n```")
        result = await branch.operate(
            instruction="say hi",
            response_format=AnswerModel,
            middle=lndl_middle,
        )
        assert isinstance(result, AnswerModel)
        assert result.answer == "hi"


class TestContinueAndCrossRound:
    @pytest.mark.asyncio
    async def test_continue_then_success_cross_round_alias(self):
        """Round 1 declares and executes a lact with no OUT{} (Continue).
        Round 2's OUT{} references that alias without redeclaring it — the
        cross-round action_results fallback resolves it."""
        branch = TestBranch.from_text(
            [
                '```lndl\n<lact q a>lookup(query="x")</lact>\n```',
                "```lndl\nOUT{answer: [a]}\n```",
            ]
        )
        branch.register_tools([lookup])
        chat_param = ChatParam(response_format=AnswerModel)
        result = await lndl_middle(branch, "look it up", chat_param)
        assert result.answer == "result for x"
        assert len(TestBranch.calls(branch)) == 2


class TestRetryAndRepair:
    @pytest.mark.asyncio
    async def test_retry_then_success_missing_lvar(self):
        branch = TestBranch.from_text(
            [
                "```lndl\nOUT{answer: [missing]}\n```",
                "```lndl\n<lvar a>fixed</lvar>\nOUT{answer: [a]}\n```",
            ]
        )
        chat_param = ChatParam(response_format=AnswerModel)
        result = await lndl_middle(branch, "answer", chat_param)
        assert result.answer == "fixed"

        calls = TestBranch.calls(branch)
        assert "Round 2 of" in calls[1].last_user_message
        assert "undeclared alias" in calls[1].last_user_message

    @pytest.mark.asyncio
    async def test_retry_then_success_missing_field(self):
        branch = TestBranch.from_text(
            [
                "```lndl\n<lvar a>hi</lvar>\nOUT{answer: [a]}\n```",
                "```lndl\n<lvar a>hi</lvar>\n<lvar b>there</lvar>\nOUT{answer: [a], detail: [b]}\n```",
            ]
        )
        chat_param = ChatParam(response_format=TwoFieldModel)
        result = await lndl_middle(branch, "answer", chat_param)
        assert result.answer == "hi"
        assert result.detail == "there"

    @pytest.mark.asyncio
    async def test_retry_then_success_invalid_constructor(self):
        branch = TestBranch.from_text(
            [
                "```lndl\n<lact a>not_a_valid_call!!!</lact>\nOUT{answer: [a]}\n```",
                "```lndl\n<lvar a>fixed</lvar>\nOUT{answer: [a]}\n```",
            ]
        )
        chat_param = ChatParam(response_format=AnswerModel)
        result = await lndl_middle(branch, "answer", chat_param)
        assert result.answer == "fixed"


class TestExhausted:
    @pytest.mark.asyncio
    async def test_exhausted_returns_last_error_string(self):
        custom_middle = build_lndl_middle(round_budget=2)
        branch = TestBranch.from_text(
            [
                "```lndl\nOUT{answer: [missing]}\n```",
                "```lndl\nOUT{answer: [still_missing]}\n```",
            ]
        )
        chat_param = ChatParam(response_format=AnswerModel)
        result = await custom_middle(branch, "answer", chat_param)
        assert isinstance(result, str)
        assert "undeclared alias" in result
        assert len(TestBranch.calls(branch)) == 2


class TestFailedPropagates:
    @pytest.mark.asyncio
    async def test_non_lndl_exception_propagates_uncaught(self):
        """A real TestBranch is always CLI-routed (ScriptedEndpoint.is_cli is
        a hardcoded ClassVar True), so the inner chat call this Middle makes
        goes through run_and_collect, not communicate — patch that."""
        branch = TestBranch.from_text("```lndl\n<lvar a>hi</lvar>\nOUT{answer: [a]}\n```")
        chat_param = ChatParam(response_format=AnswerModel)
        with patch(
            "lionagi.operations.run.run.run_and_collect",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ):
            with pytest.raises(RuntimeError, match="boom"):
                await lndl_middle(branch, "answer", chat_param)


class TestActionBridge:
    @pytest.mark.asyncio
    async def test_bridge_uses_act_and_permission_hooks_fire(self):
        branch = TestBranch.from_text(
            '```lndl\n<lact q a>lookup(query="x")</lact>\nOUT{answer: [a]}\n```'
        )
        branch.register_tools([lookup])

        bus = HookBus()
        pre_calls: list[dict] = []

        async def on_pre(**kw):
            pre_calls.append(kw)

        bus.on(HookPoint.TOOL_PRE, on_pre)
        branch._hooks = bus

        chat_param = ChatParam(response_format=AnswerModel)
        result = await lndl_middle(branch, "look it up", chat_param)

        assert result.answer == "result for x"
        assert len(pre_calls) == 1
        assert pre_calls[0]["tool_name"] == "lookup"

        from lionagi.protocols.messages import ActionRequest, ActionResponse

        assert any(isinstance(m, ActionRequest) for m in branch.messages)
        assert any(isinstance(m, ActionResponse) for m in branch.messages)


class TestPromptInjection:
    """InstructionContent renders guidance through minimal_yaml, which wraps
    multi-line text in a block scalar and re-indents every line — so the raw
    multi-line get_lndl_system_prompt() text is never a literal substring of
    the rendered message. A single-line marker (its first line) has no
    embedded newlines, so re-indentation only affects its own line start and
    it survives as a plain substring."""

    @pytest.mark.asyncio
    async def test_round_one_carries_lndl_contract(self):
        branch = TestBranch.from_text("```lndl\n<lvar a>hi</lvar>\nOUT{answer: [a]}\n```")
        chat_param = ChatParam(response_format=AnswerModel)
        await lndl_middle(branch, "say hi", chat_param)

        calls = TestBranch.calls(branch)
        marker = get_lndl_system_prompt().splitlines()[0]
        assert marker in calls[0].last_user_message

    @pytest.mark.asyncio
    async def test_preserves_callers_own_guidance(self):
        branch = TestBranch.from_text("```lndl\n<lvar a>hi</lvar>\nOUT{answer: [a]}\n```")
        chat_param = ChatParam(response_format=AnswerModel, guidance="Be terse.")
        await lndl_middle(branch, "say hi", chat_param)

        calls = TestBranch.calls(branch)
        marker = get_lndl_system_prompt().splitlines()[0]
        assert marker in calls[0].last_user_message
        assert "Be terse." in calls[0].last_user_message


class TestChatParamStripping:
    @pytest.mark.asyncio
    async def test_tool_schemas_and_response_format_stripped_from_round_call(self):
        branch = TestBranch.from_text("```lndl\n<lvar a>hi</lvar>\nOUT{answer: [a]}\n```")
        branch.register_tools([lookup])
        chat_param = ChatParam(
            response_format=AnswerModel,
            tool_schemas=branch.acts.get_tool_schema(tools=True).get("tools", []),
        )
        await lndl_middle(branch, "say hi", chat_param)

        calls = TestBranch.calls(branch)
        assert calls[0].tool_names == []


class TestRoundBudget:
    @pytest.mark.asyncio
    async def test_custom_round_budget_respected(self):
        custom_middle = build_lndl_middle(round_budget=1)
        branch = TestBranch.from_text("```lndl\nOUT{answer: [missing]}\n```")
        chat_param = ChatParam(response_format=AnswerModel)
        result = await custom_middle(branch, "answer", chat_param)
        assert isinstance(result, str)
        assert len(TestBranch.calls(branch)) == 1
