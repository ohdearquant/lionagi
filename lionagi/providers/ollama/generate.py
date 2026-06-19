# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Ollama /api/generate request model (raw completion, non-chat)."""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from lionagi.service.connections.endpoint import Endpoint
from lionagi.service.connections.endpoint_config import EndpointConfig
from lionagi.utils import is_import_installed

from ._config import OllamaConfigs, _setup_ollama_endpoint


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


__all__ = ("OllamaGenerateRequest", "OllamaGenerateEndpoint")


logger = logging.getLogger(__name__)

_HAS_OLLAMA = is_import_installed("ollama")


@OllamaConfigs.GENERATE.register
class OllamaGenerateEndpoint(Endpoint):
    """Ollama /api/generate endpoint; supports context for multi-turn and base models without chat templates."""

    def __init__(self, config: EndpointConfig = None, **kwargs):
        _setup_ollama_endpoint(_HAS_OLLAMA, kwargs)
        super().__init__(config=config, **kwargs)

    def create_payload(
        self,
        request: dict | BaseModel,
        extra_headers: dict | None = None,
        **kwargs,
    ):
        payload, headers = super().create_payload(request, extra_headers, **kwargs)
        # Ollama generate does not support OpenAI-specific params
        for unsupported in ("reasoning_effort", "stream_options"):
            payload.pop(unsupported, None)
        return payload, headers
