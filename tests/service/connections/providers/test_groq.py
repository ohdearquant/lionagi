# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Wire-level contract tests for the Groq audio transcription endpoint."""

from __future__ import annotations

from email import policy
from email.parser import BytesParser
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from lionagi.service.connections.endpoint import Endpoint
from lionagi.service.connections.endpoint_config import EndpointConfig
from lionagi.service.resilience import APIClientError, RetryConfig
from lionhub.providers.groq.audio_transcription import (
    GroqAudioTranscriptionEndpoint,
    _replayable_file_factory,
)

_NO_SLEEP = patch("lionagi.ln.concurrency.patterns.anyio.sleep", AsyncMock())


def test_single_shot_seekable_stream_is_not_buffered():
    class SeekableNoBuffer:
        def seekable(self):
            return True

        def read(self):
            raise AssertionError("single-shot stream must not be read eagerly")

    stream = SeekableNoBuffer()
    assert _replayable_file_factory(stream, "file", require_replayable=False)() is stream


def _config(base_url: str, **overrides) -> EndpointConfig:
    kwargs = dict(
        name="groq_audio_transcription",
        provider="groq",
        endpoint="audio/transcriptions",
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
def run_server(monkeypatch):
    async def _run(handler):
        def transport_handler(request):
            return handler(request)

        def create_client(self, *args, **kwargs):
            return httpx.AsyncClient(transport=httpx.MockTransport(transport_handler))

        monkeypatch.setattr(Endpoint, "_create_http_session", create_client)
        monkeypatch.setattr(Endpoint, "_assert_ssrf_safe_url", lambda self: None)
        return object()

    return _run


async def _base_url(server) -> str:
    return "https://api.example.test"


def _read_multipart(request: httpx.Request) -> dict:
    content_type = request.headers["content-type"]
    message = BytesParser(policy=policy.default).parsebytes(
        b"Content-Type: "
        + content_type.encode()
        + b"\r\nMIME-Version: 1.0\r\n\r\n"
        + request.content
    )
    fields: dict[str, object] = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        value = part.get_payload(decode=True) or b""
        fields[name] = value if part.get_filename() else value.decode()
    return fields


def _json_response(request: httpx.Request, body: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=body, request=request)


class TestGroqTranscription:
    @pytest.mark.asyncio
    async def test_wire_contract_path_model_filename_and_audio_bytes(self, run_server):
        received = []

        def handler(request: httpx.Request):
            fields = _read_multipart(request)
            received.append((request.url.path, request.headers.get("Content-Type", ""), fields))
            return _json_response(request, {"text": "groq transcript"})

        server = await run_server(handler)
        config = _config(await _base_url(server))
        endpoint = GroqAudioTranscriptionEndpoint(config=config)

        audio_bytes = b"groq-audio-payload"

        with _NO_SLEEP:
            result = await endpoint._call(
                payload={"model": "whisper-large-v3"},
                headers={"Authorization": "Bearer test", "Content-Type": "application/json"},
                file=audio_bytes,
                filename="clip.wav",
            )

        assert result == {"text": "groq transcript"}
        assert len(received) == 1
        path, content_type, fields = received[0]
        assert path == "/audio/transcriptions"
        assert "multipart/form-data" in content_type
        assert fields["model"] == "whisper-large-v3"
        assert fields["file"] == audio_bytes

    @pytest.mark.asyncio
    async def test_multipart_body_is_rebuilt_after_retryable_failure(self, run_server):
        received = []

        def handler(request: httpx.Request):
            fields = _read_multipart(request)
            received.append(fields)
            if len(received) == 1:
                return _json_response(request, {"error": "server error"}, status=500)
            return _json_response(request, {"text": "second attempt transcript"})

        server = await run_server(handler)
        config = _config(await _base_url(server))
        endpoint = GroqAudioTranscriptionEndpoint(config=config)

        audio_bytes = b"identical-groq-bytes"

        with _NO_SLEEP:
            result = await endpoint._call(
                payload={"model": "whisper-large-v3"},
                headers={"Authorization": "Bearer test", "Content-Type": "application/json"},
                file=audio_bytes,
                filename="clip.wav",
            )

        assert result == {"text": "second attempt transcript"}
        assert len(received) == 2
        assert received[0]["file"] == audio_bytes
        assert received[1]["file"] == audio_bytes

    @pytest.mark.asyncio
    async def test_non_429_4xx_sends_exactly_one_request(self, run_server):
        received = []

        def handler(request: httpx.Request):
            fields = _read_multipart(request)
            received.append(fields)
            return _json_response(request, {"error": "invalid model"}, status=422)

        server = await run_server(handler)
        config = _config(await _base_url(server))
        endpoint = GroqAudioTranscriptionEndpoint(config=config)

        import httpx

        with _NO_SLEEP:
            with pytest.raises(APIClientError) as exc_info:
                await endpoint._call(
                    payload={"model": "bad-model"},
                    headers={"Authorization": "Bearer test", "Content-Type": "application/json"},
                    file=b"audio",
                    filename="clip.wav",
                )

        assert exc_info.value.status_code == 422
        assert len(received) == 1

    @pytest.mark.asyncio
    async def test_5xx_respects_total_attempt_cap(self, run_server):
        received = []

        def handler(request: httpx.Request):
            _read_multipart(request)
            received.append(1)
            return _json_response(request, {"error": "boom"}, status=500)

        server = await run_server(handler)
        config = _config(await _base_url(server), max_retries=2)
        endpoint = GroqAudioTranscriptionEndpoint(config=config)

        with _NO_SLEEP:
            with pytest.raises(Exception):
                await endpoint._call(
                    payload={"model": "whisper-large-v3"},
                    headers={"Authorization": "Bearer test", "Content-Type": "application/json"},
                    file=b"audio",
                    filename="clip.wav",
                )

        assert len(received) == 2

    @pytest.mark.asyncio
    async def test_no_nested_retry_with_explicit_retry_config(self, run_server):
        import httpx

        received = []

        def handler(request: httpx.Request):
            _read_multipart(request)
            received.append(1)
            return _json_response(request, {"error": "boom"}, status=500)

        server = await run_server(handler)
        config = _config(await _base_url(server), max_retries=5)
        retry_config = RetryConfig(
            max_retries=1,
            base_delay=0.01,
            retry_exceptions=(APIClientError,),
        )
        endpoint = GroqAudioTranscriptionEndpoint(config=config, retry_config=retry_config)

        with _NO_SLEEP:
            with pytest.raises(APIClientError):
                await endpoint.call(
                    {"model": "whisper-large-v3"},
                    extra_headers={"Content-Type": "application/json"},
                    file=b"audio",
                    filename="clip.wav",
                )

        assert len(received) == 2

    @pytest.mark.asyncio
    async def test_non_seekable_stream_fails_before_network_io(self, run_server):
        request_count = 0

        def handler(request: httpx.Request):
            nonlocal request_count
            request_count += 1
            return _json_response(request, {"text": "should not get here"})

        server = await run_server(handler)
        config = _config(await _base_url(server))
        endpoint = GroqAudioTranscriptionEndpoint(config=config)

        class _NonSeekable:
            def read(self, n=-1):
                return b""

        with pytest.raises(TypeError, match="seekable"):
            await endpoint._call(
                payload={"model": "whisper-large-v3"},
                headers={"Authorization": "Bearer test", "Content-Type": "application/json"},
                file=_NonSeekable(),
                filename="clip.wav",
            )

        assert request_count == 0
