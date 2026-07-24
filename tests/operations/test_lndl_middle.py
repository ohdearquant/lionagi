# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for lionagi.operations.lndl_middle — the ADR-0024 §1-2 LNDL seam
Middle: round-outcome classification, the ActionCall -> ActionRequest bridge,
prompt injection, and repair semantics."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import BaseModel

import lionagi.lndl as lndl
from lionagi.hooks.bus import HookBus, HookPoint
from lionagi.lndl import Continue, LNDLError, Retry, Success, get_lndl_system_prompt
from lionagi.operations.lndl_middle import (
    DEFAULT_ROUND_BUDGET as PACKAGE_DEFAULT_ROUND_BUDGET,
)
from lionagi.operations.lndl_middle import (
    build_lndl_middle as package_build_lndl_middle,
)
from lionagi.operations.lndl_middle import (
    lndl_middle as package_lndl_middle,
)
from lionagi.operations.lndl_middle.lndl_middle import (
    _classify_round,
    _render_target_spec,
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


class OptionalFieldModel(BaseModel):
    answer: str
    detail: str | None = None


class Finding(BaseModel):
    name: str
    score: float


class ReportModel(BaseModel):
    findings: list[Finding]
    scores: dict[str, float]


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

    def test_prompt_action_rule_matches_both_round_shapes(self):
        rule = (
            "Per-round action rule: if OUT{} is present, only its referenced lacts "
            "execute; if OUT{} is absent, every declared lact executes."
        )
        assert get_lndl_system_prompt().count(rule) == 3

        with_out = (
            "```lndl\n"
            '<lact a>lookup(query="broad")</lact>\n'
            '<lact b>lookup(query="narrow")</lact>\n'
            "OUT{answer: [b]}\n"
            "```"
        )
        outcome, pending, _assembled = _classify_round(with_out, AnswerModel, {})
        assert isinstance(outcome, Success)
        assert [call.name for call in pending] == ["b"]

        without_out = with_out.replace("OUT{answer: [b]}\n", "")
        outcome, pending, _assembled = _classify_round(without_out, AnswerModel, {})
        assert isinstance(outcome, Continue)
        assert [call.name for call in pending] == ["a", "b"]


# ---------------------------------------------------------------------------
# _render_target_spec — the target schema summary injected into round-1
# guidance (LNDL_SYSTEM_PROMPT rule 5: "Use the EXACT spec names declared in
# the schema you are given"). Without this, stripping native response_format
# from the round chat call leaves a real model with no idea what fields to
# fill.
# ---------------------------------------------------------------------------


class TestRenderTargetSpec:
    def test_none_target_renders_nothing(self):
        assert _render_target_spec(None) is None

    def test_non_model_target_renders_nothing(self):
        assert _render_target_spec({"answer": {"type": "string"}}) is None

    def test_scalar_fields(self):
        spec = _render_target_spec(AnswerModel)
        assert spec == "Specs: answer(str)"

    def test_two_scalar_fields(self):
        spec = _render_target_spec(TwoFieldModel)
        assert spec == "Specs: answer(str), detail(str)"

    def test_optional_field_marked(self):
        spec = _render_target_spec(OptionalFieldModel)
        assert spec == "Specs: answer(str), detail(str, optional)"

    def test_nested_model_and_dict_and_list_fields(self):
        spec = _render_target_spec(ReportModel)
        assert spec == "Specs: findings(list[Finding: name, score]), scores(dict[str, float])"


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


class TestRoundBudgetExhaustion:
    """The Middle raises LNDLError rather than leaking a raw last_error str
    (or None, for an all-Continue run) through operate()'s return type."""

    @pytest.mark.asyncio
    async def test_exhausted_raises_with_last_error(self):
        custom_middle = build_lndl_middle(round_budget=2)
        branch = TestBranch.from_text(
            [
                "```lndl\nOUT{answer: [missing]}\n```",
                "```lndl\nOUT{answer: [still_missing]}\n```",
            ]
        )
        chat_param = ChatParam(response_format=AnswerModel)
        with pytest.raises(LNDLError, match="undeclared alias"):
            await custom_middle(branch, "answer", chat_param)
        assert len(TestBranch.calls(branch)) == 2

    @pytest.mark.asyncio
    async def test_all_continue_exhaustion_raises_without_none_leak(self):
        """Every round is a bare Continue (no OUT{} ever) — last_error stays
        None the whole run. Must still raise, never return None."""
        custom_middle = build_lndl_middle(round_budget=2)
        branch = TestBranch.from_text(["still thinking...", "still thinking..."])
        chat_param = ChatParam(response_format=AnswerModel)
        with pytest.raises(LNDLError, match="round budget"):
            await custom_middle(branch, "answer", chat_param)
        assert len(TestBranch.calls(branch)) == 2

    @pytest.mark.asyncio
    async def test_operate_integration_exhaustion_raises(self):
        """branch.operate(..., response_format=...) must not silently hand a
        structured caller a bare string or None on exhaustion."""
        assert not hasattr(lndl, "Exhausted")
        assert not hasattr(lndl, "Failed")
        custom_middle = build_lndl_middle(round_budget=1)
        branch = TestBranch.from_text("```lndl\nOUT{answer: [missing]}\n```")
        with pytest.raises(LNDLError):
            await branch.operate(
                instruction="answer",
                response_format=AnswerModel,
                middle=custom_middle,
            )


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

    @pytest.mark.asyncio
    async def test_round_one_tells_the_model_the_target_field_names(self):
        """Native response_format is stripped from the round chat call — the
        model must instead be told the target's field names via a rendered
        Specs: line, or it has no way to know what OUT{} should contain."""
        branch = TestBranch.from_text(
            "```lndl\n<lvar a>hi</lvar>\n<lvar b>there</lvar>\nOUT{answer: [a], detail: [b]}\n```"
        )
        chat_param = ChatParam(response_format=TwoFieldModel)
        await lndl_middle(branch, "say hi", chat_param)

        calls = TestBranch.calls(branch)
        assert "answer(str)" in calls[0].last_user_message
        assert "detail(str)" in calls[0].last_user_message

    @pytest.mark.asyncio
    async def test_no_response_format_omits_target_specs_line(self):
        """LNDL_SYSTEM_PROMPT's own worked examples legitimately contain
        several 'Specs:' lines — assert no *additional* one was injected for
        a target-less call, rather than a raw 'not in' check."""
        base_specs_count = get_lndl_system_prompt().count("Specs:")
        branch = TestBranch.from_text("```lndl\n<lvar a>hi</lvar>\nOUT{answer: [a]}\n```")
        chat_param = ChatParam(response_format=None)
        await lndl_middle(branch, "say hi", chat_param)

        calls = TestBranch.calls(branch)
        assert calls[0].last_user_message.count("Specs:") == base_specs_count


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


class TestTurnOriginSingleFire:
    """A repair round is an internal retry within one operate() call, not a
    fresh user-prompt submission — USER_PROMPT_SUBMIT must fire at most once
    per lndl_middle() invocation, no matter how many rounds it takes."""

    @pytest.mark.asyncio
    async def test_retry_round_does_not_refire_user_prompt_submit(self):
        branch = TestBranch.from_text(
            [
                "```lndl\nOUT{answer: [missing]}\n```",
                "```lndl\n<lvar a>fixed</lvar>\nOUT{answer: [a]}\n```",
            ]
        )

        bus = HookBus()
        submits: list[dict] = []

        async def on_submit(**kw):
            submits.append(kw)

        bus.on(HookPoint.USER_PROMPT_SUBMIT, on_submit)
        branch._hooks = bus

        chat_param = ChatParam(response_format=AnswerModel)
        result = await lndl_middle(branch, "answer", chat_param)

        assert result.answer == "fixed"
        assert len(TestBranch.calls(branch)) == 2
        assert len(submits) == 1


class TestRoundBudget:
    @pytest.mark.asyncio
    async def test_custom_round_budget_respected(self):
        custom_middle = build_lndl_middle(round_budget=1)
        branch = TestBranch.from_text("```lndl\nOUT{answer: [missing]}\n```")
        chat_param = ChatParam(response_format=AnswerModel)
        with pytest.raises(LNDLError):
            await custom_middle(branch, "answer", chat_param)
        assert len(TestBranch.calls(branch)) == 1


class TestPackageImportSurface:
    """The documented public path (CHANGELOG.md, ADR-0024 §1) is the package
    itself, not the private nested module — a caller writes
    `from lionagi.operations.lndl_middle import lndl_middle`, not
    `...lndl_middle.lndl_middle import lndl_middle`."""

    def test_package_level_lndl_middle_is_callable(self):
        assert callable(package_lndl_middle)

    def test_package_level_build_lndl_middle_is_callable_and_builds(self):
        assert callable(package_build_lndl_middle)
        custom = package_build_lndl_middle(round_budget=5)
        assert callable(custom)

    def test_package_level_default_round_budget(self):
        assert PACKAGE_DEFAULT_ROUND_BUDGET == 3
