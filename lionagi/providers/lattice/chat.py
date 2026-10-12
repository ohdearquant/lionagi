# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Lattice serves the OpenAI chat completions format, with its own sampling and adapter fields."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from lionagi.providers.openai._chat_schemas import OpenAIChatCompletionsRequest
from lionagi.service.connections.endpoint import Endpoint

from ._config import LatticeConfigs, _setup_lattice_endpoint

__all__ = ("LatticeChatCompletionsRequest", "LatticeChatEndpoint", "LatticeLoraSelection")


class LatticeLoraSelection(BaseModel):
    """One resident adapter in a request's mixture."""

    model_config = ConfigDict(extra="forbid")

    id: int = Field(
        ..., ge=0, description="Resident adapter identifier, as `GET /v1/lora` lists it."
    )
    scale: float = Field(..., description="Multiplier on the adapter's alpha/rank scale.")


class LatticeChatCompletionsRequest(OpenAIChatCompletionsRequest):
    """Request body for Lattice `POST /v1/chat/completions`.

    The server refuses `tools`, `tool_choice`, `n` above 1, the `tool` and `developer` roles,
    and a conversation that does not end on a user message; `model` must be the id it serves.
    """

    reasoning_budget: int | None = Field(
        default=None,
        ge=0,
        description="Upper bound, in tokens, on the reasoning block before the answer; 0 means none.",
    )
    top_k: int | None = Field(default=None, ge=1, description="Top-k sampling, where supported.")
    repetition_penalty: float | None = Field(
        default=None, description="Repetition penalty, where supported."
    )
    lora: list[LatticeLoraSelection] | None = Field(
        default=None,
        description=(
            "Resident adapters to apply, in order. Omitted lets the server choose; an empty "
            "list applies none."
        ),
    )


@LatticeConfigs.CHAT.register
class LatticeChatEndpoint(Endpoint):
    """Chat completions from a running `lattice serve`; no API key."""

    def __init__(self, config=None, **kwargs):
        _setup_lattice_endpoint(kwargs)
        super().__init__(config, **kwargs)
