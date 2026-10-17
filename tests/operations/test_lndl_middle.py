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


class _FalsyModel(SimpleNamespace):
    """A model stand-in that is False in a boolean context, as an adapter or a
    wrapping subclass may legitimately be."""

    def __bool__(self) -> bool:
        return False


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


# _classify_round — pure-function unit tests (no branch, no async)


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


# _render_target_spec — the target schema summary injected into round-1
# guidance (LNDL_SYSTEM_PROMPT rule 5: "Use the EXACT spec names declared in
# the schema you are given"). Without this, stripping native response_format
# from the round chat call leaves a real model with no idea what fields to
# fill.


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


# _run_round_chat — CLI vs API dispatch


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
    async def test_per_call_api_model_overrides_a_cli_branch(self):
        """A model supplied on the chat param overrides the branch default for
        this call, so an API model must take the non-streaming path even when
        the branch's own chat_model is CLI-backed."""
        branch = SimpleNamespace(chat_model=SimpleNamespace(is_cli=True))
        fake = AsyncMock(return_value="api text")
        with patch("lionagi.operations.communicate.communicate.communicate", new=fake):
            result = await _run_round_chat(
                branch, "hi", ChatParam(imodel=SimpleNamespace(is_cli=False))
            )
        assert result == "api text"
        fake.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_per_call_cli_model_overrides_an_api_branch(self):
        """The inverse override: a CLI model on the chat param takes the
        streaming path even though the branch's chat_model is an API model."""
        branch = SimpleNamespace(chat_model=SimpleNamespace(is_cli=False))
        fake = AsyncMock(return_value="cli text")
        with patch("lionagi.operations.run.run.run_and_collect", new=fake):
            result = await _run_round_chat(
                branch, "hi", ChatParam(imodel=SimpleNamespace(is_cli=True))
            )
        assert result == "cli text"
        fake.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_supplied_model_that_is_falsy_still_overrides_the_branch(self):
        """Whether a model was supplied is decided by sentinel status, not by
        truthiness. The parameter bag stores whatever the caller passes, so a
        model object defining ``__bool__`` as False — an adapter or a subclass
        wrapping another model — is still a supplied model and must select the
        transport, rather than being discarded for the branch default."""
        branch = SimpleNamespace(chat_model=SimpleNamespace(is_cli=True))
        supplied = _FalsyModel(is_cli=False)
        assert not bool(supplied)

        fake = AsyncMock(return_value="api text")
        with patch("lionagi.operations.communicate.communicate.communicate", new=fake):
            result = await _run_round_chat(branch, "hi", ChatParam(imodel=supplied))
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


# Full Middle loop — round-outcome scenarios, end to end


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

    @pytest.mark.asyncio
    async def test_repair_round_fires_user_prompt_submit_once(self):
        """A one-repair-round LNDL run is still one user turn: only round 1
        may fire USER_PROMPT_SUBMIT. A later round reusing the caller's
        unset turn_origin would mint (and fire) a fresh token every round."""
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


def _has_tools_section(message: str) -> bool:
    """The instruction's own Tools key, unindented; the guidance's worked examples
    mention tools too, but only inside its indented block."""
    return any(line.startswith("Tools:") for line in message.splitlines())


