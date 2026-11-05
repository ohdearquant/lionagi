# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Ollama /api/embeddings request model."""

from __future__ import annotations

from pydantic import BaseModel, Field

from lionagi.service.connections.endpoint import Endpoint
from lionagi.service.connections.endpoint_config import EndpointConfig
from lionagi.utils import is_import_installed

from ._config import OllamaConfigs, _setup_ollama_endpoint


class OllamaEmbedRequest(BaseModel):
    """Request body for Ollama /api/embeddings."""

    model: str = Field(
        ...,
        description="Name of the model to use for embeddings, e.g. 'nomic-embed-text'.",
    )
    prompt: str | None = Field(
        default=None,
        description="Text to generate embeddings for.",
    )
    input: str | list[str] | None = Field(
        default=None,
        description="Alias for prompt; also accepts a list of strings (batch embeddings).",
    )
    options: dict | None = Field(
        default=None,
        description="Additional model parameters (temperature, seed, etc.).",
    )
    keep_alive: str | None = Field(
        default=None,
        description="How long to keep model loaded in memory, e.g. '5m'.",
    )


__all__ = ("OllamaEmbedRequest", "OllamaEmbedEndpoint")


_HAS_OLLAMA = is_import_installed("ollama")


@OllamaConfigs.EMBED.register
class OllamaEmbedEndpoint(Endpoint):
    """Ollama native /api/embeddings endpoint; supports keep_alive and options."""

    def __init__(self, config: EndpointConfig = None, **kwargs):
        _setup_ollama_endpoint(_HAS_OLLAMA, kwargs)
        super().__init__(config=config, **kwargs)
