# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from dataclasses import fields
from typing import TYPE_CHECKING, Any

import anyio
from pydantic import JsonValue

from lionagi.ln import acreate_path, json_dumps
from lionagi.models.note import Note
from lionagi.protocols.messages import (
    ActionRequest,
    AssistantResponse,
    AssistantResponseContent,
    Instruction,
)

from ..chat._prepare import _prepare_run_kwargs
from ..types import ChatParam, ParseParam, RunParam

if TYPE_CHECKING:
    from lionagi.protocols.messages.message import RoledMessage
    from lionagi.session.branch import Branch

from lionagi.operations._observe import (
    StopStream as _StopStream,
)
from lionagi.operations._observe import (
    check_control as _check_control,
)

logger = logging.getLogger(__name__)


async def run(
    branch: Branch,
    instruction: JsonValue | Instruction,
    param: RunParam,
) -> AsyncGenerator[RoledMessage, None]:
    if not param._is_sentinel(param.imodel):
        branch.chat_model = param.imodel

    if not branch.chat_model.is_cli:
        provider = getattr(branch.chat_model.endpoint.config, "provider", "unknown")
        raise ValueError(
            f"run operation only supports CLI endpoints, but got provider={provider!r}. "
            "Use one of the CLI endpoint prefixes: claude_code, codex, gemini-cli, pi. "
            "Did you mean 'gemini-cli/<model>' instead of 'gemini/<model>'? "
            "The 'gemini' prefix routes to the REST API, not the local Gemini CLI."
        )

    ins, kw = _prepare_run_kwargs(branch, instruction, param)
    await branch.msgs.a_add_message(instruction=ins)
    yield ins

    if branch.chat_model.provider_session_id is not None:
        kw["resume"] = branch.chat_model.provider_session_id

    model = branch.chat_model
    endpoint = model.endpoint
    prev_stream_func = model.streaming_process_func
    bfp = None

    if param.stream_persist:
        # Branch snapshot lives in snapshot_dir (when set) so it lands
        # where find_branch() looks; the streaming buffer always lives
        # next to the rest of the run-time chunks under persist_dir.
        snapshot_dir = param.snapshot_dir or param.persist_dir
        fp = await acreate_path(
            snapshot_dir,
            str(branch.id),
            ".json",
            file_exist_ok=True,
        )
        async with await anyio.open_file(fp, "w") as f:
            await f.write(json_dumps(branch.to_dict()))

        # JSONL buffer for real-time monitoring
        bfp = await acreate_path(
            param.persist_dir,
            str(branch.id) + ".buffer",
            ".jsonl",
            file_exist_ok=True,
        )

        # Inject streaming persist into imodel's chunk processor
        async def _persist_chunk(chunk):
            if hasattr(chunk, "to_dict"):
                async with await anyio.open_file(bfp, "a") as f:
                    await f.write(json_dumps(chunk.to_dict()) + "\n")
            if prev_stream_func is not None:
                from lionagi.ln import is_coro_func

                if is_coro_func(prev_stream_func):
                    return await prev_stream_func(chunk)
                return prev_stream_func(chunk)
            return None

        model.streaming_process_func = _persist_chunk

    # Signal emission is now universal: every a_add_message below fires the
    # branch's on_message_added hook, which schedules the bus emission in the
    # background (branch._signal_tasks) and is drained in `finally`. The stream
    # loop only polls control between chunks (_check_control).

    # Accumulation buffers
    thinking_parts: list[str] = []
    text_parts: list[str] = []
    # Provider-reported usage/cost from the terminal ``result`` chunk
    # (codex: input/output tokens; claude_code: usage, total_cost_usd,
    # num_turns, duration_ms). Captured once at stream end and stamped onto
    # the final AssistantResponse so callers (Studio cost tracking, the
    # orchestration benchmark) can read real CLI usage — re-tokenizing the
    # message history undercounts the agent's internal tool turns.
    result_meta: dict = {}

    async def _flush_response() -> AssistantResponse | None:
        if not text_parts:
            return None
        text = "".join(text_parts)
        metadata: dict = {}
        if thinking_parts:
            metadata["thinking"] = "\n".join(thinking_parts)
        if result_meta:
            metadata["model_response"] = dict(result_meta)
        res = AssistantResponse(
            content=AssistantResponseContent(assistant_response=text),
            sender=branch.id,
            recipient=branch.user or "user",
        )
        if metadata:
            res.metadata.update(metadata)
        await branch.msgs.a_add_message(assistant_response=res)
        text_parts.clear()
        thinking_parts.clear()
        return res

    pending_requests: dict[str, ActionRequest] = {}

    # Extract caller-supplied wall-clock timeout (seconds). The CLI-provider
    # stream loops are unbounded — without an outer fail_after the codex /
    # claude_code subprocess can run indefinitely even when the caller
    # passed ``Branch.operate(timeout=N)`` or ``li agent --timeout N``.
    # ``None`` / 0 / negative disables enforcement (back-compat with existing
    # callers that never set a timeout). The downstream provider does NOT
    # consume ``timeout`` — pop it so it doesn't pollute create_event kwargs.
    _timeout = kw.pop("timeout", None)
    _stream_timeout: float | None = None
    if isinstance(_timeout, int | float) and _timeout > 0:
        _stream_timeout = float(_timeout)

    kw["stream"] = True
    api_call = await model.create_event(**kw)
    await model.executor.append(api_call)

    try:
        try:
            # ``anyio.fail_after(None)`` is a no-op context, so we can wrap
            # unconditionally and let the no-timeout case pay the (small)
            # context-manager cost rather than branching the stream loop.
            with anyio.fail_after(_stream_timeout):
                async for chunk in model.stream(api_call=api_call):
                    match chunk.type:
                        case "system":
                            if sid := chunk.metadata.get("session_id"):
                                endpoint.session_id = sid

                        case "thinking":
                            if chunk.content:
                                thinking_parts.append(chunk.content)

                        case "text":
                            if chunk.content:
                                text_parts.append(chunk.content)

                        case "tool_use":
                            if res := await _flush_response():
                                _check_control(branch)
                                yield res

                            act_req = branch.msgs.create_action_request(
                                function=chunk.tool_name or "",
                                arguments=chunk.tool_input or {},
                                sender=branch.id,
                                recipient=branch.user or "user",
                            )
                            if chunk.tool_id:
                                pending_requests[chunk.tool_id] = act_req
                            await branch.msgs.a_add_message(action_request=act_req)
                            _check_control(branch)
                            yield act_req

                        case "tool_result":
                            orig_req = (
                                pending_requests.pop(chunk.tool_id, None) if chunk.tool_id else None
                            )
                            if orig_req is None:
                                continue

                            act_res = branch.msgs.create_action_response(
                                action_request=orig_req,
                                action_output=chunk.tool_output,
                                sender=branch.user or "user",
                                recipient=branch.id,
                            )
                            if chunk.is_error:
                                act_res.metadata["is_error"] = True
                            await branch.msgs.a_add_message(
                                action_request=orig_req,
                                action_output=chunk.tool_output,
                                action_response=act_res,
                                sender=branch.user or "user",
                                recipient=branch.id,
                            )
                            _check_control(branch)
                            yield act_res

                        case "result":
                            if chunk.metadata:
                                result_meta.update(chunk.metadata)

                        case "error":
                            raise RuntimeError(chunk.content or "Stream error from CLI endpoint")

                if res := await _flush_response():
                    if hasattr(api_call, "to_dict"):
                        call_meta = Note.from_dict(api_call.to_dict())
                        call_meta.pop(["execution", "response"], None)
                        res.metadata["api_call_meta"] = call_meta.to_dict()
                    _check_control(branch)
                    yield res
        except _StopStream:
            pass
    finally:
        # Drain background signal emissions before returning so handler
        # results and exceptions are not silently lost.
        await branch.drain_signals()

        # Restore original streaming func
        model.streaming_process_func = prev_stream_func

        # Consolidate: always persist branch state on any exit
        if param.stream_persist:
            snapshot_dir = param.snapshot_dir or param.persist_dir
            fp = await acreate_path(
                snapshot_dir,
                str(branch.id),
                ".json",
                file_exist_ok=True,
            )
            async with await anyio.open_file(fp, "w") as f:
                await f.write(json_dumps(branch.to_dict()))
            if bfp is not None:
                bfp_path = anyio.Path(bfp)
                if await bfp_path.exists():
                    await bfp_path.unlink()


