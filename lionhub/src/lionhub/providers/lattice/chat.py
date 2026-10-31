# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Lattice serves the OpenAI chat completions format, with its own sampling and adapter fields."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from lionagi.service.connections._chat_schemas import OpenAIChatCompletionsRequest
from lionagi.service.connections.endpoint import Endpoint
from lionagi.service.types.stream_chunk import StreamChunk

from ._config import LatticeConfigs, LatticeRedirectsRefused, _setup_lattice_endpoint

__all__ = ("LatticeChatCompletionsRequest", "LatticeChatEndpoint", "LatticeLoraSelection")


class LatticeLoraSelection(BaseModel):
    """One resident adapter in a request's mixture."""

    model_config = ConfigDict(extra="forbid")

    id: int = Field(
        ..., ge=0, description="Resident adapter identifier, as `GET /v1/lora` lists it."
    )
    scale: float = Field(
        ...,
        allow_inf_nan=False,
        description="Multiplier on the adapter's alpha/rank scale.",
    )


class LatticeChatCompletionsRequest(OpenAIChatCompletionsRequest):
    """Request body for Lattice `POST /v1/chat/completions`.

    The OpenAI fields are forwarded, not validated here: a request that sets `tools`,
    `tool_choice`, `n` above 1, a `tool` or `developer` role, or one that does not end on a
    user message is refused by the server. `model` must be the id the server serves. The
    server ignores `frequency_penalty`, `presence_penalty`, `logit_bias` and `user`
    entirely, and it accepts `top_k` and `repetition_penalty` without applying them.
    """

    top_p: float | None = Field(
        default=None,
        gt=0.0,
        le=1.0,
        description="Nucleus sampling; the server takes a value in (0.0, 1.0].",
    )
    seed: int | None = Field(
        default=None,
        ge=0,
        le=(1 << 64) - 1,
        description="Sampling seed; the server reads it as an unsigned 64-bit integer.",
    )
    reasoning_budget: int | None = Field(
        default=None,
        ge=0,
        description="Upper bound, in tokens, on the reasoning block before the answer; 0 means none.",
    )
    top_k: int | None = Field(
        default=None,
        ge=1,
        description="Top-k sampling. Applied by the `lattice_serve` daemon; ignored by `lattice serve`.",
    )
    repetition_penalty: float | None = Field(
        default=None,
        description="Repetition penalty. Applied by the `lattice_serve` daemon; ignored by `lattice serve`.",
    )
    lora: list[LatticeLoraSelection] | None = Field(
        default=None,
        description=(
            "Resident adapters to apply, in order. Omitted selects the base model on today's "
            "servers and lets a server that enables routing choose; an empty list pins the "
            "base model."
        ),
    )


@LatticeConfigs.CHAT.register
class LatticeChatEndpoint(LatticeRedirectsRefused, Endpoint):
    """Chat completions from a running `lattice serve`; no API key, and no redirect is
    followed.

    A reply whose `content` opens with the server's inline `<think>` reasoning block has
    that block moved to `reasoning_content`, so a structured-output parse sees the answer
    alone.
    """

    def __init__(self, config=None, **kwargs):
        config = _setup_lattice_endpoint(config, kwargs, LatticeChatCompletionsRequest)
        super().__init__(config, **kwargs)

    async def _call(self, payload: dict, headers: dict, **kwargs):
        return _split_reasoning(await super()._call(payload, headers, **kwargs))

    def _event_to_stream_chunk(self, event: dict) -> StreamChunk | None:
        """Map the server's mid-stream failure envelope to an error chunk.

        A generation that fails after the stream has started ends with ``{"error": {...}}``
        and no ``choices`` of its own, then ``[DONE]``. Read as an ordinary event it would
        look like a normal end, so it is turned into an error chunk instead. Like any error
        chunk, it is marked ``call_failed``: ``iModel.stream`` yields it, then raises once the
        stream ends.
        """
        if isinstance(event.get("error"), dict) and "choices" not in event:
            return StreamChunk(
                type="error",
                content=str(event["error"]),
                is_error=True,
                metadata={"raw": event},
            )
        return super()._event_to_stream_chunk(event)


_THINK_OPEN = "<think>"
_THINK_CLOSE = "</think>"


def _split_reasoning(response):
    """Move each reply's leading `<think>...</think>` block out of `content`.

    `lattice serve` returns the model's reasoning inline, as a prefix of `content`, and an
    empty block is normal for a simple prompt. Left there, it sits in front of a JSON
    answer and structured output cannot parse the reply, so the block's text moves to
    `reasoning_content`, the field OpenAI-compatible servers use for it. A block with no
    closing tag -- a reply cut off while it was still reasoning -- is left in `content`,
    as is a reply that does not open with a block at all.
    """
    if not isinstance(response, dict):
        return response
    for choice in response.get("choices") or ():
        message = choice.get("message") if isinstance(choice, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str):
            continue
        text = content.lstrip()
        end = text.find(_THINK_CLOSE)
        if not text.startswith(_THINK_OPEN) or end == -1:
            continue
        message["reasoning_content"] = text[len(_THINK_OPEN) : end].strip()
        message["content"] = text[end + len(_THINK_CLOSE) :].lstrip()
    return response
