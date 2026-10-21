# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The endpoints' HTTP client against a real local server: what goes on the wire, what comes
back, and how a request ends when it is cancelled or runs out of time."""

from __future__ import annotations

import asyncio
import base64
import email.parser
import email.policy
import http.client
import io
import re
import selectors
import socket
import threading
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
from lionagi.service.connections.endpoint import Endpoint
from lionagi.service.connections.endpoint_config import EndpointConfig
from lionagi.service.resilience import APIClientError
from tests._local_http import (
    Application,
    LocalServer,
    Redirect,
    Request,
    Response,
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
    assert headers["Accept"] == "*/*"
    assert headers["Accept-Encoding"] == "identity"


@pytest.mark.parametrize(
    ("path", "sent"),
    [("/café", "/caf%C3%A9"), ("/a b", "/a%20b"), ("/kept%2Fescape", "/kept%2Fescape")],
    ids=["non-ascii", "space", "escape"],
)
async def test_a_path_and_query_are_sent_percent_encoded(path, sent):
    seen = {}

    async def handler(request: Request):
        seen["path"] = request.path
        seen["query"] = request.query
        return json_response({})

    async with serving(handler) as base, HTTPSession(timeout=5) as session:
        response = await session.request("GET", f"{base}{path}?q=a é&k=%2F")
        await response.read()

    assert seen["path"] == sent
    assert seen["query"] == {"q": "a é", "k": "/"}


@pytest.mark.parametrize(
    ("charset", "body"),
    [
        ("iso-8859-1", '{"text": "café"}'.encode("latin-1")),
        ("utf-8", b"\xef\xbb\xbf" + '{"text": "café"}'.encode()),
    ],
    ids=["latin-1", "utf-8-with-bom"],
)
async def test_a_json_body_is_decoded_with_the_charset_it_declares(charset, body):
    wire = (
        f"HTTP/1.1 200 OK\r\nContent-Type: application/json; charset={charset}\r\n"
        f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
    ).encode() + body
    async with answering(wire) as url, HTTPSession(timeout=5) as session:
        response = await session.request("GET", url)
        assert await response.json() == {"text": "café"}


@pytest.mark.parametrize("body", [b"", b" \r\n"], ids=["empty", "whitespace"])
async def test_a_json_body_with_nothing_in_it_reads_as_none(body):
    async def handler(request: Request):
        return Response(body=body, content_type="application/json")

    async with serving(handler) as base, HTTPSession(timeout=5) as session:
        response = await session.request("GET", base)
        assert await response.json() is None


@framings
async def test_a_body_read_twice_comes_back_the_same(announce_close):
    async def handler(request: Request):
        return json_response({"ok": True})

    async with serving(handler, announce_close) as base, HTTPSession(timeout=5) as session:
        response = await session.request("GET", base)
        body = await response.read()
        assert await response.read() == body
        assert await response.json() == {"ok": True}


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
            with pytest.raises(TimeoutError, match="did not complete within 0.3 s") as raised:
                await session.request("POST", base, json={})
            # A class of its own on Python 3.10, which aiohttp's timeout derived from.
            assert isinstance(raised.value, asyncio.TimeoutError)
            assert time.monotonic() - started < 3
            # Before the session closes, so the request's own stop is what ended the connection.
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


@framings
async def test_closing_a_session_ends_a_request_still_waiting_for_its_response(announce_close):
    closed = asyncio.Event()
    async with serving(_waits_for_close(closed), announce_close) as base:
        session = HTTPSession()
        task = asyncio.create_task(session.request("POST", base, json={}))
        await asyncio.sleep(0.2)
        session.close()
        with pytest.raises(TransportError):
            await asyncio.wait_for(task, timeout=3)
        # No timeout was set and nothing was cancelled, so only the close can have ended it.
        await asyncio.wait_for(closed.wait(), timeout=3)


@pytest.mark.parametrize("timeout", [0, -1])
async def test_a_timeout_of_zero_or_less_sets_no_bound(timeout):
    async def handler(request: Request):
        await asyncio.sleep(0.2)
        return json_response({"ok": True})

    async with serving(handler) as base, HTTPSession(timeout=timeout) as session:
        response = await session.request("POST", base, json={})
        assert await response.json() == {"ok": True}


async def test_opening_a_connection_is_bounded_by_the_time_the_request_has_left(monkeypatch):
    # One slot: the second request waits for it while the first waits for its response.
    monkeypatch.setattr(_http, "MAX_REQUESTS_IN_FLIGHT", 1)
    timeouts = []
    connect = http.client.HTTPConnection.connect

    def recording_connect(self):
        timeouts.append(self.timeout)
        connect(self)

    monkeypatch.setattr(http.client.HTTPConnection, "connect", recording_connect)

    async def handler(request: Request):
        await asyncio.sleep(0.5)
        return json_response({})

    async def call(session):
        response = await session.request("POST", base, json={})
        await response.read()

    async with serving(handler) as base, HTTPSession(timeout=5) as session:
        await asyncio.gather(call(session), call(session))

    first, second = timeouts
    assert 4.9 < first <= 5
    # The half second spent waiting for the slot is not left for the connect.
    assert second < 4.6


@pytest.mark.parametrize("stop", ["timeout", "cancel", "close"])
async def test_a_tcp_connect_in_progress_stops_soon_after_its_request_is_stopped(monkeypatch, stop):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        target = ("127.0.0.1", probe.getsockname()[1])
    connect = socket.socket.connect

    def in_progress(sock, address):
        # A host that does not answer: the connect never completes.
        if address == target:
            raise BlockingIOError("connecting")
        return connect(sock, address)

    polls = []
    done = threading.Event()

    class Unanswered:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def register(self, *args):
            pass

        def select(self, timeout):
            if done.is_set():
                raise OSError("the test is over")
            time.sleep(timeout)
            polls.append(time.monotonic())
            return []

    monkeypatch.setattr(socket.socket, "connect", in_progress)
    monkeypatch.setattr(selectors, "DefaultSelector", Unanswered)
    url = f"http://{target[0]}:{target[1]}/v1"
    try:
        session = HTTPSession(timeout=0.3 if stop == "timeout" else None)
        task = asyncio.create_task(session.request("GET", url))
        if stop == "timeout":
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(task, timeout=3)
        else:
            await asyncio.sleep(0.3)
            if stop == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, timeout=3)
            else:
                session.close()
                with pytest.raises(TransportError):
                    await asyncio.wait_for(task, timeout=3)
        await asyncio.sleep(0.2)
        settled = len(polls)
        await asyncio.sleep(0.3)
        # The connect was in progress, and its thread stopped looking once the request was stopped.
        assert settled > 0
        assert len(polls) == settled
        session.close()
    finally:
        done.set()


async def test_a_host_whose_first_address_refuses_is_reached_at_its_next(monkeypatch):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        refused = probe.getsockname()[1]
    lookup = socket.getaddrinfo

    async def handler(request: Request):
        return json_response({"host": request.headers["Host"]})

    async with serving(handler) as base:
        port = int(base.rsplit(":", 1)[1].split("/", 1)[0])

        def two_addresses(host, *args, **kwargs):
            if host != "two.test":
                return lookup(host, *args, **kwargs)
            tcp = (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "")
            return [(*tcp, ("127.0.0.1", refused)), (*tcp, ("127.0.0.1", port))]

        monkeypatch.setattr(socket, "getaddrinfo", two_addresses)
        async with HTTPSession(timeout=5) as session:
            response = await session.request("GET", f"http://two.test:{port}/v1")
            assert await response.json() == {"host": f"two.test:{port}"}


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


@asynccontextmanager
async def answering(wire: bytes):
    """A server that reads one request whole, writes *wire* as it is, and closes."""

    async def answer(reader, writer):
        head = await reader.readuntil(b"\r\n\r\n")
        # The request body is read too: closing with it unread would reset the connection
        # rather than end it.
        length = re.search(rb"(?im)^content-length:[ \t]*(\d+)", head)
        if length:
            await reader.readexactly(int(length.group(1)))
        writer.write(wire)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(answer, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}/v1"
    finally:
        server.close()
        await server.wait_closed()


_PIECE = b"data: one\n\n"


@pytest.mark.parametrize(
    "head",
    [
        pytest.param(b"Content-Length: 40\r\nConnection: close\r\n", id="length-connection-close"),
        pytest.param(b"Content-Length: 40\r\n", id="length-keep-alive"),
        pytest.param(b"Transfer-Encoding: chunked\r\n", id="chunked"),
    ],
)
async def test_a_body_cut_short_is_a_transport_error(head):
    body = b"b\r\n" + _PIECE + b"\r\n" if b"chunked" in head else _PIECE
    async with (
        answering(b"HTTP/1.1 200 OK\r\n" + head + b"\r\n" + body) as url,
        HTTPSession(timeout=5) as session,
    ):
        response = await session.request("POST", url, json={})
        pieces = []
        with pytest.raises(TransportError, match=re.escape(f"POST {url}: IncompleteRead")):
            async for piece in response.iter_chunks():
                pieces.append(piece)

    assert b"".join(pieces) == _PIECE
    assert response.closed


@pytest.mark.parametrize(
    "head",
    [
        pytest.param(b"Content-Length: 11\r\n", id="length"),
        pytest.param(b"Connection: close\r\n", id="until-close"),
        # http.client closes the socket after the last byte, before the read that finds the end.
        pytest.param(b"Content-Length: 11\r\nConnection: close\r\n", id="length-and-close"),
    ],
)
async def test_a_body_that_arrives_whole_ends_the_pieces(head):
    async with (
        answering(b"HTTP/1.1 200 OK\r\n" + head + b"\r\n" + _PIECE) as url,
        HTTPSession(timeout=5) as session,
    ):
        response = await session.request("POST", url, json={})
        pieces = [piece async for piece in response.iter_chunks()]

    assert b"".join(pieces) == _PIECE
    assert response.closed


async def test_a_body_ending_with_the_connection_stopped_midway_raises_rather_than_ending():
    released = asyncio.Event()

    async def answer(reader, writer):
        head = await reader.readuntil(b"\r\n\r\n")
        length = re.search(rb"(?im)^content-length:[ \t]*(\d+)", head)
        if length:
            await reader.readexactly(int(length.group(1)))
        writer.write(b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\nfirst")
        await writer.drain()
        await reader.read()
        released.set()
        writer.close()

    server = await asyncio.start_server(answer, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        session = HTTPSession()
        response = await session.request("POST", f"http://127.0.0.1:{port}/v1", json={})
        task = asyncio.create_task(response.read())
        await asyncio.sleep(0.2)
        session.close()
        with pytest.raises(TransportError):
            await asyncio.wait_for(task, timeout=3)
        await asyncio.wait_for(released.wait(), timeout=3)
    finally:
        server.close()
        await server.wait_closed()


async def test_a_header_value_beyond_latin_1_goes_out_as_its_utf_8_bytes():
    heads = []

    async def answer(reader, writer):
        head = await reader.readuntil(b"\r\n\r\n")
        heads.append(head)
        writer.write(b"HTTP/1.1 204 No Content\r\nConnection: close\r\n\r\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(answer, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        async with HTTPSession(headers={"X-Title": "中文"}) as session:
            response = await session.request(
                "GET", f"http://127.0.0.1:{port}/v1", headers={"X-Name": "é"}
            )
            await response.read()
    finally:
        server.close()
        await server.wait_closed()

    assert "X-Title: 中文\r\n".encode() in heads[0]
    assert "X-Name: é\r\n".encode() in heads[0]


async def test_a_user_and_password_in_the_url_go_out_as_basic_credentials():
    heads = []

    async def answer(reader, writer):
        heads.append(await reader.readuntil(b"\r\n\r\n"))
        writer.write(b"HTTP/1.1 204 No Content\r\nConnection: close\r\n\r\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(answer, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        async with HTTPSession() as session:
            response = await session.request("GET", f"http://us%40er:p%3Ass@127.0.0.1:{port}/v1")
            await response.read()
    finally:
        server.close()
        await server.wait_closed()

    expected = base64.b64encode(b"us@er:p:ss").decode()
    assert f"Authorization: Basic {expected}\r\n".encode() in heads[0]
    assert heads[0].startswith(b"GET /v1 HTTP/1.1\r\n")
    assert b"us%40er" not in heads[0]


@pytest.mark.parametrize(
    ("url", "headers", "named"),
    [
        ("http://user:pw@127.0.0.1:9/v1", {"Authorization": "Bearer x"}, "not both"),
        ("http://us%3Aer:pw@127.0.0.1:9/v1", None, "Basic login"),
    ],
)
async def test_url_credentials_that_cannot_go_out_as_basic_are_refused_before_connecting(
    url, headers, named
):
    async with HTTPSession() as session:
        with pytest.raises(ValueError, match=named):
            await session.request("GET", url, headers=headers)


async def test_a_password_in_the_url_reaches_no_error_and_no_response():
    secret = "s3cr%40t"

    async def ok(request: Request):
        return json_response({})

    async with serving(ok) as base:
        authority = base.split("//", 1)[1].split("/", 1)[0]
        url = f"http://user:{secret}@{authority}/v1"
        async with HTTPSession(timeout=5) as session:
            response = await session.request("GET", url)
            await response.read()
    assert response.url == f"http://{authority}/v1"

    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    port = closed.getsockname()[1]
    closed.close()
    async with HTTPSession(timeout=5) as session:
        with pytest.raises(TransportError) as refused:
            await session.request("GET", f"http://user:{secret}@127.0.0.1:{port}/v1")
    assert f"127.0.0.1:{port}/v1" in str(refused.value)

    stalled = asyncio.Event()
    async with serving(_waits_for_close(stalled)) as base:
        authority = base.split("//", 1)[1].split("/", 1)[0]
        async with HTTPSession(timeout=0.3) as session:
            with pytest.raises(TimeoutError) as late:
                await session.request("GET", f"http://user:{secret}@{authority}/v1")
    assert f"{authority}/v1" in str(late.value)

    async with HTTPSession() as session:
        with pytest.raises(ValueError, match="not an http or https URL") as bad:
            await session.request("GET", f"ftp://user:{secret}@127.0.0.1/v1")
        with pytest.raises(ValueError, match="not an http or https URL") as bare:
            await session.request("GET", f"user:{secret}@127.0.0.1/v1")

    for raised in (refused, late, bad, bare):
        assert secret not in str(raised.value)
        assert "s3cr@t" not in str(raised.value)


async def test_an_endpoint_stream_cut_short_raises_rather_than_ending():
    whole = b'data: {"choices":[{"delta":{"content":"hel"}}]}\n\n'
    cut = b'data: {"choices":[{"delta":{"content":"lo'
    head = b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: %d\r\n\r\n"
    async with answering(head % (len(whole) + len(cut) + 20) + whole + cut) as url:
        endpoint = Endpoint(
            config=EndpointConfig(
                name="cut_stream",
                provider="openai",
                endpoint="chat",
                base_url=url,
                api_key="dummy-key-cut",
                allow_local_network=True,
                timeout=5,
                max_retries=1,
            )
        )
        chunks = []
        with pytest.raises(TransportError, match="IncompleteRead"):
            async for chunk in endpoint.stream({"model": "m", "messages": []}):
                chunks.append(chunk)

    # The cut event is never handed on as if it had arrived whole.
    assert [chunk.content for chunk in chunks] == ["hel"]


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


@pytest.mark.parametrize(
    ("url", "host", "port"),
    [
        ("http://[::1]/v1", "::1", 80),
        ("https://[2001:db8::1]/v1", "2001:db8::1", 443),
        ("http://[::1]:8443/v1", "::1", 8443),
        ("http://example.com/v1", "example.com", 80),
    ],
)
async def test_a_url_with_no_port_connects_to_its_host_on_the_schemes_port(
    monkeypatch, url, host, port
):
    seen = []

    def send(self, *args):
        seen.append((self.conn.host, self.conn.port))
        raise ConnectionRefusedError("not connecting")

    monkeypatch.setattr(_http._Exchange, "send", send)
    async with HTTPSession() as session:
        with pytest.raises(TransportError):
            await session.request("GET", url)

    assert seen == [(host, port)]


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


@pytest.mark.parametrize("stop", ["cancel", "close"])
async def test_a_request_stopped_in_its_tls_handshake_closes_its_connection(stop):
    gone = asyncio.Event()

    async def silent(reader, writer):
        # Reads the client's hello, never answers it, and waits for the client's end to close.
        await reader.read(65536)
        await reader.read()
        gone.set()
        writer.close()

    server = await asyncio.start_server(silent, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        session = HTTPSession(timeout=10)
        task = asyncio.create_task(session.request("GET", f"https://127.0.0.1:{port}/"))
        await asyncio.sleep(0.3)
        if stop == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=3)
        else:
            session.close()
            with pytest.raises(TransportError):
                await asyncio.wait_for(task, timeout=3)
        # The timeout is 10 s, so only the stop can have ended the handshake this soon.
        await asyncio.wait_for(gone.wait(), timeout=3)
        session.close()
    finally:
        server.close()
        await server.wait_closed()


def test_a_stop_right_after_the_tls_socket_is_published_fails_the_handshake_as_a_socket_error(
    monkeypatch,
):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        tls = _http._default_ssl_context()
        conn = http.client.HTTPSConnection("127.0.0.1", port, timeout=5, context=tls)
        exchange = _http._Exchange(conn, tls)
        publish = exchange._publish

        def publish_then_stop(sock):
            publish(sock)
            exchange.stop()

        monkeypatch.setattr(exchange, "_publish", publish_then_stop)
        start = time.monotonic()
        # A socket error, which the client reports as a transport failure, never an AttributeError.
        with pytest.raises(OSError):
            exchange.run(lambda: exchange.send("GET", "/", None, {}))
        # Well inside the socket's 5 s timeout, so the stop's shutdown ended the handshake.
        assert time.monotonic() - start < 2


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
    assert parts["model"].get_content_type() == "text/plain"
    assert parts["model"].get_content_charset() == "utf-8"
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
    assert error.headers.get("retry-after") == error.headers["RETRY-AFTER"] == "2"
    assert "retry-after" in error.headers
    assert error.headers.get("x-missing") is None
    assert (error.method, error.url) == ("POST", "https://api.test/v1")
    assert str(error) == "failed with status 429"


def test_a_status_error_reads_the_headers_of_a_response_in_any_case():
    message = http.client.parse_headers(io.BytesIO(b"Retry-After: 7\r\nX-Request-Id: r1\r\n\r\n"))
    error = HTTPStatusError("failed with status 503", status=503, headers=message)
    assert error.headers == {"Retry-After": "7", "X-Request-Id": "r1"}
    assert error.headers["retry-after"] == "7"
    assert error.headers.get("x-request-id") == "r1"
