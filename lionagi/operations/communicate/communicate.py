# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

import warnings
from typing import TYPE_CHECKING, Any, Literal

from pydantic import JsonValue

from lionagi.ln.fuzzy import FuzzyMatchKeysParams
from lionagi.ln.fuzzy._fuzzy_validate import fuzzy_validate_mapping
from lionagi.ln.types import Undefined

from ..types import ChatParam, ParseParam

if TYPE_CHECKING:
    from lionagi.protocols.messages.instruction import Instruction
    from lionagi.session.branch import Branch


def prepare_communicate_kw(
    branch: "Branch",
    instruction=None,
    *,
    guidance=None,
    context=None,
    plain_content=None,
    sender=None,
    recipient=None,
    progression=None,
    response_format=None,
    request_fields=None,
    chat_model=None,
    parse_model=None,
    skip_validation=False,
    images=None,
    image_detail: Literal["low", "high", "auto"] = "auto",
    num_parse_retries=3,
    fuzzy_match_kwargs=None,
    clear_messages=False,
    include_token_usage_to_model: bool = False,
    **kwargs,
):
    imodel = chat_model or branch.chat_model
    parse_model = parse_model or branch.parse_model

    if num_parse_retries > 5:
        warnings.warn(
            f"num_parse_retries={num_parse_retries} is high. Lowering to 5. Suggestion: <3",
            UserWarning,
            stacklevel=2,
        )
        num_parse_retries = 5

    # Build contexts
    chat_param = ChatParam(
        guidance=guidance,
        context=context,
        sender=sender or branch.user or "user",
        recipient=recipient or branch.id,
        response_format=response_format,
        progression=progression,
        tool_schemas=[],
        images=images or [],
        image_detail=image_detail,
        plain_content=plain_content or "",
        include_token_usage_to_model=include_token_usage_to_model,
        imodel=imodel,
        imodel_kw=kwargs,
    )

    parse_param = None
    if response_format and not skip_validation:
        from ..parse.parse import get_default_call

        fuzzy_kw = fuzzy_match_kwargs or {}
        handle_validation = fuzzy_kw.pop("handle_validation", "raise")

        parse_param = ParseParam(
            response_format=response_format,
            fuzzy_match_params=(
                FuzzyMatchKeysParams(**fuzzy_kw) if fuzzy_kw else FuzzyMatchKeysParams()
            ),
            handle_validation=handle_validation,
            alcall_params=get_default_call().with_updates(retry_attempts=num_parse_retries),
            imodel=parse_model,
            imodel_kw={},
        )

    return {
        "instruction": instruction or "",
        "chat_param": chat_param,
        "parse_param": parse_param,
        "clear_messages": clear_messages,
        "skip_validation": skip_validation,
        "request_fields": request_fields,
    }


async def communicate(
    branch: "Branch",
    instruction: "JsonValue | Instruction",
    chat_param: ChatParam,
    parse_param: ParseParam | None = None,
    clear_messages: bool = False,
    skip_validation: bool = False,
    request_fields: dict | None = None,
) -> Any:
    if clear_messages:
        branch.msgs.clear_messages()

    from ..chat.chat import chat

    ins, res = await chat(branch, instruction, chat_param, return_ins_res_message=True)

    await branch.msgs.a_add_message(instruction=ins)
    await branch.msgs.a_add_message(assistant_response=res)

    if skip_validation:
        return res.response

    # Handle response_format with parse
    if parse_param and chat_param.response_format:
        from lionagi.protocols.messages.assistant_response import AssistantResponse

        # Pull structure from the instruction message
        from lionagi.protocols.structure.base import Structure

        from ..parse.parse import parse

        if not isinstance(parse_param.structure, Structure) and hasattr(ins, "content"):
            si = getattr(ins.content, "_structure_instance", None)
            if si is not None:
                parse_param = parse_param.with_updates(structure=si)

        try:
            out, res2 = await parse(branch, res.response, parse_param, return_res_message=True)
            if res2 and isinstance(res2, AssistantResponse):
                res.metadata["original_model_response"] = res.model_response
                # model_response is read-only property - update metadata instead
                res.metadata["model_response"] = res2.model_response
            return out
        except ValueError as e:
            # Re-raise with more context
            raise ValueError(
                f"Failed to parse model response into {chat_param.response_format}: {e}"
            ) from e

    # Handle request_fields with fuzzy validation
    if request_fields is not None:
        _d = fuzzy_validate_mapping(
            res.response,
            request_fields,
            handle_unmatched="force",
            fill_value=Undefined,
        )
        return {k: v for k, v in _d.items() if v != Undefined}

    return res.response
