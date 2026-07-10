# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Wire-level contract tests for the OpenAI media endpoints (TTS, transcription, image edit).

These tests drive the endpoints against an ephemeral local aiohttp server (local
network access explicitly enabled on the endpoint config) so the retry, multipart,
and response-decoding contract restored in Endpoint._call_aiohttp is exercised
end to end rather than through a mocked session.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from lionagi.providers.openai.audio import (
    OpenaiAudioSpeechEndpoint,
    OpenaiAudioTranscriptionEndpoint,
)
from lionagi.providers.openai.images import OpenaiImageEditEndpoint
from lionagi.service.connections.endpoint_config import EndpointConfig
from lionagi.service.resilience import RetryConfig

_NO_SLEEP = patch("lionagi.ln.concurrency.patterns.anyio.sleep", AsyncMock())


def _config(name: str, endpoint: str, base_url: str, **overrides) -> EndpointConfig:
    kwargs = dict(
        name=name,
        provider="openai",
        endpoint=endpoint,
        base_url=base_url,
        auth_type="bearer",
        api_key="test-key",
        allow_local_network=True,
        max_retries=3,
        timeout=5,
    )
    kwargs.update(overrides)
    return EndpointConfig(**kwargs)


@pytest.fixture
async def run_server():
    servers = []

    async def _run(handler) -> TestServer:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", handler)
        server = TestServer(app)
        await server.start_server()
        servers.append(server)
        return server

    yield _run

    for server in servers:
        await server.close()


async def _base_url(server: TestServer) -> str:
    return str(server.make_url("/")).rstrip("/")


async def _read_multipart(request: web.Request) -> dict:
    reader = await request.multipart()
    fields: dict[str, object] = {}
    while True:
        part = await reader.next()
        if part is None:
            break
        if part.filename:
            fields[part.name] = await part.read(decode=False)
        else:
            fields[part.name] = (await part.read()).decode()
    return fields


class TestImageEditMultipartRetry:
    @pytest.mark.asyncio
    async def test_429_then_200_sends_two_complete_bodies_with_identical_bytes(self, run_server):
        received = []

        async def handler(request: web.Request):
            fields = await _read_multipart(request)
            received.append(fields)
            if len(received) == 1:
                return web.json_response({"error": {"message": "rate limited"}}, status=429)
            return web.json_response({"data": [{"url": "https://example.com/out.png"}]})

        server = await run_server(handler)
        config = _config("openai_image_edit", "images/edits", await _base_url(server))
        endpoint = OpenaiImageEditEndpoint(config=config)

        image_bytes = b"\x89PNG-image-payload"
        mask_bytes = b"\x89PNG-mask-payload"

        with _NO_SLEEP:
            result = await endpoint._call(
                payload={"prompt": "add a rainbow", "model": "dall-e-2", "n": 1},
                headers={"Authorization": "Bearer test", "Content-Type": "application/json"},
                image=image_bytes,
                image_filename="image.png",
                mask=mask_bytes,
                mask_filename="mask.png",
            )

        assert len(received) == 2
        for fields in received:
            assert fields["image"] == image_bytes
            assert fields["mask"] == mask_bytes
            assert fields["prompt"] == "add a rainbow"
            assert fields["model"] == "dall-e-2"
        assert result == {"data": [{"url": "https://example.com/out.png"}]}

    @pytest.mark.asyncio
    async def test_non_429_4xx_sends_exactly_one_request(self, run_server):
        received = []

        async def handler(request: web.Request):
            fields = await _read_multipart(request)
            received.append(fields)
            return web.json_response({"error": {"message": "bad prompt"}}, status=400)

        server = await run_server(handler)
        config = _config("openai_image_edit", "images/edits", await _base_url(server))
        endpoint = OpenaiImageEditEndpoint(config=config)

        with _NO_SLEEP:
            with pytest.raises(Exception) as exc_info:
                await endpoint._call(
                    payload={"prompt": "bad", "model": "dall-e-2"},
                    headers={"Authorization": "Bearer test", "Content-Type": "application/json"},
                    image=b"\x89PNG",
                )

        import aiohttp

        assert isinstance(exc_info.value, aiohttp.ClientResponseError)
        assert exc_info.value.status == 400
        assert len(received) == 1

    @pytest.mark.asyncio
    async def test_5xx_respects_total_attempt_cap(self, run_server):
        received = []

        async def handler(request: web.Request):
            await _read_multipart(request)
            received.append(1)
            return web.json_response({"error": "boom"}, status=500)

        server = await run_server(handler)
        config = _config(
            "openai_image_edit",
            "images/edits",
            await _base_url(server),
            max_retries=3,
        )
        endpoint = OpenaiImageEditEndpoint(config=config)

        with _NO_SLEEP:
            with pytest.raises(Exception):
                await endpoint._call(
                    payload={"prompt": "x", "model": "dall-e-2"},
                    headers={"Authorization": "Bearer test", "Content-Type": "application/json"},
                    image=b"\x89PNG",
                )

        # max_retries is a TOTAL attempt cap.
        assert len(received) == 3

    @pytest.mark.asyncio
    async def test_no_nested_retry_with_explicit_retry_config(self, run_server):
        """An explicit RetryConfig owns retries; the native max_retries cap must not also apply."""
        import aiohttp

        received = []

        async def handler(request: web.Request):
            await _read_multipart(request)
            received.append(1)
            return web.json_response({"error": "boom"}, status=500)

        server = await run_server(handler)
        # Native cap set high; if it were nested with the outer RetryConfig, the
        # total request count would multiply well past the RetryConfig's own budget.
        config = _config(
            "openai_image_edit",
            "images/edits",
            await _base_url(server),
            max_retries=5,
        )
        retry_config = RetryConfig(
            max_retries=1,
            base_delay=0.01,
            retry_exceptions=(aiohttp.ClientError,),
        )
        endpoint = OpenaiImageEditEndpoint(config=config, retry_config=retry_config)

        with _NO_SLEEP:
            with pytest.raises(aiohttp.ClientError):
                await endpoint.call(
                    {"prompt": "x", "model": "dall-e-2"},
                    extra_headers={"Content-Type": "application/json"},
                    image=b"\x89PNG",
                )

        # RetryConfig(max_retries=1) => 2 total attempts, not 2 * native(5).
        assert len(received) == 2


