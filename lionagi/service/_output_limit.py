# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Read whether a raw provider response stopped at its output token limit."""

from __future__ import annotations

from typing import Any

__all__ = ("read_output_limit_stop",)


def read_output_limit_stop(response: Any) -> tuple[bool, bool]:
    """Read a raw provider response for a stop at its output token limit.

    Returns ``(stopped, wrote)``: whether the response stopped at its output limit and, when
    it did, whether it had written any text or a tool call before it stopped. Reads the
    Anthropic Messages (``stop_reason`` ``max_tokens``), OpenAI chat completions
    (``finish_reason`` ``length``) and OpenAI Responses (``status`` ``incomplete`` with reason
    ``max_output_tokens``) shapes; any other shape, a list of responses included, reads
    ``(False, False)``.
    """
    if not isinstance(response, dict):
        return False, False
    if "stop_reason" in response:
        if response["stop_reason"] != "max_tokens":
            return False, False
        return True, any(
            isinstance(block, dict)
            and (
                block.get("type") == "tool_use"
                or (block.get("type") == "text" and str(block.get("text") or "").strip())
            )
            for block in response.get("content") or []
        )
    choices = response.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        if choices[0].get("finish_reason") != "length":
            return False, False
        message = choices[0].get("message") or {}
        return True, bool(str(message.get("content") or "").strip() or message.get("tool_calls"))
    if response.get("status") == "incomplete":
        if (response.get("incomplete_details") or {}).get("reason") != "max_output_tokens":
            return False, False
        for item in response.get("output") or []:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "function_call":
                return True, True
            if item.get("type") == "message" and any(
                isinstance(part, dict) and str(part.get("text") or "").strip()
                for part in item.get("content") or []
            ):
                return True, True
        return True, False
    return False, False
