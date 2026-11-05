# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from lionagi.ln._lazy_init import lazy_import

_LAZY_MAP: dict[str, tuple[str, str | None]] = {
    "AudioSpeechRequest": ("audio", None),
    "AudioTranscriptionRequest": ("audio", None),
    "OpenAIBatchRequest": ("batch", None),
    "CodexCLIEndpoint": ("codex", None),
    "CodexCodeRequest": ("codex", None),
    "ImageEditRequest": ("images", None),
    "ImageGenerationRequest": ("images", None),
    "OpenAIChatCompletionsRequest": ("chat", None),
    "OpenAIEmbeddingRequest": ("embed", None),
    "OpenAIResponsesRequest": ("response", None),
    "OpenaiAudioSpeechEndpoint": ("audio", None),
    "OpenaiAudioTranscriptionEndpoint": ("audio", None),
    "OpenaiBatchEndpoint": ("batch", None),
    "OpenaiChatEndpoint": ("chat", None),
    "OpenaiEmbedEndpoint": ("embed", None),
    "OpenaiImageEditEndpoint": ("images", None),
    "OpenaiImageGenerationEndpoint": ("images", None),
    "OpenaiResponseEndpoint": ("response", None),
    "stream_codex_cli": ("codex", None),
}


def __getattr__(name: str):
    return lazy_import(name, _LAZY_MAP, __name__, globals())


def __dir__():
    return sorted(_LAZY_MAP)


__all__ = tuple(sorted(_LAZY_MAP))
