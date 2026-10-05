"""The served actor's HTTP transport (ADR-0021/D8): a FastAPI app over a `Service`, run by uvicorn on
this machine's loopback, and `Lion`, which serves an actor's offers.

uvicorn reads the requests. The app answers only a loopback peer whose Host, and Origin when given, name
the bound address; every refusal is D9's JSON error, and every answer is `nosniff` and `no-store`.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import re
import socket
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse
from starlette.exceptions import HTTPException
from starlette.requests import ClientDisconnect

from lion1.actor import Actor, Backend, Profile, RunRequest

from .service import _MIB, ServeError, Service, _Sub

__all__ = ("Lion", "app", "serve")

_HOSTS = ("127.0.0.1", "::1")
_STATUS = {
    "bad_request": 400,
    "forbidden": 403,
    "unknown_offer": 404,
    "unknown_run": 404,
    "not_found": 404,
    "method_not_allowed": 405,
    "timeout": 408,
    "ended": 409,
    "starting": 409,
    "not_conversation": 409,
    "no_resume": 409,
    "unreadable_log": 409,
    "conflict": 409,
    "gone": 410,
    "too_large": 413,
    "unsupported_type": 415,
    "refused": 422,
    "too_many": 429,
    "log_failed": 500,
    "busy": 503,
}
_CONTROLS = ("end", "interrupt", "stop", "cancel")
_START = re.compile(r"start=([0-9]{1,18})")  # a row count: never a number `int` refuses to read
_REQUESTS = 64  # requests in flight, streams among them
_DEADLINE = 10.0  # seconds for a POST's body to arrive whole
_PING = 15.0  # seconds between a stream's comment lines
_GRACE = 5.0  # seconds uvicorn gives a connection still sending once the service ends
_HEAD = 16 * 1024  # bytes of a request's head
_SAFE = [(b"x-content-type-options", b"nosniff"), (b"cache-control", b"no-store")]
_sleep = asyncio.sleep  # a stream's clock between comment lines


class Lion:
    """An actor's offers, served over HTTP on loopback (ADR-0021/D10): `lion.offer(profile, backend)`
    for each profile a client may run, then `asyncio.run(lion.serve(actor))` until cancelled. `dir`,
    `max_live` and `keep` are the service's; `host`, `port` and `grace` its transport's."""

    def __init__(
        self,
        *,
        dir: str | Path | None = None,
        max_live: int = 8,
        keep: int = 100,
        host: str = "127.0.0.1",
        port: int = 8710,
        grace: float = 10.0,
    ) -> None:
        self.dir, self.max_live, self.keep = dir, max_live, keep
        self.host, self.port, self.grace = host, port, grace
        self.offers: list[tuple[Profile, Backend, RunRequest, bool]] = []

    def offer(
        self, profile: Profile, backend: Backend, request: RunRequest | None = None, *, converse: bool = False
    ) -> Lion:
        """Offer `profile` on `backend`, `request` its template and `converse` making each run a
        conversation; checked against the actor when `serve` builds the service."""
        self.offers.append((profile, backend, request if request is not None else RunRequest(), converse))
        return self

    async def serve(self, actor: Actor, *, ready: Callable[[str, int], Any] | None = None) -> None:
        """Serve the offers of `actor` until cancelled; `ready` is called with the address bound."""
        service = Service(actor, dir=self.dir, max_live=self.max_live, keep=self.keep)
        for profile, backend, request, converse in self.offers:
            service.offer(profile, backend, request, converse=converse)
        await serve(service, self.host, self.port, self.grace, ready=ready)


async def serve(
    service: Service,
    host: str = "127.0.0.1",
    port: int = 8710,
    grace: float = 10.0,
    *,
    ready: Callable[[str, int], Any] | None = None,
) -> None:
    """`service` over HTTP on loopback until cancelled (ADR-0021/D8); a port of 0 takes a free one, and
    `ready` is called with the address bound. Ending closes admission and the streams, stops the runs,
    then closes the connections while the runs end (D10)."""
    if host not in _HOSTS:
        raise ValueError(f"host {host!r}: the transport binds 127.0.0.1 or ::1, nothing else")
    sock = socket.socket(socket.AF_INET6 if host == "::1" else socket.AF_INET)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
    except OSError:
        sock.close()
        raise
    bound = sock.getsockname()[1]
    spelled = "[::1]" if host == "::1" else host
    hosts = frozenset({f"{spelled}:{bound}", f"localhost:{bound}"})
    config = uvicorn.Config(
        app(service, hosts),
        http="h11",
        ws="none",
        lifespan="off",
        log_config=None,
        access_log=False,
        server_header=False,
        date_header=False,
        timeout_graceful_shutdown=_GRACE,
        h11_max_incomplete_event_size=_HEAD,
    )
    server = _Server(config)
    task = asyncio.ensure_future(server.serve(sockets=[sock]))
    try:
        while not server.started and not task.done():
            await asyncio.sleep(0.01)
        if server.started and ready is not None:
            ready(host, bound)
        await asyncio.wait({task})  # `wait`, which cancels nothing: a cancel of this call ends it below
    finally:
        await _through(_ended(service, server, task, sock, grace))


async def _ended(service: Service, server: uvicorn.Server, task: asyncio.Task, sock: Any, grace: float):
    service.halt()  # the streams close before any run's end can reach one
    server.should_exit = True
    runs = asyncio.ensure_future(service.close(grace))
    await asyncio.wait({task, runs})  # the connections close while the runs end: the longer bound, not both
    sock.close()  # uvicorn closed it if it started; a failed start left it to us
    await asyncio.gather(runs, task)  # a failure of either raises here, once both have ended


class _Server(uvicorn.Server):
    """uvicorn's server under its caller: a signal stays the caller's, and the serve ends by
    cancellation, so a Ctrl-C stops the runs before it leaves (C11)."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


