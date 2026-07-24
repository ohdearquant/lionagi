# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""LNDL seam Middle — advances a branch one LNDL round per inner chat call, looping up to a round budget (ADR-0024 §1-2). Opt-in via ``branch.operate(middle=lndl_middle)``."""

from __future__ import annotations

import types as _types
from typing import TYPE_CHECKING, Any, Union, get_args, get_origin

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
from .._turn_origin import TurnOrigin
from ..types import ActionParam, ChatParam, ParseParam, RunParam

if TYPE_CHECKING:
    from lionagi.protocols.messages.instruction import Instruction
    from lionagi.session.branch import Branch

__all__ = ("DEFAULT_ROUND_BUDGET", "build_lndl_middle", "lndl_middle")

DEFAULT_ROUND_BUDGET = 3

# Shared by every round's action bridge: concurrent, error-suppressed so a
# failed tool call becomes a result the model reacts to, not an aborting exception.
_ACTION_PARAM = ActionParam(
    action_call_params=get_default_action_call(),
    tools=None,
    strategy="concurrent",
    suppress_errors=True,
    verbose_action=False,
)


def _unwrap_optional_type(t: Any) -> Any:
    """If ``t`` is Optional[X] / X | None, return X. Else return ``t``."""
    origin = get_origin(t)
    if origin is Union or origin is _types.UnionType:
        args = [a for a in get_args(t) if a is not type(None)]
        if len(args) == 1:
            return args[0]
    return t


def _render_type(annotation: Any) -> str:
    """Render a field's type the way LNDL_SYSTEM_PROMPT's ``Specs:`` examples do."""
    annotation = _unwrap_optional_type(annotation)
    origin = get_origin(annotation)
    if origin is list:
        args = get_args(annotation)
        elem = args[0] if args else Any
        if isinstance(elem, type) and hasattr(elem, "model_fields"):
            fields = ", ".join(elem.model_fields.keys())
            return f"list[{elem.__name__}: {fields}]"
        return f"list[{_render_type(elem)}]"
    if origin is dict:
        args = get_args(annotation)
        key_t = _render_type(args[0]) if args else "str"
        val_t = _render_type(args[1]) if len(args) > 1 else "Any"
        return f"dict[{key_t}, {val_t}]"
    if isinstance(annotation, type) and hasattr(annotation, "model_fields"):
        fields = ", ".join(annotation.model_fields.keys())
        return f"{annotation.__name__}: {fields}"
    return getattr(annotation, "__name__", str(annotation))


def _render_target_spec(target: Any) -> str | None:
    """Render the target model's fields as an LNDL ``Specs:`` line; None for a plain-dict/no-target caller."""
    model_fields = getattr(target, "model_fields", None)
    if not model_fields:
        return None
    parts = []
    for name, info in model_fields.items():
        type_str = _render_type(info.annotation)
        if not info.is_required():
            type_str += ", optional"
        parts.append(f"{name}({type_str})")
    return "Specs: " + ", ".join(parts)


async def _bridge_action_calls(branch: Branch, calls: list[ActionCall]) -> dict[str, Any]:
    """Translate ActionCall placeholders into ActionRequests and run them through the branch's normal act() path (ADR-0024 §1)."""
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
    """Run one inner-chat turn, dispatching to communicate() (API) or run_and_collect() (CLI) — mirrors operate()'s own selection."""
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
    """Round 1 sends the instruction verbatim (LNDL contract rides in
    ``guidance``); later rounds send a short 'Round N of M.' continuation notice."""
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
    """Parse and assemble one round's raw text into a RoundOutcome; returns (outcome, pending_action_calls, assembled_dict)."""
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
    """Build an LNDL seam Middle (ADR-0024 §1) with a custom round budget; ``lndl_middle`` is the ready-to-use default."""

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

        # The only model type operate()/communicate() ever hands us; what
        # assemble()+model_validate() below must target.
        target = chat_param.response_format
        base_guidance = chat_param.guidance or ""
        guidance_parts = [get_lndl_system_prompt()]
        target_spec = _render_target_spec(target)
        if target_spec:
            guidance_parts.append(target_spec)
        if base_guidance:
            guidance_parts.append(base_guidance)
        lndl_guidance = "\n\n".join(guidance_parts)
        # LNDL uses a free-text <lact>/OUT{} protocol, not native
        # function-calling, so strip tool schemas and response_format here.
        stripped_chat_param = chat_param.with_updates(tool_schemas=[], response_format=None)

        action_results: dict[str, Any] = {}
        last_error: str | None = None

        # Rounds 2..N continue the caller's single turn rather than starting a
        # new one, so they carry a no-origin disposition: without it each round
        # reaching the model-submission boundary with the caller's default
        # (unset) disposition would mint a fresh token and re-fire
        # USER_PROMPT_SUBMIT, letting a blocking prompt guard admit round one
        # and then reject a repair round mid-turn.
        continuation_chat_param = stripped_chat_param.with_updates(
            turn_origin=TurnOrigin.no_origin()
        )

        for round_num in range(1, round_budget + 1):
            round_chat_param = (
                stripped_chat_param.with_updates(guidance=lndl_guidance)
                if round_num == 1
                else continuation_chat_param
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

        # Round budget exhausted without a valid OUT{}: raise rather than
        # return a bare str/None, which a validated-model caller must never see.
        detail = f": {last_error}" if last_error else " (no OUT{} block was produced)"
        raise LNDLError(
            f"LNDL round budget ({round_budget}) exhausted without a valid OUT{{}}{detail}"
        )

    return _lndl_middle


lndl_middle = build_lndl_middle()
