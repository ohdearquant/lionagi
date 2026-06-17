# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Ollama raw generate endpoint (POST /api/generate, non-chat)."""

from __future__ import annotations

import logging

from pydantic import BaseModel

from lionagi.service.connections.endpoint import Endpoint
from lionagi.service.connections.endpoint_config import EndpointConfig
from lionagi.utils import is_import_installed

from .._config import OllamaConfigs, _setup_ollama_endpoint

__all__ = ("OllamaGenerateEndpoint",)

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
