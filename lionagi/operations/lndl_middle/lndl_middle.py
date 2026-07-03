# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""LNDL seam Middle — advances a branch one LNDL round per inner chat call,
looping internally up to a round budget (default 3). Implements ADR-0087
§1 (the seam over ``operate()``) and §2 (round outcomes and repair
semantics).

Opt-in per call: ``branch.operate(instruction=..., middle=lndl_middle)``.
Nothing changes for callers who don't pass it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import JsonValue, ValidationError

from lionagi.lndl import (
    ActionCall,
    Continue,
    Lexer,
    LNDLError,
    Parser,
    Retry,
    RoundOutcome,
    Success,
    assemble,
    build_action_call,
    collect_actions,
    extract_lndl_blocks,
    get_lndl_system_prompt,
    normalize_lndl_text,
    replace_actions,
)

from .._defaults import get_default_action_call
from ..types import ActionParam, ChatParam, ParseParam, RunParam

if TYPE_CHECKING:
    from lionagi.protocols.messages.instruction import Instruction
    from lionagi.session.branch import Branch

__all__ = ("DEFAULT_ROUND_BUDGET", "build_lndl_middle", "lndl_middle")

DEFAULT_ROUND_BUDGET = 3

# Shared by every round's action bridge — concurrent, error-suppressed (a
# failed tool call surfaces as a tool result the model can react to next
# round, not an exception that aborts the whole LNDL run).
_ACTION_PARAM = ActionParam(
    action_call_params=get_default_action_call(),
    tools=None,
    strategy="concurrent",
    suppress_errors=True,
    verbose_action=False,
)


async def _bridge_action_calls(branch: Branch, calls: list[ActionCall]) -> dict[str, Any]:
    """Translate ActionCall placeholders into ActionRequests and execute them
    through the branch's normal act() path, so permission policies and hooks
    apply unchanged (ADR-0087 §1). Returns a dict of alias -> result."""
    if not calls:
        return {}

    from ..act.act import act

    requests = [
        branch.msgs.create_action_request(
            function=call.function,
            arguments=call.arguments,
            sender=branch.id,
            recipient=branch.user or "user",
        )
        for call in calls
    ]
    responses = await act(branch, requests, _ACTION_PARAM)
    return {call.name: resp.output for call, resp in zip(calls, responses, strict=True)}


async def _run_round_chat(
    branch: Branch,
    instruction: JsonValue | Instruction,
    chat_param: ChatParam,
) -> str:
    """Run one inner-chat turn, dispatching to communicate() (API models) or
    run_and_collect() (CLI models) — mirrors operate()'s own default-Middle
    selection so LNDL behaves the same for both endpoint families."""
    if isinstance(chat_param, RunParam) or getattr(branch.chat_model, "is_cli", False):
        from ..run.run import run_and_collect

        return await run_and_collect(branch, instruction, chat_param, skip_validation=True)

    from ..communicate.communicate import communicate

    return await communicate(branch, instruction, chat_param, skip_validation=True)


def _round_instruction(
    original_instruction: JsonValue | Instruction,
    round_num: int,
    round_budget: int,
    prior_error: str | None,
) -> JsonValue | Instruction:
    """Round 1 sends the caller's own instruction verbatim (the LNDL contract
    rides in ``guidance`` instead, see ``build_lndl_middle``). Later rounds
    send a short continuation notice — LNDL_SYSTEM_PROMPT's own MULTI-ROUND
    MODE section teaches the model to read 'Round N of M.' as a continuation
    signal, with tool results already visible in chat history."""
    if round_num == 1:
        return original_instruction
    notice = f"Round {round_num} of {round_budget}."
    if prior_error:
        notice += f"\n\nError from your previous OUT{{}}: {prior_error}\nRepair and try again."
    return notice


