# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Wire-level contract tests for the OpenAI media endpoints."""

from __future__ import annotations

import io
import json
from email import policy
from email.parser import BytesParser
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from lionagi.service.connections.endpoint import Endpoint
from lionagi.service.connections.endpoint_config import EndpointConfig
from lionagi.service.resilience import APIClientError, RetryConfig
from lionhub.providers.openai.audio import (
    OpenaiAudioSpeechEndpoint,
    OpenaiAudioTranscriptionEndpoint,
)
from lionhub.providers.openai.audio import (
    _replayable_file_factory as audio_file_factory,
)
from lionhub.providers.openai.images import (
    OpenaiImageEditEndpoint,
)
from lionhub.providers.openai.images import (
    _replayable_file_factory as image_file_factory,
)

_NO_SLEEP = patch("lionagi.ln.concurrency.patterns.anyio.sleep", AsyncMock())


@pytest.mark.asyncio
async def test_media_upload_builders_return_httpx_data_and_files():
    from lionhub.providers.groq.audio_transcription import GroqAudioTranscriptionEndpoint

    cases = [
        (
            OpenaiAudioTranscriptionEndpoint,
            _config("openai_stt", "audio/transcriptions", "https://api.openai.com/v1"),
            {"model": "whisper-1"},
            {"file": b"audio", "filename": "clip.wav"},
            {"file": ("clip.wav", b"audio", "application/octet-stream")},
        ),
        (
            OpenaiImageEditEndpoint,
            _config("openai_image_edit", "images/edits", "https://api.openai.com/v1"),
            {"prompt": "edit", "model": "dall-e-2", "n": 1},
            {"image": b"image", "image_filename": "image.png", "mask": b"mask"},
            {
                "image": ("image.png", b"image", "image/png"),
                "mask": ("mask.png", b"mask", "image/png"),
            },
        ),
        (
            GroqAudioTranscriptionEndpoint,
            EndpointConfig(
                name="groq_audio_transcription",
                provider="groq",
                endpoint="audio/transcriptions",
                base_url="https://api.groq.com/openai/v1",
                auth_type="bearer",
                api_key="test-key",
                max_retries=1,
            ),
            {"model": "whisper-large-v3"},
            {"file": b"audio", "filename": "clip.wav"},
            {"file": ("clip.wav", b"audio", "application/octet-stream")},
        ),
    ]

    for endpoint_type, config, payload, upload_kwargs, expected_files in cases:
        endpoint = endpoint_type(config=config)
        captured = {}

        async def capture(**kwargs):
            captured["headers"] = kwargs["headers"]
            captured["body"] = kwargs["request_body_factory"]()
            return {"ok": True}

        endpoint._call_http = capture
        result = await endpoint._call(
            payload=payload,
            headers={"Content-Type": "application/json"},
            **upload_kwargs,
        )

        assert result == {"ok": True}
        for name, expected in expected_files.items():
            filename, file_data, content_type = captured["body"]["files"][name]
            expected_filename, expected_data, expected_content_type = expected
            assert filename == expected_filename
            assert content_type == expected_content_type
            actual_data = file_data if isinstance(file_data, bytes) else file_data.getvalue()
            assert actual_data == expected_data
        assert captured["body"]["data"] == {key: str(value) for key, value in payload.items()}
        assert "Content-Type" not in captured["headers"]


@pytest.mark.parametrize("factory", [audio_file_factory, image_file_factory])
def test_single_shot_seekable_stream_is_not_buffered(factory):
    class SeekableNoBuffer:
        def seekable(self):
            return True

        def read(self):
            raise AssertionError("single-shot stream must not be read eagerly")

    stream = SeekableNoBuffer()
    assert factory(stream, "file", require_replayable=False)() is stream


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


