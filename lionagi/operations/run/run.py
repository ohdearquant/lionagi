# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import logging
import os
import sys
from collections.abc import AsyncGenerator
from dataclasses import fields
from functools import partial
from pathlib import Path
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
from lionagi.providers._provider_errors import WorkerLivenessError, classify_provider_error

from .._api_hooks import (
    _safe_identifier,
    emit_api_post_call,
    emit_api_pre_call,
    emit_api_stream_chunk,
)
from .._turn_origin import consume_turn_origin
from ..chat._prepare import _apply_context_providers, _build_instruction, _prepare_run_kwargs
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


async def _write_branch_snapshot(branch: Branch, snapshot_dir: str | Path) -> None:
    """Atomically write ``branch``'s current state to ``snapshot_dir/{branch.id}.json``.

    Writes to a sibling temp file then ``os.replace``s it into place, so a
    process kill mid-write never leaves a torn, unparseable snapshot behind.
    """
    fp = await acreate_path(
        snapshot_dir,
        str(branch.id),
        ".json",
        file_exist_ok=True,
    )
    tmp_fp = anyio.Path(str(fp) + ".tmp")
    async with await anyio.open_file(tmp_fp, "w") as f:
        # A non-finite float would be written as `null`, which no reader can
        # tell apart from a genuine null once the snapshot is on disk.
        await f.write(json_dumps(branch.to_dict(), check_non_finite=True))
    await anyio.to_thread.run_sync(partial(os.replace, str(tmp_fp), str(fp)))


async def _append_chunk(buffer_path, chunk) -> None:
    """Append one streamed chunk to the live JSONL buffer.

    A non-finite float is refused here rather than written as `null`, which
    a replay reader can't tell apart from a genuine null.
    """
    line = json_dumps(chunk.to_dict(), check_non_finite=True) + "\n"
    async with await anyio.open_file(buffer_path, "a") as f:
        await f.write(line)


async def _stream_with_deadline(model, api_call, deadline: float | None):
    """Iterate model.stream(api_call) with per-__anext__ anyio cancel scope; transparent passthrough when deadline is None.

    Explicitly closes the underlying stream on any exit instead of leaving it
    to async-generator GC — for a CLI provider, that cascades down to the
    subprocess reader and terminates the process group instead of leaking an
    orphaned subprocess. See docs/internals/providers.md#run-stream-cleanup-cascade.
    """
    agen = model.stream(api_call=api_call)
    try:
        stream_iter = agen.__aiter__()
        while True:
            try:
                if deadline is not None:
                    remaining = deadline - anyio.current_time()
                    if remaining <= 0:
                        raise TimeoutError("run() stream timeout exceeded")
                    with anyio.fail_after(remaining):
                        chunk = await stream_iter.__anext__()
                else:
                    chunk = await stream_iter.__anext__()
            except StopAsyncIteration:
                break
            yield chunk
    finally:
        _unwinding = sys.exc_info()[1] is not None
        try:
            await agen.aclose()
        except Exception as close_exc:
            logger.debug("run: inner stream aclose() raised during cleanup: %r", close_exc)
        except BaseException as close_exc:
            if not _unwinding:
                raise
            logger.debug(
                "run: inner stream aclose() raised %r while another exception was already "
                "propagating; suppressing the secondary cleanup failure",
                close_exc,
            )


def _stalled_worker_context(model, api_call) -> str:
    """Name the worker that stalled, for a log where many are in flight.

    A bare "no first stream output" line identifies nothing: not the call, not
    the model, not the provider. With dozens of concurrent spawns that makes a
    burst of stalls indistinguishable from one worker retrying, and it leaves
    no way to ask whether one provider or one model accounts for them. The
    fields here are the ones that let a reader join this line to the run that
    produced it.

    Every field is read defensively because this runs only on the failure
    path. An attribute error raised while describing a stall would replace the
    stall report with an unrelated traceback, which is strictly worse than the
    missing attribution it was added to fix.
    """
    fields = []
    call_id = getattr(api_call, "id", None)
    if call_id:
        fields.append(f"call={call_id}")
    # Provider and model arrive as whatever the caller put in the config, and a
    # key pasted into the wrong field is a real way for a credential to end up
    # in one of them. The API hooks already redact these two values for exactly
    # that reason, so this reads through the same helper instead of growing a
    # second one beside it. Presence is still decided by the raw value, so a
    # field that was never configured stays absent rather than becoming
    # "unknown", which would read as a value we failed to recognize.
    endpoint = getattr(model, "endpoint", None)
    config = getattr(endpoint, "config", None)
    provider = getattr(config, "provider", None) or ""
    if provider:
        fields.append(f"provider={_safe_identifier(provider)}")
    model_name = getattr(model, "model_name", None) or ""
    if model_name:
        fields.append(f"model={_safe_identifier(model_name)}")
    return " ".join(fields) or "worker unidentified"


