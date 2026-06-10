# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""
Ollama endpoint configuration.

Ollama provides local model hosting with both native and OpenAI-compatible APIs.
This module configures the OpenAI-compatible endpoint for consistency.
"""

import logging

from pydantic import BaseModel

from lionagi.ln.concurrency import run_sync
from lionagi.service.connections.endpoint import Endpoint
from lionagi.utils import is_import_installed

from .._config import OllamaConfigs

logger = logging.getLogger(__name__)

__all__ = ("OllamaChatEndpoint",)

_HAS_OLLAMA = is_import_installed("ollama")


@OllamaConfigs.CHAT.register
class OllamaChatEndpoint(Endpoint):
    """
    Documentation: https://platform.openai.com/docs/api-reference/chat/create
    """

    def __init__(self, config=None, **kwargs):
        if not _HAS_OLLAMA:
            raise ModuleNotFoundError(
                "ollama is not installed, please install it with `pip install lionagi[ollama]`"
            )

        # Ollama does not need an API key
        kwargs.pop("api_key", None)
        # Ollama runs on the local machine; allow loopback addresses in the SSRF
        # guard while keeping all other blocked ranges (IMDS etc.) enforced.
        kwargs.setdefault("allow_local_network", True)
        super().__init__(config, **kwargs)

        from ollama import list as ollama_list  # type: ignore[import]
        from ollama import pull as ollama_pull  # type: ignore[import]

        self._pull = ollama_pull
        self._list = ollama_list

    def create_payload(
        self,
        request: dict | BaseModel,
        extra_headers: dict | None = None,
        **kwargs,
    ):
        """Override to handle Ollama-specific needs."""
        payload, headers = super().create_payload(request, extra_headers, **kwargs)

        # Ollama doesn't support reasoning_effort
        payload.pop("reasoning_effort", None)

        return (payload, headers)

    async def call(self, request: dict | BaseModel, cache_control: bool = False, **kwargs):
        payload, headers = self.create_payload(request, **kwargs)

        # Check if model exists and pull if needed (off the event loop to avoid
        # blocking: both _list() and _pull() are synchronous Ollama SDK calls).
        model = payload.get("model")
        if model:
            await run_sync(self._check_model, model)

        # Pass the already-created payload directly to avoid double create_payload
        return await super().call(
            payload,
            cache_control=cache_control,
            skip_payload_creation=True,
            **kwargs,
        )

    def _pull_model(self, model: str):
        from tqdm import tqdm

        current_digest, bars = "", {}
        for progress in self._pull(model, stream=True):
            digest = progress.get("digest", "")
            if digest != current_digest and current_digest in bars:
                bars[current_digest].close()

            if not digest:
                logger.info("%s", progress.get("status"))
                continue

            if digest not in bars and (total := progress.get("total")):
                bars[digest] = tqdm(
                    total=total,
                    desc=f"pulling {digest[7:19]}",
                    unit="B",
                    unit_scale=True,
                )

            if completed := progress.get("completed"):
                bars[digest].update(completed - bars[digest].n)

            current_digest = digest

    def _check_model(self, model: str):
        try:
            available_models = [i.model for i in self._list().models]

            if model not in available_models:
                logger.info(
                    "Model '%s' not found locally. Pulling from Ollama registry...",
                    model,
                )
                self._pull_model(model)
                logger.info("Model '%s' successfully pulled.", model)
        except Exception as e:
            logger.warning("Could not check/pull model '%s': %s", model, e)
