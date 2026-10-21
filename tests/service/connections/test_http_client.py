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
from contextlib import asynccontextmanager, contextmanager

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
            # The class aiohttp's timeout was, which on Python 3.10 is not TimeoutError.
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


def _trickles(pieces: int, gap: float):
    async def handler(request: Request):
        response = StreamResponse()
        await response.prepare(request)
        for _ in range(pieces):
            await response.write(b"x")
            await asyncio.sleep(gap)
        return response

    return handler


@framings
async def test_a_body_that_keeps_arriving_still_ends_at_the_timeout(announce_close):
    # Each piece arrives well inside the timeout, so only the bound on the whole request ends
    # the read before the server's three seconds are up.
    async with serving(_trickles(15, 0.2), announce_close) as base:
        async with HTTPSession(timeout=0.5) as session:
            started = time.monotonic()
            response = await session.request("GET", base)
            with pytest.raises(TimeoutError, match="did not complete within 0.5 s"):
                await response.read()
            assert time.monotonic() - started < 1.5


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


@contextmanager
def _connect_never_answers(monkeypatch):
    """A local address whose TCP connect stays in progress: each poll for it waits its timeout and
    finds nothing. Yields the address and the list the polls append their times to."""
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
    try:
        yield target, polls
    finally:
        done.set()


async def test_the_wait_for_a_slot_counts_against_the_deadline_that_ends_a_connect(monkeypatch):
    # One slot, held here for half a second; the request's host never answers its connect. Its
    # one-second deadline counts from its start, the wait included, so the connect gets the half
    # second the wait left.
    monkeypatch.setattr(_http, "MAX_REQUESTS_IN_FLIGHT", 1)
    with _connect_never_answers(monkeypatch) as (target, polls):
        limiter = _http._limiter()
        assert limiter.total_tokens == 1
        await limiter.acquire()
        held = True
        try:
            async with HTTPSession(timeout=1) as session:
                started = time.monotonic()
                task = asyncio.create_task(
                    session.request("GET", f"http://{target[0]}:{target[1]}/v1")
                )
                await asyncio.sleep(0.5)
                # Still waiting for the slot: the connect has not started.
                assert not polls
                limiter.release()
                held = False
                with pytest.raises(TimeoutError, match="did not complete within 1 s"):
                    await asyncio.wait_for(task, timeout=3)
                ended = time.monotonic() - started
        finally:
            if held:
                limiter.release()
    assert polls
    assert 0.9 < ended < 1.4


@pytest.mark.parametrize("stop", ["timeout", "cancel", "close"])
async def test_a_tcp_connect_in_progress_stops_soon_after_its_request_is_stopped(monkeypatch, stop):
    with _connect_never_answers(monkeypatch) as (target, polls):
        url = f"http://{target[0]}:{target[1]}/v1"
        session = HTTPSession(timeout=0.3 if stop == "timeout" else None)
        started = time.monotonic()
        task = asyncio.create_task(session.request("GET", url))
        if stop == "timeout":
            with pytest.raises(TimeoutError, match="did not complete within 0.3 s"):
                await asyncio.wait_for(task, timeout=3)
            assert time.monotonic() - started < 1
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


async def test_a_host_whose_first_address_never_answers_is_reached_at_its_next(monkeypatch):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        silent = ("127.0.0.1", probe.getsockname()[1])
    lookup = socket.getaddrinfo
    connect = socket.socket.connect
    real_selector = selectors.DefaultSelector
    held = []

    def in_progress(sock, address):
        # A host that does not answer: the connect never completes.
        if address == silent:
            held.append(sock)
            raise BlockingIOError("connecting")
        return connect(sock, address)

    class SilentNeverReady:
        """The real selector, except that the silent address's socket is never ready."""

        def __init__(self):
            self._real = real_selector()

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            self._real.close()
            return False

        def register(self, sock, events):
            if sock not in held:
                self._real.register(sock, events)

        def unregister(self, sock):
            self._real.unregister(sock)

        def select(self, timeout):
            if not self._real.get_map():
                time.sleep(timeout)
                return []
            return self._real.select(timeout)

    async def handler(request: Request):
        return json_response({"ok": True})

    async with serving(handler) as base:
        port = int(base.rsplit(":", 1)[1].split("/", 1)[0])

        def two_addresses(host, *args, **kwargs):
            if host != "two.test":
                return lookup(host, *args, **kwargs)
            tcp = (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "")
            return [(*tcp, silent), (*tcp, ("127.0.0.1", port))]

        monkeypatch.setattr(socket, "getaddrinfo", two_addresses)
        monkeypatch.setattr(socket.socket, "connect", in_progress)
        monkeypatch.setattr(selectors, "DefaultSelector", SilentNeverReady)
        async with HTTPSession(timeout=5) as session:
            started = time.monotonic()
            response = await session.request("GET", f"http://two.test:{port}/v1")
            assert await response.json() == {"ok": True}
            elapsed = time.monotonic() - started

    # The silent address was tried first, the next started after the stagger rather than after
    # the deadline, and the connect left in progress was closed once the next one connected.
    assert len(held) == 1
    assert _http._CONNECT_STAGGER * 0.8 < elapsed < 2
    assert held[0].fileno() == -1