class TestRoundToolsAndResponseFormat:
    """A round replies in free text, so the response format is dropped, but a
    <lact> names a tool, so every round's instruction lists the tools it may
    call. Native tool calling is never requested."""

    @pytest.mark.asyncio
    async def test_passed_tool_schemas_listed_and_response_format_dropped(self):
        branch = TestBranch.from_text("```lndl\n<lvar a>hi</lvar>\nOUT{answer: [a]}\n```")
        branch.register_tools([lookup])
        chat_param = ChatParam(
            response_format=AnswerModel,
            tool_schemas=branch.acts.get_tool_schema(tools=True).get("tools", []),
        )
        await lndl_middle(branch, "say hi", chat_param)

        calls = TestBranch.calls(branch)
        assert calls[0].tool_names == []
        assert _has_tools_section(calls[0].last_user_message)
        assert "lookup" in calls[0].last_user_message
        assert "Return a canned lookup result" in calls[0].last_user_message
        assert "ResponseSchema" not in calls[0].last_user_message

    @pytest.mark.asyncio
    async def test_registered_tools_listed_when_none_passed(self):
        branch = TestBranch.from_text("```lndl\n<lvar a>hi</lvar>\nOUT{answer: [a]}\n```")
        branch.register_tools([lookup])
        await lndl_middle(branch, "say hi", ChatParam(response_format=AnswerModel))

        assert _has_tools_section(TestBranch.calls(branch)[0].last_user_message)
        assert "lookup" in TestBranch.calls(branch)[0].last_user_message

    @pytest.mark.asyncio
    async def test_every_round_lists_the_tools(self):
        branch = TestBranch.from_text(
            [
                '```lndl\n<lact q a>lookup(query="x")</lact>\n```',
                "```lndl\nOUT{answer: [missing]}\n```",
                "```lndl\n<lvar b>read it</lvar>\nOUT{answer: [b]}\n```",
            ]
        )
        branch.register_tools([lookup])
        await lndl_middle(branch, "look it up", ChatParam(response_format=AnswerModel))

        calls = TestBranch.calls(branch)
        assert len(calls) == 3
        assert all(_has_tools_section(call.last_user_message) for call in calls)
        assert all("lookup" in call.last_user_message for call in calls)

    @pytest.mark.asyncio
    async def test_no_tools_section_without_tools(self):
        branch = TestBranch.from_text("```lndl\n<lvar a>hi</lvar>\nOUT{answer: [a]}\n```")
        await lndl_middle(branch, "say hi", ChatParam(response_format=AnswerModel))

        assert not _has_tools_section(TestBranch.calls(branch)[0].last_user_message)


class TestMaxActions:
    @staticmethod
    def _round(n: int, *, out: bool = False) -> str:
        lacts = "".join(f'<lact q{i} a{i}>lookup(query="{i}")</lact>\n' for i in range(n))
        return f"```lndl\n{lacts}{'OUT{answer: [a0]}' + chr(10) if out else ''}```"

    def test_round_at_the_limit_runs_every_call(self):
        outcome, pending, _ = _classify_round(self._round(3), AnswerModel, {}, max_actions=3)
        assert isinstance(outcome, Continue)
        assert len(pending) == 3

    def test_round_over_the_limit_is_a_repair_round_with_nothing_pending(self):
        outcome, pending, assembled = _classify_round(
            self._round(4), AnswerModel, {}, max_actions=3
        )
        assert isinstance(outcome, Retry)
        assert "declares 4 <lact> calls" in outcome.error
        assert "at most 3" in outcome.error
        assert "none of them ran" in outcome.error
        assert pending == []
        assert assembled is None

    def test_round_with_out_over_the_limit_is_refused_too(self):
        outcome, pending, _ = _classify_round(
            self._round(4, out=True), AnswerModel, {}, max_actions=3
        )
        assert isinstance(outcome, Retry)
        assert pending == []

    def test_no_limit_by_default(self):
        outcome, pending, _ = _classify_round(self._round(20), AnswerModel, {})
        assert isinstance(outcome, Continue)
        assert len(pending) == 20

    @pytest.mark.parametrize("bad", [0, -1, 1.5, "3", True, False])
    def test_build_rejects_a_bad_limit(self, bad):
        with pytest.raises(ValueError, match="max_actions must be a positive integer"):
            build_lndl_middle(max_actions=bad)

    @pytest.mark.asyncio
    async def test_over_limit_round_runs_no_call_and_repairs(self):
        calls_made: list[str] = []

        def counted_lookup(query: str) -> str:
            """Return a canned lookup result for a query."""
            calls_made.append(query)
            return f"result for {query}"

        middle = build_lndl_middle(round_budget=3, max_actions=2)
        branch = TestBranch.from_text(
            [
                "```lndl\n"
                + "".join(f'<lact q{i} a{i}>counted_lookup(query="{i}")</lact>\n' for i in range(3))
                + "```",
                '```lndl\n<lact q a>counted_lookup(query="one")</lact>\n```',
                "```lndl\n<lvar b>done</lvar>\nOUT{answer: [b]}\n```",
            ]
        )
        branch.register_tools([counted_lookup])
        result = await middle(branch, "look it up", ChatParam(response_format=AnswerModel))

        assert result.answer == "done"
        assert calls_made == ["one"]
        calls = TestBranch.calls(branch)
        assert "declares 3 <lact> calls" in calls[1].last_user_message


