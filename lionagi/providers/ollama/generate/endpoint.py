# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Ollama raw generate endpoint (non-chat completion).

Endpoint: POST http://localhost:11434/api/generate
Docs: https://github.com/ollama/ollama/blob/main/docs/api.md#generate-a-completion
"""

from __future__ import annotations

import logging

from pydantic import BaseModel

from lionagi.service.connections.endpoint import Endpoint
from lionagi.service.connections.endpoint_config import EndpointConfig
from lionagi.utils import is_import_installed

from .._config import OllamaConfigs

__all__ = ("OllamaGenerateEndpoint",)

logger = logging.getLogger(__name__)

_HAS_OLLAMA = is_import_installed("ollama")


@OllamaConfigs.GENERATE.register
class OllamaGenerateEndpoint(Endpoint):
    """Ollama raw generate endpoint (non-chat).

    Uses the /api/generate endpoint which returns a single completion.
    Unlike the chat endpoint this supports ``context`` for multi-turn and
    works with base models that have no chat template.

    Usage::

        endpoint = OllamaGenerateEndpoint()
        result = await endpoint.call({"model": "llama3.2", "prompt": "Why is the sky blue?"})
        # result["response"]
    """

    def __init__(self, config: EndpointConfig = None, **kwargs):
        if not _HAS_OLLAMA:
            raise ModuleNotFoundError(
                "ollama is not installed, please install it with `pip install lionagi[ollama]`"
            )
        # Ollama does not need an API key
        kwargs.pop("api_key", None)
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