async def _stream_with_liveness(
    model,
    kw: dict,
    stream_deadline: float | None,
    liveness_timeout: float | None,
    api_call_holder: list,
    max_attempts: int = 2,
    *,
    idle_timeout: float | None = None,
) -> AsyncGenerator:
    """Enforce first-output and between-chunk worker liveness windows.

    See docs/internals/providers.md#run-worker-liveness-watchdog for the retry
    and deadline-interaction contract. ``api_call_holder`` is a caller-owned
    list; the winning attempt's ``api_call`` is recorded at index 0.
    """
    liveness_timeout = liveness_timeout if liveness_timeout and liveness_timeout > 0 else None
    idle_timeout = idle_timeout if idle_timeout and idle_timeout > 0 else None

    if liveness_timeout is None and idle_timeout is None:
        api_call = await model.create_event(**kw)
        api_call_holder.append(api_call)
        await model.executor.append(api_call)
        agen = _stream_with_deadline(model, api_call, stream_deadline)
        try:
            async for chunk in agen:
                yield chunk
        finally:
            # Explicit close: GeneratorExit at `yield chunk` doesn't implicitly close `agen`.
            _unwinding = sys.exc_info()[1] is not None
            try:
                await agen.aclose()
            except Exception as close_exc:
                logger.debug(
                    "run: liveness watchdog passthrough agen.aclose() raised during cleanup: %r",
                    close_exc,
                )
            except BaseException as close_exc:
                if not _unwinding:
                    raise
                logger.debug(
                    "run: liveness watchdog passthrough agen.aclose() raised %r "
                    "while another exception was already propagating; "
                    "suppressing the secondary cleanup failure",
                    close_exc,
                )
        return

    # A first-output miss is safe to retry because nothing escaped the stream.
    # Once any chunk is yielded, the idle path below always fails immediately.
    attempts = max_attempts if liveness_timeout is not None else 1
    for attempt in range(attempts):
        api_call = await model.create_event(**kw)
        await model.executor.append(api_call)
        if api_call_holder:
            api_call_holder[0] = api_call
        else:
            api_call_holder.append(api_call)

        agen = _stream_with_deadline(model, api_call, stream_deadline)
        stream_iter = agen.__aiter__()

        is_liveness_boundary = False
        try:
            if liveness_timeout is None:
                first_chunk = await stream_iter.__anext__()
            else:
                remaining_to_deadline = (
                    stream_deadline - anyio.current_time() if stream_deadline is not None else None
                )
                # Liveness "owns" the timeout only when it's the tighter bound;
                # otherwise this is the caller's total-stream deadline.
                is_liveness_boundary = (
                    remaining_to_deadline is None or liveness_timeout < remaining_to_deadline
                )
                wait_for = (
                    liveness_timeout
                    if remaining_to_deadline is None
                    else max(0.0, min(liveness_timeout, remaining_to_deadline))
                )
                with anyio.fail_after(wait_for):
                    first_chunk = await stream_iter.__anext__()
        except StopAsyncIteration:
            # Zero chunks is a legitimate empty completion, not a hang.
            return
        except TimeoutError as exc:
            try:
                await agen.aclose()
            except Exception as close_exc:
                logger.debug(
                    "run: liveness watchdog agen.aclose() raised during cleanup: %r",
                    close_exc,
                )
            if liveness_timeout is None or not is_liveness_boundary:
                raise
            stalled = _stalled_worker_context(model, api_call)
            if attempt == attempts - 1:
                raise WorkerLivenessError(
                    f"worker produced no first stream output within "
                    f"{liveness_timeout:.0f}s across {attempts} attempt(s) "
                    f"[{stalled}]",
                    reason="worker.no_first_output",
                ) from exc
            logger.warning(
                "run: no first stream output within %.0fs (attempt %d/%d) [%s]; "
                "retrying worker subprocess",
                liveness_timeout,
                attempt + 1,
                attempts,
                stalled,
            )
            continue
        except BaseException:
            # Cancellation/GeneratorExit can land before the first chunk arrives,
            # before the post-yield finally below runs — close explicitly here too.
            _unwinding = sys.exc_info()[1] is not None
            try:
                await agen.aclose()
            except Exception as close_exc:
                logger.debug(
                    "run: liveness watchdog agen.aclose() raised during first-chunk cleanup: %r",
                    close_exc,
                )
            except BaseException as close_exc:
                if not _unwinding:
                    raise
                logger.debug(
                    "run: liveness watchdog agen.aclose() raised %r while "
                    "another exception was already propagating; suppressing "
                    "the secondary cleanup failure",
                    close_exc,
                )
            raise
        else:
            try:
                yield first_chunk
                if idle_timeout is None:
                    async for chunk in agen:
                        yield chunk
                else:
                    while True:
                        remaining_to_deadline = (
                            stream_deadline - anyio.current_time()
                            if stream_deadline is not None
                            else None
                        )
                        # As for first output, the tighter bound owns the
                        # failure. Equality belongs to the overall deadline.
                        is_idle_boundary = (
                            remaining_to_deadline is None or idle_timeout < remaining_to_deadline
                        )
                        wait_for = (
                            idle_timeout
                            if remaining_to_deadline is None
                            else max(0.0, min(idle_timeout, remaining_to_deadline))
                        )
                        try:
                            with anyio.fail_after(wait_for):
                                chunk = await stream_iter.__anext__()
                        except StopAsyncIteration:
                            break
                        except TimeoutError as exc:
                            if not is_idle_boundary:
                                raise
                            raise WorkerLivenessError(
                                f"worker produced no stream output for "
                                f"{idle_timeout:.0f}s after partial output",
                                reason="worker.stream_idle",
                            ) from exc
                        yield chunk
            finally:
                # Same explicit-close reasoning as the passthrough branch above.
                _unwinding = sys.exc_info()[1] is not None
                try:
                    await agen.aclose()
                except Exception as close_exc:
                    logger.debug(
                        "run: liveness watchdog passthrough agen.aclose() raised "
                        "during cleanup: %r",
                        close_exc,
                    )
                except BaseException as close_exc:
                    if not _unwinding:
                        raise
                    logger.debug(
                        "run: liveness watchdog passthrough agen.aclose() raised %r "
                        "while another exception was already propagating; "
                        "suppressing the secondary cleanup failure",
                        close_exc,
                    )
            return


