# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from enum import Enum

from lionagi.service.connections.provider_config import LazyType, ProviderConfig
from lionagi.service.connections.registry import EndpointType


class OllamaConfigs(ProviderConfig, Enum):
    CHAT = (
        "chat/completions",
        ["chat"],
        EndpointType.API,
        LazyType("lionagi.providers.openai._chat_schemas:OpenAIChatCompletionsRequest"),
        "http://localhost:11434/v1",
        "none",
    )
    EMBED = (
        "embeddings",
        ["embed"],
        EndpointType.API,
        LazyType("lionagi.providers.ollama.embed:OllamaEmbedRequest"),
        "http://localhost:11434/api",
        "none",
    )
    GENERATE = (
        "generate",
        ["generate", "completion"],
        EndpointType.API,
        LazyType("lionagi.providers.ollama.generate:OllamaGenerateRequest"),
        "http://localhost:11434/api",
        "none",
    )


OllamaConfigs._PROVIDER = "ollama"
OllamaConfigs._PROVIDER_ALIASES = []


def _setup_ollama_endpoint(has_ollama: bool, kwargs: dict) -> None:
    """Shared init guard for all Ollama endpoints: require the ollama package,
    drop any api_key, and allow loopback addresses in the SSRF guard."""
    if not has_ollama:
        raise ModuleNotFoundError(
            "ollama is not installed, please install it with `pip install lionagi[ollama]`"
        )
    kwargs.pop("api_key", None)
    kwargs.setdefault("allow_local_network", True)


__all__ = ("OllamaConfigs", "_setup_ollama_endpoint")
