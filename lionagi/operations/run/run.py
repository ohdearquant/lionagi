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

from ..chat._prepare import _apply_context_providers, _prepare_run_kwargs
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

    Writes to a sibling temp file first, then renames it into place. A plain
    ``open(..., "w") + write`` truncates the target before the new content
    lands — a process kill (SIGTERM/SIGKILL) mid-write would leave a
    zero-byte or partially-written snapshot that ``find_branch`` locates but
    ``json.loads`` can't parse, making the branch unresumable even though a
    snapshot file exists. ``os.replace`` is atomic on POSIX and Windows, so a
    reader always sees either the previous complete snapshot or the new one,
    never a torn write.
    """
    fp = await acreate_path(
        snapshot_dir,
        str(branch.id),
        ".json",
        file_exist_ok=True,
    )
    tmp_fp = anyio.Path(str(fp) + ".tmp")
    async with await anyio.open_file(tmp_fp, "w") as f:
        await f.write(json_dumps(branch.to_dict()))
    await anyio.to_thread.run_sync(partial(os.replace, str(tmp_fp), str(fp)))


async def _stream_with_deadline(model, api_call, deadline: float | None):
    """Iterate model.stream(api_call) with per-__anext__ anyio cancel scope; transparent passthrough when deadline is None.

    Closes the underlying stream explicitly so an early exit (exception,
    consumer abandonment) deterministically closes it instead of leaving it
    to async-generator GC — for a CLI provider that close cascades down to
    the subprocess reader's own ``finally`` and terminates the process
    group; without it, an abandoned generator can leave the CLI subprocess
    running to completion, orphaned, after the caller already gave up.
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


async def _stream_with_liveness(
    model,
    kw: dict,
    stream_deadline: float | None,
    liveness_timeout: float | None,
    api_call_holder: list,
    max_attempts: int = 2,
) -> AsyncGenerator:
    """Spawn the worker subprocess and enforce a first-output liveness window.

    A worker whose subprocess dies at/near spawn (or otherwise produces
    nothing) leaves an operation awaiting a stream chunk that never arrives —
    the leg stays "running" forever and every dependent operation in the flow
    deadlocks behind it. This guards the *first* chunk only: once any chunk
    has arrived, the subprocess is alive and the rest of the stream is
    governed solely by ``stream_deadline`` (via ``_stream_with_deadline``),
    unchanged.

    On a first-output miss, the subprocess is retried once with an identical
    invocation (``kw`` unchanged). A second miss raises ``WorkerLivenessError``
    so the operation transitions to FAILED and releases its dependents,
    instead of hanging as a zombie "running" leg.

    ``liveness_timeout`` of ``None``/``<=0`` disables the watchdog entirely
    (deterministic/test runs) and falls through to the legacy single-attempt
    passthrough. When the caller's own ``stream_deadline`` is tighter than
    ``liveness_timeout``, the deadline wins and its ``TimeoutError`` is
    propagated unchanged (not treated as a liveness miss, not retried) —
    the caller asked for that total-stream budget deliberately.

    ``api_call_holder`` is a caller-owned list; the winning attempt's
    ``api_call`` is recorded at index 0 for post-stream metadata (the caller
    cannot see the api_call created inside this generator otherwise).
    """
    if not liveness_timeout or liveness_timeout <= 0:
        api_call = await model.create_event(**kw)
        api_call_holder.append(api_call)
        await model.executor.append(api_call)
        agen = _stream_with_deadline(model, api_call, stream_deadline)
        try:
            async for chunk in agen:
                yield chunk
        finally:
            # Mirror the watchdog branch's explicit close (see below): GeneratorExit
            # thrown into this frame while suspended at `yield chunk` does not
            # implicitly close `agen` — close it explicitly so cleanup cascades
            # down to the subprocess reader synchronously instead of relying on
            # async-generator GC finalization.
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

    for attempt in range(max_attempts):
        api_call = await model.create_event(**kw)
        await model.executor.append(api_call)
        if api_call_holder:
            api_call_holder[0] = api_call
        else:
            api_call_holder.append(api_call)

        agen = _stream_with_deadline(model, api_call, stream_deadline)
        stream_iter = agen.__aiter__()

        remaining_to_deadline = (
            stream_deadline - anyio.current_time() if stream_deadline is not None else None
        )
        # The liveness window only "owns" the timeout when it is the tighter
        # bound; otherwise the caller's own stream_deadline was going to fire
        # first regardless, so a timeout here is that deadline, not a
        # worker-liveness failure.
        is_liveness_boundary = (
            remaining_to_deadline is None or liveness_timeout < remaining_to_deadline
        )
        wait_for = (
            liveness_timeout
            if remaining_to_deadline is None
            else max(0.0, min(liveness_timeout, remaining_to_deadline))
        )

        try:
            with anyio.fail_after(wait_for):
                first_chunk = await stream_iter.__anext__()
        except StopAsyncIteration:
            # Stream ended with zero chunks — a legitimate (if unusual) empty
            # completion, not a hang; let the caller see an empty stream.
            return
        except TimeoutError as exc:
            try:
                await agen.aclose()
            except Exception as close_exc:
                logger.debug(
                    "run: liveness watchdog agen.aclose() raised during cleanup: %r",
                    close_exc,
                )
            if not is_liveness_boundary:
                raise
            if attempt == max_attempts - 1:
                raise WorkerLivenessError(
                    f"worker produced no first stream output within "
                    f"{liveness_timeout:.0f}s across {max_attempts} attempt(s)",
                    reason="worker.no_first_output",
                ) from exc
            logger.warning(
                "run: no first stream output within %.0fs (attempt %d/%d); "
                "retrying worker subprocess",
                liveness_timeout,
                attempt + 1,
                max_attempts,
            )
            continue
        else:
            try:
                yield first_chunk
                async for chunk in agen:
                    yield chunk
            finally:
                # Mirror _stream_with_deadline's own unwind-preserving close:
                # a caller that abandons the generator mid-stream (break +
                # aclose()) throws GeneratorExit in here while suspended at
                # either yield above, which does not implicitly close
                # `agen` — close it explicitly so the cascade down to the
                # subprocess reader's cleanup still runs synchronously.
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