def _classify_round(
    text: str,
    target: Any,
    action_results: dict[str, Any],
) -> tuple[RoundOutcome, list[ActionCall], dict[str, Any] | None]:
    """Parse and assemble one round's raw text into a RoundOutcome.

    Returns ``(outcome, pending_action_calls, assembled_dict)``. ``pending``
    is populated for a Continue round (every declared lact, executed
    unconditionally per LNDL_SYSTEM_PROMPT's "tools execute every round") and
    for a Success candidate (only the lacts actually reachable from OUT{},
    via ``collect_actions``). ``assembled`` is populated only for a Success
    candidate.
    """
    blocks = extract_lndl_blocks(text)
    if not blocks:
        return Continue(), [], None

    try:
        normalized = normalize_lndl_text("\n\n".join(blocks))
        tokens = Lexer(normalized).tokenize()
        program = Parser(tokens, source_text=normalized).parse()

        if program.out_block is None:
            pending = [build_action_call(la.alias, la) for la in program.lacts]
            return Continue(), pending, None

        assembled = assemble(program, target, action_results=action_results)
    except LNDLError as e:
        return Retry(error=str(e)), [], None

    pending = collect_actions(assembled)
    return Success(output=assembled), pending, assembled


def build_lndl_middle(round_budget: int = DEFAULT_ROUND_BUDGET):
    """Build an LNDL seam Middle (ADR-0087 §1) with a custom round budget.

    Returns a callable satisfying the Middle protocol (``operations/types.py``).
    ``lndl_middle`` (module-level, below) is the ready-to-use default.
    """

    async def _lndl_middle(
        branch: Branch,
        instruction: JsonValue | Instruction,
        chat_param: ChatParam,
        parse_param: ParseParam | None = None,
        clear_messages: bool = False,
        skip_validation: bool = False,
    ) -> Any:
        if clear_messages:
            branch.msgs.clear_messages()

        # operate() always hands us the only model type it ever constructs —
        # either the caller's bare response_format (via a direct communicate()
        # call) or the operative-wrapped subclass (via operate()). Either way
        # it's what assemble()+model_validate() below must target.
        target = chat_param.response_format
        base_guidance = chat_param.guidance or ""
        lndl_guidance = (
            f"{get_lndl_system_prompt()}\n\n{base_guidance}"
            if base_guidance
            else get_lndl_system_prompt()
        )
        # Strip native tool-calling and JSON-schema auto-rendering from the
        # per-round chat call: LNDL uses a free-text <lact>/OUT{} protocol,
        # not native function-calling, and it's assemble()+model_validate()
        # below — not the per-round chat call — that targets response_format.
        stripped_chat_param = chat_param.with_updates(tool_schemas=[], response_format=None)

        action_results: dict[str, Any] = {}
        last_error: str | None = None

        for round_num in range(1, round_budget + 1):
            round_chat_param = (
                stripped_chat_param.with_updates(guidance=lndl_guidance)
                if round_num == 1
                else stripped_chat_param
            )
            round_instruction = _round_instruction(instruction, round_num, round_budget, last_error)

            text = await _run_round_chat(branch, round_instruction, round_chat_param)
            outcome, pending, assembled = _classify_round(text, target, action_results)

            if isinstance(outcome, Retry):
                last_error = outcome.error
                continue

            if isinstance(outcome, Continue):
                if pending:
                    action_results.update(await _bridge_action_calls(branch, pending))
                continue

            assert isinstance(outcome, Success)
            # Success candidate: resolve any pending action results, then
            # validate against the caller's target model (if any).
            if pending:
                action_results.update(await _bridge_action_calls(branch, pending))
            assembled = replace_actions(assembled, action_results)

            if skip_validation or target is None or not hasattr(target, "model_validate"):
                return assembled

            try:
                return target.model_validate(assembled)
            except ValidationError as e:
                last_error = str(e)
                continue

        return last_error  # Exhausted(last_error) — round budget exhausted.

    return _lndl_middle


lndl_middle = build_lndl_middle()
