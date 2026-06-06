# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from lionagi.ln._ssrf import is_ssrf_safe

logger = logging.getLogger(__name__)

__all__ = [
    "AG2NlipRequest",
    "_assert_nlip_url_safe",
    "call_nlip_remote",
]


class AG2NlipRequest(BaseModel):
    """Request for AG2 NLIP remote endpoint."""

    messages: list[dict[str, Any]] = Field(default_factory=list)
    prompt: str = ""


def _assert_nlip_url_safe(url: str) -> None:
    """Validate *url* for scheme and SSRF safety before any NLIP connection.

    Shared by :func:`call_nlip_remote` and :func:`build_group_chat` so that
    every code path that hands a caller-supplied URL to a remote NLIP agent
    goes through the same guard.

    Raises:
        PermissionError: If the hostname resolves to a private or reserved IP
            address (SSRF guard).
        ValueError: If the URL scheme is not http or https.
    """
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


async def call_nlip_remote(
    url: str,
    messages: list[dict[str, Any]],
    agent_name: str = "remote",
    timeout: float = 60.0,
    max_retries: int = 3,
) -> dict[str, Any]:
    """Call a remote NLIP endpoint using AG2's NlipRemoteAgent.

    Falls back to direct httpx if nlip_sdk is not installed.

    Raises:
        PermissionError: If the hostname resolves to a private or reserved IP
            address (SSRF guard).
        ValueError: If the URL scheme is not http/https.
    """
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
    import httpx
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

    async with httpx.AsyncClient(timeout=timeout) as client:
        for attempt in range(max_retries):
            try:
                response = await client.post(
                    f"{url.rstrip('/')}/nlip/",
                    json=nlip_msg.model_dump(exclude_none=True),
                )
                response.raise_for_status()

                data = response.json()
                nlip_response = NLIP_Message.model_validate(data)
                content = nlip_response.content if isinstance(nlip_response.content, str) else ""

                return {
                    "content": content,
                    "context": None,
                    "input_required": None,
                }

            except httpx.TimeoutException:
                if attempt == max_retries - 1:
                    raise
                logger.warning("NLIP timeout (attempt %d/%d)", attempt + 1, max_retries)
            except httpx.ConnectError:
                if attempt == max_retries - 1:
                    raise
                logger.warning("NLIP connect failed (attempt %d/%d)", attempt + 1, max_retries)

    return {"content": "", "context": None, "input_required": None}


async def _call_direct(
    url: str,
    messages: list[dict[str, Any]],
    timeout: float,
    max_retries: int,
) -> dict[str, Any]:
    """Direct HTTP fallback when nlip_sdk is not installed.

    Sends the last message as plain text to the NLIP endpoint.
    """
    import httpx

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
        for attempt in range(max_retries):
            try:
                response = await client.post(
                    f"{url.rstrip('/')}/nlip/",
                    json=payload,
                )
                response.raise_for_status()
                data = response.json()

                content = ""
                if isinstance(data, dict):
                    content = data.get("content", "")
                    if isinstance(content, dict):
                        content = content.get("content", str(content))

                return {"content": content, "context": None, "input_required": None}

            except httpx.TimeoutException:
                if attempt == max_retries - 1:
                    raise
            except httpx.ConnectError:
                if attempt == max_retries - 1:
                    raise

    return {"content": "", "context": None, "input_required": None}