def app(service: Service, hosts: frozenset[str]) -> Any:
    """The routes of D8's table over `service`, behind the loopback guard (C9)."""
    api = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)

    @api.exception_handler(ServeError)
    async def refused(request: Request, e: ServeError) -> Response:
        return _json(_STATUS[e.code], _error(e))

    @api.exception_handler(HTTPException)
    async def unrouted(request: Request, e: HTTPException) -> Response:
        path = request.url.path
        if e.status_code == 405:
            allowed = (e.headers or {}).get("Allow", "")
            return _json(405, _error(ServeError("method_not_allowed", f"{path} answers {allowed}")))
        return _json(404, _error(ServeError("not_found", f"no route {path}")))

    @api.exception_handler(ClientDisconnect)
    async def gone(request: Request, e: ClientDisconnect) -> Response:
        return Response(status_code=400)  # the peer left mid-body: nobody reads this

    @api.get("/")
    async def card(request: Request) -> Response:
        _no_query(request)
        return _json(200, service.card())

    @api.get("/runs")
    async def runs(request: Request) -> Response:
        _no_query(request)
        return _json(200, service.runs_state())

    @api.post("/runs")
    async def start(request: Request) -> Response:
        _no_query(request)
        state, first = await service.start(await _body(request))
        return _json(201 if first else 200, state)

    @api.get("/events")
    async def every_run(request: Request) -> Response:
        _no_query(request)
        return _Stream(service.events())

    @api.get("/runs/{sid}")
    async def run(request: Request, sid: str) -> Response:
        _no_query(request)
        return _json(200, service.state(sid))

    @api.get("/runs/{sid}/record")
    async def record(request: Request, sid: str) -> Response:
        start = 0
        query = request.scope["query_string"].decode("latin-1")
        if query:
            m = _START.fullmatch(query)
            if m is None:
                raise ServeError("bad_request", "a record read takes start=N and nothing else")
            start = int(m[1])
        return _json(200, service.record(sid, start))

    @api.get("/runs/{sid}/events")
    async def one_run(request: Request, sid: str) -> Response:
        _no_query(request)
        return _Stream(service.events(sid))  # a refusal is raised before any byte of the stream

    @api.post("/runs/{sid}/input")
    async def say(request: Request, sid: str) -> Response:
        _no_query(request)
        body = await _body(request)
        if "text" not in body or set(body) - {"text", "once"} or body.get("once", "") is None:
            raise ServeError("bad_request", "input is {text, once?} and no other key, once never null")
        return _json(202, {"id": sid, "handle": service.say(sid, body["text"], body.get("once"))})

    for name in _CONTROLS:
        api.add_api_route(f"/runs/{{sid}}/{name}", _control(service, name), methods=["POST"])

    return _guarded(api, service, hosts)


def _control(service: Service, name: str) -> Callable[..., Any]:
    async def control(request: Request, sid: str) -> Response:
        _no_query(request)
        if await _body(request):
            raise ServeError("bad_request", f"{name} takes an empty object")
        if name == "cancel":
            return _json(202, await service.cancel(sid))
        return _json(202, getattr(service, name)(sid))

    return control


