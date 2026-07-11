# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Wire-level contract tests for the Groq audio transcription endpoint.

Drives GroqAudioTranscriptionEndpoint against an ephemeral local aiohttp server so
the multipart-rebuild-on-retry contract restored in Endpoint._call_aiohttp is
exercised end to end rather than through a mocked session.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from lionagi.providers.groq.audio_transcription import GroqAudioTranscriptionEndpoint
from lionagi.service.connections.endpoint_config import EndpointConfig
from lionagi.service.resilience import RetryConfig

_NO_SLEEP = patch("lionagi.ln.concurrency.patterns.anyio.sleep", AsyncMock())


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


class TestGroqTranscription:
    @pytest.mark.asyncio
    async def test_wire_contract_path_model_filename_and_audio_bytes(self, run_server):
        received = []

        async def handler(request: web.Request):
            fields = await _read_multipart(request)
            received.append((request.path, request.headers.get("Content-Type", ""), fields))
            return web.json_response({"text": "groq transcript"})

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

        async def handler(request: web.Request):
            fields = await _read_multipart(request)
            received.append(fields)
            if len(received) == 1:
                return web.json_response({"error": "server error"}, status=500)
            return web.json_response({"text": "second attempt transcript"})

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

        async def handler(request: web.Request):
            fields = await _read_multipart(request)
            received.append(fields)
            return web.json_response({"error": "invalid model"}, status=422)

        server = await run_server(handler)
        config = _config(await _base_url(server))
        endpoint = GroqAudioTranscriptionEndpoint(config=config)

        import aiohttp

        with _NO_SLEEP:
            with pytest.raises(aiohttp.ClientResponseError) as exc_info:
                await endpoint._call(
                    payload={"model": "bad-model"},
                    headers={"Authorization": "Bearer test", "Content-Type": "application/json"},
                    file=b"audio",
                    filename="clip.wav",
                )

        assert exc_info.value.status == 422
        assert len(received) == 1

    @pytest.mark.asyncio
    async def test_5xx_respects_total_attempt_cap(self, run_server):
        received = []

        async def handler(request: web.Request):
            await _read_multipart(request)
            received.append(1)
            return web.json_response({"error": "boom"}, status=500)

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
        import aiohttp

        received = []

        async def handler(request: web.Request):
            await _read_multipart(request)
            received.append(1)
            return web.json_response({"error": "boom"}, status=500)

        server = await run_server(handler)
        config = _config(await _base_url(server), max_retries=5)
        retry_config = RetryConfig(
            max_retries=1,
            base_delay=0.01,
            retry_exceptions=(aiohttp.ClientError,),
        )
        endpoint = GroqAudioTranscriptionEndpoint(config=config, retry_config=retry_config)

        with _NO_SLEEP:
            with pytest.raises(aiohttp.ClientError):
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

        async def handler(request: web.Request):
            nonlocal request_count
            request_count += 1
            return web.json_response({"text": "should not get here"})

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