@pytest.mark.skipif(not socket.has_ipv6, reason="no IPv6 sockets in this build")
async def test_a_hosts_addresses_are_tried_alternating_their_families(monkeypatch):
    tried = []

    def refuse(sock, address):
        tried.append(address[0])
        raise ConnectionRefusedError("refused")

    def lookup(host, port, *args, **kwargs):
        v6 = (socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "")
        v4 = (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "")
        return [(*v6, ("::1", port, 0, 0)), (*v6, ("::2", port, 0, 0)), (*v4, ("127.0.0.1", port))]

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", lookup)
    async with HTTPSession(timeout=5) as session:
        with pytest.raises(TransportError):
            await session.request("GET", "http://many.test:8080/v1")
    assert tried == ["::1", "127.0.0.1", "::2"]


async def test_a_refused_ipv6_connection_names_its_host_in_brackets(monkeypatch):
    def send(self, *args):
        raise ConnectionRefusedError("refused")

    monkeypatch.setattr(_http._Exchange, "send", send)
    async with HTTPSession(timeout=5) as session:
        with pytest.raises(TransportError) as refused:
            await session.request("GET", "http://[::1]:8443/v1")
    assert str(refused.value).startswith("GET http://[::1]:8443: ")


async def test_a_refused_connection_is_a_transport_error():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    url = f"http://127.0.0.1:{port}/v1"
    async with HTTPSession(timeout=5) as session:
        with pytest.raises(TransportError) as caught:
            await session.request("POST", url, json={})
    # Named by method, scheme, host and port, as aiohttp's connect error named host and port.
    assert str(caught.value).startswith(f"POST http://127.0.0.1:{port}: ConnectionRefusedError")
    assert "/v1" not in str(caught.value)
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
        origin = url.removesuffix("/v1")
        with pytest.raises(TransportError, match=re.escape(f"POST {origin}: IncompleteRead")):
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
        ("http://user:pw@127.0.0.1:9/v1", {"authorization": "Bearer x"}, "not both"),
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
    assert str(refused.value).startswith(f"GET http://127.0.0.1:{port}: ")

    stalled = asyncio.Event()
    async with serving(_waits_for_close(stalled)) as base:
        authority = base.split("//", 1)[1].split("/", 1)[0]
        async with HTTPSession(timeout=0.3) as session:
            with pytest.raises(TimeoutError) as late:
                await session.request("GET", f"http://user:{secret}@{authority}/v1")
    assert str(late.value) == f"GET http://{authority} did not complete within 0.3 s"

    async with HTTPSession() as session:
        with pytest.raises(ValueError, match="not an http or https URL") as bad:
            await session.request("GET", f"ftp://user:{secret}@127.0.0.1/v1")
        with pytest.raises(ValueError, match="not an http or https URL") as bare:
            await session.request("GET", f"user:{secret}@127.0.0.1/v1")

    for raised in (refused, late, bad, bare):
        assert secret not in str(raised.value)
        assert "s3cr@t" not in str(raised.value)


