# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from typing import TYPE_CHECKING

from pydantic import JsonValue

from lionagi.protocols.generic import EventStatus
from lionagi.protocols.messages import AssistantResponse, Instruction

from ..types import ChatParam
from ._prepare import _prepare_run_kwargs

if TYPE_CHECKING:
    from lionagi.session.branch import Branch


async def chat(
    branch: "Branch",
    instruction: JsonValue | Instruction,
    chat_param: ChatParam,
    return_ins_res_message: bool = False,
) -> tuple[Instruction, AssistantResponse] | str:
    ins, kw = _prepare_run_kwargs(branch, instruction, chat_param)

    imodel = chat_param.imodel or branch.chat_model
    if not chat_param._is_sentinel(chat_param.include_token_usage_to_model):
        kw["include_token_usage_to_model"] = chat_param.include_token_usage_to_model
    api_call = await imodel.invoke(**kw)

    await branch.emit_and_log(api_call)

    # Surface API errors before trying to parse a null response
    if api_call.status == EventStatus.FAILED:
        raise RuntimeError(f"API call failed: {api_call.execution.error}")

    if return_ins_res_message:
        # Wrap result in `AssistantResponse` and return
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
