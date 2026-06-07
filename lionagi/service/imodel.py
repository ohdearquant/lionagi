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

from .connections import APICalling, CLIEndpoint, Endpoint, match_endpoint
from .hooks import (
    HookedEvent,
    HookEvent,
    HookEventTypes,
    HookRegistry,
    global_hook_logger,
)
from .rate_limited_processor import RateLimitedAPIExecutor


class iModel:  # noqa: N801
    """Manages API calls for a specific provider with optional rate-limiting.

    The iModel class encapsulates a specific endpoint configuration (e.g.,
    chat or completion endpoints). It determines and sets the necessary
    API key based on the provider and uses a RateLimitedAPIExecutor to
    handle queuing and throttling requests.

    Attributes:
        endpoint (Endpoint):
            The chosen endpoint object (constructed via `match_endpoint` if
            none is provided).
        executor (RateLimitedAPIExecutor):
            The rate-limited executor that queues and runs API calls in a
            controlled fashion.
    """

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
        """Initializes the iModel instance.

        Args:
            provider (str, optional):
                Name of the provider (e.g., 'openai', 'anthropic').
            base_url (str, optional):
                Base URL for the API (if a custom endpoint is needed).
            endpoint (str | Endpoint, optional):
                Either a string representing the endpoint type (e.g., 'chat')
                or an `Endpoint` instance.
            endpoint_params (list[str] | None, optional):
                Additional parameters for the endpoint (e.g., 'v1' or other).
            api_key (str, optional):
                An explicit API key. If not given, tries to load one from
                environment variables based on the provider.
            queue_capacity (int, optional):
                Maximum number of requests allowed in the queue before
                executing them.
            capacity_refresh_time (float, optional):
                Time interval (in seconds) after which the queue capacity
                is refreshed.
            interval (float | None, optional):
                Interval in seconds to check or process requests in
                the queue. If None, defaults to capacity_refresh_time.
            limit_requests (int | None, optional):
                Maximum number of requests allowed per cycle, if any.
            limit_tokens (int | None, optional):
                Maximum number of tokens allowed per cycle, if any.
            concurrency_limit (int | None, optional):
                Maximum number of streaming concurrent requests allowed.
                only applies to streaming requests.
            provider_metadata (dict | None, optional):
                Provider-specific metadata, such as session IDs for
            **kwargs:
                Additional keyword arguments, such as `model`, or any other
                provider-specific fields.
        """

        # 1. put in ID and timestamp -----------------------------------------
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

        # 2. Configure Endpoint ---------------------------------------------
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

        if api_key is not None:
            kwargs["api_key"] = api_key
        if isinstance(endpoint, Endpoint):
            self.endpoint = endpoint
        else:
            self.endpoint = match_endpoint(
                provider=provider,
                endpoint=endpoint,
                **kwargs,
            )
        if provider:
            self.endpoint.config.provider = provider
        if base_url:
            self.endpoint.config.base_url = base_url

        # 3. Configure executor ---------------------------------------------
        # Resolve defaults based on endpoint type
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

        # 4. other configurations --------------------------------------------
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
    ) -> tuple[HookEvent | None, APICalling]:
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
            if create_event_type is APICalling:
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
        """Constructs an `APICalling` object from endpoint-specific payload.

        Args:
            **kwargs:
                Additional arguments used to generate the payload.

        Returns:
            APICalling:
                An `APICalling` instance with the constructed payload,
                headers, and the selected endpoint.
        """
        # For CLI endpoints, auto-inject session_id for resume if available
        if (
            isinstance(self.endpoint, CLIEndpoint)
            and "resume" not in kwargs
            and "session_id" not in kwargs
            and self.endpoint.session_id
        ):
            kwargs["resume"] = self.endpoint.session_id

        transport_arg_keys = getattr(self.endpoint, "transport_arg_keys", ())
        call_kwargs = {k: kwargs.pop(k) for k in list(kwargs) if k in transport_arg_keys}

        # The new Endpoint.create_payload returns (payload, headers)
        payload, headers = self.endpoint.create_payload(request=kwargs)

        # Extract cache_control if provided
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
        """Processes a chunk of streaming data.

        Override this method in subclasses if you need custom handling
        of streaming responses from the API.

        Args:
            chunk:
                A portion of the streamed data returned by the API.
        """
        processed = None
        chunk_type = type(chunk)
        chunk_key = None
        if self.hook_registry._can_handle(ct_=chunk_type):
            chunk_key = chunk_type
        elif self.hook_registry._can_handle(ct_=chunk_type.__name__):
            chunk_key = chunk_type.__name__

        # Hook registry takes priority over streaming_process_func.
        # If the registry handles the chunk type, streaming_process_func is not called.
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
        """Performs a streaming API call with the given arguments.

        Args:
            **kwargs:
                Arguments for the request, merged with self.kwargs.

        Returns:
            `APICalling` | None:
                An APICalling instance upon success, or None if something
                goes wrong.
        """
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
                    # Pop without yielding — yield-in-finally swallows
                    # CancelledError during generator cleanup, breaking
                    # anyio.fail_after timeout enforcement.
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
        """Invokes a rate-limited API call with the given arguments.

        Args:
            **kwargs:
                Arguments for the request, merged with self.kwargs.

        Returns:
            APICalling | None:
                The `APICalling` object if successfully invoked and
                completed; otherwise None.

        Raises:
            ValueError:
                If the call fails or if an error occurs during invocation.
        """
        try:
            if api_call is None:
                kw.pop("stream", None)
                api_call = await self.create_event(**kw)

            if self.executor.processor is None or self.executor.processor.is_stopped():
                await self.executor.start()

            await self.executor.append(api_call)
            await self.executor.forward()

            # Wait for the event to reach a terminal status via
            # asyncio.Event instead of busy-polling.  The completion
            # event is signalled by Event.status setter when the
            # status transitions to COMPLETED, FAILED, etc.
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
                    pass  # Fall through — same as old ctr>100 break

            # Get the completed API call
            completed_call = self.executor.pile.pop(api_call.id)

            # Store session_id for CLI endpoints
            if (
                isinstance(self.endpoint, CLIEndpoint)
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
        """Whether this model uses a CLI-based endpoint."""
        return self.endpoint.is_cli

    @property
    def model_name(self) -> str:
        """str: The name of the model used by the endpoint.

        Returns:
            The model name if available; otherwise, an empty string.
        """
        return self.endpoint.config.kwargs.get("model", "")

    @property
    def request_options(self) -> type[BaseModel] | None:
        """type[BaseModel] | None: The request options model for the endpoint.

        Returns:
            The request options model if available; otherwise, None.
        """
        return self.endpoint.request_options

    async def __aenter__(self) -> iModel:
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.close()

    async def close(self) -> None:
        """Stop the executor and release resources."""
        await self.executor.stop()

    def copy(self, share_session: bool = False) -> iModel:
        """Create a new iModel with the same configuration but a fresh ID and executor.

        Args:
            share_session: If True, carry over the CLI session_id for resume.
                Defaults to False (fresh instance, no session state).
        """
        endpoint_cls = type(self.endpoint)
        new_endpoint = endpoint_cls(
            config=self.endpoint.config.model_copy(deep=True),
            circuit_breaker=self.endpoint.circuit_breaker,
            retry_config=self.endpoint.retry_config,
        )
        self.endpoint.copy_runtime_state_to(new_endpoint)
        if (
            share_session
            and isinstance(new_endpoint, CLIEndpoint)
            and isinstance(self.endpoint, CLIEndpoint)
        ):
            new_endpoint.session_id = self.endpoint.session_id
        return iModel(
            endpoint=new_endpoint,
            provider_metadata=self.provider_metadata.copy(),
            streaming_process_func=self.streaming_process_func,
            hook_registry=self.hook_registry,
            exit_hook=self.exit_hook,
            **self.executor.config,
        )

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

        if e1 := match_endpoint(
            provider=endpoint.config.provider,
            endpoint=endpoint.config.endpoint,
        ):
            # Preserve the API key from the freshly matched endpoint
            # (reads from env), then apply the serialized config on top
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
        """Get the session ID from provider metadata if available."""
        if self.is_cli:
            return self.endpoint.session_id
        return self.provider_metadata.get("session_id")
