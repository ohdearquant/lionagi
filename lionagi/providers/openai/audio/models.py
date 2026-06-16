# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from typing import Literal

from pydantic import BaseModel, Field


class AudioSpeechRequest(BaseModel):
    """Request body for OpenAI Audio Speech TTS (POST /v1/audio/speech)."""

    model: str = Field(
        default="tts-1",
        description="TTS model: 'tts-1', 'tts-1-hd', or 'gpt-4o-mini-tts'.",
    )
    input: str = Field(
        ...,
        description="Text to convert to speech. Maximum 4096 characters.",
    )
    voice: Literal["alloy", "echo", "fable", "onyx", "nova", "shimmer"] = Field(
        default="alloy",
        description="Voice to use for synthesis.",
    )
    response_format: Literal["mp3", "opus", "aac", "flac", "wav", "pcm"] = Field(
        default="mp3",
        description="Audio output format.",
    )
    speed: float = Field(
        default=1.0,
        ge=0.25,
        le=4.0,
        description="Speed of the generated audio. Range: 0.25 to 4.0.",
    )


class AudioTranscriptionRequest(BaseModel):
    """Request body for OpenAI Audio Transcription (POST /v1/audio/transcriptions); file sent as multipart/form-data."""

    model: str = Field(
        default="whisper-1",
        description="Transcription model: 'whisper-1', 'gpt-4o-transcribe', 'gpt-4o-mini-transcribe'.",
    )
    language: str | None = Field(
        default=None,
        description="ISO-639-1 language code (e.g., 'en'). If omitted, auto-detected.",
    )
    prompt: str | None = Field(
        default=None,
        description="Optional text to guide the model's style or continue a prior segment.",
    )
    response_format: Literal["json", "text", "srt", "verbose_json", "vtt"] = Field(
        default="json",
        description="Output format for the transcription.",
    )
    temperature: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Sampling temperature (0.0–1.0). 0 uses greedy decoding.",
    )


__all__ = ("AudioSpeechRequest", "AudioTranscriptionRequest")