def _guarded(inner: Any, service: Service, hosts: frozenset[str]) -> Any:
    """The app behind C9's checks, read before any route: the peer, then Host, then Origin; then the
    service ending or the requests in flight at their cap. Every answer gains `nosniff` and `no-store`."""
    active = 0

    async def guarded(scope: dict, receive: Any, send: Any) -> None:
        nonlocal active

        async def safe(message: dict) -> None:
            if message["type"] == "http.response.start":
                message = {**message, "headers": [*message.get("headers", []), *_SAFE]}
            await send(message)

        refusal = _refusal(scope, hosts)
        if refusal is None and service.closing:
            refusal = ServeError("busy", "the service is ending")
        if refusal is None and active >= _REQUESTS:
            refusal = ServeError("busy", f"{_REQUESTS} requests are in flight, the most this service takes")
        if refusal is not None:
            await _json(_STATUS[refusal.code], _error(refusal))(scope, receive, safe)
            return

        active += 1
        try:
            await inner(scope, receive, safe)
        finally:
            active -= 1

    async def asgi(scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":  # no lifespan and no websocket is served
            return
        await guarded(scope, receive, send)

    return asgi


def _refusal(scope: dict, hosts: frozenset[str]) -> ServeError | None:
    peer = (scope.get("client") or ("",))[0]
    if not _loopback(peer):
        return ServeError("forbidden", "the peer is not loopback")
    given: dict[bytes, list[str]] = {}
    for name, value in scope["headers"]:
        if name in (b"host", b"origin"):
            given.setdefault(name, []).append(value.decode("latin-1"))
    host = given.get(b"host", [])
    if len(host) != 1 or host[0].lower() not in hosts:
        return ServeError("forbidden", f"Host {', '.join(host) or 'absent'} is not this server")
    origin = given.get(b"origin")
    if origin is not None and not (
        len(origin) == 1 and origin[0].startswith("http://") and origin[0][7:].lower() in hosts
    ):
        return ServeError("forbidden", f"Origin {', '.join(origin)!r} is not this server")
    return None


def _loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _no_query(request: Request) -> None:
    if request.scope["query_string"]:
        raise ServeError("bad_request", "only a record read takes a query")


async def _body(request: Request) -> dict:
    """A POST's body: `application/json`, at most 1 MiB, whole within the deadline, one JSON object."""
    types = request.headers.getlist("content-type")
    if len(types) != 1 or types[0].lower() != "application/json":
        raise ServeError("unsupported_type", "a POST carries application/json")
    length = request.headers.get("content-length")
    # the digits decide before `int` reads them: thousands of digits are past the cap, where `int` raises
    if length is not None and (len(length.lstrip("0")) > len(str(_MIB)) or int(length) > _MIB):
        raise ServeError("too_large", f"the body is past {_MIB} bytes")
    body = bytearray()
    try:
        async with asyncio.timeout(_DEADLINE):
            async for chunk in request.stream():
                body += chunk
                if len(body) > _MIB:
                    raise ServeError("too_large", f"the body is past {_MIB} bytes")
    except TimeoutError:
        raise ServeError("timeout", f"the body did not arrive whole within {_DEADLINE:g} seconds") from None
    return _object(bytes(body))


def _object(body: bytes) -> dict:
    """One JSON object in UTF-8, no key twice and no NaN or Infinity."""
    try:
        doc = json.loads(body.decode(), object_pairs_hook=_unique, parse_constant=_constant)
    except (ValueError, RecursionError) as e:  # UnicodeDecodeError and JSONDecodeError are ValueErrors
        raise ServeError("bad_request", f"the body is not one JSON object in UTF-8: {e}") from None
    if not isinstance(doc, dict):
        raise ServeError("bad_request", "the body is not a JSON object")
    return doc


def _unique(pairs: list[tuple[str, Any]]) -> dict:
    doc = dict(pairs)
    if len(doc) != len(pairs):
        raise ValueError("a key is given twice")
    return doc


def _constant(name: str) -> Any:
    raise ValueError(f"{name} is not JSON")


def _error(e: ServeError) -> dict:
    return {"error": e.code, "reason": e.reason}


def _json(status: int, doc: dict) -> Response:
    return Response(json.dumps(doc), status_code=status, media_type="application/json")


class _Stream(StreamingResponse):
    """A run's stream, or every run's, as server-sent events (D12): a comment line each time the clock
    steps, and the subscription closed however the response ends."""

    def __init__(self, sub: _Sub) -> None:
        self.sub = sub
        super().__init__(self._frames(), media_type="text/event-stream")

    async def _frames(self) -> Any:
        async for name, data in self.sub:
            yield b": ping\n\n" if name is None else f"event: {name}\ndata: {json.dumps(data)}\n\n".encode()

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        ping = asyncio.ensure_future(_ping(self.sub))
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.sub.close()
            ping.cancel()
            await asyncio.wait({ping})
            await self.body_iterator.aclose()


async def _ping(sub: _Sub) -> None:
    while True:
        await _sleep(_PING)
        sub.ping()


async def _through(coro: Any) -> None:
    """Await `coro` to its end whatever cancels the caller meanwhile: an ending cut short would leave
    alive what it was ending."""
    task = asyncio.ensure_future(coro)
    while not task.done():
        # `wait` cancels nothing; a shield would have the loop report the task's later failure as
        # nobody's (CPython 3.14.7), though the last line reads it
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait((task,))
    task.result()
