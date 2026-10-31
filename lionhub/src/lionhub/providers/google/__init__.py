# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from lionagi.ln._lazy_init import lazy_import

_LAZY_MAP: dict[str, tuple[str, str | None]] = {
    "GeminiCLIEndpoint": ("gemini_code", None),
    "GeminiChatEndpoint": ("chat", None),
    "GeminiChunk": ("gemini_code", None),
    "GeminiCodeRequest": ("gemini_code", None),
    "GeminiSession": ("gemini_code", None),
    "OpenAIChatCompletionsRequest": ("chat", None),
    "stream_gemini_cli": ("gemini_code", None),
}


def __getattr__(name: str):
    return lazy_import(name, _LAZY_MAP, __name__, globals())


def __dir__():
    return sorted(_LAZY_MAP)


__all__ = tuple(sorted(_LAZY_MAP))
