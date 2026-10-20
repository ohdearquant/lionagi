# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The HTTP client the endpoints use: the standard library's ``http.client`` in worker threads.

Each request opens one connection and closes it once its response is read or released. Every
blocking step (connect and send, wait for the status line, read the body) runs in a worker
thread under a limiter of this module's own, so HTTP requests never take threads from the
default pool other code in the process waits on. A request that is cancelled, or that runs past
its session's timeout, shuts its socket down, which ends the blocked step and frees the thread;
a request whose connection is still being opened when it is stopped is never sent. A TLS
handshake is ended the same way, but the TCP connect before it cannot be, so the thread doing it
stays until the attempt ends: the time the request had left bounds the connect, but not the
address lookup before it, and such a thread no longer counts against ``MAX_REQUESTS_IN_FLIGHT``.

A URL's path and query are sent percent-encoded: a character that cannot appear in a request
line (a space, a non-ASCII letter) is encoded, and an escape already in the URL is kept.

Redirects are never followed, since the address guard checks one URL and a redirect could lead
anywhere; a 3xx response is returned like any other status. Proxy settings in the environment
are not read. The client sends no ``Accept-Encoding`` other than ``identity``, so response
bodies arrive uncompressed. A ``timeout`` of 0 or less sets no bound, as ``None`` does.
"""

from __future__ import annotations

import asyncio
import codecs
import http.client
import json as _json
import socket
import ssl
import threading
import time
import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from typing import Any, TypeVar
from urllib.parse import quote, urlencode, urlsplit

import anyio
import anyio.lowlevel
import anyio.to_thread

from lionagi.service.resilience import APIClientError
from lionagi.version import __version__

__all__ = (
    "HTTPResponse",
    "HTTPSession",
    "HTTPStatusError",
    "MultipartForm",
    "TransportError",
)

T = TypeVar("T")

#: The most HTTP requests in flight at once in one event loop. Each holds a worker thread while
#: it waits on the network, so this bounds those threads, apart from those still in the TCP
#: connect of a request that was stopped (module docstring); a request past it waits for a
#: slot, and the wait counts against its timeout.
MAX_REQUESTS_IN_FLIGHT = 256
_READ_SIZE = 65536
_USER_AGENT = f"lionagi/{__version__}"
# RFC 3986 pchar plus "/" and "%", and for the query "?" as well.
_PATH_SAFE = "/%:@!$&'()*+,;=-._~"
_QUERY_SAFE = _PATH_SAFE + "?"

_limiter_var: anyio.lowlevel.RunVar[anyio.CapacityLimiter] = anyio.lowlevel.RunVar(
    "lionagi_http_limiter"
)
_ssl_context: ssl.SSLContext | None = None
_ssl_context_lock = threading.Lock()


class TransportError(ConnectionError):
    """The request got no complete HTTP response: the connection could not be opened, it broke,
    or what came back was not HTTP."""


# What a request past its deadline raises. On Python 3.10 ``asyncio.TimeoutError`` is a class of
# its own, and ``aiohttp``'s timeout derived from it, so there this one derives from both.
if asyncio.TimeoutError is TimeoutError:
    _RequestTimeoutError = TimeoutError
else:  # Python 3.10

    class _RequestTimeoutError(TimeoutError, asyncio.TimeoutError):
        pass


class _ResponseHeaders(dict):
    """Header values by name as the server spelled it, looked up in any case."""

    def _name(self, name: object) -> object:
        if not isinstance(name, str) or dict.__contains__(self, name):
            return name
        lowered = name.lower()
        return next((key for key in self if key.lower() == lowered), name)

    def __getitem__(self, name: object) -> str:
        return dict.__getitem__(self, self._name(name))

    def __contains__(self, name: object) -> bool:
        return dict.__contains__(self, self._name(name))

    def get(self, name: object, default: Any = None) -> Any:
        return dict.get(self, self._name(name), default)


class HTTPStatusError(APIClientError):
    """The server answered with a status the caller treats as a failure. ``headers`` are its
    response headers, looked up in any case."""

    def __init__(
        self,
        message: str,
        *,
        status: int,
        headers: Mapping[str, str] | None = None,
        method: str = "",
        url: str = "",
    ):
        super().__init__(
            message, status_code=status, headers=_ResponseHeaders(_header_items(headers))
        )
        self.status = status
        self.method = method
        self.url = url


def _limiter() -> anyio.CapacityLimiter:
    try:
        return _limiter_var.get()
    except LookupError:
        limiter = anyio.CapacityLimiter(MAX_REQUESTS_IN_FLIGHT)
        _limiter_var.set(limiter)
        return limiter


def _default_ssl_context() -> ssl.SSLContext:
    global _ssl_context
    with _ssl_context_lock:
        if _ssl_context is None:
            _ssl_context = ssl.create_default_context()
        return _ssl_context


def _header_items(headers: Any) -> list[tuple[str, str]]:
    if headers is None:
        return []
    # A response's ``http.client.HTTPMessage`` is not a Mapping, and iterating it gives names only.
    if isinstance(headers, Mapping) or hasattr(headers, "items"):
        return list(headers.items())
    return [tuple(pair) for pair in headers]


def _merged_headers(*sources: Any) -> dict[str, str]:
    """Headers from each source in turn, a later name replacing an earlier one of any case."""
    merged: dict[str, tuple[str, str]] = {}
    for source in sources:
        for name, value in _header_items(source):
            merged[name.lower()] = (name, value)
    return dict(merged.values())


class MultipartForm:
    """A ``multipart/form-data`` body (RFC 7578): text fields and file parts.

    A file part takes bytes, or a file object that is read once when the body is built.
    """

    def __init__(self) -> None:
        self._fields: list[tuple[str, Any, str | None, str | None]] = []
        self.boundary = uuid.uuid4().hex

    def add_field(
        self,
        name: str,
        value: Any,
        *,
        filename: str | None = None,
        content_type: str | None = None,
    ) -> None:
        self._fields.append((name, value, filename, content_type))

    @property
    def content_type(self) -> str:
        return f"multipart/form-data; boundary={self.boundary}"

    @staticmethod
    def _quote(value: str) -> str:
        # As browsers write a form's names: a quote and a line break cannot end the parameter.
        return value.replace("\r", "%0D").replace("\n", "%0A").replace('"', "%22")

    def encode(self) -> bytes:
        parts: list[bytes] = []
        for name, value, filename, content_type in self._fields:
            disposition = f'form-data; name="{self._quote(name)}"'
            if filename is not None:
                disposition += f'; filename="{self._quote(filename)}"'
            if hasattr(value, "read"):
                value = value.read()
            if isinstance(value, str):
                value = value.encode("utf-8")
                # A text part says so, as aiohttp wrote it.
                content_type = content_type or "text/plain; charset=utf-8"
            elif not isinstance(value, (bytes, bytearray)):
                raise TypeError(f"form field {name!r} must be str, bytes or a file object")
            if self.boundary.encode() in value:
                raise ValueError(f"form field {name!r} contains the form's boundary")
            head = f"--{self.boundary}\r\nContent-Disposition: {disposition}\r\n"
            if content_type is not None:
                head += f"Content-Type: {content_type}\r\n"
            parts.append(head.encode("utf-8") + b"\r\n" + bytes(value) + b"\r\n")
        parts.append(f"--{self.boundary}--\r\n".encode())
        return b"".join(parts)


class _Exchange:
    """One connection, used by one worker thread at a time and stopped from any thread.

    The exchange keeps its own reference to the socket: once a response says the server will
    close the connection, ``http.client`` hands the socket to the response and the connection
    no longer holds it.
    """

    def __init__(self, conn: http.client.HTTPConnection, tls: ssl.SSLContext | None = None):
        self.conn = conn
        self._tls = tls
        self.sock: socket.socket | None = None
        self._response: http.client.HTTPResponse | None = None
        self._lock = threading.Lock()
        self._stopped = False
        self._busy = False

    def stop(self) -> None:
        """End the exchange: a step running now fails at once, and no request is sent after."""
        with self._lock:
            self._stopped = True
            if not self._busy:
                self._close()
            elif self.sock is not None:
                # A step blocked on the socket returns once it is shut down. The thread running
                # the step closes everything when it returns. The plain socket's shutdown: an SSL
                # socket's own also drops its TLS state, which a handshake starting on the other
                # thread would fail on with an AttributeError.
                try:
                    socket.socket.shutdown(self.sock, socket.SHUT_RDWR)
                except OSError:
                    pass

    def _close(self) -> None:
        if self._response is not None:
            self._response.close()
        self.conn.close()

    def run(self, step: Callable[[], T]) -> T:
        with self._lock:
            if self._stopped:
                raise TransportError("the request was stopped")
            self._busy = True
        try:
            return step()
        finally:
            with self._lock:
                self._busy = False
                if self._stopped:
                    self._close()

    def _publish(self, sock: socket.socket) -> None:
        with self._lock:
            self.sock = sock
            # Stopped while the connection was opened: nothing has been sent, and nothing is.
            if self._stopped:
                raise TransportError("the request was stopped")

    def send(self, method: str, target: str, body: bytes | None, headers: dict[str, str]):
        if self._tls is None:
            self.conn.connect()
            self._publish(self.conn.sock)
        else:
            # What HTTPSConnection.connect() does, with the TLS socket published before its
            # handshake, which waits on the server: stopping the request ends the handshake too.
            http.client.HTTPConnection.connect(self.conn)
            self.conn.sock = self._tls.wrap_socket(
                self.conn.sock, server_hostname=self.conn.host, do_handshake_on_connect=False
            )
            self._publish(self.conn.sock)
            self.conn.sock.do_handshake()
        self.conn.request(method, target, body=body, headers=headers)
        response = self.conn.getresponse()
        with self._lock:
            self._response = response
        return response


def _transport_failure(method: str, url: str, exc: BaseException) -> BaseException:
    if isinstance(exc, (TimeoutError, TransportError)):
        return exc
    if isinstance(exc, (OSError, http.client.HTTPException)):
        error = TransportError(f"{method} {url}: {type(exc).__name__}: {exc}")
        error.__cause__ = exc
        return error
    return exc


class HTTPResponse:
    """A response whose status line and headers have arrived; the body is read on demand."""

    def __init__(
        self,
        exchange: _Exchange,
        raw: http.client.HTTPResponse,
        *,
        method: str,
        url: str,
        deadline: float | None,
        timeout: float | None,
    ):
        self._exchange = exchange
        self._raw = raw
        self.method = method
        self.url = url
        self.status: int = raw.status
        self.reason: str = raw.reason
        self.headers: http.client.HTTPMessage = raw.headers
        self._deadline = deadline
        self._timeout = timeout
        self.closed = False

    async def _step(self, step: Callable[[], T]) -> T:
        return await _run_step(
            self._exchange,
            step,
            method=self.method,
            url=self.url,
            deadline=self._deadline,
            timeout=self._timeout,
        )

    async def read(self) -> bytes:
        """The whole body; the connection is closed once it is read."""
        try:
            return await self._step(self._raw.read)
        finally:
            self.close()

    def _charset(self) -> str | None:
        """The codec of the charset ``Content-Type`` declares; ``None`` when it declares none
        Python knows."""
        charset = self.headers.get_content_charset()
        if charset:
            try:
                return codecs.lookup(charset).name
            except (LookupError, ValueError):
                pass
        return None

    async def text(self, encoding: str | None = None) -> str:
        """The body as text, in *encoding*, else the declared charset, else UTF-8."""
        body = await self.read()
        return body.decode(encoding or self._charset() or "utf-8", errors="replace")

    async def json(self) -> Any:
        """The body parsed as JSON, decoded with the charset the response declares; ``None`` when
        the body is empty or only whitespace."""
        body = await self.read()
        if not body.strip():
            return None
        charset = self._charset()
        # Without a declared charset, or with UTF-8, the parser detects UTF-8 (a byte-order mark
        # too), UTF-16 and UTF-32 itself.
        return _json.loads(body if charset in (None, "utf-8") else body.decode(charset))

    def _read_piece(self) -> bytes:
        chunk = self._raw.read1(_READ_SIZE)
        if not chunk and self._raw.length:
            # read1 answers b"" both at the body's end and when the peer closes early; with part of
            # the declared Content-Length still owed the body was cut, which read() reports too.
            raise http.client.IncompleteRead(b"", self._raw.length)
        return chunk

    async def iter_chunks(self) -> AsyncIterator[bytes]:
        """The body in the pieces it arrives in, as bytes; a piece can end inside a line or a
        character. A body that ends before its declared ``Content-Length`` raises
        ``TransportError``, as ``read()`` does."""
        try:
            while True:
                chunk = await self._step(self._read_piece)
                if not chunk:
                    return
                yield chunk
        finally:
            self.close()

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self._exchange.stop()

    release = close


async def _run_step(
    exchange: _Exchange,
    step: Callable[[], T],
    *,
    method: str,
    url: str,
    deadline: float | None,
    timeout: float | None,
) -> T:
    def blocking() -> T:
        if deadline is not None:
            # Bounds each blocking socket operation, opening the connection included, by the time
            # the request has left; time spent waiting for a slot is not left any more.
            left = max(deadline - time.monotonic(), 0.001)
            exchange.conn.timeout = left
            sock = exchange.sock
            # http.client closes the socket itself once a body that ends with the connection has
            # been read whole; the read after that answers from the closed response.
            if sock is not None and sock.fileno() != -1:
                sock.settimeout(left)
        return exchange.run(step)

    try:
        if deadline is None:
            return await anyio.to_thread.run_sync(
                blocking, abandon_on_cancel=True, limiter=_limiter()
            )
        with anyio.fail_after(max(deadline - time.monotonic(), 0)):
            return await anyio.to_thread.run_sync(
                blocking, abandon_on_cancel=True, limiter=_limiter()
            )
    except TimeoutError:
        exchange.stop()
        raise _RequestTimeoutError(f"{method} {url} did not complete within {timeout} s") from None
    except BaseException as exc:
        # Cancelled, or the step failed: either way the exchange is over.
        exchange.stop()
        failure = _transport_failure(method, url, exc)
        if failure is exc:
            raise
        raise failure from exc


class HTTPSession:
    """Default headers and a per-request timeout, shared by the requests made through it.

    ``timeout`` bounds a request from its start until its response has been read, a streamed
    response included; ``None``, or a value of 0 or less, sets no bound. Closing the session
    closes the responses still open and ends the requests still waiting for theirs.
    """

    def __init__(self, *, timeout: float | None = None, headers: Any = None):
        # aiohttp armed no timer for a total of 0 or less either.
        self.timeout = timeout if timeout is not None and timeout > 0 else None
        self.headers = headers
        self._responses: list[HTTPResponse] = []
        self._exchanges: list[_Exchange] = []

    async def __aenter__(self) -> HTTPSession:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        for response in self._responses:
            response.close()
        for exchange in self._exchanges:
            exchange.stop()
        self._responses.clear()
        self._exchanges.clear()

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Any = None,
        json: Any = None,
        data: bytes | str | MultipartForm | None = None,
        params: Mapping[str, Any] | None = None,
        allow_redirects: bool = False,
    ) -> HTTPResponse:
        if allow_redirects:
            raise ValueError("this client does not follow redirects")
        if json is not None and data is not None:
            raise ValueError("pass json or data, not both")
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError(f"not an http or https URL: {url!r}")

        send_headers = _merged_headers(
            {"User-Agent": _USER_AGENT, "Accept": "*/*"}, self.headers, headers
        )
        names = {name.lower() for name in send_headers}
        body: bytes | None
        if json is not None:
            body = _json.dumps(json).encode("utf-8")
            if "content-type" not in names:
                send_headers["Content-Type"] = "application/json"
        elif isinstance(data, MultipartForm):
            body = data.encode()
            send_headers = _merged_headers(send_headers, {"Content-Type": data.content_type})
        elif isinstance(data, str):
            body = data.encode("utf-8")
        else:
            body = data
        if body is not None and "content-length" not in names:
            send_headers["Content-Length"] = str(len(body))

        # Encoded as a browser sends them: what a request line cannot carry is percent-encoded,
        # and an escape already in the URL stays as it is.
        target = quote(parts.path or "/", safe=_PATH_SAFE)
        query = quote(parts.query, safe=_QUERY_SAFE)
        if params:
            extra = urlencode(list(params.items()))
            query = f"{query}&{extra}" if query else extra
        if query:
            target += f"?{query}"
        deadline = None if self.timeout is None else time.monotonic() + self.timeout
        tls = _default_ssl_context() if parts.scheme == "https" else None
        # Given no port, http.client reads one off the host after its last colon, which in an
        # IPv6 literal is part of the address.
        port = parts.port if parts.port is not None else (443 if tls is not None else 80)
        if tls is not None:
            conn: http.client.HTTPConnection = http.client.HTTPSConnection(
                parts.hostname, port, timeout=self.timeout, context=tls
            )
        else:
            conn = http.client.HTTPConnection(parts.hostname, port, timeout=self.timeout)
        exchange = _Exchange(conn, tls)
        self._exchanges.append(exchange)
        method = method.upper()
        raw = await _run_step(
            exchange,
            lambda: exchange.send(method, target, body, send_headers),
            method=method,
            url=url,
            deadline=deadline,
            timeout=self.timeout,
        )
        response = HTTPResponse(
            exchange, raw, method=method, url=url, deadline=deadline, timeout=self.timeout
        )
        self._responses.append(response)
        return response


def check_client_options(options: Mapping[str, Any]) -> None:
    """Refuse a client option this client does not have: it takes only ``headers``."""
    unknown = sorted(name for name in options if name != "headers")
    if unknown:
        raise TypeError(
            f"client_kwargs supports only 'headers'; got {', '.join(map(repr, unknown))}"
        )
