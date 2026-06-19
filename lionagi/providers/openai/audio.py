# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""OpenAI Audio endpoints: TTS (/v1/audio/speech) and STT/Whisper (/v1/audio/transcriptions)."""

from __future__ import annotations

import io

from pydantic import BaseModel

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
        """Override to return raw bytes instead of parsed JSON."""
        self._assert_ssrf_safe_url()

        import aiohttp

        async with self._create_http_session() as session:
            async with session.request(
                method=self.config.method,
                url=self.config.full_url,
                headers=headers,
                json=payload,
                **kwargs,
            ) as response:
                if response.status != 200:
                    error_body = await response.text()
                    raise aiohttp.ClientResponseError(
                        request_info=response.request_info,
                        history=response.history,
                        status=response.status,
                        message=f"TTS request failed ({response.status}): {error_body}",
                        headers=response.headers,
                    )
                return await response.read()


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
        self._assert_ssrf_safe_url()

        import aiohttp

        file_data: bytes | None = kwargs.pop("file", None)
        filename: str = kwargs.pop("filename", "audio.mp3")

        # Build multipart form
        form = aiohttp.FormData()
        for key, value in payload.items():
            if value is not None:
                form.add_field(key, str(value))

        if file_data is not None:
            if isinstance(file_data, (bytes, bytearray)):
                file_obj = io.BytesIO(file_data)
            else:
                file_obj = file_data
            form.add_field(
                "file",
                file_obj,
                filename=filename,
                content_type="application/octet-stream",
            )

        # Remove Content-Type from headers — aiohttp sets it automatically with boundary
        multipart_headers = {k: v for k, v in headers.items() if k.lower() != "content-type"}

        async with self._create_http_session() as session:
            async with session.post(
                url=self.config.full_url,
                headers=multipart_headers,
                data=form,
            ) as response:
                if response.status != 200:
                    error_body = await response.text()
                    raise aiohttp.ClientResponseError(
                        request_info=response.request_info,
                        history=response.history,
                        status=response.status,
                        message=f"Transcription request failed ({response.status}): {error_body}",
                        headers=response.headers,
                    )
                return await response.json()
