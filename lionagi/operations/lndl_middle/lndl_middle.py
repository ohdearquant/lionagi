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
    # Transport follows the effective model's CLI flag, same precedence as
    # chat()/operate(). Sentinel status, not truthiness, decides "supplied" —
    # a model whose __bool__ is False must still win over the branch default.
    effective_imodel = (
        branch.chat_model if chat_param._is_sentinel(chat_param.imodel) else chat_param.imodel
    )
    if isinstance(chat_param, RunParam) or getattr(effective_imodel, "is_cli", False):
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
    ``guidance``); later rounds send a short 'Round N of M.' continuation notice,
    which on the last round says so and after a refused round carries its error."""
    if round_num == 1:
        return original_instruction
    notice = f"Round {round_num} of {round_budget}."
    if round_num == round_budget:
        notice += " This is the last round: write your answer in OUT{} now."
    if prior_error:
        notice += f"\n\nError from your previous round: {prior_error}\nRepair and try again."
    return notice


def _classify_round(
    text: str,
    target: Any,
    action_results: dict[str, Any],
    max_actions: int | None = None,
    lacts_with_out: bool = True,
) -> tuple[RoundOutcome, list[ActionCall], dict[str, Any] | None]:
    """Parse and assemble one round's raw text into a RoundOutcome; returns (outcome, pending_action_calls, assembled_dict).

    A round declaring more than ``max_actions`` ``<lact>`` calls is a repair round: none of its calls runs.
    With ``lacts_with_out=False``, so is a round that declares ``<lact>`` calls beside its ``OUT{}``.
    """
    blocks = extract_lndl_blocks(text)
    if not blocks:
        return Continue(), [], None

    try:
        normalized = normalize_lndl_text("\n\n".join(blocks))
        tokens = Lexer(normalized).tokenize()
        program = Parser(tokens, source_text=normalized).parse()

        if max_actions is not None and len(program.lacts) > max_actions:
            return (
                Retry(
                    error=(
                        f"this round declares {len(program.lacts)} <lact> calls and a round may "
                        f"declare at most {max_actions}; none of them ran. Declare at most "
                        f"{max_actions} now and the rest in a later round."
                    )
                ),
                [],
                None,
            )

        if not lacts_with_out and program.lacts and program.out_block is not None:
            return (
                Retry(
                    error=(
                        "this round declares <lact> calls and also writes OUT{}; none of its "
                        "calls ran and the answer was not taken. Declare calls in a round "
                        "without OUT{}, read their results, then write OUT{} in a later round "
                        "that declares no <lact>."
                    )
                ),
                [],
                None,
            )

        if program.out_block is None:
            pending = [build_action_call(la.alias, la) for la in program.lacts]
            return Continue(), pending, None

        assembled = assemble(program, target, action_results=action_results)
    except LNDLError as e:
        return Retry(error=str(e)), [], None

    pending = collect_actions(assembled)
    return Success(output=assembled), pending, assembled


def build_lndl_middle(
    round_budget: int = DEFAULT_ROUND_BUDGET,
    max_actions: int | None = None,
    lacts_with_out: bool = True,
):
    """Build an LNDL seam Middle (ADR-0024 §1) with a custom round budget; ``lndl_middle`` is the ready-to-use default.

    ``max_actions`` bounds the ``<lact>`` calls one round may declare; None leaves them unbounded.
    ``lacts_with_out=False`` keeps calls and the answer in separate rounds: a round that declares
    ``<lact>`` calls beside its ``OUT{}`` is a repair round and none of its calls runs.
    """
    if max_actions is not None and (
        isinstance(max_actions, bool) or not isinstance(max_actions, int) or max_actions < 1
    ):
        raise ValueError(f"max_actions must be a positive integer or None, got {max_actions!r}")

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
        # LNDL replies in free text, so the response format is dropped. The tools stay: a
        # <lact> names a tool, and an endpoint that does not place the prompt cache shows the
        # model its tools only in the current instruction. With none passed, every tool the
        # branch registers is listed, since a <lact> may call any of them.
        tool_schemas = chat_param.tool_schemas or branch.acts.schema_list
        stripped_chat_param = chat_param.with_updates(
            tool_schemas=tool_schemas, response_format=None
        )

        action_results: dict[str, Any] = {}
        # The most recent refusal, for the exhaustion error, and the one the next round is
        # asked to repair; a round that only declares calls clears the second, since the
        # model has already moved past it.
        last_error: str | None = None
        repair_error: str | None = None

        for round_num in range(1, round_budget + 1):
            if round_num == 1:
                round_chat_param = stripped_chat_param.with_updates(guidance=lndl_guidance)
            else:
                # A continuation/repair round is not a fresh user submission —
                # the outer call's boundary already fired at most once for
                # round 1; later rounds must never re-fire USER_PROMPT_SUBMIT.
                round_chat_param = stripped_chat_param.with_updates(
                    turn_origin=TurnOrigin.no_origin()
                )
            round_instruction = _round_instruction(
                instruction, round_num, round_budget, repair_error
            )
            repair_error = None

            text = await _run_round_chat(branch, round_instruction, round_chat_param)
            outcome, pending, assembled = _classify_round(
                text,
                target,
                action_results,
                max_actions=max_actions,
                lacts_with_out=lacts_with_out,
            )

            if isinstance(outcome, Retry):
                last_error = repair_error = outcome.error
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
                last_error = repair_error = str(e)
                continue

        # Round budget exhausted without a valid OUT{}: raise rather than
        # return a bare str/None, which a validated-model caller must never see.
        detail = f": {last_error}" if last_error else " (no OUT{} block was produced)"
        raise LNDLError(
            f"LNDL round budget ({round_budget}) exhausted without a valid OUT{{}}{detail}"
        )

    return _lndl_middle


lndl_middle = build_lndl_middle()
