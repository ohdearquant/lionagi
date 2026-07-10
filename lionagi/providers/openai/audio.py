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


def _replayable_file_factory(file_data, field_name: str, *, require_replayable: bool = True):
    """Return a zero-arg callable producing a fresh file object for one attempt.

    Bytes/bytearray are snapshotted once and re-wrapped in a new BytesIO per
    attempt. A seekable stream is seeked back to its starting position before
    each attempt. A non-seekable stream cannot be replayed safely: when a
    retry can occur (*require_replayable*), it fails here, before any network
    I/O, instead of silently resending an exhausted stream; on a single-shot
    endpoint it is handed to aiohttp once, as before.
    """
    if file_data is None:
        return lambda: None
    if isinstance(file_data, (bytes, bytearray)):
        snapshot = bytes(file_data)
        return lambda: io.BytesIO(snapshot)

    seekable = getattr(file_data, "seekable", None)
    if not callable(seekable) or not seekable():
        if require_replayable:
            raise TypeError(
                f"{field_name} must be bytes, bytearray, or a seekable stream to "
                "support retries; pass bytes, or configure the endpoint with "
                "max_retries=1 for a non-seekable stream."
            )
        return lambda: file_data
    start_pos = file_data.tell()

    def _factory():
        file_data.seek(start_pos)
        return file_data

    return _factory


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

        return await self._call_aiohttp(
            payload=payload,
            headers=multipart_headers,
            request_body_factory=_build_form,
            response_mode="json",
            error_context="Transcription request",
        )
