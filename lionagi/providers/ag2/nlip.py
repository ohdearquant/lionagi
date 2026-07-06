# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""AG2 NLIP Remote Agent endpoint: connects to a remote NLIP server and streams responses."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from typing import Any
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field

from lionagi.ln import is_ssrf_safe
from lionagi.service.connections import AgenticEndpoint, EndpointConfig
from lionagi.service.resilience import retry_with_backoff
from lionagi.service.types import StreamChunk
from lionagi.utils import to_dict

from ._config import AG2Configs

logger = logging.getLogger(__name__)

__all__ = ["AG2NlipRequest", "_assert_nlip_url_safe", "call_nlip_remote", "AG2NlipEndpoint"]


class AG2NlipRequest(BaseModel):
    """Request for AG2 NLIP remote endpoint."""

    messages: list[dict[str, Any]] = Field(default_factory=list)
    prompt: str = ""


def _assert_nlip_url_safe(url: str) -> None:
    """Validate URL scheme (http/https) and SSRF safety; raises PermissionError or ValueError on rejection."""
    _parsed = urlparse(url)
    if _parsed.scheme not in ("http", "https"):
        raise ValueError(
            f"NLIP URL has unsupported scheme {_parsed.scheme!r}. Only http and https are allowed."
        )
    if not is_ssrf_safe(_parsed.hostname or ""):
        raise PermissionError(
            "SSRF guard: NLIP URL blocked — hostname resolves to a private "
            f"or reserved IP address: {url!r}"
        )


async def _post_json_with_retry(
    client: httpx.AsyncClient,
    url: str,
    json_payload: dict[str, Any],
    max_retries: int,
    on_retry: Callable[[Exception, int, int], None] | None = None,
) -> httpx.Response | None:
    """POST with exponential backoff, retrying only on timeout/connect errors; max_retries<=0 makes no request."""
    if max_retries <= 0:
        return None

    attempt = 0

    async def _post() -> httpx.Response:
        nonlocal attempt
        attempt += 1
        try:
            response = await client.post(url, json=json_payload)
            response.raise_for_status()
            return response
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            if on_retry is not None and attempt < max_retries:
                on_retry(exc, attempt, max_retries)
            raise

    return await retry_with_backoff(
        _post,
        retry_exceptions=(httpx.TimeoutException, httpx.ConnectError),
        max_retries=max_retries - 1,
    )


async def call_nlip_remote(
    url: str,
    messages: list[dict[str, Any]],
    agent_name: str = "remote",
    timeout: float = 60.0,
    max_retries: int = 3,
) -> dict[str, Any]:
    """Call a remote NLIP endpoint; applies SSRF guard then falls back to direct httpx if nlip_sdk is absent."""
    # SSRF guard: reject calls to private/reserved IP ranges.
    _assert_nlip_url_safe(url)
    try:
        return await _call_nlip_sdk(url, messages, timeout, max_retries)
    except ImportError:
        logger.info("nlip_sdk not installed, using direct HTTP")
        return await _call_direct(url, messages, timeout, max_retries)


async def _call_nlip_sdk(
    url: str,
    messages: list[dict[str, Any]],
    timeout: float,
    max_retries: int,
) -> dict[str, Any]:
    """Use nlip_sdk for proper NLIP message format."""
    from nlip_sdk.nlip import NLIP_Factory, NLIP_Message

    last_content = ""
    for msg in reversed(messages):
        content = msg.get("content", "")
        if content and content != "None":
            last_content = content
            break

    nlip_msg = NLIP_Factory.create_text(last_content, language="english")

    if len(messages) > 1:
        sanitized = [
            {"role": m.get("role", "user"), "content": m.get("content", "")}
            for m in messages
            if m.get("content")
        ]
        if sanitized:
            nlip_msg.add_json({"messages": sanitized}, label="ag2_chat_history")

    def _log_retry(exc: Exception, attempt: int, total: int) -> None:
        if isinstance(exc, httpx.TimeoutException):
            logger.warning("NLIP timeout (attempt %d/%d)", attempt, total)
        else:
            logger.warning("NLIP connect failed (attempt %d/%d)", attempt, total)

    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await _post_json_with_retry(
            client,
            f"{url.rstrip('/')}/nlip/",
            nlip_msg.model_dump(exclude_none=True),
            max_retries,
            on_retry=_log_retry,
        )
        if response is None:
            return {"content": "", "context": None, "input_required": None}

        data = response.json()
        nlip_response = NLIP_Message.model_validate(data)
        content = nlip_response.content if isinstance(nlip_response.content, str) else ""

    return {"content": content, "context": None, "input_required": None}


