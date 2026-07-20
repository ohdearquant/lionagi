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

from .._api_hooks import emit_api_post_call, emit_api_pre_call, emit_api_stream_chunk
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
        except BaseException:
            # Cancellation or GeneratorExit can land while the first chunk is
            # still pending, before control reaches the post-yield finally
            # below. Close the owned stream explicitly so subprocess cleanup
            # runs synchronously, then preserve the original unwind reason.
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


# Fields inside a "result" chunk's metadata that CLI providers may emit as a
# per-turn delta rather than a running total (see codex.py's turn.completed
# handler). Every provider that only ever emits one "result" chunk per run()
# call (claude_code, gemini_code) reports these as the final total, which sums
# correctly here too since there is nothing else to add it to.
_RESULT_META_DELTA_KEYS = ("total_cost_usd", "num_turns")


def _accumulate_result_meta(result_meta: dict, metadata: dict) -> None:
    """Merge a "result" chunk's metadata into the in-progress accumulator.

    Numeric usage/cost/turn fields are summed (codex emits marginal deltas
    across multiple turn.completed events within one flush window); every
    other field (e.g. duration_ms, a point-in-time snapshot rather than a
    delta) is overwritten with the latest value.
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

    Emits at most one terminal signal (RunEnd on clean exit or consumer
    abandon, RunFailed on any failure) per call when an observer is
    attached. RunStart precedes it only once the turn has passed the
    origin guard below — a prompt the guard rejects is recorded as
    RunFailed with no preceding RunStart and no other lifecycle trace.
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

    # Built synchronously and purely from (instruction, param) — no context-
    # provider I/O, no persistence, no signal emission. This is the only
    # thing the origin guard below needs, so it happens before any other
    # awaited operation for this turn.
    ins = _build_instruction(branch, instruction, param)

    from lionagi.session._lifecycle_ctx import suppress_lifecycle_var

    _suppress_lifecycle = suppress_lifecycle_var.get()
    has_observer = branch._observer is not None and not _suppress_lifecycle

    _run_exc: BaseException | None = None
    _terminal_emitted: bool = False
    _api_call_started: bool = False
    _t0_run = _time.monotonic()

    try:
        # Consumed exactly once, as the first awaited operation for this
        # turn — before context providers run, before RunStart is emitted
        # (which persists via the observer), before anything is committed
        # or yielded: fires USER_PROMPT_SUBMIT iff the operation context
        # carries a turn-origin token (see operations/_turn_origin.py). A
        # handler that rejects this prompt must leave no lifecycle trace
        # beyond the rejection itself — no context-provider side effects,
        # no RunStart, nothing committed to branch.messages, nothing
        # yielded to a consumer. The rejection is still recorded as this
        # run's failure (not silently dropped) so the terminal signal below
        # reports it correctly.
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

        # Committed before the yield below: any consumer that receives this
        # Instruction from the generator must find it already present in
        # branch.messages, not merely in flight.
        await branch.msgs.a_add_message(instruction=ins)

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
        # Whole-call usage accumulator: never cleared (unlike result_meta,
        # which resets on every flush so each AssistantResponse only carries
        # its own window's metadata). Codex splits one run() call into
        # multiple flush windows (tool-response flushes between "result"
        # chunks) and emits marginal per-window deltas, so the terminal
        # API_POST_CALL must sum every window's usage, not just the last
        # one. _accumulate_result_meta's "usage" branch always adds
        # (never overwrites), so feeding it every incoming chunk here, in
        # parallel with result_meta, sums exactly once per chunk — no
        # double-count regardless of how many flushes land in between.
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
        await emit_api_pre_call(branch, model)
        _api_call_started = True
        stream_gen = _stream_with_liveness(
            model, kw, _stream_deadline, _liveness_timeout, _api_call_holder
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
    except BaseException as _exc:
        # Catches anything raised in this turn's setup/commit/yield/stream
        # path that the more specific handlers above didn't already
        # classify (context providers, kw preparation, message persistence,
        # pre-stream snapshot/liveness setup) — without this, the finally
        # below would see _run_exc still None for those failures and emit a
        # false RunEnd instead of RunFailed.
        if _run_exc is None:
            _run_exc = _exc
        raise
    finally:
        # _terminal_emitted guards against double emission on Python <3.11 where finally also runs after GeneratorExit.
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