class TestOpenaiTranscription:
    @pytest.mark.asyncio
    async def test_wire_contract_filename_bytes_model_and_transcript(self, run_server):
        received = []

        async def handler(request: web.Request):
            fields = await _read_multipart(request)
            received.append((request.headers.get("Content-Type", ""), fields))
            return web.json_response({"text": "hello world"})

        server = await run_server(handler)
        config = _config("openai_stt", "audio/transcriptions", await _base_url(server))
        endpoint = OpenaiAudioTranscriptionEndpoint(config=config)

        audio_bytes = b"RIFF-fake-audio-bytes"

        with _NO_SLEEP:
            result = await endpoint._call(
                payload={"model": "whisper-1"},
                headers={"Authorization": "Bearer test", "Content-Type": "application/json"},
                file=audio_bytes,
                filename="clip.wav",
            )

        assert result == {"text": "hello world"}
        assert len(received) == 1
        content_type, fields = received[0]
        assert "multipart/form-data" in content_type
        assert fields["file"] == audio_bytes
        assert fields["model"] == "whisper-1"

    @pytest.mark.asyncio
    async def test_retryable_failure_rebuilds_multipart_body(self, run_server):
        received = []

        async def handler(request: web.Request):
            fields = await _read_multipart(request)
            received.append(fields)
            if len(received) == 1:
                return web.json_response({"error": "rate limited"}, status=429)
            return web.json_response({"text": "second attempt"})

        server = await run_server(handler)
        config = _config("openai_stt", "audio/transcriptions", await _base_url(server))
        endpoint = OpenaiAudioTranscriptionEndpoint(config=config)

        audio_bytes = b"identical-bytes-both-attempts"

        with _NO_SLEEP:
            result = await endpoint._call(
                payload={"model": "whisper-1"},
                headers={"Authorization": "Bearer test", "Content-Type": "application/json"},
                file=audio_bytes,
                filename="clip.wav",
            )

        assert result == {"text": "second attempt"}
        assert len(received) == 2
        assert received[0]["file"] == audio_bytes
        assert received[1]["file"] == audio_bytes

    @pytest.mark.asyncio
    async def test_non_seekable_stream_fails_before_network_io(self, run_server):
        request_count = 0

        async def handler(request: web.Request):
            nonlocal request_count
            request_count += 1
            return web.json_response({"text": "should not get here"})

        server = await run_server(handler)
        config = _config("openai_stt", "audio/transcriptions", await _base_url(server))
        endpoint = OpenaiAudioTranscriptionEndpoint(config=config)

        class _NonSeekable:
            def read(self, n=-1):
                return b""

        with pytest.raises(TypeError, match="seekable"):
            await endpoint._call(
                payload={"model": "whisper-1"},
                headers={"Authorization": "Bearer test", "Content-Type": "application/json"},
                file=_NonSeekable(),
                filename="clip.wav",
            )

        assert request_count == 0

    async def test_4xx_with_retry_config_raises_client_response_error(self, run_server):
        """With an explicit RetryConfig the internal non-retryable sentinel must
        still unwrap so callers see the aiohttp exception type they catch."""
        import aiohttp

        async def handler(request: web.Request):
            return web.json_response({"error": "bad request"}, status=400)

        server = await run_server(handler)
        config = _config("openai_stt", "audio/transcriptions", await _base_url(server))
        endpoint = OpenaiAudioTranscriptionEndpoint(
            config=config, retry_config=RetryConfig(max_retries=2, base_delay=0.001)
        )

        with pytest.raises(aiohttp.ClientResponseError):
            await endpoint._call(
                payload={"model": "whisper-1"},
                headers={"Authorization": "Bearer test", "Content-Type": "application/json"},
                file=b"audio",
                filename="clip.wav",
            )

    async def test_payload_field_kwargs_are_not_sent_as_request_kwargs(self, run_server):
        """Endpoint.call passes the same kwargs to create_payload and _call; API
        fields like language must land in the multipart body only, never be
        forwarded to aiohttp.ClientSession.request."""
        received: dict = {}

        async def handler(request: web.Request):
            received.update(await _read_multipart(request))
            return web.json_response({"text": "ok"})

        server = await run_server(handler)
        config = _config("openai_stt", "audio/transcriptions", await _base_url(server))
        endpoint = OpenaiAudioTranscriptionEndpoint(config=config)

        result = await endpoint._call(
            payload={"model": "whisper-1", "language": "en"},
            headers={"Authorization": "Bearer test", "Content-Type": "application/json"},
            file=b"audio-bytes",
            filename="clip.wav",
            language="en",
        )

        assert result == {"text": "ok"}
        assert received["language"] == "en"

    async def test_single_shot_endpoint_accepts_non_seekable_stream(self, run_server):
        """With max_retries=1 and no RetryConfig no replay can occur, so a
        non-seekable stream is handed to aiohttp once, as before."""
        received: dict = {}

        async def handler(request: web.Request):
            received.update(await _read_multipart(request))
            return web.json_response({"text": "single shot ok"})

        server = await run_server(handler)
        config = _config(
            "openai_stt", "audio/transcriptions", await _base_url(server), max_retries=1
        )
        endpoint = OpenaiAudioTranscriptionEndpoint(config=config)

        import io

        class _NonSeekableBody(io.RawIOBase):
            def __init__(self, data: bytes):
                self._data = data

            def readable(self):
                return True

            def seekable(self):
                return False

            def readinto(self, b):
                chunk, self._data = self._data[: len(b)], self._data[len(b) :]
                b[: len(chunk)] = chunk
                return len(chunk)

        result = await endpoint._call(
            payload={"model": "whisper-1"},
            headers={"Authorization": "Bearer test", "Content-Type": "application/json"},
            file=_NonSeekableBody(b"one-shot-audio"),
            filename="clip.wav",
        )

        assert result == {"text": "single shot ok"}
        assert received["file"] == b"one-shot-audio"