async def _call_direct(
    url: str,
    messages: list[dict[str, Any]],
    timeout: float,
    max_retries: int,
) -> dict[str, Any]:
    """Direct httpx fallback (no nlip_sdk): POSTs the last message as plain text."""
    last_content = ""
    for msg in reversed(messages):
        content = msg.get("content", "")
        if content and content != "None":
            last_content = content
            break

    payload = {
        "format": "text",
        "subformat": "english",
        "content": last_content,
    }

    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await _post_json_with_retry(
            client,
            f"{url.rstrip('/')}/nlip/",
            payload,
            max_retries,
        )
        if response is None:
            return {"content": "", "context": None, "input_required": None}

        data = response.json()

    content = ""
    if isinstance(data, dict):
        content = data.get("content", "")
        if isinstance(content, dict):
            content = content.get("content", str(content))

    return {"content": content, "context": None, "input_required": None}


logger = logging.getLogger(__name__)


@AG2Configs.NLIP.register
class AG2NlipEndpoint(AgenticEndpoint):
    """Connects to a remote NLIP-compliant server and streams responses as StreamChunks."""

    DEFAULT_CONCURRENCY_LIMIT = 3
    DEFAULT_QUEUE_CAPACITY = 10

    def __init__(self, config: EndpointConfig | None = None, **kwargs):
        super().__init__(config=config, **kwargs)
        self._url: str = kwargs.get("url", "")
        self._timeout: float = kwargs.get("timeout", 60.0)
        self._max_retries: int = kwargs.get("max_retries", 3)
        self._agent_name: str = kwargs.get("agent_name", "remote")

    async def _call(self, payload, headers, **kwargs):
        raise NotImplementedError(
            "AG2 NLIP endpoint is stream-only. Use stream() to iterate events."
        )

    def create_payload(self, request: dict | BaseModel, **kwargs):

        req_dict = {**self.config.kwargs, **to_dict(request), **kwargs}
        messages = req_dict.pop("messages", [])
        prompt = req_dict.pop("prompt", "")
        return {"request": AG2NlipRequest(messages=messages, prompt=prompt)}, {}

    async def stream(self, request: dict | BaseModel, **kwargs) -> AsyncIterator[StreamChunk]:

        if isinstance(request, dict) and "request" in request:
            request_obj = request["request"]
        else:
            payload, _ = self.create_payload(request, **kwargs)
            request_obj = payload["request"]

        prompt = request_obj.prompt or (
            request_obj.messages[-1]["content"] if request_obj.messages else ""
        )
        if not prompt:
            raise ValueError("AG2NlipEndpoint requires a non-empty prompt or at least one message.")

        url = kwargs.get("url", self._url)
        if not url:
            raise ValueError("AG2NlipEndpoint requires a url")

        timeout = kwargs.get("timeout", self._timeout)
        max_retries = kwargs.get("max_retries", self._max_retries)
        agent_name = kwargs.get("agent_name", self._agent_name)

        yield StreamChunk(
            type="system",
            metadata={
                "provider": "ag2",
                "api": "nlip",
                "url": url,
                "agent": agent_name,
            },
        )

        messages = request_obj.messages or [{"role": "user", "content": prompt}]

        try:
            result = await call_nlip_remote(
                url=url,
                messages=messages,
                agent_name=agent_name,
                timeout=timeout,
                max_retries=max_retries,
            )

            if result.get("content"):
                yield StreamChunk(
                    type="text",
                    content=result["content"],
                    metadata={
                        "agent": agent_name,
                        "url": url,
                        "context": result.get("context"),
                    },
                )

            if result.get("input_required"):
                yield StreamChunk(
                    type="system",
                    content=f"Input required: {result['input_required']}",
                    metadata={"event": "input_required", "agent": agent_name},
                )

        except Exception:
            logger.exception("AG2 NLIP remote call failed")
            raise

        yield StreamChunk(
            type="result",
            content=result.get("content", ""),
            metadata={"agent": agent_name, "url": url},
        )
