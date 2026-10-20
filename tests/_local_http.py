# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""A small asyncio HTTP/1.1 server for wire-level tests of the HTTP client.

The server runs on the test's own event loop. The client does its blocking I/O
in worker threads, so a handler can await events the test sets. One request is
served per connection, which is how the client connects. A response says
``Connection: close`` unless the server is made with ``announce_close=False``;
the connection is closed after the response either way.
"""

from __future__ import annotations

import asyncio
import io
import json
import re
from collections.abc import Awaitable, Callable
from http import HTTPStatus
from http.client import HTTPMessage, parse_headers
from typing import Any
from urllib.parse import parse_qsl, urlsplit

__all__ = (
    "Application",
    "LocalServer",
    "Redirect",
    "Request",
    "Response",
    "StreamResponse",
    "json_response",
)

Handler = Callable[["Request"], Awaitable["Response | StreamResponse"]]


class Request:
    def __init__(
        self,
        method: str,
        target: str,
        headers: HTTPMessage,
        body: bytes,
        reader,
        writer,
        announce_close: bool = True,
    ):
        parts = urlsplit(target)
        self.method = method
        self.path = parts.path
        self.query = dict(parse_qsl(parts.query, keep_blank_values=True))
        self.headers = headers
        self.match_info: dict[str, str] = {}
        self._body = body
        self._reader = reader
        self._writer = writer
        self._announce_close = announce_close

    async def read(self) -> bytes:
        return self._body

    async def client_closed(self) -> None:
        """Return once the client has closed its end of the connection. A client that closes
        with response data still unread resets the connection rather than ending it."""
        try:
            while await self._reader.read(65536):
                pass
        except ConnectionResetError:
            pass

    async def text(self) -> str:
        return self._body.decode()

    async def json(self) -> Any:
        return json.loads(self._body)

    async def form(self) -> dict[str, bytes | str]:
        """Parse a multipart/form-data body: file parts as bytes, the rest as str."""
        match = re.search(r'boundary="?([^";]+)"?', self.headers.get("Content-Type", ""))
        if match is None:
            raise ValueError("request is not multipart/form-data")
        delimiter = b"--" + match.group(1).encode()
        fields: dict[str, bytes | str] = {}
        for chunk in self._body.split(delimiter)[1:]:
            if chunk.startswith(b"--"):
                break
            head, _, value = chunk[2:].partition(b"\r\n\r\n")
            value = value.removesuffix(b"\r\n")
            disposition = parse_headers(io.BytesIO(head + b"\r\n\r\n"))["Content-Disposition"]
            name = re.search(r'\bname="([^"]*)"', disposition).group(1)
            is_file = re.search(r'\bfilename="', disposition) is not None
            fields[name] = value if is_file else value.decode()
        return fields


class Response:
    def __init__(
        self,
        *,
        status: int = 200,
        body: bytes = b"",
        text: str | None = None,
        content_type: str | None = None,
        headers: dict[str, str] | None = None,
    ):
        self.status = status
        self.body = text.encode() if text is not None else body
        self.headers = dict(headers or {})
        if content_type is not None:
            self.headers["Content-Type"] = content_type


def json_response(data: Any, *, status: int = 200, headers: dict[str, str] | None = None):
    return Response(
        status=status,
        body=json.dumps(data).encode(),
        content_type="application/json",
        headers=headers,
    )


class Redirect(Exception):
    """Raised by a handler to answer with a redirect to ``location``."""

    def __init__(self, location: str, status: int = 307):
        super().__init__(location)
        self.response = Response(status=status, headers={"Location": location})


def _status_line(status: int) -> bytes:
    try:
        reason = HTTPStatus(status).phrase
    except ValueError:
        reason = "Unknown"
    return f"HTTP/1.1 {status} {reason}\r\n".encode()


def _header_block(headers: dict[str, str]) -> bytes:
    lines = "".join(f"{name}: {value}\r\n" for name, value in headers.items())
    return lines.encode("latin-1") + b"\r\n"


class StreamResponse:
    """A chunked response the handler writes piece by piece after ``prepare``."""

    def __init__(self, *, status: int = 200, headers: dict[str, str] | None = None):
        self.status = status
        self.headers = dict(headers or {})
        self._writer = None
        self._eof = False

    async def prepare(self, request: Request) -> None:
        self._writer = request._writer
        headers = {**self.headers, "Transfer-Encoding": "chunked"}
        if request._announce_close:
            headers["Connection"] = "close"
        self._writer.write(_status_line(self.status) + _header_block(headers))
        await self._writer.drain()

    async def write(self, data: bytes) -> None:
        if data:
            self._writer.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
            await self._writer.drain()

    async def write_eof(self) -> None:
        if not self._eof:
            self._eof = True
            self._writer.write(b"0\r\n\r\n")
            await self._writer.drain()


def _compile(pattern: str) -> re.Pattern[str]:
    def group(match: re.Match[str]) -> str:
        name, _, expr = match.group(1).partition(":")
        return f"(?P<{name}>{expr or '[^/]+'})"

    pieces = re.split(r"(\{[^}]+\})", pattern)
    regex = "".join(
        group(re.match(r"\{([^}]+)\}", piece)) if piece.startswith("{") else re.escape(piece)
        for piece in pieces
    )
    return re.compile(regex + r"\Z")


class Application:
    def __init__(self):
        self._routes: list[tuple[str, re.Pattern[str], Handler]] = []

    def add_route(self, method: str, pattern: str, handler: Handler) -> None:
        self._routes.append((method.upper(), _compile(pattern), handler))

    def add_post(self, pattern: str, handler: Handler) -> None:
        self.add_route("POST", pattern, handler)

    def resolve(self, method: str, path: str) -> tuple[Handler, dict[str, str]] | None:
        for route_method, regex, handler in self._routes:
            match = regex.match(path)
            if match is not None and route_method in ("*", method):
                return handler, match.groupdict()
        return None


class LocalServer:
    def __init__(
        self,
        app: Application | None = None,
        *,
        host: str = "127.0.0.1",
        announce_close: bool = True,
    ):
        self.app = app if app is not None else Application()
        self.host = host
        self.announce_close = announce_close
        self.port: int | None = None
        self._server: asyncio.AbstractServer | None = None
        self._tasks: set[asyncio.Task] = set()

    async def start_server(self) -> None:
        self._server = await asyncio.start_server(self._serve, self.host, 0)
        self.port = self._server.sockets[0].getsockname()[1]

    def make_url(self, path: str = "/") -> str:
        return f"http://{self.host}:{self.port}{path}"

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._server is not None:
            await self._server.wait_closed()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        self._tasks.add(task)
        try:
            await self._handle(reader, writer)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            self._tasks.discard(task)
            writer.close()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        request_line, _, header_bytes = head.partition(b"\r\n")
        method, target, _ = request_line.decode("latin-1").split(" ", 2)
        headers = parse_headers(io.BytesIO(header_bytes))
        body = await reader.readexactly(int(headers.get("Content-Length", "0")))
        request = Request(method, target, headers, body, reader, writer, self.announce_close)

        resolved = self.app.resolve(method, request.path)
        if resolved is None:
            response: Response | StreamResponse = Response(status=404)
        else:
            handler, request.match_info = resolved
            try:
                response = await handler(request)
            except Redirect as redirect:
                response = redirect.response
            except Exception:
                response = Response(status=500, text="handler raised")

        if isinstance(response, StreamResponse):
            if response._writer is None:
                await response.prepare(request)
            await response.write_eof()
            return
        headers_out = {**response.headers, "Content-Length": str(len(response.body))}
        if self.announce_close:
            headers_out["Connection"] = "close"
        writer.write(_status_line(response.status) + _header_block(headers_out) + response.body)
        await writer.drain()