@pytest.mark.parametrize(
    "url",
    [
        # A raw "/", "?" or "#" ends the authority (RFC 3986 section 3.2), so the password's
        # lead is read as a port, and as no number the URL does not parse.
        "http://user:Lk3d/Zq9w@127.0.0.1:9/v1",
        "http://user:Lk3d?Zq9w@127.0.0.1:9/v1",
        "http://user:Lk3d#Zq9w@127.0.0.1:9/v1",
        # Characters NFKC folds into "@" and ":", which urlsplit refuses in an authority.
        "http://user:Lk3d\uff20Zq9w@127.0.0.1:9/v1",
        "http://user:Lk3d\uff1aZq9w@127.0.0.1:9/v1",
        # Basic credentials go out as latin-1.
        "http://user:Lk3d\u4e2dZq9w@127.0.0.1:9/v1",
        # No "scheme://": the text before the first colon, which urlsplit takes for the scheme,
        # is the user.
        "Lk3d:Zq9w@127.0.0.1/v1",
        # A backslash in the authority, which RFC 3986 does not allow and a parser that reads it
        # as the end of the host takes for a different host.
        "http://Lk3d\\:Zq9w@127.0.0.1:9/v1",
    ],
)
async def test_a_url_refused_before_connecting_quotes_none_of_its_credentials(url):
    async with HTTPSession() as session:
        with pytest.raises(ValueError) as refused:
            await session.request("GET", url)

    error = refused.value
    assert type(error) is ValueError
    # Compared in lower case: urlsplit lowercases what it takes for the scheme.
    for text in (str(error).lower(), repr(error).lower()):
        for fragment in ("lk3d", "zq9w", "\u4e2d", "4e2d", "position"):
            assert fragment not in text
    # Nor through the error the refusal could have been raised from.
    assert error.__context__ is None
    assert error.__cause__ is None


async def test_a_raw_slash_after_a_numeric_lead_ends_the_authority():
    # "http://user:123/Zq9w@host/v1" names host "user", port 123 and the path "/Zq9w@host/v1":
    # RFC 3986 gives it no user or password, and a password holding "/", "?" or "#" has to be
    # percent-encoded. The same shape with a host that answers shows the reading.
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
            response = await session.request("GET", f"http://127.0.0.1:{port}/Zq9w@host.invalid/v1")
            await response.read()
    finally:
        server.close()
        await server.wait_closed()

    assert heads[0].startswith(b"GET /Zq9w@host.invalid/v1 HTTP/1.1\r\n")
    assert b"Authorization" not in heads[0]
    assert response.url == f"http://127.0.0.1:{port}/Zq9w@host.invalid/v1"


@pytest.mark.parametrize(
    "tail", ["/Zq9w@127.0.0.1/v1", "/v1?key=Zq9w"], ids=["password-tail-in-path", "key-in-query"]
)
async def test_a_refused_connection_names_neither_the_path_nor_the_query(tail):
    # The path can hold the rest of a password with a raw "/" after a numeric start, and the
    # query can hold a key.
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    port = closed.getsockname()[1]
    closed.close()
    async with HTTPSession(timeout=5) as session:
        with pytest.raises(TransportError) as refused:
            await session.request("GET", f"http://127.0.0.1:{port}{tail}")
    assert str(refused.value).startswith(f"GET http://127.0.0.1:{port}: ")
    assert "zq9w" not in str(refused.value).lower()


async def test_an_endpoint_refuses_a_url_that_does_not_parse_without_quoting_it():
    # "\uff20" is a character NFKC folds into "@", which urlsplit refuses in an authority, quoting
    # the authority, password included.
    endpoint = Endpoint(
        config=EndpointConfig(
            name="unparsed",
            provider="unparsed",
            base_url="http://user:Lk3d\uff20Zq9w@127.0.0.1:9",
            endpoint="v1",
            method="GET",
            auth_type="none",
            allow_local_network=True,
            timeout=5,
            max_retries=1,
        )
    )
    with pytest.raises(ValueError) as refused:
        await endpoint.call({})

    error = refused.value
    assert str(error) == "the URL does not parse (not shown: it can hold a password)"
    assert "zq9w" not in repr(error).lower()
    assert error.__context__ is None
    assert error.__cause__ is None


async def test_a_percent_encoded_slash_question_mark_and_hash_stay_in_the_password():
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
            response = await session.request(
                "GET", f"http://user:Lk3d%2FZq%3F9w%23@127.0.0.1:{port}/v1"
            )
            await response.read()
    finally:
        server.close()
        await server.wait_closed()

    expected = base64.b64encode(b"user:Lk3d/Zq?9w#").decode()
    assert f"Authorization: Basic {expected}\r\n".encode() in heads[0]
    assert heads[0].startswith(b"GET /v1 HTTP/1.1\r\n")
    assert response.url == f"http://127.0.0.1:{port}/v1"


@pytest.mark.parametrize("headers", [None, {"Authorization": "Bearer x"}], ids=["none", "bearer"])
async def test_an_empty_user_with_no_password_is_no_credentials(headers):
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
            response = await session.request("GET", f"http://@127.0.0.1:{port}/v1", headers=headers)
            await response.read()
    finally:
        server.close()
        await server.wait_closed()

    sent = [line for line in heads[0].split(b"\r\n") if line.lower().startswith(b"authorization:")]
    assert sent == ([] if headers is None else [b"Authorization: Bearer x"])