class TestImageEditMultipartRetry:
    @pytest.mark.asyncio
    async def test_429_then_200_sends_two_complete_bodies_with_identical_bytes(self, run_server):
        received = []

        def handler(request: httpx.Request):
            fields = _read_multipart(request)
            received.append(fields)
            if len(received) == 1:
                return _json_response(request, {"error": {"message": "rate limited"}}, status=429)
            return _json_response(request, {"data": [{"url": "https://example.com/out.png"}]})

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

        def handler(request: httpx.Request):
            fields = _read_multipart(request)
            received.append(fields)
            return _json_response(request, {"error": {"message": "bad prompt"}}, status=400)

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

        assert isinstance(exc_info.value, APIClientError)
        assert exc_info.value.status_code == 400
        assert len(received) == 1

    @pytest.mark.asyncio
    async def test_5xx_respects_total_attempt_cap(self, run_server):
        received = []

        def handler(request: httpx.Request):
            _read_multipart(request)
            received.append(1)
            return _json_response(request, {"error": "boom"}, status=500)

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
        received = []

        def handler(request: httpx.Request):
            _read_multipart(request)
            received.append(1)
            return _json_response(request, {"error": "boom"}, status=500)

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
            retry_exceptions=(APIClientError,),
        )
        endpoint = OpenaiImageEditEndpoint(config=config, retry_config=retry_config)

        with _NO_SLEEP:
            with pytest.raises(APIClientError):
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

        def handler(request: httpx.Request):
            fields = _read_multipart(request)
            received.append((request.headers.get("Content-Type", ""), fields))
            return _json_response(request, {"text": "hello world"})

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

        def handler(request: httpx.Request):
            fields = _read_multipart(request)
            received.append(fields)
            if len(received) == 1:
                return _json_response(request, {"error": "rate limited"}, status=429)
            return _json_response(request, {"text": "second attempt"})

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

        def handler(request: httpx.Request):
            nonlocal request_count
            request_count += 1
            return _json_response(request, {"text": "should not get here"})

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
        """A RetryConfig that retries API errors must still treat other 4xx as single-shot."""

        request_count = 0

        def handler(request: httpx.Request):
            nonlocal request_count
            request_count += 1
            return _json_response(request, {"error": "bad request"}, status=400)

        server = await run_server(handler)
        config = _config("openai_stt", "audio/transcriptions", await _base_url(server))
        endpoint = OpenaiAudioTranscriptionEndpoint(
            config=config,
            retry_config=RetryConfig(
                max_retries=2, base_delay=0.001, retry_exceptions=(APIClientError,)
            ),
        )

        with pytest.raises(APIClientError), _NO_SLEEP:
            await endpoint.call(
                {"model": "whisper-1"},
                file=b"audio",
                filename="clip.wav",
            )

        assert request_count == 1

    async def test_payload_field_kwargs_are_not_sent_as_request_kwargs(self, run_server):
        """Endpoint.call passes the same kwargs to create_payload and _call; API
        fields like language must land in the multipart body only, never be
        forwarded to httpx.AsyncClient.request."""
        received: dict = {}

        def handler(request: httpx.Request):
            received.update(_read_multipart(request))
            return _json_response(request, {"text": "ok"})

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

    async def test_seekable_stream_replays_full_content_across_explicit_retries(self, run_server):
        """An explicit RetryConfig retries by re-invoking _call; the seekable
        stream is snapshotted so the second attempt does not upload an empty
        file from an EOF position."""
        import httpx

        bodies: list[bytes] = []

        def handler(request: httpx.Request):
            fields = _read_multipart(request)
            bodies.append(fields.get("file", b""))
            if len(bodies) == 1:
                return _json_response(request, {"error": "boom"}, status=500)
            return _json_response(request, {"text": "ok"})

        server = await run_server(handler)
        config = _config("openai_stt", "audio/transcriptions", await _base_url(server))
        endpoint = OpenaiAudioTranscriptionEndpoint(
            config=config,
            retry_config=RetryConfig(
                max_retries=2, base_delay=0.001, retry_exceptions=(APIClientError,)
            ),
        )

        stream = io.BytesIO(b"full-audio-content")
        with _NO_SLEEP:
            result = await endpoint.call({"model": "whisper-1"}, file=stream, filename="clip.wav")

        assert result == {"text": "ok"}
        assert bodies == [b"full-audio-content", b"full-audio-content"]

    async def test_transport_kwargs_are_forwarded(self, run_server):
        """httpx transport options (params here) must reach the HTTP layer
        while API fields stay confined to the multipart body."""
        seen: dict = {}

        def handler(request: httpx.Request):
            seen["query"] = dict(request.url.params)
            seen.update(_read_multipart(request))
            return _json_response(request, {"text": "ok"})

        server = await run_server(handler)
        config = _config("openai_stt", "audio/transcriptions", await _base_url(server))
        endpoint = OpenaiAudioTranscriptionEndpoint(config=config)

        result = await endpoint._call(
            payload={"model": "whisper-1", "language": "en"},
            headers={"Authorization": "Bearer test", "Content-Type": "application/json"},
            file=b"audio",
            filename="clip.wav",
            language="en",
            params={"beta": "1"},
        )

        assert result == {"text": "ok"}
        assert seen["query"] == {"beta": "1"}
        assert seen["language"] == "en"

    async def test_zero_retry_config_accepts_non_seekable_stream(self, run_server):
        """RetryConfig(max_retries=0) is explicitly single-shot: no replay can
        occur, so a non-seekable stream must be accepted."""
        received: dict = {}

        def handler(request: httpx.Request):
            received.update(_read_multipart(request))
            return _json_response(request, {"text": "ok"})

        server = await run_server(handler)
        config = _config("openai_stt", "audio/transcriptions", await _base_url(server))
        endpoint = OpenaiAudioTranscriptionEndpoint(
            config=config, retry_config=RetryConfig(max_retries=0)
        )

        class _OneShot(io.RawIOBase):
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

        result = await endpoint.call(
            {"model": "whisper-1"}, file=_OneShot(b"zero-retry-audio"), filename="clip.wav"
        )

        assert result == {"text": "ok"}
        assert received["file"] == b"zero-retry-audio"

    async def test_single_shot_endpoint_accepts_non_seekable_stream(self, run_server):
        """With max_retries=1 and no RetryConfig no replay can occur, so a
        non-seekable stream is handed to HTTPX once, as before."""
        received: dict = {}

        def handler(request: httpx.Request):
            received.update(_read_multipart(request))
            return _json_response(request, {"text": "single shot ok"})

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

        def handler(request: httpx.Request):
            body = json.loads(request.content)
            received.append(body)
            return httpx.Response(
                200,
                content=b"\xff\xfb\x90audio-bytes",
                headers={"content-type": "audio/mpeg"},
                request=request,
            )

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

        def handler(request: httpx.Request):
            nonlocal request_count
            request_count += 1
            return _json_response(request, {"error": "boom"}, status=500)

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

        def handler(request: httpx.Request):
            nonlocal request_count
            request_count += 1
            return _json_response(request, {"error": "bad request"}, status=400)

        server = await run_server(handler)
        config = _config("openai_tts", "audio/speech", await _base_url(server))
        endpoint = OpenaiAudioSpeechEndpoint(config=config)

        with _NO_SLEEP:
            with pytest.raises(APIClientError) as exc_info:
                await endpoint._call(
                    payload={"model": "tts-1", "input": "hello", "voice": "nova"},
                    headers={"Authorization": "Bearer test", "Content-Type": "application/json"},
                )

        assert exc_info.value.status_code == 400
        assert request_count == 1


class TestResponseClientLifecycle:
    """Each retry closes its HTTP client and its response stream."""

    @pytest.mark.asyncio
    async def test_transcription_closes_each_attempt_client(self):
        responses = iter(
            [
                httpx.Response(429, json={"error": "rate limited"}),
                httpx.Response(200, json={"text": "ok"}),
            ]
        )
        clients = []

        def create_client(*args, **kwargs):
            client = httpx.AsyncClient(
                transport=httpx.MockTransport(lambda request: next(responses))
            )
            clients.append(client)
            return client

        config = _config(
            "openai_stt",
            "audio/transcriptions",
            "https://api.openai.com/v1",
            allow_local_network=False,
        )
        endpoint = OpenaiAudioTranscriptionEndpoint(config=config)

        with patch("lionagi.ln._ssrf.is_ssrf_safe", return_value=True):
            with patch.object(endpoint, "_create_http_session", side_effect=create_client):
                with _NO_SLEEP:
                    result = await endpoint._call(
                        payload={"model": "whisper-1"},
                        headers={"Authorization": "Bearer test"},
                        file=b"audio-bytes",
                        filename="clip.wav",
                    )

        assert result == {"text": "ok"}
        assert len(clients) == 2
        assert all(client.is_closed for client in clients)