class TestOpenaiTTS:
    @pytest.mark.asyncio
    async def test_byte_round_trip_and_json_request(self, run_server):
        received = []

        async def handler(request: web.Request):
            body = await request.json()
            received.append(body)
            return web.Response(body=b"\xff\xfb\x90audio-bytes", content_type="audio/mpeg")

        server = await run_server(handler)
        config = _config("openai_tts", "audio/speech", await _base_url(server))
        endpoint = OpenaiAudioSpeechEndpoint(config=config)

        with _NO_SLEEP:
            result = await endpoint._call(
                payload={"model": "tts-1", "input": "hello", "voice": "nova"},
                headers={"Authorization": "Bearer test", "Content-Type": "application/json"},
            )

        assert result == b"\xff\xfb\x90audio-bytes"
        assert len(received) == 1
        assert received[0] == {"model": "tts-1", "input": "hello", "voice": "nova"}

    @pytest.mark.asyncio
    async def test_5xx_respects_total_attempt_cap(self, run_server):
        request_count = 0

        async def handler(request: web.Request):
            nonlocal request_count
            request_count += 1
            return web.json_response({"error": "boom"}, status=500)

        server = await run_server(handler)
        config = _config(
            "openai_tts",
            "audio/speech",
            await _base_url(server),
            max_retries=3,
        )
        endpoint = OpenaiAudioSpeechEndpoint(config=config)

        with _NO_SLEEP:
            with pytest.raises(Exception):
                await endpoint._call(
                    payload={"model": "tts-1", "input": "hello", "voice": "nova"},
                    headers={"Authorization": "Bearer test", "Content-Type": "application/json"},
                )

        assert request_count == 3

    @pytest.mark.asyncio
    async def test_non_429_4xx_sends_exactly_one_request(self, run_server):
        request_count = 0

        async def handler(request: web.Request):
            nonlocal request_count
            request_count += 1
            return web.json_response({"error": "bad request"}, status=400)

        server = await run_server(handler)
        config = _config("openai_tts", "audio/speech", await _base_url(server))
        endpoint = OpenaiAudioSpeechEndpoint(config=config)

        import aiohttp

        with _NO_SLEEP:
            with pytest.raises(aiohttp.ClientResponseError) as exc_info:
                await endpoint._call(
                    payload={"model": "tts-1", "input": "hello", "voice": "nova"},
                    headers={"Authorization": "Bearer test", "Content-Type": "application/json"},
                )

        assert exc_info.value.status == 400
        assert request_count == 1