class TestSeparateAnswerRounds:
    _MIXED = (
        '```lndl\n<lact q a>lookup(query="x")</lact>\n<lvar b>guess</lvar>\nOUT{answer: [b]}\n```'
    )

    def test_a_round_that_calls_and_answers_is_refused_with_nothing_pending(self):
        outcome, pending, assembled = _classify_round(
            self._MIXED, AnswerModel, {}, lacts_with_out=False
        )
        assert isinstance(outcome, Retry)
        assert "declares <lact> calls and also writes OUT{}" in outcome.error
        assert pending == []
        assert assembled is None

    def test_calls_alone_and_an_answer_alone_still_classify(self):
        calls = '```lndl\n<lact q a>lookup(query="x")</lact>\n```'
        outcome, pending, _ = _classify_round(calls, AnswerModel, {}, lacts_with_out=False)
        assert isinstance(outcome, Continue) and len(pending) == 1
        answer = "```lndl\nOUT{answer: [a]}\n```"
        outcome, _, assembled = _classify_round(
            answer, AnswerModel, {"a": "read"}, lacts_with_out=False
        )
        assert isinstance(outcome, Success) and assembled == {"answer": "read"}

    def test_the_default_still_takes_a_round_that_calls_and_answers(self):
        outcome, _, assembled = _classify_round(self._MIXED, AnswerModel, {})
        assert isinstance(outcome, Success)
        assert assembled == {"answer": "guess"}

    @pytest.mark.asyncio
    async def test_the_refused_round_runs_no_call(self):
        calls_made: list[str] = []

        def counted_lookup(query: str) -> str:
            """Return a canned lookup result for a query."""
            calls_made.append(query)
            return f"result for {query}"

        middle = build_lndl_middle(round_budget=3, lacts_with_out=False)
        branch = TestBranch.from_text(
            [
                '```lndl\n<lact q a>counted_lookup(query="x")</lact>\n<lvar b>guess</lvar>\n'
                "OUT{answer: [b]}\n```",
                '```lndl\n<lact q a>counted_lookup(query="y")</lact>\n```',
                "```lndl\n<lvar b>read</lvar>\nOUT{answer: [b]}\n```",
            ]
        )
        branch.register_tools([counted_lookup])
        result = await middle(branch, "look it up", ChatParam(response_format=AnswerModel))

        assert result.answer == "read"
        assert calls_made == ["y"]


class TestRoundNotices:
    @pytest.mark.asyncio
    async def test_the_last_round_says_it_is_the_last(self):
        middle = build_lndl_middle(round_budget=3)
        branch = TestBranch.from_text(
            ["thinking", "thinking", "```lndl\n<lvar a>hi</lvar>\nOUT{answer: [a]}\n```"]
        )
        await middle(branch, "say hi", ChatParam(response_format=AnswerModel))

        calls = TestBranch.calls(branch)
        assert "This is the last round" not in calls[1].last_user_message
        assert "Round 3 of 3. This is the last round" in calls[2].last_user_message

    @pytest.mark.asyncio
    async def test_a_repair_error_is_shown_once_and_not_after_a_call_round(self):
        middle = build_lndl_middle(round_budget=4)
        branch = TestBranch.from_text(
            [
                "```lndl\nOUT{answer: [missing]}\n```",
                '```lndl\n<lact q a>lookup(query="x")</lact>\n```',
                "```lndl\n<lvar b>done</lvar>\nOUT{answer: [b]}\n```",
            ]
        )
        branch.register_tools([lookup])
        await middle(branch, "look it up", ChatParam(response_format=AnswerModel))

        calls = TestBranch.calls(branch)
        assert "undeclared alias" in calls[1].last_user_message
        assert "Error from your previous round" not in calls[2].last_user_message

    @pytest.mark.asyncio
    async def test_exhaustion_still_reports_the_last_refusal(self):
        middle = build_lndl_middle(round_budget=2)
        branch = TestBranch.from_text(["```lndl\nOUT{answer: [missing]}\n```", "thinking"])
        with pytest.raises(LNDLError, match="undeclared alias"):
            await middle(branch, "answer", ChatParam(response_format=AnswerModel))


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
