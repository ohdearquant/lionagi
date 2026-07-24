# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel

from lionagi.ln import is_coro_func, now_utc
from lionagi.protocols.generic import ID, Event, EventStatus, Log

from .connections import AgenticEndpoint, APICalling, Endpoint, match_endpoint
from .hooks import (
    HookedEvent,
    HookEvent,
    HookEventTypes,
    HookRegistry,
    global_hook_logger,
)
from .rate_limited_processor import RateLimitedAPIExecutor


class iModel:  # noqa: N801
    """Provider endpoint wrapper with rate-limiting, hooks, and streaming."""

    def __init__(
        self,
        provider: str = None,
        base_url: str = None,
        endpoint: str | Endpoint = "chat",
        api_key: str = None,
        queue_capacity: int | None = None,
        capacity_refresh_time: float = 60,
        interval: float | None = None,
        limit_requests: int = None,
        limit_tokens: int = None,
        concurrency_limit: int | None = None,
        streaming_process_func: Callable = None,
        provider_metadata: dict | None = None,
        hook_registry: HookRegistry | dict | None = None,
        exit_hook: bool = False,
        id: UUID | str = None,  # noqa: A002
        created_at: float | None = None,
        **kwargs,
    ) -> None:
        self.id = None
        self.created_at = None
        if id is not None:
            self.id = ID.get_id(id)
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
                    provider = model.split("/")[0]
                    model = model.replace(provider + "/", "")
                    kwargs["model"] = model
                else:
                    from lionagi.config import settings

                    provider = settings.LIONAGI_CHAT_PROVIDER

            # Effort-suffixed model names ("gpt-5.6-luna-high") work at every
            # construction site: strip the suffix and route it to the
            # provider's effort kwarg. Gated to providers with an effort kwarg
            # so a real model name can never be mangled; an explicit effort
            # kwarg always wins over the suffix.
            from .providers import (
                _CLAUDE_PROVIDER_NAMES,
                _EFFORT_SUFFIX_RE,
                PROVIDER_EFFORT_KWARG,
                _clamp_claude_effort,
                normalize_effort,
            )

            _effort_kwarg = PROVIDER_EFFORT_KWARG.get(provider) if provider else None
            _model_name = kwargs.get("model")
            if _effort_kwarg and isinstance(_model_name, str):
                _suffix = _EFFORT_SUFFIX_RE.match(_model_name)
                if _suffix:
                    kwargs["model"] = _suffix.group(1)
                    if _effort_kwarg not in kwargs:
                        _eff = normalize_effort(_suffix.group(2))
                        if provider in _CLAUDE_PROVIDER_NAMES:
                            _eff = _clamp_claude_effort(_eff, kwargs["model"])
                        kwargs[_effort_kwarg] = _eff

        if api_key is not None:
            kwargs["api_key"] = api_key
        if isinstance(endpoint, Endpoint):
            self.endpoint = endpoint
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

    async def create_event(
        self,
        create_event_type: type[Event] = APICalling,
        create_event_exit_hook: bool = None,
        create_event_hook_timeout: float = 10.0,
        create_event_hook_params: dict = None,
        pre_invoke_event_exit_hook: bool = None,
        pre_invoke_event_hook_timeout: float = 30.0,
        pre_invoke_event_hook_params: dict = None,
        post_invoke_event_exit_hook: bool = None,
        post_invoke_event_hook_timeout: float = 30.0,
        post_invoke_event_hook_params: dict = None,
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
            if (
                h_ev is not None
                and h_ev.execution.status == EventStatus.COMPLETED
                and isinstance(h_ev.execution.response, create_event_type)
            ):
                # PreEventCreate returned a prepared replacement event; honor
                # it instead of constructing a fresh one from kwargs.
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

        return APICalling(
            payload=payload,
            headers=headers,
            endpoint=self.endpoint,
            call_kwargs=call_kwargs,
            cache_control=cache_control,
            include_token_usage_to_model=include_token_usage_to_model,
        )

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

    async def stream(self, api_call=None, **kw) -> AsyncGenerator:
        if api_call is None:
            kw["stream"] = True
            api_call = await self.create_event(**kw)
            await self.executor.append(api_call)

        if self.executor.processor is None or self.executor.processor.is_stopped():
            await self.executor.start()

        if self.executor.processor._concurrency_sem:
            async with self.executor.processor._concurrency_sem:
                try:
                    async for i in api_call.stream():
                        result = await self.process_chunk(i)
                        yield result if result is not None else i
                except Exception as e:
                    raise ValueError(f"Failed to stream API call: {e}") from e
                finally:
                    # Pop without yielding — yield-in-finally would swallow CancelledError
                    # during generator cleanup, breaking anyio.fail_after timeout enforcement.
                    self.executor.pile.pop(api_call.id, None)
        else:
            try:
                async for i in api_call.stream():
                    result = await self.process_chunk(i)
                    yield result if result is not None else i
            except Exception as e:
                raise ValueError(f"Failed to stream API call: {e}") from e
            finally:
                self.executor.pile.pop(api_call.id, None)

    async def invoke(self, api_call: APICalling = None, **kw) -> APICalling:
        try:
            if api_call is None:
                kw.pop("stream", None)
                api_call = await self.create_event(**kw)

            if self.executor.processor is None or self.executor.processor.is_stopped():
                await self.executor.start()

            await self.executor.append(api_call)
            await self.executor.forward()

            if api_call.status in (
                EventStatus.PROCESSING,
                EventStatus.PENDING,
            ):
                try:
                    # TODO(#1043 Phase 2): migrate to anyio cancel scope for timeout
                    await asyncio.wait_for(
                        api_call.completion_event.wait(),
                        timeout=10.0,
                    )
                except asyncio.TimeoutError:
                    pass

            completed_call = self.executor.pile.pop(api_call.id)
            if (
                isinstance(self.endpoint, AgenticEndpoint)
                and completed_call
                and completed_call.response
            ):
                response = completed_call.response
                if isinstance(response, dict) and "session_id" in response:
                    self.endpoint.session_id = response["session_id"]

            return completed_call
        except Exception as e:
            raise ValueError(f"Failed to invoke API call: {e}") from e

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
        docs/internals/runtime.md for what state is/isn't shared with the copy."""
        endpoint_cls = type(self.endpoint)
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
        new = iModel(
            endpoint=new_endpoint,
            provider_metadata=self.provider_metadata.copy(),
            streaming_process_func=self.streaming_process_func,
            hook_registry=self.hook_registry,
            exit_hook=self.exit_hook,
            **self.executor.config,
        )
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
        return data

    @classmethod
    def from_dict(cls, data: dict):
        endpoint = Endpoint.from_dict(data.get("endpoint", {}))

        # openai_compatible=True: rehydrating a persisted iModel must never
        # raise just because its provider isn't (or is no longer) registered
        # -- the deserialized `endpoint` below is already a complete,
        # authoritative config either way, this lookup only exists to recover
        # a registered subclass and a freshly env-sourced API key when one
        # applies.
        if e1 := match_endpoint(
            provider=endpoint.config.provider,
            endpoint=endpoint.config.endpoint,
            openai_compatible=True,
        ):
            # Preserve the freshly resolved (env-sourced) API key before overwriting config
            fresh_api_key = e1.config._api_key
            e1.config = endpoint.config
            if e1.config._api_key is None and fresh_api_key:
                e1.config._api_key = fresh_api_key
        else:
            e1 = endpoint

        return cls(
            endpoint=e1,
            provider_metadata=data.get("provider_metadata"),
            id=data.get("id"),
            created_at=data.get("created_at"),
            **data.get("processor_config", {}),
        )

    @property
    def provider_session_id(self):
        if self.is_cli:
            return self.endpoint.session_id
        return self.provider_metadata.get("session_id")