async def test_a_session_holds_only_the_requests_and_responses_still_open():
    async def ok(request: Request):
        return json_response({})

    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    refused = f"http://127.0.0.1:{closed.getsockname()[1]}/v1"
    closed.close()
    async with serving(ok) as base:
        async with HTTPSession() as session:
            for _ in range(50):
                response = await session.request("GET", base)
                assert session._responses == {response}
                await response.read()
                assert not session._responses
                assert not session._exchanges
            with pytest.raises(TransportError):
                await session.request("GET", refused)
            assert not session._exchanges


async def test_a_closed_session_refuses_a_request():
    session = HTTPSession()
    session.close()
    with pytest.raises(RuntimeError, match="the session is closed"):
        await session.request("GET", "http://127.0.0.1:9/v1")


@framings
async def test_a_body_read_as_a_stream_is_not_read_again_as_if_stopped(announce_close):
    async def ok(request: Request):
        return json_response({"a": 1})

    async with serving(ok, announce_close) as base:
        async with HTTPSession() as session:
            streamed = await session.request("GET", base)
            pieces = [chunk async for chunk in streamed.iter_chunks()]
            with pytest.raises(TransportError, match="already read as a stream"):
                await streamed.read()

            read = await session.request("GET", base)
            body = await read.read()
            assert [chunk async for chunk in read.iter_chunks()] == [body]
    assert b"".join(pieces) == body


_WHOLE = b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nhello"
_CUT = b"HTTP/1.1 200 OK\r\nContent-Length: 40\r\n\r\nhello"


async def _consume(response, how):
    if how == "read":
        return await response.read()
    return [chunk async for chunk in response.iter_chunks()]


@pytest.mark.parametrize(
    ("wire", "first", "then", "message"),
    [
        pytest.param(_WHOLE, "iter", "iter", "already read as a stream", id="whole-iter-iter"),
        pytest.param(_CUT, "iter", "iter", "already read as a stream", id="cut-iter-iter"),
        pytest.param(_CUT, "iter", "read", "already read as a stream", id="cut-iter-read"),
        pytest.param(_CUT, "read", "read", "earlier read of the body did not", id="cut-read-read"),
        pytest.param(_CUT, "read", "iter", "earlier read of the body did not", id="cut-read-iter"),
    ],
)
async def test_a_body_read_again_after_it_ended_or_failed_says_so_not_stopped(
    wire, first, then, message
):
    async with answering(wire) as url, HTTPSession(timeout=5) as session:
        response = await session.request("POST", url, json={})
        if wire is _CUT:
            with pytest.raises(TransportError, match="IncompleteRead"):
                await _consume(response, first)
        else:
            await _consume(response, first)
        with pytest.raises(TransportError, match=message):
            await _consume(response, then)


async def test_a_streamed_empty_body_reads_back_as_empty():
    async with (
        answering(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n") as url,
        HTTPSession(timeout=5) as session,
    ):
        response = await session.request("POST", url, json={})
        assert await _consume(response, "iter") == []
        assert await response.read() == b""


async def test_two_reads_at_once_both_return_the_body():
    async with answering(_WHOLE) as url, HTTPSession(timeout=5) as session:
        for _ in range(10):
            response = await session.request("POST", url, json={})
            assert await asyncio.gather(response.read(), response.read()) == [b"hello"] * 2


async def test_a_stream_started_while_a_read_waits_for_the_body_is_refused():
    sent = asyncio.Event()

    async def answer(reader, writer):
        head = await reader.readuntil(b"\r\n\r\n")
        length = re.search(rb"(?im)^content-length:[ \t]*(\d+)", head)
        if length:
            await reader.readexactly(int(length.group(1)))
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\n")
        await writer.drain()
        await sent.wait()
        writer.write(b"hello")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(answer, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        async with HTTPSession(timeout=5) as session:
            response = await session.request("POST", f"http://127.0.0.1:{port}/v1", json={})
            reading = asyncio.create_task(response.read())
            # The read is in its worker thread, waiting for the body.
            await asyncio.sleep(0.2)
            with pytest.raises(TransportError, match="already read as a stream"):
                await _consume(response, "iter")
            sent.set()
            assert await asyncio.wait_for(reading, timeout=3) == b"hello"
    finally:
        sent.set()
        server.close()
        await server.wait_closed()


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
