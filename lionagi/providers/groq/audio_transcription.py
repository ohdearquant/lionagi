# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

import io

from lionagi.service.connections.endpoint import Endpoint

from ._config import GroqConfigs

__all__ = ("GroqAudioTranscriptionEndpoint",)


def _replayable_file_factory(file_data, field_name: str, *, require_replayable: bool = True):
    """Return a zero-arg callable producing a fresh file object for one retry attempt.
    See docs/internals/runtime.md for the replay-safety invariant."""
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
    # Snapshot once, restore position — a live stream handed to each attempt would
    # already be at EOF on retry (RetryConfig re-invokes _call), uploading empty.
    start_pos = file_data.tell()
    snapshot = file_data.read()
    file_data.seek(start_pos)
    return lambda: io.BytesIO(snapshot)


@GroqConfigs.AUDIO_TRANSCRIPTION.register
class GroqAudioTranscriptionEndpoint(Endpoint):
    """Groq Whisper transcription endpoint; sends audio as multipart/form-data."""

    transport_arg_keys = ("file", "filename")

    def __init__(self, config=None, **kwargs):
        if config is None:
            kwargs.setdefault("timeout", 120)
            kwargs.setdefault("max_retries", 3)
        super().__init__(config, **kwargs)

    async def _call(self, payload: dict, headers: dict, **kwargs):
        import aiohttp

        file_data = kwargs.pop("file", None)
        filename: str = kwargs.pop("filename", "audio.mp3")
        file_factory = _replayable_file_factory(
            file_data, "file", require_replayable=self._can_retry()
        )

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
            error_context="Groq transcription",
            **transport_kwargs,
        )
