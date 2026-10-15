# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any, ClassVar, Literal

from pydantic import BaseModel

from lionagi.service.resilience import CircuitBreaker, RetryConfig, retry_with_backoff
from lionagi.service.types.stream_chunk import StreamChunk

from .endpoint_config import AUTHORIZATION_KWARG_NAMES, EndpointConfig
from .header_factory import HeaderFactory

_CONFIGURED_TIMEOUT = object()


class _NonRetryableClientError(Exception):
    """Sentinel: 4xx (non-429) error that must not be retried. Wraps the original
    via __cause__ so callers can inspect it while the retry engine sees an excluded type."""

    def __init__(self, original: Exception):
        super().__init__(str(original))
        self.original = original


class _RoutedAttemptError(Exception):
    """Private failure passed between the request and retry layers.

    Its message holds only the code; ``account``, ``attempt_id`` and ``http_status`` are
    attributes.
    Raised ``from None``, it still keeps the exception it replaced as ``__context__``.
    """

    def __init__(
        self,
        code: str,
        *,
        account: str | None = None,
        attempt_id: Any = None,
        http_status: int | None = None,
    ) -> None:
        self.code = code
        self.account = account
        self.attempt_id = attempt_id
        self.http_status = http_status
        super().__init__(code)


class _RoutedRetryableError(_RoutedAttemptError):
    pass


class _RoutedNoRetryError(_RoutedAttemptError):
    pass


def _error_in_body(payload: Any, body: Any) -> Any:
    """The provider error a successful reply to a chat request carries instead of a result.

    Returns None for a result. Only a chat request (one that sends ``messages``) is
    checked, since other APIs, such as a scrape that answers ``success: false`` with an
    ``error``, return their own result shapes. Only a bare error envelope counts. A
    typed API object (one with ``object`` set, such as an OpenAI Responses result whose
    ``status`` is ``failed``) is a result even when it carries an ``error`` field.
    """
    if (
        isinstance(payload, dict)
        and "messages" in payload
        and isinstance(body, dict)
        and body.get("error")
        and "choices" not in body
        and "object" not in body
    ):
        return body["error"]
    return None


logger = logging.getLogger(__name__)


__all__ = ("Endpoint",)


