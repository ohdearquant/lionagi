# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import math
from collections.abc import AsyncGenerator, Callable, Mapping
from contextlib import nullcontext
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, SecretStr

from lionagi._errors import CredentialMissingError, InputTokenCapError
from lionagi.ln import is_coro_func, now_utc
from lionagi.protocols.generic import Event, EventStatus, Log
from lionagi.protocols.ids import to_uuid

from .connections import AgenticEndpoint, APICalling, Endpoint, match_endpoint
from .connections.endpoint import (
    _event_reports_call_failure,
    _names_credential_setting,
    _stored_base_url,
)
from .connections.endpoint_config import RUNTIME_STATE_NAMES
from .hooks import (
    HookedEvent,
    HookEvent,
    HookEventTypes,
    HookRegistry,
    global_hook_logger,
)
from .rate_limited_processor import RateLimitedAPIExecutor
from .types import StreamChunk

# A request under this many counted tokens does not calibrate input_token_scale.
_CALIBRATION_FLOOR = 4_000


def reported_input_tokens(usage: Any) -> int | None:
    """The input tokens a provider's usage block reports for one request; None when it reports none.

    Chat Completions reports ``prompt_tokens``, cached tokens included. The Messages API reports
    ``input_tokens`` net of ``cache_read_input_tokens`` and ``cache_creation_input_tokens``, so the
    three are summed; the Responses API reports ``input_tokens`` with cached tokens included and
    neither cache key. A count that is not a non-negative integer is not read.
    """
    if not isinstance(usage, dict):
        return None

    def count(key: str) -> int | None:
        value = usage.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        return value

    prompt = count("prompt_tokens")
    if prompt is not None:
        return prompt
    fresh = count("input_tokens")
    if fresh is None:
        return None
    return fresh + sum(
        count(key) or 0 for key in ("cache_read_input_tokens", "cache_creation_input_tokens")
    )


def _merge_streamed_usage(usage: dict[str, Any], chunk: Any) -> None:
    """Fold the usage block one streamed event carries into ``usage``.

    The Messages API reports the input counts on ``message_start`` (under ``message``) and
    cumulative counts on ``message_delta``; Chat Completions ends with a usage chunk; the
    Responses API reports usage on ``response.completed`` (under ``response``). The counts are
    cumulative, so a key an event leaves out keeps the count held for it: an event's input
    counts are laid over the ones held and taken only when that total
    (``reported_input_tokens``) can be read and is not below the one held. A later event
    reporting less, zero included, does not erase them; a count moved from one key to another,
    the event reporting both keys, is not added twice; an event carrying only some of the keys
    adds to the counts held. A set with no readable total (cache keys without
    ``input_tokens``, a count that is not a non-negative integer) is not taken. Any other value
    is the newest one seen.
    """
    if isinstance(chunk, StreamChunk):
        chunk = chunk.metadata.get("raw") if isinstance(chunk.metadata, dict) else None
    if not isinstance(chunk, dict):
        return
    for holder in (chunk, chunk.get("message"), chunk.get("response")):
        block = holder.get("usage") if isinstance(holder, dict) else None
        if not isinstance(block, dict):
            continue
        new = {key: value for key, value in block.items() if value is not None}
        held = {key: usage[key] for key in _INPUT_KEYS if key in usage}
        laid = {**held, **{key: new.pop(key) for key in _INPUT_KEYS if key in new}}
        reported = reported_input_tokens(laid)
        if reported is not None and reported >= (reported_input_tokens(held) or 0):
            usage.update(laid)
        usage.update(new)
        return


