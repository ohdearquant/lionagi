# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Ollama /api/generate request model (raw completion, non-chat)."""

from pydantic import BaseModel, Field


class OllamaGenerateRequest(BaseModel):
    """Request body for Ollama /api/generate (raw completion, non-chat)."""

    model: str = Field(
        ...,
        description="Name of the Ollama model, e.g. 'llama3.2'.",
    )
    prompt: str = Field(
        ...,
        description="The prompt to generate a response for.",
    )
    suffix: str | None = Field(
        default=None,
        description="Text after the model response (fill-in-the-middle).",
    )
    images: list[str] | None = Field(
        default=None,
        description="List of base64-encoded images (for multimodal models).",
    )
    format: str | dict | None = Field(
        default=None,
        description="Output format: 'json' or a JSON Schema dict.",
    )
    options: dict | None = Field(
        default=None,
        description="Additional model parameters (temperature, seed, num_ctx, etc.).",
    )
    system: str | None = Field(
        default=None,
        description="System prompt to override the model's default.",
    )
    template: str | None = Field(
        default=None,
        description="Override the model template.",
    )
    context: list[int] | None = Field(
        default=None,
        description="Encoding context from a prior response for multi-turn.",
    )
    stream: bool | None = Field(
        default=False,
        description="If True, stream partial responses.",
    )
    raw: bool | None = Field(
        default=None,
        description="If True, no formatting is applied to the prompt.",
    )
    keep_alive: str | None = Field(
        default=None,
        description="How long to keep model in memory after the request, e.g. '5m'.",
    )


__all__ = ("OllamaGenerateRequest",)