class Endpoint:
    is_cli: ClassVar[bool] = False
    transport_arg_keys: ClassVar[tuple[str, ...]] = ()
    _account_routing_provider: ClassVar[str | None] = None

    def __init__(
        self,
        config: dict | EndpointConfig | None = None,
        circuit_breaker: CircuitBreaker | None = None,
        retry_config: RetryConfig | None = None,
        **kwargs,
    ):
        if config is None:
            meta = getattr(type(self), "_ENDPOINT_META", None)
            if meta is not None:
                _config = meta.create_config(**kwargs)
            else:
                raise ValueError(
                    "No config provided and no _ENDPOINT_META on class. "
                    "Either pass a config or use @register_endpoint."
                )
        elif isinstance(config, dict):
            _config = EndpointConfig(**config, **kwargs)
        elif isinstance(config, EndpointConfig):
            _config = config.model_copy(deep=True)
            _config.update(**kwargs)
        else:
            raise ValueError("Config must be a dict, EndpointConfig, or None")
        self.config = _config
        self.circuit_breaker = circuit_breaker
        self.retry_config = retry_config
        self._direct_account_scope_id = None

        logger.debug(
            f"Initialized Endpoint with provider={self.config.provider}, "
            f"endpoint={self.config.endpoint}, circuit_breaker={circuit_breaker is not None}, "
            f"retry_config={retry_config is not None}"
        )

    def _create_http_session(
        self, client_kwargs: dict[str, Any] | None = None, *, timeout: Any = _CONFIGURED_TIMEOUT
    ):
        import aiohttp

        if timeout is _CONFIGURED_TIMEOUT:
            timeout = self.config.timeout
        return aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(timeout),
            **(self.config.client_kwargs if client_kwargs is None else client_kwargs),
        )

    def copy_runtime_state_to(self, other: Endpoint) -> None:
        pass

    def drain_runtime_state(self) -> None:
        """Endpoints that hold runtime-only state move it out of the config here.

        A plain endpoint has none, so this does nothing. It is declared beside
        ``copy_runtime_state_to`` because the two are called as a pair, and a
        caller that has to ask whether an endpoint supports draining will
        eventually skip it for the one that needs it.
        """

    @property
    def request_options(self):
        return self.config.request_options

    @request_options.setter
    def request_options(self, value):
        self.config.request_options = EndpointConfig._validate_request_options(value)

    def create_payload(
        self,
        request: dict | BaseModel,
        extra_headers: dict | None = None,
        **kwargs,
    ):
        routed = self._account_routing_enabled()
        if routed:
            from lionagi.service.accounts._router import header_pairs

            # Read as every routed path reads headers, so a form the merge cannot read is
            # refused as a credential conflict rather than failing inside the merge.
            # A later pair replaces an earlier one of the same name, compared case-insensitively,
            # so a caller's `content-type` or `anthropic-version` takes the default's place.
            headers = HeaderFactory.get_header(
                auth_type="none", content_type=self.config.content_type
            )
            for name, value in (
                *header_pairs(self.config.default_headers),
                *header_pairs(extra_headers),
            ):
                for same in [key for key in headers if key.lower() == name.lower()]:
                    del headers[same]
                headers[name] = value
        else:
            headers = HeaderFactory.get_header(
                auth_type=self.config.auth_type,
                content_type=self.config.content_type,
                api_key=self.config._api_key,
                default_headers=self.config.default_headers,
            )
            if extra_headers:
                headers.update(extra_headers)

        request = request if isinstance(request, dict) else request.model_dump(exclude_none=True)
        if routed and ("api_key" in request or "api_key" in kwargs):
            from lionagi.service.accounts._types import AccountRoutingError, RouterErrorCode

            raise AccountRoutingError(RouterErrorCode.CREDENTIAL_CONFLICT) from None

        if routed and any(
            source.get("response_mode", "json") != "json" for source in (request, kwargs)
        ):
            from lionagi.service.accounts._types import AccountRoutingError, RouterErrorCode

            # A routed call is settled from the usage in its JSON body and its key is
            # masked in that body as parsed; a mode that returns anything else can do
            # neither, so it is refused here, before a request is built or sent.
            raise AccountRoutingError(RouterErrorCode.UNSUPPORTED_PRICING) from None

        payload = self.config.kwargs.copy()
        payload.update(request)

        if kwargs:
            payload.update(kwargs)

        if self.config.request_options is not None:
            valid_fields = set(self.config.request_options.model_fields.keys())
            filtered_payload = {k: v for k, v in payload.items() if k in valid_fields}
            payload = self.config.validate_payload(filtered_payload)
        else:
            non_api_params = {
                "provider",
                "base_url",
                "endpoint",
                "endpoint_params",
                "api_key",
                "queue_capacity",
                "capacity_refresh_time",
                "invoke_with_endpoint",
                "extra_headers",
                "headers",
                "include_token_usage_to_model",
                "chat_model",
                "imodel",
                "branch",
                "aggregation_sources",
                "aggregation_count",
                "action_strategy",
                "parse_model",
                "actions",
                "return_operative",
                # Removed operation aliases — drop so a stale caller passing
                # one as **kwargs can't leak it into a schema-less payload.
                "request_model",
                "operative_model",
            }
            payload = {k: v for k, v in payload.items() if k not in non_api_params}

        if routed:
            from lionagi.service.accounts._router import validate_routed_request

            validate_routed_request(
                self,
                headers,
                request_kwargs={},
                payload=payload,
            )
        return (payload, headers)

    def _account_routing_enabled(self) -> bool:
        if self._account_routing_provider != "anthropic":
            return False
        from lionagi.config import settings

        return bool(settings.ANTHROPIC_ACCOUNTS)

    def _account_scope(self, supplied_scope: Any = None) -> Any:
        if supplied_scope is not None:
            return supplied_scope
        from uuid import uuid4

        from lionagi.service.accounts._types import Scope

        if self._direct_account_scope_id is None:
            self._direct_account_scope_id = uuid4()
        return Scope("endpoint", self._direct_account_scope_id, "anthropic")

    @staticmethod
    def _public_routed_failure(exc: _RoutedAttemptError) -> Exception:
        from lionagi.service.accounts._types import AccountRoutingError, RouterErrorCode

        return AccountRoutingError(
            RouterErrorCode(exc.code),
            account=exc.account,
            attempt_id=exc.attempt_id,
            http_status=exc.http_status,
        )

    def _assert_ssrf_safe_url(self) -> None:
        """Raise PermissionError if full_url resolves to a blocked address."""
        from urllib.parse import urlparse

        from lionagi.ln._ssrf import is_ssrf_safe

        parsed = urlparse(self.config.full_url)
        hostname = parsed.hostname or ""
        allow_local = getattr(self.config, "allow_local_network", False)
        if not is_ssrf_safe(hostname, allow_local=allow_local):
            raise PermissionError(
                f"SSRF guard: request to {hostname!r} is blocked "
                "(hostname resolves to a private or reserved IP address)"
            )

    async def _call(self, payload: dict, headers: dict, **kwargs):
        return await self._call_aiohttp(payload=payload, headers=headers, **kwargs)

    def _with_idempotency_key(self, headers: dict, *, routed: bool) -> dict:
        """*headers* with the key one logical request sends on every attempt, when retries are
        idempotent.

        Minted once per logical request, before any retry path (native _call_aiohttp, the
        opt-in RetryConfig wrapper, or a routed stream's attempts) is entered, and reused
        verbatim across every attempt, so the provider can recognize a lost-response replay as
        the same request. It also takes the place of a client-level key: one key shared by every
        request would let the provider answer a new request with an earlier one's response.
        """
        if not (self.config.idempotent_retries and self.config.method.upper() == "POST"):
            return headers
        import uuid

        if routed:
            # Routed headers were read as name-value pairs: each name is a token given once
            # and each value a string. A caller's key is kept whatever the case of its name,
            # and an empty or blank one, which names no request, is replaced.
            named = [name for name in headers if name.lower() == "idempotency-key"]
            if not any(headers[name].strip() for name in named):
                headers = {name: value for name, value in headers.items() if name not in named}
                headers["Idempotency-Key"] = str(uuid.uuid4())
        elif "Idempotency-Key" not in headers:
            headers = {**headers, "Idempotency-Key": str(uuid.uuid4())}
        return headers

    async def call(
        self,
        request: dict | BaseModel,
        cache_control: bool = False,
        skip_payload_creation: bool = False,
        _routing_scope: Any = None,
        **kwargs,
    ):
        routed = self._account_routing_enabled()
        # Only a routed call hands the scope down: an unrouted endpoint's _call never
        # receives a keyword its signature may not take.
        scope_kwargs = {"_routing_scope": self._account_scope(_routing_scope)} if routed else {}
        use_cache = cache_control and not routed
        extra_headers = kwargs.pop("extra_headers", None)

        payload, headers = None, None
        if skip_payload_creation:
            # Treat request as the ready payload
            payload = request if isinstance(request, dict) else request.model_dump()
            headers = extra_headers or {}
            if routed:
                from lionagi.service.accounts._router import header_pairs

                # A list of pairs becomes the mapping the steps below expect; any other form is
                # refused as a credential conflict before anything is reserved.
                headers = dict(header_pairs(headers))
        else:
            payload, headers = self.create_payload(request, extra_headers=extra_headers, **kwargs)

        headers = self._with_idempotency_key(headers, routed=routed)

        call_func = self._call

        if self.retry_config:

            async def call_func(p, h, **kw):
                # 4xx sentinel stays excluded until this retry layer gives up, else a
                # broad retry_exceptions config could replay a single-shot 4xx response.
                retry_kwargs = self.retry_config.as_kwargs()
                retry_kwargs["exclude_exceptions"] = (
                    *retry_kwargs.get("exclude_exceptions", ()),
                    _NonRetryableClientError,
                )
                if routed:
                    retry_kwargs["exclude_exceptions"] = (
                        *retry_kwargs["exclude_exceptions"],
                        _RoutedNoRetryError,
                    )
                    retry_kwargs["retry_exceptions"] = (
                        *retry_kwargs["retry_exceptions"],
                        _RoutedRetryableError,
                    )
                try:
                    return await retry_with_backoff(self._call, p, h, **kw, **retry_kwargs)
                except _NonRetryableClientError as exc:
                    raise exc.original from exc.__cause__
                except _RoutedAttemptError as exc:
                    raise self._public_routed_failure(exc) from None

        if self.circuit_breaker:
            if self.retry_config:
                if not use_cache:
                    return await self.circuit_breaker.execute(
                        call_func,
                        payload,
                        headers,
                        **scope_kwargs,
                        **kwargs,
                    )
            else:
                if not use_cache:
                    return await self.circuit_breaker.execute(
                        self._call,
                        payload,
                        headers,
                        **scope_kwargs,
                        **kwargs,
                    )

        if use_cache:
            from aiocache import cached

            from lionagi.config import settings

            @cached(**settings.aiocache_config.as_kwargs())
            async def _cached_call(payload: dict, headers: dict, **kwargs):
                if self.circuit_breaker and self.retry_config:
                    return await self.circuit_breaker.execute(call_func, payload, headers, **kwargs)
                if self.circuit_breaker:
                    return await self.circuit_breaker.execute(
                        self._call, payload, headers, **kwargs
                    )
                if self.retry_config:
                    return await call_func(payload, headers, **kwargs)

                return await self._call(payload, headers, **kwargs)

            return await _cached_call(payload, headers, **kwargs)

        if self.retry_config:
            return await call_func(
                payload,
                headers,
                **scope_kwargs,
                **kwargs,
            )

        return await self._call(
            payload,
            headers,
            **scope_kwargs,
            **kwargs,
        )

    def _can_retry(self) -> bool:
        """Whether more than one request attempt can occur for this endpoint.
        Single-shot endpoints (max_retries<=1, no RetryConfig) never replay a body —
        see docs/internals/runtime.md."""
        if self.retry_config:
            return self.retry_config.max_retries > 0
        return self.config.max_retries > 1

    async def _request_attempt(
        self,
        *,
        payload: dict[str, Any],
        headers: dict[str, str],
        body_kwargs: dict[str, Any],
        request_kwargs: dict[str, Any],
        response_mode: Literal["json", "bytes"],
        error_context: str,
        _routing_scope: Any = None,
    ) -> Any:
        import aiohttp

        async with self._create_http_session() as session:
            response = None
            try:
                response = await session.request(
                    method=self.config.method,
                    url=self.config.full_url,
                    headers=headers,
                    **body_kwargs,
                    **request_kwargs,
                )

                if response.status == 429 or response.status >= 500:
                    response.raise_for_status()
                elif response.status != 200:
                    try:
                        error_body = await response.json()
                        error_message = (
                            f"{error_context} failed with status {response.status}: {error_body}"
                        )
                    except Exception:
                        error_message = f"{error_context} failed with status {response.status}"

                    original = aiohttp.ClientResponseError(
                        request_info=response.request_info,
                        history=response.history,
                        status=response.status,
                        message=error_message,
                        headers=response.headers,
                    )
                    # 4xx (non-429): wrap in sentinel so the native retry layer
                    # gives up immediately, preserving the original error on __cause__.
                    raise _NonRetryableClientError(original) from original

                if response_mode == "bytes":
                    return await response.read()
                body = await response.json()
                error = _error_in_body(payload, body)
                if error is None:
                    return body

                # Some OpenAI-compatible gateways answer a refused request with
                # HTTP 200 and an error object in place of a result. Fail it the
                # way the status it names would have failed.
                code = error.get("code") if isinstance(error, dict) else None
                status = (
                    code
                    if isinstance(code, int) and not isinstance(code, bool) and 400 <= code < 600
                    else response.status
                )
                original = aiohttp.ClientResponseError(
                    request_info=response.request_info,
                    history=response.history,
                    status=status,
                    message=f"{error_context} failed with status {status}: {body}",
                    headers=response.headers,
                )
                if status == 429 or status >= 500:
                    raise original
                raise _NonRetryableClientError(original) from original
            finally:
                # Ensure response is properly released if coroutine is cancelled between retries.
                # aiohttp.ClientResponse.release() is synchronous (not a coroutine) — do not await.
                if response is not None and not response.closed:
                    response.release()

    async def _call_aiohttp(
        self,
        payload: dict[str, Any],
        headers: dict[str, str],
        *,
        request_body_factory: Callable[[], dict[str, Any]] | None = None,
        response_mode: Literal["json", "bytes"] = "json",
        error_context: str = "Request",
        _routing_scope: Any = None,
        **request_kwargs: Any,
    ) -> Any:
        routed = self._account_routing_enabled()
        if routed:
            from lionagi.service.accounts._router import validate_routed_request
            from lionagi.service.accounts._types import AccountRoutingError

            try:
                validate_routed_request(
                    self,
                    headers,
                    request_kwargs=request_kwargs,
                    payload=payload,
                )
            except AccountRoutingError as exc:
                if not self.retry_config:
                    raise
                # call() unwraps this after its RetryConfig layer, which must not replay it.
                raise _RoutedNoRetryError(
                    exc.code.value,
                    account=exc.account,
                    attempt_id=exc.attempt_id,
                    http_status=exc.http_status,
                ) from None
            _routing_scope = self._account_scope(_routing_scope)
        self._assert_ssrf_safe_url()

        import aiohttp

        build_body = request_body_factory or (lambda: {"json": payload})

        async def _make_request():
            # Body rebuilt per-attempt, not before retry orchestration: FormData/BytesIO/
            # file streams get consumed by the first POST and must not be silently replayed.
            body_kwargs = build_body()
            collision = set(body_kwargs) & set(request_kwargs)
            if collision:
                if routed:
                    from lionagi.service.accounts._types import AccountRoutingError, RouterErrorCode

                    raise AccountRoutingError(RouterErrorCode.CREDENTIAL_CONFLICT) from None
                raise ValueError(
                    f"request_body_factory and request_kwargs both supply {sorted(collision)}"
                )
            return await self._request_attempt(
                payload=payload,
                headers=headers,
                body_kwargs=body_kwargs,
                request_kwargs=request_kwargs,
                response_mode=response_mode,
                error_context=error_context,
                _routing_scope=_routing_scope,
            )

        # When retry_config is set, call() already wraps this in retry_with_backoff and
        # owns the sentinel unwrap — just run the raw request and let it propagate.
        if self.retry_config:
            return await _make_request()

        # No RetryConfig: native path excludes the 4xx sentinel so it gives up immediately.
        # max_retries is a total-attempt cap; retry_with_backoff runs +1 attempts, so subtract one.
        retry_exceptions = (aiohttp.ClientError, asyncio.TimeoutError)
        excluded_exceptions: tuple[type[Exception], ...] = (_NonRetryableClientError,)
        if routed:
            retry_exceptions = (*retry_exceptions, _RoutedRetryableError)
            excluded_exceptions = (*excluded_exceptions, _RoutedNoRetryError)
        try:
            return await retry_with_backoff(
                _make_request,
                retry_exceptions=retry_exceptions,
                exclude_exceptions=excluded_exceptions,
                max_retries=max(0, self.config.max_retries - 1),
                base_delay=1.0,
                max_delay=60.0,
                backoff_factor=2.0,
                jitter=True,
                jitter_factor=0.5,
            )
        except _NonRetryableClientError as exc:
            raise exc.original from exc.__cause__
        except _RoutedAttemptError as exc:
            raise self._public_routed_failure(exc) from None

    async def stream(
        self,
        request: dict | BaseModel,
        extra_headers: dict | None = None,
        _routing_scope: Any = None,
        **kwargs,
    ):
        routed = self._account_routing_enabled()
        scope_kwargs = {"_routing_scope": self._account_scope(_routing_scope)} if routed else {}
        payload, headers = self.create_payload(request, extra_headers, **kwargs)
        if routed:
            # A routed stream retries before its first chunk, so its attempts share one key
            # as a routed call's do.
            headers = self._with_idempotency_key(headers, routed=True)

        stream = self._stream_aiohttp(
            payload=payload,
            headers=headers,
            **scope_kwargs,
            **kwargs,
        )
        if not routed:
            async for chunk in stream:
                yield chunk
            return
        try:
            async for chunk in stream:
                yield chunk
        finally:
            # A consumer that stops early closes this generator. Close the routed attempt
            # too, so it records how the stream ended now rather than when collected.
            await stream.aclose()

    async def _stream_aiohttp(
        self,
        payload: dict,
        headers: dict,
        *,
        _routing_scope: Any = None,
        **kwargs,
    ):
        self._assert_ssrf_safe_url()

        payload["stream"] = True

        async with self._create_http_session() as session:
            async with session.request(
                method=self.config.method,
                url=self.config.full_url,
                headers=headers,
                json=payload,
                **kwargs,
            ) as response:
                if response.status != 200:
                    import aiohttp

                    raise aiohttp.ClientResponseError(
                        request_info=response.request_info,
                        history=response.history,
                        status=response.status,
                        message=f"Request failed with status {response.status}",
                        headers=response.headers,
                    )

                pending = ""
                event_data: list[str] = []
                async for raw in response.content:
                    if not raw:
                        continue
                    pending += raw.decode("utf-8")
                    while "\n" in pending:
                        line, pending = pending.split("\n", 1)
                        chunk = self._stream_line_to_chunk(line, event_data)
                        if chunk is not None:
                            yield chunk

                if pending:
                    chunk = self._stream_line_to_chunk(pending, event_data)
                    if chunk is not None:
                        yield chunk
                if event_data:
                    yield self._line_to_stream_chunk("\n".join(event_data))

    def _stream_line_to_chunk(
        self,
        line: str,
        event_data: list[str],
    ) -> StreamChunk | None:
        """Parse one HTTP stream line, handling SSE framing when present."""
        text = line.rstrip("\r")
        stripped = text.strip()

        if not stripped:
            if not event_data:
                return None
            data = "\n".join(event_data)
            event_data.clear()
            return self._line_to_stream_chunk(data)

        if stripped.startswith(":"):
            return None

        if stripped.startswith(("event:", "id:", "retry:")):
            return None

        if stripped.startswith("data:"):
            data = stripped.removeprefix("data:")
            if data.startswith(" "):
                data = data[1:]
            if event_data and self._looks_like_complete_stream_data(event_data):
                previous = "\n".join(event_data)
                event_data.clear()
                event_data.append(data)
                return self._line_to_stream_chunk(previous)
            event_data.append(data)
            return None

        if event_data:
            data = "\n".join(event_data)
            event_data.clear()
            return self._line_to_stream_chunk(data)

        return self._line_to_stream_chunk(stripped)

    @staticmethod
    def _looks_like_complete_stream_data(data_lines: list[str]) -> bool:
        data = "\n".join(data_lines).strip()
        if data == "[DONE]":
            return True
        try:
            json.loads(data)
        except json.JSONDecodeError:
            return False
        return True

    def _line_to_stream_chunk(self, line: str) -> StreamChunk:
        """Convert a generic HTTP stream line into the StreamChunk contract."""
        data = line
        if line.startswith("data:"):
            data = line.removeprefix("data:").strip()

        if data == "[DONE]":
            return StreamChunk(type="result", metadata={"done": True})

        try:
            event = json.loads(data)
        except json.JSONDecodeError:
            return StreamChunk(
                type="text",
                content=data,
                is_delta=True,
                metadata={"raw": line},
            )

        if not isinstance(event, dict):
            return StreamChunk(
                type="text",
                content=str(event),
                is_delta=True,
                metadata={"raw": event},
            )

        chunk = self._event_to_stream_chunk(event)
        if chunk is not None:
            return chunk
        return StreamChunk(type="system", metadata={"raw": event})

    def _event_to_stream_chunk(self, event: dict) -> StreamChunk | None:
        """Best-effort conversion for OpenAI-compatible and SSE JSON events."""
        typ = event.get("type")
        if typ in {"error", "response.error"}:
            err = event.get("error", event)
            return StreamChunk(
                type="error",
                content=str(err),
                is_error=True,
                metadata={"raw": event},
            )

        if typ in {"response.output_text.delta", "response.refusal.delta"}:
            return StreamChunk(
                type="text",
                content=event.get("delta", ""),
                is_delta=True,
                metadata={"raw": event},
            )
        if typ == "response.completed":
            return StreamChunk(type="result", metadata={"raw": event})

        if typ == "content_block_delta":
            delta = event.get("delta", {})
            if delta.get("type") in {"text_delta", "thinking_delta"}:
                chunk_type = "thinking" if delta.get("type") == "thinking_delta" else "text"
                return StreamChunk(
                    type=chunk_type,
                    content=delta.get("text") or delta.get("thinking", ""),
                    is_delta=True,
                    metadata={"raw": event},
                )

        choices = event.get("choices")
        if isinstance(choices, list) and choices:
            choice = choices[0] or {}
            delta = choice.get("delta") or choice.get("message") or {}
            if isinstance(delta, dict):
                content = delta.get("content")
                if content:
                    return StreamChunk(
                        type="text",
                        content=content,
                        is_delta=True,
                        metadata={"raw": event},
                    )
                tool_calls = delta.get("tool_calls")
                if isinstance(tool_calls, list) and tool_calls:
                    call = tool_calls[0] or {}
                    function = call.get("function") or {}
                    return StreamChunk(
                        type="tool_use",
                        tool_name=function.get("name"),
                        tool_id=call.get("id"),
                        tool_input={"arguments": function.get("arguments")},
                        is_delta=True,
                        metadata={"raw": event},
                    )
            return StreamChunk(type="system", metadata={"raw": event})

        # Usage-only terminal event: a Chat Completions stream with
        # stream_options.include_usage ends with {"choices": [], "usage": {...}}.
        # Without this branch the empty-choices list would fall through to a
        # "system" chunk and consumers that read terminal accounting from result
        # chunks would miss the provider-reported usage.
        usage = event.get("usage")
        if usage is not None and isinstance(choices, list):
            return StreamChunk(type="result", metadata={"raw": event, "usage": usage})

        return None

    def to_dict(self):
        return {
            "retry_config": (self.retry_config.to_dict() if self.retry_config else None),
            "circuit_breaker": (self.circuit_breaker.to_dict() if self.circuit_breaker else None),
            "config": self.config.model_dump(exclude_none=True),
        }

    @classmethod
    def from_dict(cls, data: dict):
        from lionagi.utils import to_dict

        data = to_dict(data, recursive=True)
        retry_config = data.get("retry_config")
        circuit_breaker = data.get("circuit_breaker")
        config = data.get("config")

        if retry_config:
            retry_config = RetryConfig(**retry_config)
        if circuit_breaker:
            circuit_breaker = CircuitBreaker(**circuit_breaker)
        if config:
            # The config never writes a grant down, but this reads a stored row
            # as it stands: one written by an older version, or by any other
            # writer, may still carry it. A grant belongs to the role of the
            # caller restoring the row, who hands it again if the role names it.
            kwargs = config.get("kwargs")
            if isinstance(kwargs, dict):
                config = {
                    **config,
                    "kwargs": {
                        k: v for k, v in kwargs.items() if k not in AUTHORIZATION_KWARG_NAMES
                    },
                }
            config = EndpointConfig(**config)

        return cls(
            config=config,
            circuit_breaker=circuit_breaker,
            retry_config=retry_config,
        )