# Result-chunk metadata fields some CLI providers (codex) emit as a per-turn
# delta rather than a running total; providers emitting one result chunk per
# call sum correctly here too since there's nothing else to add.
_RESULT_META_DELTA_KEYS = ("total_cost_usd", "num_turns")


def _accumulate_result_meta(result_meta: dict, metadata: dict) -> None:
    """Merge a "result" chunk's metadata into the in-progress accumulator.

    Numeric usage/cost/turn fields are summed; everything else is overwritten
    with the latest value.
    """
    for key, value in metadata.items():
        if key == "usage" and isinstance(value, dict):
            usage = result_meta.setdefault("usage", {})
            for uk, uv in value.items():
                if isinstance(uv, (int, float)) and not isinstance(uv, bool):
                    usage[uk] = usage.get(uk, 0) + uv
                else:
                    usage[uk] = uv
        elif (
            key in _RESULT_META_DELTA_KEYS
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
        ):
            result_meta[key] = result_meta.get(key, 0) + value
        else:
            result_meta[key] = value


async def run(
    branch: Branch,
    instruction: JsonValue | Instruction,
    param: RunParam,
) -> AsyncGenerator[RoledMessage]:
    """Stream a CLI-backed model turn, yielding Instruction/AssistantResponse/ActionRequest/ActionResponse messages.

    Emits at most one terminal signal (RunEnd or RunFailed) per call when an
    observer is attached; see docs/internals/providers.md#run-lifecycle-signal-ordering.
    """
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

    import time as _time  # noqa: PLC0415

    # Built synchronously, no I/O — the origin guard below needs it before any
    # other awaited operation for this turn.
    ins = _build_instruction(branch, instruction, param)

    from lionagi.session._lifecycle_ctx import suppress_lifecycle_var

    _suppress_lifecycle = suppress_lifecycle_var.get()
    has_observer = branch._observer is not None and not _suppress_lifecycle

    _run_exc: BaseException | None = None
    _terminal_emitted: bool = False
    _api_call_started: bool = False
    _t0_run = _time.monotonic()

    try:
        # Must run first, before context providers / RunStart / any commit or
        # yield: a rejection here must leave no lifecycle trace beyond itself.
        # See docs/internals/providers.md#run-lifecycle-signal-ordering.
        _turn_origin_token = consume_turn_origin(param.turn_origin)
        if _turn_origin_token is not None and branch._hooks is not None:
            from lionagi.hooks.bus import HookPoint

            _prompt = ins.rendered
            if not isinstance(_prompt, str):
                _prompt = str(_prompt)
            try:
                await branch._hooks.emit(
                    HookPoint.USER_PROMPT_SUBMIT,
                    session_id=str(branch._owning_session_id or branch.id),
                    branch_id=str(branch.id),
                    prompt=_prompt,
                    model=getattr(branch.chat_model, "model_name", None) or "",
                    permission_mode="default",
                )
            except GeneratorExit:
                raise
            except BaseException as _exc:
                _run_exc = _exc
                raise

        if has_observer:
            from lionagi.session.signal import RunStart

            try:
                await branch.emit(RunStart())
            except Exception:
                logger.exception(
                    "run: observer raised during RunStart emission; run proceeds normally"
                )

        provider_ins, context_report = await _apply_context_providers(
            branch, instruction, param, ins=ins
        )
        ins, kw = _prepare_run_kwargs(
            branch,
            instruction,
            param,
            ins=provider_ins or ins,
            context_blocks=context_report.blocks if context_report else None,
        )

        # Committed before yielding: a consumer receiving this Instruction must
        # find it already in branch.messages, not merely in flight.
        await branch.msgs.a_add_message(instruction=ins)

        yield ins

        if branch.chat_model.provider_session_id is not None:
            kw["resume"] = branch.chat_model.provider_session_id

        model = branch.chat_model
        endpoint = model.endpoint
        prev_stream_func = model.streaming_process_func
        bfp = None

        if param.stream_persist:
            # snapshot_dir for find_branch() lookups; persist_dir for the live JSONL
            # buffer. Written before the stream starts so a mid-turn kill still
            # leaves a resumable checkpoint; the finally block overwrites it on exit.
            snapshot_dir = param.snapshot_dir or param.persist_dir
            await _write_branch_snapshot(branch, snapshot_dir)

            bfp = await acreate_path(
                param.persist_dir,
                str(branch.id) + ".buffer",
                ".jsonl",
                file_exist_ok=True,
            )

            async def _persist_chunk(chunk):
                if hasattr(chunk, "to_dict"):
                    await _append_chunk(bfp, chunk)
                if prev_stream_func is not None:
                    from lionagi.ln import is_coro_func

                    if is_coro_func(prev_stream_func):
                        return await prev_stream_func(chunk)
                    return prev_stream_func(chunk)
                return None

            model.streaming_process_func = _persist_chunk

        thinking_parts: list[str] = []
        text_parts: list[str] = []
        # Provider-reported usage from the terminal "result" chunk, stamped onto
        # the final AssistantResponse (re-tokenizing history undercounts tool turns).
        result_meta: dict = {}
        # Whole-call accumulator, never cleared (unlike result_meta, which resets
        # per flush): codex splits one run() into multiple flush windows with
        # marginal per-window deltas, so API_POST_CALL must sum every window.
        _total_usage_meta: dict = {}
        last_usage: dict | None = None

        async def _flush_response() -> AssistantResponse | None:
            if not text_parts:
                return None
            text = "".join(text_parts)
            metadata: dict = {}
            if thinking_parts:
                metadata["thinking"] = "\n".join(thinking_parts)
            if result_meta:
                metadata["model_response"] = dict(result_meta)
                # Clear after stamping: a later flush in the same run() call must
                # not restamp already-recorded usage and double-count it.
                result_meta.clear()
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

        # CLI providers don't consume timeout; None/0/negative disables enforcement.
        _timeout = kw.pop("timeout", None)
        _stream_deadline: float | None = None
        if isinstance(_timeout, int | float) and _timeout > 0:
            _stream_deadline = anyio.current_time() + float(_timeout)

        request_model = getattr(endpoint, "_request_model", None)
        if request_model is not None:
            from lionagi.providers.google.gemini_code import (
                GeminiCodeRequest,
                derive_print_timeout,
                format_print_timeout,
            )

            endpoint_kwargs = getattr(getattr(endpoint, "config", None), "kwargs", {})
            if (
                request_model is GeminiCodeRequest
                and kw.get("print_timeout") is None
                and endpoint_kwargs.get("print_timeout") is None
            ):
                if _stream_deadline is not None:
                    kw["print_timeout"] = derive_print_timeout(_timeout)
                else:
                    from lionagi.config import settings as _app_settings  # noqa: PLC0415

                    default_cap = _app_settings.LIONAGI_ANTIGRAVITY_PRINT_TIMEOUT
                    kw["print_timeout"] = format_print_timeout(default_cap)

        # Explicit values always honored; absent values fall back to configured
        # defaults only for endpoints declaring streams_first_output_early. A
        # buffered transport would otherwise misdiagnose a healthy long call.
        _liveness_timeout_explicit = "liveness_timeout" in kw
        _liveness_timeout = kw.pop("liveness_timeout", None)
        _idle_timeout_explicit = "idle_timeout" in kw
        _idle_timeout = kw.pop("idle_timeout", None)
        _streams_output_early = getattr(endpoint, "streams_first_output_early", False)
        if _streams_output_early and (
            (_liveness_timeout is None and not _liveness_timeout_explicit)
            or (_idle_timeout is None and not _idle_timeout_explicit)
        ):
            from lionagi.config import settings as _app_settings  # noqa: PLC0415

            if _liveness_timeout is None and not _liveness_timeout_explicit:
                _liveness_timeout = _app_settings.LIONAGI_WORKER_LIVENESS_TIMEOUT
            if _idle_timeout is None and not _idle_timeout_explicit:
                _idle_timeout = _app_settings.LIONAGI_WORKER_IDLE_TIMEOUT
        if not isinstance(_liveness_timeout, int | float) or _liveness_timeout <= 0:
            _liveness_timeout = None
        if not isinstance(_idle_timeout, int | float) or _idle_timeout <= 0:
            _idle_timeout = None

        kw["stream"] = True
        _api_call_holder: list = []
        await emit_api_pre_call(branch, model)
        _api_call_started = True
        stream_gen = _stream_with_liveness(
            model,
            kw,
            _stream_deadline,
            _liveness_timeout,
            _api_call_holder,
            idle_timeout=_idle_timeout,
        )
        try:
            try:
                async for chunk in stream_gen:
                    if branch._hooks is not None:
                        await emit_api_stream_chunk(branch, model, chunk)
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
                                _accumulate_result_meta(result_meta, chunk.metadata)
                                _accumulate_result_meta(_total_usage_meta, chunk.metadata)
                                if isinstance(_total_usage_meta.get("usage"), dict):
                                    last_usage = dict(_total_usage_meta["usage"])

                        case "error":
                            # Only metadata={"benign_eos": True} marks a resumed-session
                            # end-of-stream; any other error chunk is a real failure.
                            if chunk.metadata.get("benign_eos"):
                                logger.debug(
                                    "run: provider end-of-stream sentinel received, "
                                    "ending stream cleanly"
                                )
                                break
                            # A reconnect notice is the provider CLI retrying its own
                            # dropped stream: the process is still running and will
                            # either resume emitting events or produce a real terminal
                            # error, so the run keeps consuming instead of raising.
                            if chunk.metadata.get("reconnect_notice"):
                                logger.warning(
                                    "run: provider reconnecting mid-stream (%s); "
                                    "continuing to consume",
                                    chunk.content,
                                )
                                continue
                            # Persist text already delivered before a late failure destroys it.
                            if res := await _flush_response():
                                yield res
                            content = chunk.content or "(empty error)"
                            raise classify_provider_error(content)

                if res := await _flush_response():
                    _final_api_call = _api_call_holder[0] if _api_call_holder else None
                    if _final_api_call is not None and hasattr(_final_api_call, "to_dict"):
                        call_meta = Note.from_dict(_final_api_call.to_dict())
                        call_meta.pop(["execution", "response"], None)
                        res.metadata["api_call_meta"] = call_meta.to_dict()
                    _check_control(branch)
                    yield res
            except _StopStream:
                pass
            except GeneratorExit:
                # Consumer abandoned the generator (break / aclose()).  Classify
                # as RunEnd (clean abandonment).  The outer finally will emit the
                # terminal signal; re-raise here so the outer try sees it too.
                raise
            except RuntimeError as _exc:
                # ProviderError is a RuntimeError subclass — avoid double-wrapping; re-raise if already classified.
                from lionagi.providers._provider_errors import ProviderError

                if isinstance(_exc, ProviderError):
                    _run_exc = _exc
                    raise
                classified = classify_provider_error(str(_exc))
                _run_exc = classified
                raise classified from _exc
            except BaseException as _exc:
                _run_exc = _exc
                raise
        finally:
            # Explicit close on ANY exit cascades down to the subprocess reader and
            # terminates the process group instead of leaking it. The close chain
            # can raise CancelledError (a BaseException `except Exception` won't
            # catch) — preserve whatever exception is already propagating instead
            # of letting a secondary cleanup failure replace it.
            _unwinding = sys.exc_info()[1] is not None
            try:
                await stream_gen.aclose()
            except Exception as _close_exc:
                logger.debug("run: stream_gen.aclose() raised during cleanup: %r", _close_exc)
            except BaseException as _close_exc:
                if not _unwinding:
                    raise
                logger.debug(
                    "run: aclose() raised %r while another exception was already "
                    "propagating; suppressing the secondary cleanup failure",
                    _close_exc,
                )
            model.streaming_process_func = prev_stream_func
            if param.stream_persist:
                snapshot_dir = param.snapshot_dir or param.persist_dir
                await _write_branch_snapshot(branch, snapshot_dir)
                if bfp is not None:
                    bfp_path = anyio.Path(bfp)
                    if await bfp_path.exists():
                        await bfp_path.unlink()

    except GeneratorExit:
        # Never suppressed — emit RunEnd then re-raise for runtime teardown.
        await branch.drain_signals()
        if has_observer and not _terminal_emitted:
            _terminal_emitted = True
            try:
                from lionagi.session.signal import build_run_end

                duration_ms = (_time.monotonic() - _t0_run) * 1000.0
                await branch.emit(build_run_end(branch, duration_ms=duration_ms))
            except Exception:
                logger.exception("run: observer raised during RunEnd emission on GeneratorExit")
        raise
    except BaseException as _exc:
        # Catches anything the more specific handlers above didn't classify;
        # without this the finally below would emit a false RunEnd instead of RunFailed.
        if _run_exc is None:
            _run_exc = _exc
        raise
    finally:
        # _terminal_emitted guards double emission on Python <3.11, where finally also runs after GeneratorExit.
        await branch.drain_signals()

        if _api_call_started:
            _terminal_api_call = _api_call_holder[0] if _api_call_holder else None
            await emit_api_post_call(
                branch,
                branch.chat_model,
                _terminal_api_call,
                error=_run_exc,
                tokens=last_usage,
            )

        if has_observer and not _terminal_emitted:
            _terminal_emitted = True
            try:
                if _run_exc is None:
                    from lionagi.session.signal import build_run_end

                    duration_ms = (_time.monotonic() - _t0_run) * 1000.0
                    await branch.emit(build_run_end(branch, duration_ms=duration_ms))
                else:
                    from lionagi.session.signal import RunFailed

                    await branch.emit(RunFailed(data=_run_exc))
            except GeneratorExit:
                raise
            except Exception:
                logger.exception(
                    "run: observer raised during lifecycle signal emission; "
                    "run outcome is preserved"
                )


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
    """Middle-protocol implementation for CLI endpoints: stream via run(), accumulate assistant text, optionally parse."""
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

    from ..parse.parse import _try_propagate_structure
    from ..parse.parse import parse as _parse

    ins_content = getattr(ins_msg, "content", None) if ins_msg is not None else None
    parse_param = _try_propagate_structure(ins_content, parse_param)

    return await _parse(branch, full_text, parse_param)