_INPUT_KEYS = (
    "prompt_tokens",
    "input_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


def _branch_routing_context(branch_id: UUID, imodel: iModel):
    if not _endpoint_routing_enabled(imodel.endpoint):
        return nullcontext()
    from .accounts._scope import routing_scope
    from .accounts._types import Scope

    return routing_scope(Scope("branch", branch_id, "anthropic"))


def _endpoint_routing_enabled(endpoint) -> bool:
    check = getattr(endpoint, "_account_routing_enabled", None)
    return bool(check and check())


def _terminal_stream_error(api_call: APICalling) -> BaseException | None:
    """The captured cause when a streamed call ended FAILED, else None.

    ``Event.stream()`` records a transport/provider failure as ``FAILED`` and
    stops yielding rather than re-raising, so the shared processor can run
    events concurrently without one failure cancelling the batch. Always
    returns a ``BaseException`` (never a bare string) so it is safe to chain
    with ``raise ... from``.
    """
    if api_call.status != EventStatus.FAILED:
        return None
    err = api_call.execution.error
    if isinstance(err, BaseException):
        return err
    return RuntimeError(str(err) if err is not None else "stream failed without a recorded cause")


class iModel:  # noqa: N801
    """Provider endpoint wrapper with rate-limiting, hooks, and streaming."""

    def __init__(
        self,
        provider: str | None = None,
        base_url: str | None = None,
        endpoint: str | Endpoint = "chat",
        api_key: str | None = None,
        queue_capacity: int | None = None,
        capacity_refresh_time: float = 60,
        interval: float | None = None,
        limit_requests: int | None = None,
        limit_tokens: int | None = None,
        concurrency_limit: int | None = None,
        streaming_process_func: Callable | None = None,
        provider_metadata: dict | None = None,
        hook_registry: HookRegistry | dict | None = None,
        exit_hook: bool = False,
        id: UUID | str | None = None,  # noqa: A002
        created_at: float | None = None,
        max_input_tokens: int | None = None,
        tool_result_max_chars: int | None = None,
        **kwargs,
    ) -> None:
        self.id = None
        self.created_at = None
        if max_input_tokens is not None and (
            isinstance(max_input_tokens, bool)
            or not isinstance(max_input_tokens, int)
            or max_input_tokens < 1
        ):
            raise ValueError("max_input_tokens must be a positive integer or None.")
        if tool_result_max_chars is not None and (
            isinstance(tool_result_max_chars, bool)
            or not isinstance(tool_result_max_chars, int)
            or tool_result_max_chars < 1024
        ):
            raise ValueError("tool_result_max_chars must be an integer >= 1024 or None.")
        # Every APICalling this model builds refuses a request whose estimated input exceeds
        # it. The first refusal is kept, and every later request on this model is refused with
        # it: callers that catch exceptions mid-loop (a ReAct final-answer turn, a parse
        # repair) would otherwise lose it, or go on to send a smaller request after the run
        # was already refused. A caller clears input_cap_refusal to send again.
        self.max_input_tokens: int | None = max_input_tokens
        # Local presentation policy; never an endpoint or provider request setting.
        self.tool_result_max_chars: int | None = tool_result_max_chars
        self.input_cap_refusal: Exception | None = None
        # The cap counts o200k_base tokens and a provider counts its own way: Anthropic reported
        # about 1.37 times the count on agent transcripts. Each completed response raises this to
        # the provider's count per counted token (_calibrate_input_scale), and the cap compares
        # the count times this. It starts at 1.0 and only rises, so the cap never admits more
        # than its own count.
        self.input_token_scale: float = 1.0
        if id is not None:
            self.id = to_uuid(id)
        else:
            self.id = uuid4()
        if created_at is not None:
            if not isinstance(created_at, float):
                raise ValueError("created_at must be a float timestamp.")
            self.created_at = created_at
        else:
            self.created_at = now_utc().timestamp()

        model = kwargs.get("model", None)
        if model:
            if not provider:
                if "/" in model:
                    model_provider = model.split("/")[0]
                    provider = model_provider
                    model = model.replace(model_provider + "/", "")
                    kwargs["model"] = model
                else:
                    from lionagi.config import settings

                    provider = settings.LIONAGI_CHAT_PROVIDER

            # Effort-suffixed model names ("gpt-5.6-luna-high") are stripped and
            # routed to the provider's effort kwarg here so it works at every
            # construction site. See docs/internals/service-layer.md#effort-suffix-routing.
            from .connections.registry import EndpointRegistry
            from .providers import (
                _CLAUDE_PROVIDER_NAMES,
                PROVIDER_EFFORT_KWARG,
                _clamp_claude_effort,
                normalize_effort,
                split_effort_suffix,
            )

            # An alias ("openai-codex") routes as the provider it names.
            _canonical = EndpointRegistry.canonical_provider(provider) if provider else None
            _effort_kwarg = PROVIDER_EFFORT_KWARG.get(_canonical) if _canonical else None
            _model_name = kwargs.get("model")
            if _effort_kwarg and isinstance(_model_name, str):
                _split = split_effort_suffix(_model_name)
                if _split is not None:
                    _name, _raw_effort = _split
                    kwargs["model"] = _name
                    if _effort_kwarg not in kwargs:
                        _eff = normalize_effort(_raw_effort)
                        if _canonical in _CLAUDE_PROVIDER_NAMES:
                            _eff = _clamp_claude_effort(_eff, kwargs["model"])
                        kwargs[_effort_kwarg] = _eff

        if api_key is not None:
            kwargs["api_key"] = api_key
        if isinstance(endpoint, Endpoint):
            self.endpoint = endpoint
            # Runtime state (spawned-child hooks, env) that missed the normal
            # config window is placed now; refused outright if it has nowhere
            # to go, rather than silently dropped. See
            # docs/internals/service-layer.md#runtime-state-adoption.
            adopt = getattr(self.endpoint, "adopt_runtime_state", None)
            unplaced = (
                adopt(kwargs)
                if callable(adopt)
                else tuple(n for n in RUNTIME_STATE_NAMES if kwargs.get(n) is not None)
            )
            if unplaced:
                raise TypeError(
                    f"{type(self.endpoint).__name__} has no runtime state to hold "
                    f"{', '.join(unplaced)}. These configure a spawned child process "
                    "and only a CLI endpoint has one."
                )
        else:
            match_kwargs = dict(kwargs)
            if base_url:
                # A caller-supplied base_url is the same explicit signal that
                # already means "route this custom host through the generic
                # OpenAI-compatible endpoint" -- surface it to match_endpoint
                # so an unregistered provider name doesn't raise here.
                match_kwargs.setdefault("base_url", base_url)
            self.endpoint = match_endpoint(
                provider=provider,
                endpoint=endpoint,
                **match_kwargs,
            )
        if provider:
            self.endpoint.config.provider = provider
        if base_url:
            self.endpoint.config.base_url = base_url

        if queue_capacity is None:
            queue_capacity = self.endpoint.DEFAULT_QUEUE_CAPACITY if self.endpoint.is_cli else 100
        if concurrency_limit is None and self.endpoint.is_cli:
            concurrency_limit = self.endpoint.DEFAULT_CONCURRENCY_LIMIT

        self.executor = RateLimitedAPIExecutor(
            queue_capacity=queue_capacity,
            capacity_refresh_time=capacity_refresh_time,
            interval=interval,
            limit_requests=limit_requests,
            limit_tokens=limit_tokens,
            concurrency_limit=concurrency_limit,
        )

        self.streaming_process_func = streaming_process_func
        self.provider_metadata = provider_metadata or {}
        self.hook_registry = hook_registry or HookRegistry()
        if isinstance(self.hook_registry, dict):
            self.hook_registry = HookRegistry(**self.hook_registry)
        self.exit_hook: bool = exit_hook
        # Requests this instance has sent, failed ones included, so a caller can read its
        # own spend as a difference. A request its endpoint retries counts once, and a copy
        # starts from zero.
        self.calls_sent: int = 0

    async def create_event(
        self,
        create_event_type: type[Event] = APICalling,
        create_event_exit_hook: bool | None = None,
        create_event_hook_timeout: float = 10.0,
        create_event_hook_params: dict | None = None,
        pre_invoke_event_exit_hook: bool | None = None,
        pre_invoke_event_hook_timeout: float = 30.0,
        pre_invoke_event_hook_params: dict | None = None,
        post_invoke_event_exit_hook: bool | None = None,
        post_invoke_event_hook_timeout: float = 30.0,
        post_invoke_event_hook_params: dict | None = None,
        **kwargs,
    ) -> APICalling:
        h_ev = None
        if self.hook_registry._can_handle(ht_=HookEventTypes.PreEventCreate):
            h_ev = HookEvent(
                hook_type=HookEventTypes.PreEventCreate,
                registry=self.hook_registry,
                event_like=create_event_type,
                params=create_event_hook_params or {},
                exit=(self.exit_hook if create_event_exit_hook is None else create_event_exit_hook),
                timeout=create_event_hook_timeout,
            )
            await h_ev.invoke()
            if h_ev._should_exit:
                raise h_ev._exit_cause or RuntimeError(
                    "PreEventCreate hook requested exit without a cause"
                )

        if issubclass(create_event_type, HookedEvent):
            api_call = None
            if h_ev and isinstance(h_ev.execution.response, create_event_type):
                # PreEventCreate replaced the event outright — use it as-is
                # instead of building a fresh one from the original kwargs.
                api_call = h_ev.execution.response
            elif create_event_type is APICalling:
                api_call = self.create_api_calling(**kwargs)
            else:
                api_call = create_event_type(**kwargs)
            if h_ev:
                h_ev.associated_event_info["event_id"] = str(api_call.id)
                h_ev.associated_event_info["event_created_at"] = api_call.created_at
                await global_hook_logger.alog(Log(content=h_ev.to_dict()))

            if self.hook_registry._can_handle(ht_=HookEventTypes.PreInvocation):
                api_call.create_pre_invoke_hook(
                    hook_registry=self.hook_registry,
                    exit_hook=(
                        self.exit_hook
                        if pre_invoke_event_exit_hook is None
                        else pre_invoke_event_exit_hook
                    ),
                    hook_timeout=pre_invoke_event_hook_timeout,
                    hook_params=pre_invoke_event_hook_params or {},
                )

            if self.hook_registry._can_handle(ht_=HookEventTypes.PostInvocation):
                api_call.create_post_invoke_hook(
                    hook_registry=self.hook_registry,
                    exit_hook=(
                        self.exit_hook
                        if post_invoke_event_exit_hook is None
                        else post_invoke_event_exit_hook
                    ),
                    hook_timeout=post_invoke_event_hook_timeout,
                    hook_params=post_invoke_event_hook_params or {},
                )

            return api_call

        raise ValueError(
            f"Unsupported event type: {create_event_type}. Only APICalling is supported."
        )

    def create_api_calling(
        self, include_token_usage_to_model: bool = False, **kwargs
    ) -> APICalling:
        """Construct an APICalling from endpoint-specific payload."""
        if self.input_cap_refusal is not None:
            raise self.input_cap_refusal
        # Auto-inject session_id for CLI endpoint resume
        if (
            isinstance(self.endpoint, AgenticEndpoint)
            and "resume" not in kwargs
            and "session_id" not in kwargs
            and self.endpoint.session_id
        ):
            kwargs["resume"] = self.endpoint.session_id

        transport_arg_keys = getattr(self.endpoint, "transport_arg_keys", ())
        call_kwargs = {k: kwargs.pop(k) for k in list(kwargs) if k in transport_arg_keys}

        payload, headers = self.endpoint.create_payload(request=kwargs)
        cache_control = kwargs.pop("cache_control", False)

        try:
            api_call = APICalling(
                payload=payload,
                headers=headers,
                endpoint=self.endpoint,
                call_kwargs=call_kwargs,
                cache_control=cache_control,
                include_token_usage_to_model=include_token_usage_to_model,
                max_input_tokens=self.max_input_tokens,
                input_token_scale=self.input_token_scale,
            )
        except InputTokenCapError as exc:
            if self.input_cap_refusal is None:
                self.input_cap_refusal = exc
            raise
        self._capture_routing_scope(api_call)
        return api_call

    def _expected_routing_scope(self):
        from .accounts._scope import current_scope
        from .accounts._types import Scope

        return current_scope() or Scope("imodel", self.id, "anthropic")

    def _capture_routing_scope(self, api_call: APICalling) -> None:
        if not _endpoint_routing_enabled(self.endpoint):
            return
        from .accounts._types import AccountRoutingError, RouterErrorCode

        expected = self._expected_routing_scope()
        captured = api_call._routing_scope
        if captured is None:
            api_call._routing_scope = expected
        elif captured != expected:
            # The call belongs to the scope that built it. Refusing changes nothing on it: it
            # stays where its owner put it, and whoever sends it later (the owner's invoke, or
            # any caller's forward while it is pending) sends it under the owner's scope.
            raise AccountRoutingError(RouterErrorCode.ACCOUNT_CONFIG_CONFLICT) from None

    def _is_routing_error(self, error: BaseException) -> bool:
        if not _endpoint_routing_enabled(self.endpoint):
            return False
        from .accounts._types import AccountRoutingError

        return isinstance(error, AccountRoutingError)

    async def process_chunk(self, chunk) -> Any:
        """Process a streaming data chunk. Override for custom handling."""
        processed = None
        chunk_type = type(chunk)
        chunk_key = None
        if self.hook_registry._can_handle(ct_=chunk_type):
            chunk_key = chunk_type
        elif self.hook_registry._can_handle(ct_=chunk_type.__name__):
            chunk_key = chunk_type.__name__

        # Hook registry takes priority over streaming_process_func.
        if chunk_key is not None:
            hook_result, should_exit, _status = await self.hook_registry.handle_streaming_chunk(
                chunk_key, chunk, exit=self.exit_hook
            )
            if should_exit:
                if (
                    isinstance(hook_result, tuple)
                    and len(hook_result) == 2
                    and isinstance(hook_result[1], BaseException)
                ):
                    raise hook_result[1]
                if isinstance(hook_result, BaseException):
                    raise hook_result
                raise RuntimeError("Streaming hook requested exit without a cause")
            if not isinstance(hook_result, BaseException):
                return hook_result
            return processed

        if self.streaming_process_func and not isinstance(chunk, APICalling):
            if is_coro_func(self.streaming_process_func):
                return await self.streaming_process_func(chunk)
            return self.streaming_process_func(chunk)
        return processed

    @staticmethod
    def _event_failed(event: Any) -> bool:
        """Whether a provider event or response says the call failed.

        The rule is the endpoint's (``_event_reports_call_failure``), so the chunk the
        endpoint marks ``call_failed`` and the event read here agree.
        """
        return _event_reports_call_failure(event)

    @staticmethod
    def _reports_failure(value: Any) -> bool:
        """Whether a stream chunk or a response says the call failed.

        An error chunk counts only when it is marked as an error: a Codex
        reconnect notice or a resumed session's clean end arrives as an error
        chunk that is not one. A chunk's provider event is read under
        ``metadata["raw"]``. A CLI system chunk has no such event, so its own
        metadata is read in its place: the whole event for Codex, the session id
        and model for agy, and those and the tools for Claude Code.
        """
        if isinstance(value, StreamChunk):
            if value.type == "error" and value.is_error:
                return True
            metadata = value.metadata if isinstance(value.metadata, dict) else {}
            raw = metadata.get("raw")
            if isinstance(raw, dict):
                return iModel._event_failed(raw)
            return value.type == "system" and iModel._event_failed(metadata)
        return iModel._event_failed(value)

    @staticmethod
    def _reported_served_model(value: Any) -> str | None:
        """The model the provider named in a response or stream chunk, else None.

        A CLI provider's report sits on its system chunk: Claude Code and agy
        put the model there, and Codex passes its session event through whole.
        pi streams no system chunk, so a pi stream reports none, while pi's
        invoke result carries the model pi reported, so invoke reads one. An API
        stream chunk keeps the provider event under ``metadata["raw"]``, where
        the model sits at the top level (chat completion chunks), under
        ``message`` (Anthropic ``message_start``) or under ``response``
        (Responses events). Result chunks are not read: a CLI result chunk is
        the run record, which may name the requested model. The one result chunk
        read is the Responses API's own ``response.completed`` event, kept under
        ``metadata["raw"]`` by the HTTP converter: it is the provider's terminal
        event, and for a stream that named its model nowhere earlier it is the only
        report. A chunk or response
        that says the call failed (see ``_reports_failure``) is never read
        either. A call cut short rather than refused is read, because the model
        it names is the one that ran: an incomplete response (a content filter
        included), a ``status`` of ``cancelled``, and a stream that ends without
        a terminal event.
        """
        if iModel._reports_failure(value):
            return None
        if isinstance(value, StreamChunk):
            metadata = value.metadata if isinstance(value.metadata, dict) else {}
            raw = metadata.get("raw")
            provider_terminal = (
                value.type == "result"
                and isinstance(raw, dict)
                and raw.get("type") == "response.completed"
            )
            if value.type not in ("system", "text") and not provider_terminal:
                return None
            if isinstance(raw, dict):
                holders = [raw, raw.get("message"), raw.get("response")]
            elif value.type == "system":
                holders = [metadata]
            else:
                return None
        else:
            holders = [value]

        for holder in holders:
            if not isinstance(holder, dict):
                continue
            served_model = holder.get("model")
            if isinstance(served_model, str) and served_model.strip():
                return served_model
        return None

    def _store_served_model(
        self,
        api_call: APICalling | None,
        served_model: str | None,
    ) -> None:
        """Record the provider's report on the call and on the model.

        ``APICalling.metadata`` is the call-correlated evidence. The model-level
        value holds what the last invoke or stream on this model to write it
        wrote, so under concurrency it is never evidence about a particular call.
        Recording none clears both values. A call that failed records none. A
        stream its consumer closed early, and a stream cancelled after the provider
        named its model (a timeout that cancels it included), have not failed: each
        keeps the model read from what it delivered. A timeout raised inside the
        stream fails the call, so it records none. An invoke cancelled before it
        returns has read no response, so it records none, and so does a call still
        in flight when its invoke's wait ends. An invoke that fails or is cancelled
        before its call exists sends nothing and leaves both values as they were,
        and so does a call still pending, which has not been sent. If the queue
        sends that call later, nothing reads the response, so the model it was
        served is recorded nowhere.
        """
        if api_call is not None and api_call.status == EventStatus.PENDING:
            return
        if api_call is not None and api_call.status == EventStatus.FAILED:
            served_model = None
        if api_call is not None:
            # Hooks receive the mutable event. Re-establish this field at the
            # finalization boundary so hook or caller metadata can never pass
            # for provider-reported identity.
            api_call.metadata.pop("served_model", None)
            if served_model is not None:
                api_call.metadata["served_model"] = served_model
        if served_model is None:
            self.provider_metadata.pop("served_model", None)
        else:
            self.provider_metadata["served_model"] = served_model

    async def stream(self, api_call=None, **kw) -> AsyncGenerator:
        served_model = None
        # What the stream's events report about the request, for _calibrate_input_scale.
        usage: dict[str, Any] = {}
        if api_call is None:
            kw["stream"] = True
            api_call = await self.create_event(**kw)
        api_call.endpoint._require_api_key()
        self._capture_routing_scope(api_call)
        # A call stream() creates is consumed by this generator, never by the
        # executor's queue: it goes into the pile with no pending entry, because
        # forward() sends every pending id the pile still holds and would send
        # this call a second time while the stream is consuming it. A caller
        # that appended the call before passing it in did put it in the queue at
        # that append, so the entry is dropped here; a forward() between the two
        # can still have sent it.
        self.executor.pending.exclude(api_call.id)
        api_call.metadata.pop("served_model", None)
        self.calls_sent += 1

        stream_error = None
        failed = False
        try:
            await self.executor.pile.ainclude(api_call)
            if self.executor.processor is None or self.executor.processor.is_stopped():
                await self.executor.start()

            if self.executor.processor._concurrency_sem:
                async with self.executor.processor._concurrency_sem:
                    try:
                        async for i in api_call.stream():
                            if failed or self._reports_failure(i):
                                # A failure voids any model read before it, and no chunk after
                                # it names one: the call failed whatever came next.
                                failed = True
                                served_model = None
                            else:
                                reported_model = self._reported_served_model(i)
                                if reported_model is not None:
                                    served_model = reported_model
                            _merge_streamed_usage(usage, i)
                            result = await self.process_chunk(i)
                            yield result if result is not None else i
                        # api_call.stream() captures a transport/provider failure as
                        # FAILED instead of raising (so the shared processor can fire
                        # events concurrently without one failure cancelling the batch).
                        # This public boundary must not report that as a clean end —
                        # surface it after iteration so the caller sees the error.
                        stream_error = _terminal_stream_error(api_call)
                    except Exception as e:
                        if isinstance(e, CredentialMissingError) or self._is_routing_error(e):
                            raise
                        raise ValueError(f"Failed to stream API call: {e}") from e
            else:
                try:
                    async for i in api_call.stream():
                        if failed or self._reports_failure(i):
                            failed = True
                            served_model = None
                        else:
                            reported_model = self._reported_served_model(i)
                            if reported_model is not None:
                                served_model = reported_model
                        _merge_streamed_usage(usage, i)
                        result = await self.process_chunk(i)
                        yield result if result is not None else i
                    stream_error = _terminal_stream_error(api_call)
                except Exception as e:
                    if isinstance(e, CredentialMissingError) or self._is_routing_error(e):
                        raise
                    raise ValueError(f"Failed to stream API call: {e}") from e
        finally:
            # Runs on every exit from the point this generator owns the call: a
            # clean end, a consumer that stops early, cancellation (including
            # while waiting for the concurrency slot), an endpoint failure, and
            # a processor start that raises.
            self._store_served_model(api_call, served_model)
            # Pop without yielding — yield-in-finally would swallow CancelledError
            # during generator cleanup, breaking anyio.fail_after timeout enforcement.
            self.executor.pile.pop(api_call.id, None)
        if stream_error is not None:
            if isinstance(stream_error, CredentialMissingError) or self._is_routing_error(
                stream_error
            ):
                raise stream_error from None
            raise ValueError(f"Failed to stream API call: {stream_error}") from stream_error
        if not failed and api_call.status == EventStatus.COMPLETED:
            self._calibrate_input_scale(api_call, usage)

    def _calibrate_input_scale(self, call: APICalling, usage: Any) -> None:
        """Raise input_token_scale to the provider's reported input per token the cap counted.

        Only a call the cap counted calibrates, and only from _CALIBRATION_FLOOR counted tokens up:
        a fixed per-request overhead weighs most on a small request, and the scale never falls.
        """
        counted = call.counted_input_tokens
        if counted is None or counted < _CALIBRATION_FLOOR:
            return
        reported = reported_input_tokens(usage)
        if reported is None:
            return
        self.input_token_scale = max(self.input_token_scale, reported / counted)

    async def invoke(self, api_call: APICalling = None, **kw) -> APICalling:
        try:
            if api_call is None:
                kw.pop("stream", None)
                api_call = await self.create_event(**kw)
            api_call.endpoint._require_api_key()
            self._capture_routing_scope(api_call)
            api_call.metadata.pop("served_model", None)

            if self.executor.processor is None or self.executor.processor.is_stopped():
                await self.executor.start()

            # Only this call: forward() would also send every call another caller appended
            # and has not forwarded yet. Taking it out of pending keeps a later forward()
            # from queueing it a second time.
            await self.executor.pile.ainclude(api_call)
            self.executor.pending.exclude(api_call.id)
            self.calls_sent += 1
            await self.executor.processor.enqueue(api_call)
            await self.executor.processor.process()

            if api_call.status in (
                EventStatus.PROCESSING,
                EventStatus.PENDING,
            ):
                try:
                    # TODO: migrate to anyio cancel scope for timeout
                    await asyncio.wait_for(
                        api_call.completion_event.wait(),
                        timeout=10.0,
                    )
                except asyncio.TimeoutError:
                    pass

            completed_call = self.executor.pile.pop(api_call.id)
            response = completed_call.response if completed_call else None
            self._store_served_model(
                completed_call,
                self._reported_served_model(response),
            )
            if (
                completed_call is not None
                and completed_call.status == EventStatus.COMPLETED
                and isinstance(response, dict)
            ):
                self._calibrate_input_scale(completed_call, response.get("usage"))
            if response:
                # An empty id never erases the last confirmed one: a call whose
                # stream carried no id-bearing chunk returns the field empty,
                # and storing that would start the next call in a new provider
                # session instead of continuing this one.
                if (
                    isinstance(self.endpoint, AgenticEndpoint)
                    and isinstance(response, dict)
                    and response.get("session_id")
                ):
                    self.endpoint.session_id = response["session_id"]

            return completed_call
        except (CredentialMissingError, InputTokenCapError):
            # Preserve typed pre-send refusals instead of hiding them in the generic wrapper.
            raise
        except Exception as e:
            if self._is_routing_error(e):
                raise
            if api_call is not None:
                self._store_served_model(api_call, None)
            raise ValueError(f"Failed to invoke API call: {e}") from e
        except BaseException:
            # Cancelled (a caller's timeout included) before any response was read.
            if api_call is not None:
                self._store_served_model(api_call, None)
            raise

    @property
    def is_cli(self) -> bool:
        return self.endpoint.is_cli

    @property
    def model_name(self) -> str:
        return self.endpoint.config.kwargs.get("model", "")

    @property
    def request_options(self) -> type[BaseModel] | None:
        return self.endpoint.request_options

    async def __aenter__(self) -> iModel:
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.close()

    async def close(self) -> None:
        await self.executor.stop()

    def copy(self, share_session: bool = False, share_executor: bool = False) -> iModel:
        """Create a new iModel with the same config but a fresh ID. See
        docs/internals/service-layer.md#copy-and-runtime-state for what state
        is/isn't shared with the copy."""
        endpoint_cls = type(self.endpoint)
        # Drain before the deep copy so no runtime-only state (child env, bound
        # callbacks) rides along duplicated; see
        # docs/internals/service-layer.md#copy-and-runtime-state.
        self.endpoint.drain_runtime_state()
        new_endpoint = endpoint_cls(
            config=self.endpoint.config.model_copy(deep=True),
            circuit_breaker=self.endpoint.circuit_breaker,
            retry_config=self.endpoint.retry_config,
        )
        self.endpoint.copy_runtime_state_to(new_endpoint)
        if (
            share_session
            and isinstance(new_endpoint, AgenticEndpoint)
            and isinstance(self.endpoint, AgenticEndpoint)
        ):
            new_endpoint.session_id = self.endpoint.session_id
        # served_model reports this model's last call. A copy is a new model (a new
        # id) that has made none; from_dict restores the same model, so it keeps it.
        new = iModel(
            endpoint=new_endpoint,
            provider_metadata={
                k: v for k, v in self.provider_metadata.items() if k != "served_model"
            },
            streaming_process_func=self.streaming_process_func,
            hook_registry=self.hook_registry,
            exit_hook=self.exit_hook,
            max_input_tokens=self.max_input_tokens,
            tool_result_max_chars=self.tool_result_max_chars,
            **self.executor.config,
        )
        new.input_token_scale = self.input_token_scale
        if share_executor:
            new.executor = self.executor
        return new

    def to_dict(
        self,
        include_request_options: bool = False,
        include_processor_config: bool = True,
    ) -> dict:
        endpoint = self.endpoint.to_dict()
        if not include_request_options and isinstance(endpoint.get("config"), dict):
            endpoint["config"].pop("request_options", None)

        data = {
            "id": str(self.id) if self.id else None,
            "created_at": self.created_at,
            "provider_metadata": self.provider_metadata,
            "endpoint": endpoint,
        }
        if include_processor_config:
            data["processor_config"] = self.executor.config
        if self.max_input_tokens is not None:
            # Only when set, so a model without a cap serializes as it always has.
            data["max_input_tokens"] = self.max_input_tokens
        if self.tool_result_max_chars is not None:
            data["tool_result_max_chars"] = self.tool_result_max_chars
        if self.input_token_scale != 1.0:
            data["input_token_scale"] = self.input_token_scale
        return data

    @classmethod
    def from_dict(
        cls,
        data: dict,
        *,
        base_url: str | None = None,
        api_key: str | SecretStr | None = None,
        allow_local_network: bool | None = None,
    ):
        """Rebuild a model from ``to_dict`` output, read as trusted input.

        ``data`` chooses the provider, model and settings. It never supplies the API
        key, a header or a setting whose name reads as a credential, a proxy, or the
        user and password of a stored URL, and the provider metadata keeps no entry so
        named either; its other request settings are sent as written. It can turn
        the local-network opt-in off but not on. Without
        ``api_key``, the environment's credential is attached only at the provider's
        registered base URL. Without ``allow_local_network``, the provider's
        registered setting applies. ``base_url`` replaces the stored URL. The stored
        ``retry_config`` and ``circuit_breaker`` are kept.
        """
        endpoint = Endpoint.from_dict(
            data.get("endpoint", {}),
            base_url=base_url,
            api_key=api_key,
            allow_local_network=allow_local_network,
        )

        # openai_compatible=True: rehydrating a persisted iModel must never
        # raise just because its provider isn't (or is no longer) registered.
        # This lookup only recovers a registered subclass, its env-sourced API key
        # and its local-network setting; `endpoint` is already complete.
        if e1 := match_endpoint(
            provider=endpoint.config.provider,
            endpoint=endpoint.config.endpoint,
            openai_compatible=True,
        ):
            registered = e1.config
            e1.config = endpoint.config
            # The registered endpoint is built without them; copy() keeps them too.
            e1.retry_config = endpoint.retry_config
            e1.circuit_breaker = endpoint.circuit_breaker
            if allow_local_network is None:
                e1.config.allow_local_network = _restored_opt_in(registered, data)
            # The environment's credential is for the provider's own URL only.
            if api_key is None and _same_base_url(
                _as_written(data, base_url, e1.config), registered.base_url
            ):
                e1.config.api_key = registered.api_key
                e1.config.api_key_env = registered.api_key_env
                e1.config._api_key = registered._api_key
        else:
            e1 = endpoint

        # The executor's rate limits only: any other key would reach __init__ as an
        # endpoint setting, such as a base_url or an env, past the rules above.
        stored_limits = data.get("processor_config") or {}
        # to_dict writes the metadata back, so an entry named as a credential is not kept,
        # and metadata that is not a mapping cannot be read for its names and is not kept.
        stored_metadata = data.get("provider_metadata")
        if isinstance(stored_metadata, dict):
            stored_metadata = {
                k: v for k, v in stored_metadata.items() if not _names_credential_setting(k)
            }
        else:
            stored_metadata = None
        model = cls(
            endpoint=e1,
            provider_metadata=stored_metadata,
            id=data.get("id"),
            created_at=data.get("created_at"),
            max_input_tokens=data.get("max_input_tokens"),
            tool_result_max_chars=data.get("tool_result_max_chars"),
            **{name: stored_limits[name] for name in _EXECUTOR_LIMITS if name in stored_limits},
        )
        scale = data.get("input_token_scale")
        if (
            isinstance(scale, (int, float))
            and not isinstance(scale, bool)
            and math.isfinite(scale)
            and scale > 1.0
        ):
            # A resumed run keeps what its provider has already shown about its counts.
            model.input_token_scale = float(scale)
        return model

    @property
    def provider_session_id(self):
        if self.is_cli:
            return self.endpoint.session_id
        return self.provider_metadata.get("session_id")


# The settings iModel.to_dict stores as processor_config: RateLimitedAPIExecutor's config.
_EXECUTOR_LIMITS = (
    "queue_capacity",
    "capacity_refresh_time",
    "interval",
    "limit_requests",
    "limit_tokens",
    "concurrency_limit",
)


def _same_base_url(stored: str | None, registered: str | None) -> bool:
    """Whether a stored base URL is the registered one, spelled another way: scheme and
    host in any case, the scheme's default port written out, a trailing slash. A URL
    with credentials in it, or one that does not parse, matches only itself."""
    from urllib.parse import urlsplit

    if stored == registered:
        return True
    if not stored or not registered:
        return False
    keys = []
    for url in (stored, registered):
        try:
            parts = urlsplit(url)
            port = parts.port
        except ValueError:
            return False
        if parts.username is not None or parts.password is not None:
            return False
        if port == {"http": 80, "https": 443}.get(parts.scheme):
            port = None
        path = parts.path.rstrip("/")
        keys.append((parts.scheme, parts.hostname, port, path, parts.query, parts.fragment))
    return keys[0] == keys[1]


def _restored_opt_in(registered: Any, data: Mapping) -> bool:
    """The provider's registered local-network opt-in, unless the stored config turned it
    off. Off, the model reaches public hosts only; on, loopback hosts only. A stored row
    may turn it off, as its writer did for a local provider served elsewhere, but never
    on, which would point the model at this machine. A stored value other than ``True``,
    at the top of the config or in its ``kwargs``, reads as off: ``"false"`` and ``0``
    were off when the config parsed them, and an unknown spelling fails closed."""
    endpoint = data.get("endpoint")
    config = endpoint.get("config") if isinstance(endpoint, Mapping) else None
    if not isinstance(config, Mapping):
        return registered.allow_local_network
    kwargs = config.get("kwargs")
    places = (config, kwargs) if isinstance(kwargs, Mapping) else (config,)
    for place in places:
        if "allow_local_network" in place and place["allow_local_network"] is not True:
            return False
    return registered.allow_local_network


def _as_written(data: Mapping, base_url: str | None, config: Any) -> str | None:
    """The base URL the model was restored with as its writer gave it: the caller's, else
    the stored one, so a stored URL that carried a user or password matches only itself
    although the endpoint restores it without them."""
    if base_url is not None:
        return base_url
    endpoint = data.get("endpoint")
    stored = endpoint.get("config") if isinstance(endpoint, Mapping) else None
    if isinstance(stored, Mapping) and isinstance(url := _stored_base_url(stored), str):
        return url
    return config.base_url