class TestResponseRelease:
    """Every attempt's response must be released, including retried ones."""

    @pytest.mark.asyncio
    async def test_transcription_releases_every_attempt_response(self):
        import aiohttp

        r429 = AsyncMock(spec=aiohttp.ClientResponse)
        r429.status = 429
        r429.closed = False
        r429.release = MagicMock()
        r429.request_info = MagicMock()
        r429.history = []
        r429.headers = {}

        def _raise_for_status():
            raise aiohttp.ClientResponseError(
                request_info=r429.request_info,
                history=r429.history,
                status=429,
                message="rate limited",
                headers=r429.headers,
            )

        r429.raise_for_status = _raise_for_status

        ok = AsyncMock(spec=aiohttp.ClientResponse)
        ok.status = 200
        ok.closed = False
        ok.release = MagicMock()
        ok.json = AsyncMock(return_value={"text": "ok"})

        responses = iter([r429, ok])

        mock_session = AsyncMock(spec=aiohttp.ClientSession)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        async def _request(*args, **kwargs):
            return next(responses)

        mock_session.request = _request

        config = _config(
            "openai_stt",
            "audio/transcriptions",
            "https://api.openai.com/v1",
            allow_local_network=False,
        )
        endpoint = OpenaiAudioTranscriptionEndpoint(config=config)

        with patch("lionagi.ln._ssrf.is_ssrf_safe", return_value=True):
            with patch.object(endpoint, "_create_http_session", return_value=mock_session):
                with _NO_SLEEP:
                    result = await endpoint._call(
                        payload={"model": "whisper-1"},
                        headers={"Authorization": "Bearer test"},
                        file=b"audio-bytes",
                        filename="clip.wav",
                    )

        assert result == {"text": "ok"}
        r429.release.assert_called_once()
        ok.release.assert_called_once()