def _promote_to_run_param(chat_param: ChatParam) -> RunParam:
    if isinstance(chat_param, RunParam):
        return chat_param
    kw = {f.name: getattr(chat_param, f.name) for f in fields(ChatParam)}
    return RunParam(**kw)


async def run_and_collect(
    branch: Branch,
    instruction: JsonValue | Instruction,
    chat_param: ChatParam,
    parse_param: ParseParam | None = None,
    clear_messages: bool = False,
    skip_validation: bool = False,
) -> Any:
    """Stream via run(), accumulate assistant text, optionally parse.

    Satisfies the ``Middle`` protocol for operate(). Stream the model,
    collect assistant text across chunks, then parse via ``parse_param``
    if a response_format is set. ``clear_messages`` clears branch
    messages before the turn.
    """
    if clear_messages:
        branch.msgs.clear_messages()

    run_param = _promote_to_run_param(chat_param)

    all_texts: list[str] = []
    ins_msg = None
    async for msg in run(branch, instruction, run_param):
        if isinstance(msg, Instruction) and ins_msg is None:
            ins_msg = msg
        if isinstance(msg, AssistantResponse):
            text = msg.response or ""
            if text:
                all_texts.append(text)

    if not all_texts:
        return None

    full_text = "\n\n".join(all_texts)

    if skip_validation:
        return full_text

    if parse_param is None or parse_param.response_format is None:
        return full_text

    # Pull structure from the instruction message
    from lionagi.operations.schema.structure import Structure

    if not isinstance(parse_param.structure, Structure) and ins_msg is not None:
        si = getattr(ins_msg.content, "_structure_instance", None)
        if si is not None:
            parse_param = parse_param.with_updates(structure=si)

    from ..parse.parse import parse as _parse

    return await _parse(branch, full_text, parse_param)
