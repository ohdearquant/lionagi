# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from lionagi.ln._lazy_init import lazy_import

_LAZY_MAP: dict[str, tuple[str, str | None]] = {
    "GroqAudioTranscriptionEndpoint": ("audio_transcription", None),
    "GroqChatEndpoint": ("chat", None),
}


def __getattr__(name: str):
    return lazy_import(name, _LAZY_MAP, __name__, globals())


def __dir__():
    return sorted(_LAZY_MAP)


__all__ = tuple(sorted(_LAZY_MAP))
