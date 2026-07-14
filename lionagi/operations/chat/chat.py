# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from typing import TYPE_CHECKING

from pydantic import JsonValue

from lionagi._errors import ExecutionError
from lionagi.protocols.generic import EventStatus
from lionagi.protocols.messages import AssistantResponse, Instruction

from .._turn_origin import consume_turn_origin
from ..types import ChatParam
from ._prepare import _apply_context_providers, _build_instruction, _prepare_run_kwargs

if TYPE_CHECKING:
    from lionagi.session.branch import Branch


async def _emit_user_prompt_submit(
    branch: "Branch", chat_param: ChatParam, ins: Instruction
) -> None:
    """Fire USER_PROMPT_SUBMIT exactly once, iff the operation context carries a token."""
    token = consume_turn_origin(chat_param.turn_origin)
    if token is None or branch._hooks is None:
        return
    from lionagi.hooks.bus import HookPoint

    prompt = ins.rendered
    if not isinstance(prompt, str):
        prompt = str(prompt)
    await branch._hooks.emit(
        HookPoint.USER_PROMPT_SUBMIT,
        session_id=str(branch._owning_session_id or branch.id),
        branch_id=str(branch.id),
        prompt=prompt,
    )


async def chat(
    branch: "Branch",
    instruction: JsonValue | Instruction,
    chat_param: ChatParam,
    return_ins_res_message: bool = False,
) -> tuple[Instruction, AssistantResponse] | str:
    # Built synchronously and purely from (instruction, chat_param) — no
    # context-provider I/O. This is the only thing the guard below needs,
    # so it happens before any other awaited operation for this turn,
    # mirroring run()'s ordering (operations/run/run.py): the guard is
    # evaluated before context providers get a chance to run their
    # (potentially side-effecting) gather.
    ins = _build_instruction(branch, instruction, chat_param)

    await _emit_user_prompt_submit(branch, chat_param, ins)

    provider_ins, context_report = await _apply_context_providers(
        branch, instruction, chat_param, ins=ins
    )
    ins, kw = _prepare_run_kwargs(
        branch,
        instruction,
        chat_param,
        ins=provider_ins or ins,
        context_blocks=context_report.blocks if context_report else None,
    )

    imodel = chat_param.imodel or branch.chat_model
    if not chat_param._is_sentinel(chat_param.include_token_usage_to_model):
        kw["include_token_usage_to_model"] = chat_param.include_token_usage_to_model
    api_call = await imodel.invoke(**kw)

    await branch.emit_and_log(api_call)

    # Surface API errors before trying to parse a null response
    if api_call.status == EventStatus.FAILED:
        raise ExecutionError(f"API call failed: {api_call.execution.error}")

    if return_ins_res_message:
        return ins, AssistantResponse.from_response(
            api_call.response,
            sender=branch.id,
            recipient=branch.user,
        )
    return AssistantResponse.from_response(
        api_call.response,
        sender=branch.id,
        recipient=branch.user,
    ).response
