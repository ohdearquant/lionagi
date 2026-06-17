# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Ollama embeddings endpoint (POST /api/embeddings)."""

from __future__ import annotations

from lionagi.service.connections.endpoint import Endpoint
from lionagi.service.connections.endpoint_config import EndpointConfig
from lionagi.utils import is_import_installed

from .._config import OllamaConfigs, _setup_ollama_endpoint

__all__ = ("OllamaEmbedEndpoint",)

_HAS_OLLAMA = is_import_installed("ollama")


@OllamaConfigs.EMBED.register
class OllamaEmbedEndpoint(Endpoint):
    """Ollama native /api/embeddings endpoint; supports keep_alive and options."""

    def __init__(self, config: EndpointConfig = None, **kwargs):
        _setup_ollama_endpoint(_HAS_OLLAMA, kwargs)
        super().__init__(config=config, **kwargs)
