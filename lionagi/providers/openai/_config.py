# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from enum import Enum

from lionagi.service.connections.provider_config import LazyType, ProviderConfig
from lionagi.service.connections.registry import EndpointType


class OpenAIConfigs(ProviderConfig, Enum):
    CHAT = (
        "chat/completions",
        ["chat"],
        EndpointType.API,
        LazyType("lionagi.providers.openai.chat.models:OpenAIChatCompletionsRequest"),
        "https://api.openai.com/v1",
        "bearer",
    )
    RESPONSE = (
        "responses",
        ["response"],
        EndpointType.API,
        LazyType("lionagi.providers.openai.response.models:OpenAIResponsesRequest"),
        "https://api.openai.com/v1",
        "bearer",
    )
    EMBED = (
        "embeddings",
        ["embed"],
        EndpointType.API,
        LazyType("lionagi.providers.openai.embed.models:OpenAIEmbeddingRequest"),
        "https://api.openai.com/v1",
        "bearer",
    )
    AUDIO_SPEECH = (
        "audio/speech",
        ["tts"],
        EndpointType.API,
        LazyType("lionagi.providers.openai.audio.models:AudioSpeechRequest"),
        "https://api.openai.com/v1",
        "bearer",
    )
    AUDIO_TRANSCRIPTION = (
        "audio/transcriptions",
        ["stt", "whisper"],
        EndpointType.API,
        LazyType("lionagi.providers.openai.audio.models:AudioTranscriptionRequest"),
        "https://api.openai.com/v1",
        "bearer",
    )
    IMAGE_GENERATION = (
        "images/generations",
        ["dalle", "image"],
        EndpointType.API,
        LazyType("lionagi.providers.openai.images.models:ImageGenerationRequest"),
        "https://api.openai.com/v1",
        "bearer",
    )
    IMAGE_EDIT = (
        "images/edits",
        ["image_edit"],
        EndpointType.API,
        LazyType("lionagi.providers.openai.images.models:ImageEditRequest"),
        "https://api.openai.com/v1",
        "bearer",
    )


OpenAIConfigs._PROVIDER = "openai"
OpenAIConfigs._PROVIDER_ALIASES = []


class CodexConfigs(ProviderConfig, Enum):
    CLI = (
        "query_cli",
        ["cli", "code"],
        EndpointType.AGENTIC,
        LazyType("lionagi.providers.openai.codex.models:CodexCodeRequest"),
    )


CodexConfigs._PROVIDER = "codex"
CodexConfigs._PROVIDER_ALIASES = ["openai-codex"]

CONTEXT_WINDOWS: dict[str, int] = {
    "gpt-5.5": 1_000_000,
    "gpt-5.4-mini": 1_000_000,
    "gpt-5.4": 1_048_576,
    "gpt-5": 1_000_000,
    "gpt-4.1-mini": 1_000_000,
    "gpt-4.1-nano": 1_000_000,
    "gpt-4.1": 1_000_000,
    "gpt-4o-mini": 128_000,
    "gpt-4o": 128_000,
    "gpt-4-turbo": 128_000,
    "gpt-4": 128_000,
    "o4-mini": 200_000,
    "o3-mini": 200_000,
    "o3": 200_000,
    "o1-pro": 200_000,
    "o1-mini": 128_000,
    "o1": 200_000,
}
