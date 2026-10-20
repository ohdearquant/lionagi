# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The endpoints' HTTP client against a real local server: what goes on the wire, what comes
back, and how a request ends when it is cancelled or runs out of time."""

from __future__ import annotations

import asyncio
import email.parser
import email.policy
import io
import socket
import time
from contextlib import asynccontextmanager

import pytest

from lionagi.service.connections import _http
from lionagi.service.connections._http import (
    HTTPSession,
    HTTPStatusError,
    MultipartForm,
    TransportError,
    check_client_options,
)
from lionagi.service.resilience import APIClientError
from tests._local_http import (
    Application,
    LocalServer,
    Redirect,
    Request,
    StreamResponse,
    json_response,
)

# http.client hands the socket from the connection to the response when the server says it
# will close, and keeps it on the connection otherwise; each test that reads a response runs
# against both.
framings = pytest.mark.parametrize(
    "announce_close", [True, False], ids=["connection-close", "keep-alive"]
)


@asynccontextmanager
async def serving(handler, announce_close: bool = True):
    app = Application()
    app.add_route("*", "/{tail:.*}", handler)
    server = LocalServer(app, announce_close=announce_close)
    await server.start_server()
    try:
        yield server.make_url("")
    finally:
        await server.close()


@framings
async def test_a_json_request_and_its_response(announce_close):
    seen = {}

    async def handler(request: Request):
        seen["method"] = request.method
        seen["path"] = request.path
        seen["query"] = request.query
        seen["headers"] = request.headers
        seen["body"] = await request.json()
        return json_response({"ok": True}, status=201, headers={"X-Reply": "1"})

    async with serving(handler, announce_close) as base, HTTPSession(timeout=5) as session:
        response = await session.request(
            "post", f"{base}/v1/chat?a=1", json={"q": "hi"}, params={"b": "2 3"}
        )
        assert (response.status, response.reason) == (201, "Created")
        assert response.headers["x-reply"] == "1"
        assert await response.json() == {"ok": True}
        assert response.closed

    assert seen["method"] == "POST"
    assert seen["path"] == "/v1/chat"
    assert seen["query"] == {"a": "1", "b": "2 3"}
    assert seen["body"] == {"q": "hi"}
    headers = seen["headers"]
    assert headers["Content-Type"] == "application/json"
    assert headers["Content-Length"] == str(len(b'{"q": "hi"}'))
    assert headers["User-Agent"] == _http._USER_AGENT
    assert headers["Accept-Encoding"] == "identity"


async def test_a_request_header_replaces_a_session_header_of_any_case():
    seen = []

    async def handler(request: Request):
        seen.append(request.headers)
        return json_response({})

    async with serving(handler) as base:
        async with HTTPSession(headers={"X-Version": "old", "X-Kept": "k"}) as session:
            response = await session.request(
                "POST", base, headers={"x-version": "new", "Content-Type": "text/plain"}, json={}
            )
            await response.read()

    (headers,) = seen
    assert headers.get_all("x-version") == ["new"]
    assert headers["X-Kept"] == "k"
    # A caller's content type is kept over the JSON default.
    assert headers.get_all("content-type") == ["text/plain"]


async def test_a_redirect_comes_back_as_a_response_and_is_never_followed():
    hits = []

    async def handler(request: Request):
        hits.append(request.path)
        if request.path == "/start":
            raise Redirect("/elsewhere")
        return json_response({})

    async with serving(handler) as base, HTTPSession() as session:
        response = await session.request("POST", f"{base}/start", json={})
        assert response.status == 307
        assert response.headers["Location"] == "/elsewhere"
        await response.read()
        with pytest.raises(ValueError, match="redirects"):
            await session.request("POST", f"{base}/start", json={}, allow_redirects=True)

    assert hits == ["/start"]


@framings
async def test_a_streamed_body_arrives_piece_by_piece(announce_close):
    second = asyncio.Event()

    async def handler(request: Request):
        response = StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        await response.write(b"data: one\n\n")
        await second.wait()
        await response.write(b"data: two\n\n")
        await response.write_eof()
        return response

    async with serving(handler, announce_close) as base, HTTPSession(timeout=5) as session:
        response = await session.request("POST", base, json={})
        chunks = response.iter_chunks()
        # The first piece is read while the server still holds the second.
        assert await anext(chunks) == b"data: one\n\n"
        second.set()
        rest = [chunk async for chunk in chunks]

    assert b"".join(rest) == b"data: two\n\n"
    assert response.closed


def _waits_for_close(closed: asyncio.Event):
    async def handler(request: Request):
        await request.client_closed()
        closed.set()
        return json_response({})

    return handler


def _sends_part_then_waits_for_close(closed: asyncio.Event):
    async def handler(request: Request):
        response = StreamResponse()
        await response.prepare(request)
        await response.write(b"first")
        await request.client_closed()
        closed.set()
        return response

    return handler


@framings
async def test_a_request_past_its_timeout_raises_and_closes_its_connection(announce_close):
    closed = asyncio.Event()
    async with serving(_waits_for_close(closed), announce_close) as base:
        started = time.monotonic()
        async with HTTPSession(timeout=0.3) as session:
            with pytest.raises(TimeoutError, match="did not complete within 0.3 s"):
                await session.request("POST", base, json={})
        assert time.monotonic() - started < 3
        await asyncio.wait_for(closed.wait(), timeout=3)


@framings
async def test_a_timeout_counts_the_whole_request_including_its_body(announce_close):
    closed = asyncio.Event()
    async with serving(_sends_part_then_waits_for_close(closed), announce_close) as base:
        async with HTTPSession(timeout=0.5) as session:
            response = await session.request("POST", base, json={})
            chunks = response.iter_chunks()
            assert await anext(chunks) == b"first"
            with pytest.raises(TimeoutError):
                await anext(chunks)
            assert response.closed
            # The server sees the connection end, so the blocked read has returned.
            await asyncio.wait_for(closed.wait(), timeout=3)


@framings
async def test_a_request_cancelled_before_its_response_closes_its_connection(announce_close):
    closed = asyncio.Event()
    async with serving(_waits_for_close(closed), announce_close) as base:
        async with HTTPSession() as session:
            task = asyncio.create_task(session.request("POST", base, json={}))
            await asyncio.sleep(0.2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=3)
            # No timeout was set, so only the cancellation can have ended the connection.
            await asyncio.wait_for(closed.wait(), timeout=3)


@framings
async def test_a_body_read_cancelled_midway_closes_its_connection(announce_close):
    closed = asyncio.Event()
    async with serving(_sends_part_then_waits_for_close(closed), announce_close) as base:
        async with HTTPSession() as session:
            response = await session.request("POST", base, json={})
            chunks = response.iter_chunks()
            assert await anext(chunks) == b"first"
            task = asyncio.create_task(anext(chunks))
            await asyncio.sleep(0.2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=3)
            await asyncio.wait_for(closed.wait(), timeout=3)


@framings
async def test_closing_a_session_closes_the_responses_still_open(announce_close):
    closed = asyncio.Event()
    async with serving(_sends_part_then_waits_for_close(closed), announce_close) as base:
        async with HTTPSession() as session:
            response = await session.request("POST", base, json={})
            assert not response.closed
        assert response.closed
        await asyncio.wait_for(closed.wait(), timeout=3)
        # Released, not only shut down: the descriptor does not wait for garbage collection.
        assert response._exchange.sock.fileno() == -1


async def test_a_refused_connection_is_a_transport_error():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    url = f"http://127.0.0.1:{port}/v1"
    async with HTTPSession(timeout=5) as session:
        with pytest.raises(TransportError, match=f"POST {url}") as caught:
            await session.request("POST", url, json={})
    assert isinstance(caught.value, ConnectionError)
    assert isinstance(caught.value.__cause__, ConnectionRefusedError)


async def test_a_response_that_is_not_http_is_a_transport_error():
    async def answer(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"SSH-2.0-not-http\r\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(answer, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        async with HTTPSession(timeout=5) as session:
            with pytest.raises(TransportError, match="BadStatusLine"):
                await session.request("GET", f"http://127.0.0.1:{port}/")
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"json": {}, "data": b"x"}, "json or data"),
        ({"url": "ftp://example.com/x"}, "not an http"),
        ({"url": "http:///nohost"}, "not an http"),
    ],
)
async def test_a_malformed_request_is_refused_before_it_connects(kwargs, message):
    url = kwargs.pop("url", "http://127.0.0.1:9/")
    async with HTTPSession() as session:
        with pytest.raises(ValueError, match=message):
            await session.request("POST", url, **kwargs)


async def test_a_request_stopped_while_connecting_is_never_sent():
    class Connection:
        sock = None
        requests = []

        def connect(self):
            exchange.stop()

        def request(self, *args, **kwargs):
            self.requests.append(args)

        def close(self):
            pass

    conn = Connection()
    exchange = _http._Exchange(conn)
    with pytest.raises(TransportError, match="stopped"):
        exchange.run(lambda: exchange.send("POST", "/", b"{}", {}))
    assert conn.requests == []


def test_a_multipart_form_reads_back_through_the_standard_library_parser():
    form = MultipartForm()
    form.add_field("model", "whisper-1")
    form.add_field("note", 'quote " and\r\nbreak')
    payload = bytes(range(256)) * 4
    form.add_field("file", io.BytesIO(payload), filename="clip.wav", content_type="audio/wav")
    body = form.encode()

    message = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
        f"Content-Type: {form.content_type}\r\n\r\n".encode() + body
    )
    parts = {
        part.get_param("name", header="content-disposition"): part for part in message.iter_parts()
    }
    assert set(parts) == {"model", "note", "file"}
    assert parts["model"].get_payload(decode=True) == b"whisper-1"
    assert parts["note"].get_payload(decode=True) == b'quote " and\r\nbreak'
    assert parts["file"].get_filename() == "clip.wav"
    assert parts["file"].get_content_type() == "audio/wav"
    assert parts["file"].get_payload(decode=True) == payload


def test_a_multipart_name_cannot_end_its_parameter():
    form = MultipartForm()
    form.add_field('a"; filename="x', "v")
    form.add_field("b\r\nX-Injected: 1", "v")
    body = form.encode()
    assert b'name="a%22; filename=%22x"' in body
    assert b"X-Injected: 1\r\n" not in body
    assert b'name="b%0D%0AX-Injected: 1"' in body


def test_a_multipart_value_holding_the_boundary_is_refused():
    form = MultipartForm()
    form.add_field("file", f"--{form.boundary}\r\n".encode(), filename="f")
    with pytest.raises(ValueError, match="boundary"):
        form.encode()


def test_a_multipart_value_of_another_type_is_refused():
    form = MultipartForm()
    form.add_field("count", 3)
    with pytest.raises(TypeError, match="'count'"):
        form.encode()


def test_client_options_take_only_headers():
    check_client_options({"headers": {"X": "1"}})
    with pytest.raises(TypeError, match="supports only 'headers'"):
        check_client_options({"headers": {}, "connector": object(), "trust_env": True})


def test_a_status_error_carries_the_response_it_describes():
    error = HTTPStatusError(
        "failed with status 429",
        status=429,
        headers={"Retry-After": "2"},
        method="POST",
        url="https://api.test/v1",
    )
    assert isinstance(error, APIClientError)
    assert not isinstance(error, ConnectionError)
    assert (error.status, error.status_code) == (429, 429)
    assert error.headers == {"Retry-After": "2"}
    assert (error.method, error.url) == ("POST", "https://api.test/v1")
    assert str(error) == "failed with status 429"
