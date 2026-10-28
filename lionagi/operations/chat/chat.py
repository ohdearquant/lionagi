# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from typing import TYPE_CHECKING, Any

from pydantic import JsonValue

from lionagi._errors import ExecutionError
from lionagi.protocols.generic import EventStatus
from lionagi.protocols.messages import AssistantResponse, Instruction

from .._api_hooks import emit_api_post_call, emit_api_pre_call
from .._turn_origin import consume_turn_origin
from ..types import ChatParam
from ._prepare import _apply_context_providers, _build_instruction, _prepare_run_kwargs

if TYPE_CHECKING:
    from lionagi.session.branch import Branch


async def _emit_user_prompt_submit(
    branch: "Branch", chat_param: ChatParam, ins: Instruction, imodel: Any = None
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
        model=getattr(imodel, "model_name", None) or "",
        permission_mode="default",
    )


async def chat(
    branch: "Branch",
    instruction: JsonValue | Instruction,
    chat_param: ChatParam,
    return_ins_res_message: bool = False,
) -> tuple[Instruction, AssistantResponse] | str:
    # Built synchronously, no I/O — the guard below must run before context
    # providers get a chance to run their (potentially side-effecting) gather,
    # mirroring run()'s ordering (operations/run/run.py).
    ins = _build_instruction(branch, instruction, chat_param)

    # Sentinel status, not truthiness: a supplied model that defines __bool__
    # as False is still a supplied model and must override the branch default.
    imodel = branch.chat_model if chat_param._is_sentinel(chat_param.imodel) else chat_param.imodel
    await _emit_user_prompt_submit(branch, chat_param, ins, imodel=imodel)

    provider_ins, context_report = await _apply_context_providers(
        branch, instruction, chat_param, ins=ins
    )
    # Fired before the request is compiled, so a handler that curates the active view (the
    # host-side context fold) shapes the request it precedes rather than the next one.
    await emit_api_pre_call(branch, imodel)
    try:
        places_cache = getattr(getattr(imodel, "endpoint", None), "places_prompt_cache", False)
        cache_view: dict | None = {} if places_cache else None
        ins, kw = _prepare_run_kwargs(
            branch,
            instruction,
            chat_param,
            ins=provider_ins or ins,
            context_blocks=context_report.blocks if context_report else None,
            imodel=imodel,
            cache_view=cache_view,
        )

        if not chat_param._is_sentinel(chat_param.include_token_usage_to_model):
            kw["include_token_usage_to_model"] = chat_param.include_token_usage_to_model

        from lionagi.service._prompt_cache import cache_anchors, conversation_call
        from lionagi.service.imodel import _branch_routing_context

        anchors: tuple[tuple[int, int], ...] = ()
        frontier = None
        if cache_view is not None:
            # This is the request's own compile, so the first round is decided here, once
            # (ADR-0130 Amendment 4); the fold's sizing compiles only read the decision.
            anchors, frontier = cache_anchors(
                branch.metadata, cache_view, kw["messages"], branch._prompt_cache_protect
            )

        with (
            _branch_routing_context(branch.id, imodel),
            conversation_call(
                branch.metadata, imodel, anchors=anchors, frontier=frontier
            ) as cache_call,
        ):
            api_call = await imodel.invoke(**kw)
    except BaseException as exc:
        # BaseException (asyncio.CancelledError included): cancellation mid-invoke
        # must still pair with a post emission, or a hook-bus observer sees an open span.
        await emit_api_post_call(branch, imodel, error=exc)
        raise
    if cache_call is not None:
        from lionagi.service._prompt_cache import settle

        # Recorded on the call before it is logged, so the branch log carries the marks,
        # the intent and the read verdict beside the usage they are judged against.
        api_call.metadata["prompt_cache"] = settle(cache_call, api_call)
    await emit_api_post_call(branch, imodel, api_call)

    await branch.emit_and_log(api_call)

    # Surface API errors before trying to parse a null response
    if api_call.status == EventStatus.FAILED:
        cause = api_call.execution.error
        error = ExecutionError(f"API call failed: {cause}")
        if isinstance(cause, BaseException):
            # Preserve the original exception so callers can classify the failure by type.
            raise error from cause
        # Recorded only as text: no cause, and an exception the caller is handling stays the context.
        raise error

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
