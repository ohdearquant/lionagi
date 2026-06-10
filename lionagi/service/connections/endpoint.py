# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import json
import logging
from typing import ClassVar

from pydantic import BaseModel

from lionagi.service.resilience import CircuitBreaker, RetryConfig, retry_with_backoff
from lionagi.service.types.stream_chunk import StreamChunk

from .endpoint_config import EndpointConfig
from .header_factory import HeaderFactory

logger = logging.getLogger(__name__)


__all__ = ("Endpoint",)


class Endpoint:
    is_cli: ClassVar[bool] = False
    transport_arg_keys: ClassVar[tuple[str, ...]] = ()

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

        logger.debug(
            f"Initialized Endpoint with provider={self.config.provider}, "
            f"endpoint={self.config.endpoint}, circuit_breaker={circuit_breaker is not None}, "
            f"retry_config={retry_config is not None}"
        )

    def _create_http_session(self):
        import aiohttp

        return aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(self.config.timeout),
            **self.config.client_kwargs,
        )

    def copy_runtime_state_to(self, other: Endpoint) -> None:
        pass

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
        headers = HeaderFactory.get_header(
            auth_type=self.config.auth_type,
            content_type=self.config.content_type,
            api_key=self.config._api_key,
            default_headers=self.config.default_headers,
        )
        if extra_headers:
            headers.update(extra_headers)

        request = request if isinstance(request, dict) else request.model_dump(exclude_none=True)

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

        return (payload, headers)

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

    async def call(
        self,
        request: dict | BaseModel,
        cache_control: bool = False,
        skip_payload_creation: bool = False,
        **kwargs,
    ):
        extra_headers = kwargs.pop("extra_headers", None)

        payload, headers = None, None
        if skip_payload_creation:
            # Treat request as the ready payload
            payload = request if isinstance(request, dict) else request.model_dump()
            headers = extra_headers or {}
        else:
            payload, headers = self.create_payload(request, extra_headers=extra_headers, **kwargs)

        call_func = self._call

        if self.retry_config:

            async def call_func(p, h, **kw):
                return await retry_with_backoff(
                    self._call, p, h, **kw, **self.retry_config.as_kwargs()
                )

        if self.circuit_breaker:
            if self.retry_config:
                if not cache_control:
                    return await self.circuit_breaker.execute(call_func, payload, headers, **kwargs)
            else:
                if not cache_control:
                    return await self.circuit_breaker.execute(
                        self._call, payload, headers, **kwargs
                    )

        if cache_control:
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
            return await call_func(payload, headers, **kwargs)

        return await self._call(payload, headers, **kwargs)

    async def _call_aiohttp(self, payload: dict, headers: dict, **kwargs):
        self._assert_ssrf_safe_url()

        import aiohttp
        import backoff

        async def _make_request_with_backoff():
            async with self._create_http_session() as session:
                response = None
                try:
                    response = await session.request(
                        method=self.config.method,
                        url=self.config.full_url,
                        headers=headers,
                        json=payload,
                        **kwargs,
                    )

                    if response.status == 429 or response.status >= 500:
                        response.raise_for_status()  # This will be caught by backoff
                    elif response.status != 200:
                        try:
                            error_body = await response.json()
                            error_message = (
                                f"Request failed with status {response.status}: {error_body}"
                            )
                        except Exception:
                            error_message = f"Request failed with status {response.status}"

                        raise aiohttp.ClientResponseError(
                            request_info=response.request_info,
                            history=response.history,
                            status=response.status,
                            message=error_message,
                            headers=response.headers,
                        )

                    return await response.json()
                finally:
                    # Ensure response is properly released if coroutine is cancelled between retries.
                    # aiohttp.ClientResponse.release() is synchronous (not a coroutine) — do not await.
                    if response is not None and not response.closed:
                        response.release()

        # When retry_config is set, the outer call() already wraps this method in
        # retry_with_backoff. Skip the internal backoff layer to prevent double-retry.
        if self.retry_config:
            return await _make_request_with_backoff()

        def giveup_on_client_error(e):
            # Don't retry on 4xx except 429 (rate limit)
            if isinstance(e, aiohttp.ClientResponseError):
                return 400 <= e.status < 500 and e.status != 429
            return False

        backoff_handler = backoff.on_exception(
            backoff.expo,
            (aiohttp.ClientError, asyncio.TimeoutError),
            max_tries=self.config.max_retries,
            giveup=giveup_on_client_error,
            jitter=backoff.full_jitter,
        )

        return await backoff_handler(_make_request_with_backoff)()

    async def stream(
        self,
        request: dict | BaseModel,
        extra_headers: dict | None = None,
        **kwargs,
    ):
        payload, headers = self.create_payload(request, extra_headers, **kwargs)

        async for chunk in self._stream_aiohttp(payload=payload, headers=headers, **kwargs):
            yield chunk

    async def _stream_aiohttp(self, payload: dict, headers: dict, **kwargs):
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
            config = EndpointConfig(**config)

        return cls(
            config=config,
            circuit_breaker=circuit_breaker,
            retry_config=retry_config,
        )
