# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""OpenAI Audio endpoints: TTS (/v1/audio/speech) and STT/Whisper (/v1/audio/transcriptions)."""

from __future__ import annotations

from pydantic import BaseModel

from lionagi.providers._multipart import _replayable_file_factory
from lionagi.service.connections.endpoint import Endpoint
from lionagi.service.connections.endpoint_config import EndpointConfig

from ._audio_schemas import AudioSpeechRequest, AudioTranscriptionRequest
from ._config import OpenAIConfigs

__all__ = (
    "AudioSpeechRequest",
    "AudioTranscriptionRequest",
    "OpenaiAudioSpeechEndpoint",
    "OpenaiAudioTranscriptionEndpoint",
)


@OpenAIConfigs.AUDIO_SPEECH.register
class OpenaiAudioSpeechEndpoint(Endpoint):
    """TTS endpoint; returns raw audio bytes."""

    def __init__(self, config: EndpointConfig = None, **kwargs):
        if config is None:
            kwargs.setdefault("timeout", 120)
            kwargs.setdefault("max_retries", 3)
        super().__init__(config=config, **kwargs)

    def create_payload(
        self,
        request: dict | BaseModel,
        extra_headers: dict | None = None,
        **kwargs,
    ):
        payload, headers = super().create_payload(request, extra_headers, **kwargs)
        return payload, headers

    async def _call(self, payload: dict, headers: dict, **kwargs):
        """Delegate to the base transport; the response is raw audio bytes."""
        return await self._call_aiohttp(
            payload=payload,
            headers=headers,
            response_mode="bytes",
            error_context="TTS request",
            **kwargs,
        )


@OpenAIConfigs.AUDIO_TRANSCRIPTION.register
class OpenaiAudioTranscriptionEndpoint(Endpoint):
    """STT/Whisper endpoint; pass file bytes via kwargs, encodes as multipart/form-data."""

    transport_arg_keys = ("file", "filename")

    def __init__(self, config: EndpointConfig = None, **kwargs):
        if config is None:
            kwargs.setdefault("timeout", 120)
            kwargs.setdefault("max_retries", 3)
        super().__init__(config=config, **kwargs)

    def create_payload(
        self,
        request: dict | BaseModel,
        extra_headers: dict | None = None,
        **kwargs,
    ):
        # We only validate the model-level fields; file is handled separately.
        payload, headers = super().create_payload(request, extra_headers, **kwargs)
        return payload, headers

    async def _call(self, payload: dict, headers: dict, **kwargs):
        """Encode audio as multipart/form-data and POST to the transcription endpoint."""
        import aiohttp

        file_data = kwargs.pop("file", None)
        filename: str = kwargs.pop("filename", "audio.mp3")
        file_factory = _replayable_file_factory(
            file_data, "file", require_replayable=self._can_retry()
        )

        # Remove Content-Type from headers — aiohttp sets it automatically with boundary
        multipart_headers = {k: v for k, v in headers.items() if k.lower() != "content-type"}

        def _build_form():
            form = aiohttp.FormData()
            for key, value in payload.items():
                if value is not None:
                    form.add_field(key, str(value))

            file_obj = file_factory()
            if file_obj is not None:
                form.add_field(
                    "file",
                    file_obj,
                    filename=filename,
                    content_type="application/octet-stream",
                )
            return {"data": form}

        # API fields stay in the multipart body; only transport kwargs
        # (proxy, ssl, timeout, ...) are forwarded to the HTTP layer.
        transport_kwargs = {k: v for k, v in kwargs.items() if k not in payload}
        return await self._call_aiohttp(
            payload=payload,
            headers=multipart_headers,
            request_body_factory=_build_form,
            response_mode="json",
            error_context="Transcription request",
            **transport_kwargs,
        )