async def run(
    branch: Branch,
    instruction: JsonValue | Instruction,
    param: RunParam,
) -> AsyncGenerator[RoledMessage]:
    """Stream a CLI-backed model turn, yielding Instruction/AssistantResponse/ActionRequest/ActionResponse messages.

    Emits exactly one RunEnd (clean exit or consumer abandon) or RunFailed per RunStart.
    suppress_lifecycle_var suppresses nested signals inside Branch.ReAct() turns.
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

    pre_ins = await _apply_context_providers(branch, instruction, param)
    try:
        ins, kw = _prepare_run_kwargs(branch, instruction, param, ins=pre_ins)
    finally:
        branch._context_injection_slot = None
    await branch.msgs.a_add_message(instruction=ins)

    from lionagi.session._lifecycle_ctx import suppress_lifecycle_var

    _suppress_lifecycle = suppress_lifecycle_var.get()
    has_observer = branch._observer is not None and not _suppress_lifecycle

    _run_exc: BaseException | None = None
    _terminal_emitted: bool = False
    _t0_run = _time.monotonic()

    if has_observer:
        from lionagi.session.signal import RunStart

        try:
            await branch.emit(RunStart())
        except Exception:
            logger.exception("run: observer raised during RunStart emission; run proceeds normally")

    try:
        yield ins

        if branch.chat_model.provider_session_id is not None:
            kw["resume"] = branch.chat_model.provider_session_id

        model = branch.chat_model
        endpoint = model.endpoint
        prev_stream_func = model.streaming_process_func
        bfp = None

        if param.stream_persist:
            # snapshot_dir for find_branch() lookups; persist_dir for the live JSONL buffer.
            # Written here, before the stream starts, so a branch killed at any
            # point during a long-running turn (e.g. SIGTERM mid-stream) still
            # has a resumable checkpoint — the finally block below overwrites
            # it with the completed turn's state on a clean exit.
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
                    async with await anyio.open_file(bfp, "a") as f:
                        await f.write(json_dumps(chunk.to_dict()) + "\n")
                if prev_stream_func is not None:
                    from lionagi.ln import is_coro_func

                    if is_coro_func(prev_stream_func):
                        return await prev_stream_func(chunk)
                    return prev_stream_func(chunk)
                return None

            model.streaming_process_func = _persist_chunk

        thinking_parts: list[str] = []
        text_parts: list[str] = []
        # Provider-reported usage from the terminal "result" chunk (codex: tokens; claude_code: cost/turns/duration).
        # Stamped onto the final AssistantResponse; re-tokenizing message history undercounts internal tool turns.
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
                # Clear after stamping so a later flush within the same run()
                # call (e.g. a provider that reports usage per internal turn,
                # with tool calls in between) doesn't restamp already-recorded
                # usage onto a second AssistantResponse -- _collect_branch_usage
                # sums across every message on the branch, so a stale/repeated
                # result_meta here would double-count tokens/cost.
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

        # Pop timeout before create_event — CLI providers don't consume it; None/0/negative disables enforcement.
        _timeout = kw.pop("timeout", None)
        _stream_deadline: float | None = None
        if isinstance(_timeout, int | float) and _timeout > 0:
            _stream_deadline = anyio.current_time() + float(_timeout)

        # Pop liveness_timeout before create_event — CLI providers don't consume it either.
        # Explicit values (including 0/negative-to-disable) are always honored. Absent
        # falls back to the configured default, but only for endpoints that declare
        # streams_first_output_early — a buffered transport (e.g. gemini_code, whose
        # first chunk arrives only once the whole result is in) would have a healthy
        # long call misdiagnosed as a dead worker under the default watchdog.
        _liveness_timeout_explicit = "liveness_timeout" in kw
        _liveness_timeout = kw.pop("liveness_timeout", None)
        if _liveness_timeout is None and not _liveness_timeout_explicit:
            if getattr(endpoint, "streams_first_output_early", False):
                from lionagi.config import settings as _app_settings  # noqa: PLC0415

                _liveness_timeout = _app_settings.LIONAGI_WORKER_LIVENESS_TIMEOUT
        if not isinstance(_liveness_timeout, int | float) or _liveness_timeout <= 0:
            _liveness_timeout = None

        kw["stream"] = True
        _api_call_holder: list = []
        stream_gen = _stream_with_liveness(
            model, kw, _stream_deadline, _liveness_timeout, _api_call_holder
        )
        try:
            try:
                async for chunk in stream_gen:
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
                            # A CLI provider marks a resumed-session end-of-stream by
                            # emitting a StreamChunk(type="error", ...,
                            # metadata={"benign_eos": True}).  Only suppress the error
                            # when that explicit marker is present; any error chunk
                            # without it is treated as a real provider failure and
                            # surfaces as RunFailed.  This prevents genuine empty-error
                            # objects (turn.failed with no message) from being silently
                            # swallowed as success.
                            if chunk.metadata.get("benign_eos"):
                                logger.debug(
                                    "run: provider end-of-stream sentinel received, "
                                    "ending stream cleanly"
                                )
                                break
                            # Persist text the provider already delivered before
                            # failing — a late failure (timeout after streaming a
                            # response) must not destroy content the caller and
                            # state.db would otherwise have received.
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
            # Deterministically close the stream chain on ANY exit (normal
            # StopAsyncIteration, an "error" chunk raise, a control-signal
            # _StopStream, GeneratorExit) rather than leaving it to
            # async-generator GC — for a CLI provider that cascades down to
            # the subprocess reader's own cleanup and terminates the process
            # group instead of leaving it running, orphaned, in the background.
            #
            # The close chain (ndjson_from_cli -> aterminate_process_group ->
            # asyncio.wait_for) can raise asyncio.CancelledError, a
            # BaseException that a plain `except Exception` will not catch —
            # left unguarded it escapes this finally block and REPLACES
            # whatever provider/control exception is already propagating
            # (_run_exc, an in-flight ProviderError, a _StopStream signal).
            # Preserve the primary: a close failure while already unwinding
            # is a secondary cleanup failure, logged and swallowed, never
            # allowed to mask the real reason the stream ended.
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
        # GeneratorExit must never be suppressed — emit RunEnd then re-raise for runtime teardown.
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
    finally:
        # _terminal_emitted guards against double emission on Python <3.11 where finally also runs after GeneratorExit.
        await branch.drain_signals()

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
