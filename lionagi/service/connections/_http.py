# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The HTTP client the endpoints use: the standard library's ``http.client`` in worker threads.

Each request opens one connection and closes it once its response is read or released. Every
blocking step (connect and send, wait for the status line, read the body) runs in a worker
thread under a limiter of this module's own, so HTTP requests never take threads from the
default pool other code in the process waits on. A request that is cancelled, or that runs past
its session's timeout, shuts its socket down, which ends the blocked step and frees the thread;
a request whose connection is still being opened when it is stopped is never sent.

Redirects are never followed, since the address guard checks one URL and a redirect could lead
anywhere; a 3xx response is returned like any other status. Proxy settings in the environment
are not read. The client sends no ``Accept-Encoding`` other than ``identity``, so response
bodies arrive uncompressed.
"""

from __future__ import annotations

import http.client
import json as _json
import socket
import ssl
import threading
import time
import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from typing import Any, TypeVar
from urllib.parse import urlencode, urlsplit

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
#: it waits on the network, so this bounds those threads; a request past it waits for a slot,
#: and the wait counts against its timeout.
MAX_REQUESTS_IN_FLIGHT = 256
_READ_SIZE = 65536
_USER_AGENT = f"lionagi/{__version__}"

_limiter_var: anyio.lowlevel.RunVar[anyio.CapacityLimiter] = anyio.lowlevel.RunVar(
    "lionagi_http_limiter"
)
_ssl_context: ssl.SSLContext | None = None
_ssl_context_lock = threading.Lock()


class TransportError(ConnectionError):
    """The request got no complete HTTP response: the connection could not be opened, it broke,
    or what came back was not HTTP."""


class HTTPStatusError(APIClientError):
    """The server answered with a status the caller treats as a failure."""

    def __init__(
        self,
        message: str,
        *,
        status: int,
        headers: Mapping[str, str] | None = None,
        method: str = "",
        url: str = "",
    ):
        super().__init__(message, status_code=status, headers=dict(headers or {}))
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
    if isinstance(headers, Mapping):
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
            head = f"--{self.boundary}\r\nContent-Disposition: {disposition}\r\n"
            if content_type is not None:
                head += f"Content-Type: {content_type}\r\n"
            if hasattr(value, "read"):
                value = value.read()
            if isinstance(value, str):
                value = value.encode("utf-8")
            elif not isinstance(value, (bytes, bytearray)):
                raise TypeError(f"form field {name!r} must be str, bytes or a file object")
            if self.boundary.encode() in value:
                raise ValueError(f"form field {name!r} contains the form's boundary")
            parts.append(head.encode("utf-8") + b"\r\n" + bytes(value) + b"\r\n")
        parts.append(f"--{self.boundary}--\r\n".encode())
        return b"".join(parts)


class _Exchange:
    """One connection, used by one worker thread at a time and stopped from any thread.

    The exchange keeps its own reference to the socket: once a response says the server will
    close the connection, ``http.client`` hands the socket to the response and the connection
    no longer holds it.
    """

    def __init__(self, conn: http.client.HTTPConnection):
        self.conn = conn
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
                # the step closes everything when it returns.
                try:
                    self.sock.shutdown(socket.SHUT_RDWR)
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

    def send(self, method: str, target: str, body: bytes | None, headers: dict[str, str]):
        self.conn.connect()
        with self._lock:
            self.sock = self.conn.sock
            # Stopped while the connection was opened: nothing has been sent, and nothing is.
            if self._stopped:
                raise TransportError("the request was stopped")
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

    async def text(self, encoding: str = "utf-8") -> str:
        return (await self.read()).decode(encoding, errors="replace")

    async def json(self) -> Any:
        return _json.loads(await self.read())

    async def iter_chunks(self) -> AsyncIterator[bytes]:
        """The body in the pieces it arrives in, as bytes; a piece can end inside a line or a
        character."""
        try:
            while True:
                chunk = await self._step(lambda: self._raw.read1(_READ_SIZE))
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
            # Bounds each blocking socket operation by the time the request has left.
            sock = exchange.sock
            if sock is not None:
                sock.settimeout(max(deadline - time.monotonic(), 0.001))
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
        raise TimeoutError(f"{method} {url} did not complete within {timeout} s") from None
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
    response included; ``None`` sets no bound. Responses still open when the session closes
    are closed with it.
    """

    def __init__(self, *, timeout: float | None = None, headers: Any = None):
        self.timeout = timeout
        self.headers = headers
        self._responses: list[HTTPResponse] = []

    async def __aenter__(self) -> HTTPSession:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        for response in self._responses:
            response.close()
        self._responses.clear()

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

        send_headers = _merged_headers({"User-Agent": _USER_AGENT}, self.headers, headers)
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

        target = parts.path or "/"
        query = parts.query
        if params:
            extra = urlencode(list(params.items()))
            query = f"{query}&{extra}" if query else extra
        if query:
            target += f"?{query}"
        deadline = None if self.timeout is None else time.monotonic() + self.timeout
        if parts.scheme == "https":
            conn: http.client.HTTPConnection = http.client.HTTPSConnection(
                parts.hostname, parts.port, timeout=self.timeout, context=_default_ssl_context()
            )
        else:
            conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=self.timeout)
        exchange = _Exchange(conn)
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
