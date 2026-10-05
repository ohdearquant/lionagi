"""The served actor's HTTP transport (ADR-0021/D8): loopback only, D9's shapes and codes over FastAPI on
uvicorn, streams as server-sent events, and an end that leaves nothing running. Every test here but the
peer's speaks to a real socket on loopback."""

import asyncio
import contextlib
import gc
import json
import signal
import socket
from types import SimpleNamespace

import pytest

import hub.serve.service as serve
import hub.serve.transport as transport
from hub.serve import Lion, Service
from hub.serve import serve as serving_forever
from lion1 import Actor, Profile, RunRequest
from tests.lionfake import unflushed_store
from tests.test_serve import FINISH, Answer, Held, actor, bounded, log_lines, turns


@contextlib.asynccontextmanager
async def serving(service: Service, host: str = "127.0.0.1", grace: float = 2.0):
    """`service` served on a free port for the block; cancelled, and seen to return, after it."""
    bound = asyncio.get_running_loop().create_future()
    served = serving_forever(service, host, 0, grace, ready=lambda h, p: bound.set_result(p))
    task = asyncio.ensure_future(served)
    await asyncio.wait({bound, task}, return_when=asyncio.FIRST_COMPLETED)
    if task.done():
        task.result()
    try:
        yield bound.result()
    finally:
        task.cancel()
        await asyncio.wait({task})
        assert task.cancelled()


def http(method: str, path: str, port: int, body=None, *, headers=(), host: str | None = None) -> bytes:
    """One request that asks the server to close after its answer, so a read to the close is the answer."""
    lines = [f"{method} {path} HTTP/1.1", f"Host: {host if host is not None else f'127.0.0.1:{port}'}"]
    lines.append("Connection: close")
    data = b""
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        lines += ["Content-Type: application/json", f"Content-Length: {len(data)}"]
    return ("\r\n".join([*lines, *headers]) + "\r\n\r\n").encode() + data


def parsed(raw: bytes) -> tuple[int | None, dict[str, str], bytes]:
    if not raw:
        return None, {}, b""
    head, _, body = raw.partition(b"\r\n\r\n")
    status, *lines = head.decode().split("\r\n")
    fields = {}
    for header in lines:
        name, _, value = header.partition(": ")
        assert name.lower() not in fields, f"{name} twice"
        fields[name.lower()] = value
    return int(status.split(" ")[1]), fields, body


async def ask(port: int, data: bytes | list[bytes], *, host: str = "127.0.0.1", pause: float = 0.0):
    """Send `data`, whole or piece by piece, and read to the server's close: (status, headers, body)."""
    reader, writer = await asyncio.open_connection(host, port)
    try:
        for piece in [data] if isinstance(data, bytes) else data:
            writer.write(piece)
            await writer.drain()
            await asyncio.sleep(pause)
        raw = await reader.read()
    finally:
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()
    return parsed(raw)


async def call(port: int, method: str, path: str, body=None, **kw) -> tuple[int, dict]:
    status, fields, data = await ask(port, http(method, path, port, body, **kw))
    assert fields["content-type"] == "application/json" and int(fields["content-length"]) == len(data)
    return status, json.loads(data)


def frames(body: bytes) -> list[tuple[str | None, dict | None]]:
    """A server-sent event stream as (event, data); a comment line as (None, None)."""
    out = []
    for block in body.decode().split("\n\n")[:-1]:
        if block.startswith(":"):
            out.append((None, None))
            continue
        event, data = block.split("\n")
        assert event.startswith("event: ") and data.startswith("data: ")
        out.append((event[7:], json.loads(data[6:])))
    return out


SAFE = {"x-content-type-options": "nosniff", "cache-control": "no-store"}


def assert_safe(fields: dict) -> None:
    assert {k: fields.get(k) for k in SAFE} == SAFE
    assert not [k for k in fields if k.startswith("access-control-")]


class Stream:
    """A stream read off its socket: the head, then its frames, each a chunk as uvicorn sends them."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, fields: dict):
        self.reader, self.writer, self.fields = reader, writer, fields
        self.buf = b""

    async def frame(self) -> bytes:
        while b"\n\n" not in self.buf:
            line = await self.reader.readline()
            size = int(line.strip() or b"0", 16)
            if size == 0:
                raise EOFError
            self.buf += (await self.reader.readexactly(size + 2))[:-2]
        frame, _, self.buf = self.buf.partition(b"\n\n")
        return frame + b"\n\n"

    async def drained(self) -> list:
        """What the stream sent to its close, cut or whole."""
        got = []
        with contextlib.suppress(EOFError, ValueError, asyncio.IncompleteReadError, ConnectionError):
            while True:
                got.append(await self.frame())
        self.writer.close()
        with contextlib.suppress(OSError):
            await self.writer.wait_closed()
        return frames(b"".join(got))


# --- D8: the routes ---------------------------------------------------------------------------------


async def stream_open(port: int, path: str) -> Stream:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(http("GET", path, port))
    head = await reader.readuntil(b"\r\n\r\n")
    status, fields, _ = parsed(head)
    assert status == 200 and fields["content-type"] == "text/event-stream"  # D8's value, no parameter
    assert "content-length" not in fields and fields["transfer-encoding"] == "chunked"
    assert_safe(fields)
    return Stream(reader, writer, fields)


def test_each_route_answers_as_the_table_says_in_the_shapes_d9_gives():
    a, _ = actor("http-routes")
    service = Service(a)
    backend = Held()
    service.offer(
        Profile("task"), turns('<lact a1>echo(text="x")</lact>', FINISH), RunRequest(emits=(Answer,))
    )
    service.offer(Profile("talk"), backend, converse=True)

    async def main():
        got = {}
        async with serving(service) as port:
            every = await stream_open(port, "/events")
            got["card"] = await call(port, "GET", "/")
            start = {"offer": "task", "inputs": ["go"], "once": "k"}
            got["start"] = await call(port, "POST", "/runs", start)
            got["again"] = await call(port, "POST", "/runs", start)
            sid = got["start"][1]["id"]
            got["events"] = await (await stream_open(port, f"/runs/{sid}/events")).drained()
            got["runs"] = await call(port, "GET", "/runs")
            got["state"] = await call(port, "GET", f"/runs/{sid}")
            got["record"] = await call(port, "GET", f"/runs/{sid}/record")
            got["record 2"] = await call(port, "GET", f"/runs/{sid}/record?start=2")
            # a conversation: input, an interrupt that cuts its call, and an end
            talk = (await call(port, "POST", "/runs", {"offer": "talk", "inputs": ["hi"]}))[1]["id"]
            await backend.next()
            got["input"] = await call(port, "POST", f"/runs/{talk}/input", {"text": "then?", "once": "i"})
            got["input again"] = await call(
                port, "POST", f"/runs/{talk}/input", {"text": "then?", "once": "i"}
            )
            got["interrupt"] = await call(port, "POST", f"/runs/{talk}/interrupt", {})
            second = await backend.next()  # the cut turn made no message; the next took the input
            got["end"] = await call(port, "POST", f"/runs/{talk}/end", {})
            second.set_result("Fine.")
            got["talk"] = await (await stream_open(port, f"/runs/{talk}/events")).drained()
            ids = [talk]
            for control in ("stop", "cancel"):
                ids.append((await call(port, "POST", "/runs", {"offer": "talk", "inputs": ["hi"]}))[1]["id"])
                await backend.next()
                got[control] = await call(port, "POST", f"/runs/{ids[-1]}/{control}", {})
                await (await stream_open(port, f"/runs/{ids[-1]}/events")).drained()
        got["every"] = await every.drained()  # open until the server ended
        return sid, ids, got

    sid, ids, got = bounded(main)
    assert got["card"] == (200, service.card())
    assert got["start"][0] == 201 and got["again"][0] == 200 and got["again"][1]["id"] == sid
    assert got["events"][0][0] == "state" and got["events"][-1] == ("state", service.state(sid))
    assert got["runs"][0] == 200 and [s["id"] for s in got["runs"][1]["runs"]] == [sid]
    assert got["state"] == (200, service.state(sid)) and got["state"][1]["phase"] == "ended"
    assert got["record"] == (200, service.record(sid)) and got["record 2"] == (200, service.record(sid, 2))
    assert got["input"][0] == 202 and got["input"][1] == {"id": ids[0], "handle": got["input"][1]["handle"]}
    assert got["input again"] == got["input"]
    assert got["interrupt"][0] == 202 and got["interrupt"][1]["cut"] is True
    assert got["end"][0] == 202 and got["end"][1]["closed"] is True and "cut" not in got["end"][1]
    assert got["talk"][-1][1]["outcome"] == {"kind": "Stopped", "turn": 3}
    assert got["stop"][0] == 202 and service.state(ids[1])["outcome"] == {"kind": "Stopped", "turn": 2}
    assert got["cancel"] == (202, service.state(ids[2])) and got["cancel"][1]["phase"] == "aborted"
    assert got["every"][0] == ("runs", {"runs": []})
    assert {d["id"] for name, d in got["every"] if name == "state"} == {sid, *ids}
    assert [d["phase"] for name, d in got["every"] if name == "state" and d["id"] == ids[2]] == [
        "working",
        "aborted",
    ]


# --- D9: a request per code -------------------------------------------------------------------------


class _Planless:
    @property
    def plan(self):
        raise RuntimeError("no plan today")

    async def __call__(self, messages):
        raise AssertionError("never called")


D9_STATUS = {  # D9's table, written out here so a change to the transport's own table cannot pass
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


def test_each_code_is_answered_with_its_status_and_a_reason(tmp_path, monkeypatch):
    a, _ = actor("http-codes")
    service = Service(a, dir=tmp_path, keep=1)
    backend = Held()
    service.offer(Profile("task"), turns(FINISH, FINISH, FINISH), RunRequest(emits=(Answer,)))
    service.offer(Profile("talk"), backend, converse=True)
    service.offer(Profile("broken"), _Planless())
    service.offer(Profile("work"), backend, RunRequest(emits=(Answer,)))
    (tmp_path / f"{'b' * 16}.jsonl").write_text(json.dumps(serve_row("b" * 16)) + "\nnot json\n{}\n")
    monkeypatch.setattr(serve, "_UNTAKEN", 1)

    async def main():
        got = {}
        async with serving(service) as port:
            ended = (await call(port, "POST", "/runs", {"offer": "task", "once": "k"}))[1]["id"]
            await (await stream_open(port, f"/runs/{ended}/events")).drained()
            talk = (await call(port, "POST", "/runs", {"offer": "talk", "inputs": ["hi"]}))[1]["id"]
            await backend.next()
            await call(port, "POST", f"/runs/{talk}/input", {"text": "one"})
            work = (await call(port, "POST", "/runs", {"offer": "work"}))[1]["id"]
            await backend.next()
            cases = {
                "bad_request": ("POST", "/runs", {"offer": 1}),
                "forbidden": ("GET", "/", None, {"host": "evil.example"}),
                "unknown_offer": ("POST", "/runs", {"offer": "nothing"}),
                "unknown_run": ("GET", "/runs/" + "0" * 16, None),
                "not_found": ("GET", "/nowhere", None),
                "method_not_allowed": ("GET", f"/runs/{talk}/input", None),
                "ended": ("POST", f"/runs/{ended}/input", {"text": "late"}),
                "not_conversation": ("POST", f"/runs/{work}/end", {}),
                "no_resume": ("POST", "/runs", {"offer": "task", "resume": "c" * 16}),
                "unreadable_log": ("POST", "/runs", {"offer": "task", "resume": "b" * 16}),
                "conflict": ("POST", "/runs", {"offer": "task", "once": "k", "inputs": ["other"]}),
                "refused": ("POST", "/runs", {"offer": "broken"}),
                "too_many": ("POST", f"/runs/{talk}/input", {"text": "two"}),
            }
            for code, (method, path, body, *kw) in cases.items():
                got[code] = await call(port, method, path, body, **(kw[0] if kw else {}))
            for code, raw in (
                (
                    "unsupported_type",
                    http("POST", "/runs", port, {}).replace(b"application/json", b"text/plain"),
                ),
                (
                    "too_large",
                    http("POST", f"/runs/{talk}/input", port, b"").replace(b"Length: 0", b"Length: 1048577"),
                ),
            ):
                status, fields, data = await ask(port, raw)
                got[code] = (status, json.loads(data))
            # dropped with no log to read: keep=1 and two runs that ended since, its log removed
            for _ in range(2):
                sid = (await call(port, "POST", "/runs", {"offer": "task"}))[1]["id"]
                await (await stream_open(port, f"/runs/{sid}/events")).drained()
            (tmp_path / f"{ended}.jsonl").unlink()
            got["gone"] = await call(port, "GET", f"/runs/{ended}")
            monkeypatch.setattr(
                serve, "_opened", lambda path: (_ for _ in ()).throw(PermissionError(13, "denied"))
            )
            got["log_failed"] = await call(port, "POST", "/runs", {"offer": "task"})
            monkeypatch.setattr(serve, "_SUBSCRIBERS", 0)
            got["busy"] = await call(port, "GET", "/events")
            for sid in (talk, work):
                await call(port, "POST", f"/runs/{sid}/cancel", {})
        return got

    got = bounded(main)
    assert transport._STATUS == D9_STATUS
    for code, (status, doc) in got.items():
        assert status == D9_STATUS[code] and doc["error"] == code and doc["reason"], (code, doc)
        assert list(doc) == ["error", "reason"]
    assert set(got) == set(transport._STATUS) - {"timeout", "starting"}  # those two have tests of their own
    assert got["refused"][1]["reason"] == "RuntimeError: no plan today"


def serve_row(sid: str) -> dict:
    return {"kind": "SERVE", "version": 1, "id": sid, "offer": "task", "resumed": None, "replayed": 0}


# --- C10, D9: the body ------------------------------------------------------------------------------

HOST = b"Host: 127.0.0.1:{p}\r\nConnection: close\r\n"
JSON = b"Content-Type: application/json\r\n"


def post(body: bytes, *, path: bytes = b"/runs", length: bytes | None = None, kind: bytes = JSON) -> bytes:
    size = str(len(body)).encode() if length is None else length
    return b"POST " + path + b" HTTP/1.1\r\n" + HOST + kind + b"Content-Length: " + size + b"\r\n\r\n" + body


def chunked(body: bytes, kind: bytes = JSON) -> bytes:
    """`body` sent in chunks of 64 KiB, with no length given."""
    pieces = [body[i : i + 65536] for i in range(0, len(body), 65536)]
    data = b"".join(f"{len(p):x}\r\n".encode() + p + b"\r\n" for p in pieces) + b"0\r\n\r\n"
    return b"POST /runs HTTP/1.1\r\n" + HOST + kind + b"Transfer-Encoding: chunked\r\n\r\n" + data


START = b'{"offer": "p"}'
REFUSED = [
    ("bytes that are not UTF-8", post(b'{"offer": "\xff"}'), "bad_request"),
    ("bytes that are not JSON", post(b"offer=p"), "bad_request"),
    ("an empty body", post(b""), "bad_request"),
    ("no body and no length", post(START).replace(b"Content-Length: 14\r\n", b"")[:-14], "bad_request"),
    ("a JSON array", post(b"[]"), "bad_request"),
    ("a key twice", post(b'{"offer": "p", "offer": "p"}'), "bad_request"),
    ("NaN", post(b'{"offer": "p", "bounds": {"time_budget": NaN}}'), "bad_request"),
    ("Infinity", post(b'{"offer": "p", "bounds": {"cost_budget": -Infinity}}'), "bad_request"),
    (
        "a number of 5,000 digits in the body",
        post(b'{"offer": "p", "bounds": {"max_turns": ' + b"9" * 5000 + b"}}"),
        "bad_request",
    ),
    (
        "a parameter on the type",
        post(START, kind=b"Content-Type: application/json; charset=utf-8\r\n"),
        "unsupported_type",
    ),
    ("no type", post(START, kind=b""), "unsupported_type"),
    ("another type", post(START, kind=b"Content-Type: text/plain\r\n"), "unsupported_type"),
    ("two types", post(START, kind=JSON + JSON), "bad_request"),
    ("a length past 1 MiB", post(b"", length=b"1048577"), "too_large"),
    ("a chunked body past 1 MiB", chunked(b" " * (1 << 20) + START), "too_large"),
]
ACCEPTED = [
    ("the type in capitals", post(START, kind=b"Content-Type: APPLICATION/JSON\r\n"), 201),
    ("a chunked body", chunked(START), 201),
    ("a chunked body of exactly 1 MiB", chunked(START + b" " * ((1 << 20) - len(START))), 201),
]


async def finish(messages):
    return FINISH


@pytest.mark.parametrize("name, raw, code", REFUSED, ids=[c[0] for c in REFUSED])
def test_a_body_outside_d8s_form_is_answered_with_its_error_and_never_dispatched(name, raw, code, store):
    a = Actor("http-body", store=store)
    service = Service(a)
    service.offer(Profile("p"), finish, RunRequest(emits=(Answer,)))

    async def main():
        async with serving(service) as port:
            return await ask(port, raw.replace(b"{p}", str(port).encode()))

    status, fields, body = bounded(main)
    assert (status, json.loads(body)["error"]) == (D9_STATUS[code], code) and service.runs == {}
    assert_safe(fields)


@pytest.mark.parametrize("name, raw, status", ACCEPTED, ids=[c[0] for c in ACCEPTED])
def test_a_body_in_d8s_form_is_dispatched_however_it_is_framed(name, raw, status, store):
    a = Actor("http-body-ok", store=store)
    service = Service(a)
    service.offer(Profile("p"), finish, RunRequest(emits=(Answer,)))

    async def main():
        async with serving(service) as port:
            return await ask(port, raw.replace(b"{p}", str(port).encode()))

    got, fields, body = bounded(main)
    assert got == status, body
    assert_safe(fields)


def get(path: bytes, framing: bytes, body: bytes = b"") -> bytes:
    return b"GET " + path + b" HTTP/1.1\r\n" + HOST + framing + b"\r\n" + body


GETS = [
    ("a length and a body on the card", get(b"/", b"Content-Length: 2\r\n", b"{}"), 400),
    (
        "a chunked body on the runs",
        get(b"/runs", b"Transfer-Encoding: chunked\r\n", b"2\r\n{}\r\n0\r\n\r\n"),
        400,
    ),
    ("a length on every run's stream", get(b"/events", b"Content-Length: 1\r\n", b" "), 400),
    ("a length of zero", get(b"/", b"Content-Length: 0\r\n"), 200),
    ("a length of zeros", get(b"/", b"Content-Length: 000\r\n"), 200),
]


@pytest.mark.parametrize("name, raw, status", GETS, ids=[c[0] for c in GETS])
def test_a_get_carrying_a_body_is_a_bad_request_and_a_zero_length_is_none(name, raw, status, store):
    service = Service(Actor("http-get-body", store=store))

    async def main():
        async with serving(service) as port:
            return await ask(port, raw.replace(b"{p}", str(port).encode()))

    got, fields, body = bounded(main)
    assert got == status, body
    if status == 400:
        assert json.loads(body) == {"error": "bad_request", "reason": "a GET carries no body"}
    assert_safe(fields)


def test_a_body_nested_past_the_recursion_limit_is_a_bad_request(no_collection, store):
    a = Actor("http-deep", store=store)
    service = Service(a)
    service.offer(Profile("p"), finish)

    async def main():
        async with serving(service) as port:
            return await ask(port, post(b"[" * 100_000).replace(b"{p}", str(port).encode()))

    status, fields, body = bounded(main)
    assert status == 400 and json.loads(body)["error"] == "bad_request" and service.runs == {}


async def phase_reached(service: Service, sid: str, phase: str) -> None:
    """Read off the state, never a stream of its own: a stream here would count against the caps."""
    while service.state(sid)["phase"] != phase:
        await asyncio.sleep(0.01)


LONG = "start=" + "9" * 5000  # past the digits `int` reads from a string


def test_a_route_outside_the_table_or_a_query_where_none_is_taken_is_refused():
    a, _ = actor("http-edges")
    service = Service(a)
    service.offer(Profile("p"), finish, RunRequest(emits=(Answer,)))

    async def main():
        got = {}
        async with serving(service) as port:
            sid = (await call(port, "POST", "/runs", {"offer": "p"}))[1]["id"]
            await (await stream_open(port, f"/runs/{sid}/events")).drained()
            for path in ("/?a=1", "/runs?start=1", f"/runs/{sid}?x", f"/runs/{sid}/events?x", "/events?x"):
                got[path] = await call(port, "GET", path)
            for query in (
                "start=x",
                "foo=1",
                "start=",
                "start=1&start=2",
                "start=-1",
                "start=4",
                LONG,
                "start=" + "1" * 19,
            ):
                got[query] = await call(port, "GET", f"/runs/{sid}/record?{query}")
            for path in ("/runs/", f"/runs/{sid}/nope", "//", "/runs/x/y/z", "/events/", "/run", "/docs"):
                got[path] = await call(port, "GET", path)
            got["POST /"] = await call(port, "POST", "/", {})
            got["POST record"] = await call(port, "POST", f"/runs/{sid}/record", {})
            got["GET input"] = await call(port, "GET", f"/runs/{sid}/input")
            got["GET stop"] = await call(port, "GET", f"/runs/{sid}/stop")
            got["stop with a body"] = await call(port, "POST", f"/runs/{sid}/stop", {"now": True})
            for body in ({}, {"text": "x", "extra": 1}, {"text": "x", "once": None}):
                got[f"input {json.dumps(body)}"] = await call(port, "POST", f"/runs/{sid}/input", body)
            got["record 3"] = await call(port, "GET", f"/runs/{sid}/record?start=3")
        return sid, got

    sid, got = bounded(main)
    rows = len(service.record(sid)["rows"])
    assert rows == 3, "the end of the record is the last start a read takes"
    queried = ["/?a=1", "/runs?start=1", f"/runs/{sid}?x", f"/runs/{sid}/events?x", "/events?x"]
    unrouted = ["/runs/", f"/runs/{sid}/nope", "//", "/runs/x/y/z", "/events/", "/run", "/docs"]
    want = {
        **dict.fromkeys(queried, 400),
        **dict.fromkeys(["start=x", "foo=1", "start=", "start=1&start=2", "start=-1", "start=4"], 400),
        **dict.fromkeys([LONG, "start=" + "1" * 19], 400),  # a start `int` would refuse to read is no start
        **dict.fromkeys(unrouted, 404),
        "POST /": 405,
        "POST record": 405,
        "GET input": 405,
        "GET stop": 405,
        "stop with a body": 400,
        "input {}": 400,
        'input {"text": "x", "extra": 1}': 400,
        'input {"text": "x", "once": null}': 400,
    }
    assert {k: v[0] for k, v in got.items() if k != "record 3"} == want
    assert {got[p][1]["error"] for p in ("/runs/", "//", "/docs")} == {"not_found"}
    assert got["POST record"][1] == {
        "error": "method_not_allowed",
        "reason": f"/runs/{sid}/record answers GET",
    }
    assert got["record 3"] == (200, {"id": sid, "start": 3, "rows": [], "more": False})


# --- C9: loopback only ------------------------------------------------------------------------------

V4 = [  # (Host, Origin or None, admitted) on a 127.0.0.1 bind
    ("127.0.0.1:{p}", None, True),
    ("localhost:{p}", None, True),
    ("LocalHost:{p}", None, True),
    ("127.0.0.1:{p}", "http://127.0.0.1:{p}", True),
    ("localhost:{p}", "http://LOCALHOST:{p}", True),
    ("127.0.0.1", None, False),
    ("localhost", None, False),
    ("127.0.0.1:1", None, False),
    ("[::1]:{p}", None, False),
    ("127.0.0.2:{p}", None, False),
    ("evil.example", None, False),
    ("evil.example:{p}", None, False),
    ("127.0.0.1:{p}.evil.example", None, False),
    ("127.0.0.1:{p}", "http://evil.example", False),
    ("127.0.0.1:{p}", "http://127.0.0.1:1", False),
    ("127.0.0.1:{p}", "https://127.0.0.1:{p}", False),
    ("127.0.0.1:{p}", "HTTP://127.0.0.1:{p}", False),
    ("127.0.0.1:{p}", "http://127.0.0.1:{p}/", False),
    ("127.0.0.1:{p}", "null", False),
    ("127.0.0.1:{p}", "", False),
]
V6 = [
    ("[::1]:{p}", None, True),
    ("localhost:{p}", None, True),
    ("[::1]:{p}", "http://[::1]:{p}", True),
    ("127.0.0.1:{p}", None, False),
    ("::1:{p}", None, False),
    ("[::1]", None, False),
    ("[::1]:{p}", "http://127.0.0.1:{p}", False),
]


def loopback_v6() -> bool:
    with socket.socket(socket.AF_INET6) as s:
        try:
            s.bind(("::1", 0))
        except OSError:
            return False
    return True


@pytest.mark.parametrize("bind, cases", [("127.0.0.1", V4), ("::1", V6)])
def test_a_host_or_origin_d8_does_not_list_for_the_bound_address_is_forbidden_on_every_route(
    bind, cases, store
):
    if bind == "::1" and not loopback_v6():
        pytest.skip("no IPv6 loopback on this machine")
    a = Actor("http-hosts", store=store)
    service = Service(a)
    service.offer(Profile("p"), finish, RunRequest(emits=(Answer,)))

    async def main():
        got = []
        async with serving(service, bind) as port:
            for host, origin, _ in cases:
                headers = () if origin is None else (f"Origin: {origin.replace('{p}', str(port))}",)
                for method, path, body in (
                    ("GET", "/", None),
                    ("GET", "/runs", None),
                    ("POST", "/runs", {"offer": "p"}),
                    ("GET", "/nowhere", None),
                ):
                    raw = http(method, path, port, body, headers=headers, host=host.replace("{p}", str(port)))
                    status, fields, data = await ask(port, raw, host=bind)
                    assert_safe(fields)
                    got.append((host, origin, path, method, status, json.loads(data).get("error")))
            origin = f"Origin: http://localhost:{port}"
            raw = http("GET", "/", port, host=f"localhost:{port}", headers=[origin] * 2)
            status, _, data = await ask(port, raw, host=bind)
        return got, (status, json.loads(data)["error"])

    got, twice = bounded(main)
    for host, origin, admitted in cases:
        answers = {(m, p): (s, e) for h, o, p, m, s, e in got if (h, o) == (host, origin)}
        if admitted:
            assert answers == {
                ("GET", "/"): (200, None),
                ("GET", "/runs"): (200, None),
                ("POST", "/runs"): (201, None),
                ("GET", "/nowhere"): (404, "not_found"),
            }
        else:
            assert set(answers.values()) == {(403, "forbidden")}, (host, origin, answers)
    assert twice == (403, "forbidden")  # an Origin each allowed, but two of them
    assert len(service.runs) == sum(admitted for *_, admitted in cases)


@pytest.mark.parametrize(
    "peer, admitted",
    [
        ("127.0.0.1", True),
        ("::1", True),
        ("192.0.2.7", False),
        ("10.0.0.1", False),
        ("::ffff:192.0.2.7", False),
        ("", False),
        ("not an address", False),
        (None, False),
    ],
)
def test_a_peer_off_loopback_is_forbidden_before_any_route(peer, admitted, store):
    """The peer as the server read it off the connection, handed to the app: only a loopback address is
    answered, before any route or any byte of the body is read."""
    service = Service(Actor("http-peer", store=store))
    service.offer(Profile("p"), finish, RunRequest(emits=(Answer,)))
    asgi = transport.app(service, frozenset({"127.0.0.1:1"}))
    read, sent = [], []
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"127.0.0.1:1")],
        "client": None if peer is None else (peer, 5),
        "server": ("127.0.0.1", 1),
    }

    async def receive():
        read.append(True)
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(asgi(scope, receive, send))
    status, body = sent[0]["status"], json.loads(sent[1]["body"])
    if admitted:
        assert (status, body) == (200, service.card())
    else:
        assert (status, body) == (403, {"error": "forbidden", "reason": "the peer is not loopback"})
        assert read == [] and (b"x-content-type-options", b"nosniff") in sent[0]["headers"]
    assert [transport._loopback(h) for h in ("127.0.0.1", "127.9.9.9", "::1", "192.0.2.7", "", "x")] == [
        True,
        True,
        True,
        False,
        False,
        False,
    ]


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "localhost", "127.0.0.2", "192.0.2.7", ""])
def test_a_host_off_loopback_is_refused_with_valueerror_before_any_bind(host, monkeypatch, store):
    service = Service(Actor("http-bind", store=store))
    made = []
    monkeypatch.setattr(transport, "socket", SimpleNamespace(socket=lambda *a: made.append(a)))
    with pytest.raises(ValueError, match="binds 127.0.0.1 or ::1, nothing else"):
        bounded(lambda: serving_forever(service, host, 0))
    assert made == [] and not service.closing


def test_a_port_in_use_raises_the_bind_error_and_leaves_no_socket_open(store):
    service = Service(Actor("http-in-use", store=store))
    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        held.listen()
        with pytest.raises(OSError):
            bounded(lambda: serving_forever(service, "127.0.0.1", held.getsockname()[1]))
    gc.collect()  # a socket left open would warn as it is collected, and a warning fails the test
    assert not service.closing


def test_lion_serves_its_offers_on_the_port_it_is_given_until_cancelled(store):
    """`Lion` as a program uses it: offers, then `serve(actor)` with no callback, cancelled to end."""
    actor_ = Actor("http-lion", store=store)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    lion = Lion(port=port, grace=1.0).offer(Profile("p"), finish, RunRequest(emits=(Answer,)))

    async def main():
        served = asyncio.ensure_future(lion.serve(actor_))
        while True:
            try:
                card = await call(port, "GET", "/")
                break
            except ConnectionRefusedError:
                await asyncio.sleep(0.01)
        sid = (await call(port, "POST", "/runs", {"offer": "p"}))[1]["id"]
        served.cancel()
        await asyncio.wait({served})
        return card, sid, served.cancelled()

    card, sid, cancelled = bounded(main)
    assert card[0] == 200 and [o["name"] for o in card[1]["offers"]] == ["p"]
    assert cancelled and len(sid) == 16


def test_a_scope_that_is_not_http_is_left_unanswered(store):
    asgi = transport.app(Service(Actor("http-scopes", store=store)), frozenset({"127.0.0.1:1"}))
    touched = []

    async def touch(*args):
        touched.append(args)

    for kind in ("lifespan", "websocket"):
        asyncio.run(asgi({"type": kind}, touch, touch))
    assert touched == []


@pytest.mark.parametrize("sent", [b"", b"GET / HTTP/1.1\r\nHo", post(b'{"of', length=b"14")])
def test_a_peer_that_closes_before_its_request_is_whole_is_never_answered(sent, caplog, store):
    service = Service(Actor("http-gone", store=store))
    service.offer(Profile("p"), finish, RunRequest(emits=(Answer,)))

    async def main():
        async with serving(service) as port:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(sent.replace(b"{p}", str(port).encode()))
            await writer.drain()
            await asyncio.sleep(0.05)
            writer.transport.abort()
        return await reader.read()  # the serve ended only once every request had

    assert bounded(main) == b"" and service.runs == {}
    assert [r for r in caplog.records if r.levelname == "ERROR"] == []


# --- C10: the body's deadline -----------------------------------------------------------------------


DEADLINE = [  # what is sent, and in how many pieces 0.05 seconds apart: all of it well before the deadline
    ("a short body", post(b'{"offer"', length=b"14"), 1),
    ("a body sent slowly", post(b'{"offer": "p"', length=b"14"), 6),
]


def pieces(data: bytes, count: int) -> list[bytes]:
    size = -(-len(data) // count)
    return [data[i : i + size] for i in range(0, len(data), size)]


@pytest.mark.parametrize("name, raw, count", DEADLINE, ids=[c[0] for c in DEADLINE])
def test_a_body_not_whole_by_the_deadline_is_answered_timeout_however_its_bytes_arrive(
    name, raw, count, monkeypatch, store
):
    monkeypatch.setattr(transport, "_DEADLINE", 0.6)
    a = Actor("http-deadline", store=store)
    service = Service(a)
    service.offer(Profile("p"), finish, RunRequest(emits=(Answer,)))

    async def main():
        async with serving(service) as port:
            data = raw.replace(b"{p}", str(port).encode())
            loop = asyncio.get_running_loop()
            began = loop.time()
            got = await ask(port, pieces(data, count), pause=0.05)
            return got, loop.time() - began

    (status, fields, body), took = bounded(main)
    assert (status, json.loads(body)["error"]) == (408, "timeout") and service.runs == {}
    assert_safe(fields)
    # one deadline over the whole body: a deadline per read would answer 0.6 seconds after the last
    assert 0.6 <= took < 1.2


# --- C10, D10: the caps -----------------------------------------------------------------------------


def test_a_request_past_the_cap_is_refused_busy_and_the_next_after_one_ends_is_not(monkeypatch, store):
    monkeypatch.setattr(transport, "_REQUESTS", 1)
    a = Actor("http-requests", store=store)
    service = Service(a)
    service.offer(Profile("p"), finish, RunRequest(emits=(Answer,)))

    async def main():
        async with serving(service) as port:
            held = await stream_open(port, "/events")  # a stream is a request in flight until it ends
            past = await ask(port, http("GET", "/", port))
            held.writer.transport.abort()
            await asyncio.sleep(0.2)
            after = await call(port, "GET", "/")
            left = set(service._subs)  # read while the service still serves: its end would close it too
        return past, after, left

    past, after, left = bounded(main)
    assert (past[0], json.loads(past[2])) == (
        503,
        {"error": "busy", "reason": "1 requests are in flight, the most this service takes"},
    )
    assert_safe(past[1])
    assert after == (200, service.card()) and left == set()


def test_a_stream_past_the_subscriber_cap_is_refused_busy(monkeypatch, store):
    monkeypatch.setattr(serve, "_SUBSCRIBERS", 1)
    a = Actor("http-subs", store=store)
    service = Service(a)
    service.offer(Profile("p"), finish, RunRequest(emits=(Answer,)))

    async def main():
        async with serving(service) as port:
            first = await stream_open(port, "/events")
            past = await call(port, "GET", "/events")
        return past, await first.drained()

    past, frames_ = bounded(main)
    assert past == (503, {"error": "busy", "reason": "1 streams are open, the most this service holds"})
    assert frames_ == [("runs", {"runs": []})] and service._subs == set()


def test_a_request_after_the_end_began_is_refused_busy(store):
    a = Actor("http-closing", store=store)
    service = Service(a)
    service.offer(Profile("p"), finish, RunRequest(emits=(Answer,)))

    async def main():
        async with serving(service) as port:
            service.halt()  # the end began; the listener is not yet closed
            return await ask(port, http("GET", "/", port))  # a route the service itself would answer

    status, fields, body = bounded(main)
    assert (status, json.loads(body)) == (503, {"error": "busy", "reason": "the service is ending"})
    assert_safe(fields)


def test_a_start_while_the_service_ends_is_refused_busy():
    a, gates = actor("http-ending-start")
    service = Service(a)
    service.offer(Profile("work"), turns('<lact a1>wait(key="g")</lact>', FINISH))

    async def main():
        bound = asyncio.get_running_loop().create_future()
        served = asyncio.ensure_future(
            serving_forever(service, "127.0.0.1", 0, grace=5.0, ready=lambda h, p: bound.set_result(p))
        )
        await bound
        await service.start({"offer": "work"})
        while "g" not in gates:  # the command is in flight
            await asyncio.sleep(0)
        served.cancel()
        while not service.closing:
            await asyncio.sleep(0)
        with pytest.raises(serve.ServeError) as refused:
            await service.start({"offer": "work"})
        gates["g"].set()  # the command ends, and the stopped run with it, inside the grace
        await asyncio.wait({served})
        return refused.value

    refused = bounded(main)
    assert (refused.code, refused.reason) == ("busy", "the service is ending")
    assert [s["outcome"] for s in service.runs_state()["runs"]] == [{"kind": "Stopped", "turn": 2}]


# --- D12: the stream --------------------------------------------------------------------------------


def test_a_stream_sends_a_comment_line_each_time_its_clock_steps_and_frames_in_between(monkeypatch, store):
    a = Actor("http-ping", store=store)
    service = Service(a)
    service.offer(Profile("p"), finish, RunRequest(emits=(Answer,)))
    ticks: asyncio.Queue = asyncio.Queue()
    asked: list[float] = []

    async def clock(seconds):
        asked.append(seconds)
        await ticks.get()

    monkeypatch.setattr(transport, "_sleep", clock)

    async def main():
        async with serving(service) as port:
            stream = await stream_open(port, "/events")
            first = await stream.frame()
            ticks.put_nowait(None)
            ping = await stream.frame()
            sid = (await call(port, "POST", "/runs", {"offer": "p"}))[1]["id"]
            await phase_reached(service, sid, "ended")
            ticks.put_nowait(None)
            await asyncio.sleep(0.05)
        rest = await stream.drained()
        return first, ping, sid, rest

    first, ping, sid, rest = bounded(main)
    assert frames(first) == [("runs", {"runs": []})] and ping == b": ping\n\n"
    assert set(asked) == {transport._PING} and len(asked) >= 2
    assert (None, None) in rest and [d["phase"] for n, d in rest if n == "state"] == ["working", "ended"]
    assert rest[[n for n, _ in rest].index("state")][1]["id"] == sid


# --- C3, C11, D10: a stalled reader, and the end ----------------------------------------------------


def small_window() -> socket.socket:
    """A client socket that takes little before its reader reads."""
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    s.setblocking(False)
    return s


async def stalled(port: int, path: str) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """A stream opened by a client that never reads past its head."""
    sock = small_window()
    await asyncio.get_running_loop().sock_connect(sock, ("127.0.0.1", port))
    reader, writer = await asyncio.open_connection(sock=sock)
    writer.write(http("GET", path, port))
    await reader.readuntil(b"\r\n\r\n")
    return reader, writer


BIG = "x" * 8_000_000


@pytest.mark.parametrize("reads", [True, False], ids=["a reader that reads", "a reader that stalls"])
def test_a_stream_whose_reader_stalls_is_closed_at_its_frame_cap_and_the_run_never_waits(reads, monkeypatch):
    """The large row fills what a stalled client takes, so the frames after it queue on the stream; one
    past the cap closes it, and the run goes on as if no one watched (C3). A client that reads takes every
    frame under the same cap."""
    monkeypatch.setattr(serve, "_FRAMES", 4)
    a = Actor("http-stall", store=unflushed_store())  # BIG is 8 MB: see `unflushed_store`
    service = Service(a)
    backend = Held()
    service.offer(Profile("talk"), backend, RunRequest(context_budget=10**8), converse=True)

    async def main():
        async with serving(service) as port:
            sid = (await call(port, "POST", "/runs", {"offer": "talk", "inputs": ["hi"]}))[1]["id"]
            if reads:
                stream = await stream_open(port, f"/runs/{sid}/events")
                seen = asyncio.ensure_future(stream.drained())
            else:
                _, writer = await stalled(port, f"/runs/{sid}/events")
            await backend.answer(BIG)
            await phase_reached(service, sid, "waiting")
            for text in ("one", "two", "three"):  # each turn queues frames on the stream
                while reads and any(sub._frames for sub in service._subs):  # the reader keeps up
                    await asyncio.sleep(0.01)
                await call(port, "POST", f"/runs/{sid}/input", {"text": text})
                await backend.answer("Noted.")
                await phase_reached(service, sid, "waiting")
            open_ = len(service._subs)
            state = service.end(sid)
            await phase_reached(service, sid, "ended")
            if reads:
                return open_, state, await seen
            writer.transport.abort()
            return open_, state, None

    open_, state, seen = bounded(main)
    assert state["closed"] is True
    assert service.state(state["id"])["outcome"] == {"kind": "Stopped", "turn": 5}
    if reads:
        assert open_ == 1 and seen[-1] == ("state", service.state(state["id"]))
    else:
        assert open_ == 0


def test_ending_the_service_refuses_admission_cuts_each_connection_and_ends_each_run(tmp_path, monkeypatch):
    """An idle stream, a stalled writer, a half-read request, a conversation waiting and a run
    mid-command: the end closes the streams first, so no stream takes a final state, then stops the runs
    while the connections close, so a run ends without waiting on the stalled write; each run's END row is
    written."""
    monkeypatch.setattr(transport, "_GRACE", 2.0)
    a, gates = actor("http-end")
    a.store = unflushed_store()  # BIG is 8 MB: see `unflushed_store`
    subscriptions = list(a.bus._subs)
    service = Service(a, dir=tmp_path)
    backend = Held()
    service.offer(Profile("work"), turns('<lact a1>wait(key="g")</lact>', FINISH))
    service.offer(Profile("talk"), backend, converse=True)

    async def main():
        loop = asyncio.get_running_loop()
        bound = loop.create_future()
        served = asyncio.ensure_future(
            serving_forever(service, "127.0.0.1", 0, grace=0.2, ready=lambda h, p: bound.set_result(p))
        )
        port = await bound
        stall = await stalled(port, "/events")  # the large row below fills what this client takes
        talk = (await call(port, "POST", "/runs", {"offer": "talk", "inputs": ["hi"]}))[1]["id"]
        await backend.answer(BIG)
        await phase_reached(service, talk, "waiting")
        work = (await call(port, "POST", "/runs", {"offer": "work"}))[1]["id"]
        while "g" not in gates:  # the command is in flight
            await asyncio.sleep(0)
        idle = await stream_open(port, f"/runs/{work}/events")
        half = await asyncio.open_connection("127.0.0.1", port)
        half[1].write(b"GET / HTTP/1.1\r\nHo")
        await half[1].drain()
        await asyncio.sleep(0.05)

        async def ended(sid):
            await phase_reached(service, sid, "ended")
            return loop.time() - began

        began = loop.time()
        served.cancel()
        stopped = asyncio.ensure_future(ended(talk))
        await asyncio.sleep(0.05)
        served.cancel()  # a second cancel while it ends: the end still runs to its close
        await asyncio.wait({served})
        took = loop.time() - began
        unanswered = await half[0].read()
        half[1].close()
        stall[1].transport.abort()
        seen = await idle.drained()
        try:
            await asyncio.open_connection("127.0.0.1", port)
        except ConnectionRefusedError:
            refused = True
        return served.cancelled(), took, await stopped, unanswered, seen, refused, talk, work

    cancelled, took, talk_took, unanswered, seen, refused, talk, work = bounded(main)
    assert cancelled and refused and unanswered == b""
    assert 2.0 <= took < 5.0  # the stalled write held the end for uvicorn's grace, and no longer
    assert talk_took < 1.0  # the conversation ended long before that grace was out
    assert service._subs == set() and service._all == set()
    assert a.bus._subs == subscriptions and service._by_run == {}
    assert {sid: service.state(sid)["phase"] for sid in (talk, work)} == {talk: "ended", work: "aborted"}
    assert service.state(talk)["outcome"] == {"kind": "Stopped", "turn": 2}
    for sid in (talk, work):
        assert log_lines(tmp_path / f"{sid}.jsonl")[-1] == {"kind": "END", "state": service.state(sid)}
    # the idle stream was closed before its run ended: its one frame is the state it opened with
    assert [(n, d["phase"]) for n, d in seen] == [("state", "working")]


def test_serving_leaves_the_signals_to_its_caller(store):
    """uvicorn would take SIGINT and SIGTERM for itself; served here, a Ctrl-C stays the caller's, and so
    reaches the serve as a cancel that stops the runs (C11)."""
    service = Service(Actor("http-signals", store=store))

    def handlers():
        return signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM)

    async def main():
        before = handlers()
        async with serving(service):
            return before, handlers()

    before, served = bounded(main)
    assert served == before


def test_an_ending_seen_through_a_cancel_that_then_fails_reaches_its_caller_and_no_one_else(monkeypatch):
    """`_through` waits for a shutdown step whatever cancels its caller. A step that fails after such a
    cancel is raised to the caller, and the loop's exception handler hears nothing: the wait is never a
    shield, which reports that failure as nobody's on CPython 3.14.7."""

    def refuse(*_args, **_kwargs):
        raise AssertionError("the service waits on a task without a shield")

    monkeypatch.setattr(asyncio, "shield", refuse)

    async def main():
        told: list = []
        asyncio.get_running_loop().set_exception_handler(
            lambda _loop, context: told.append(context["message"])
        )
        started, release = asyncio.Event(), asyncio.Event()

        async def step():
            started.set()
            await release.wait()
            raise RuntimeError("the step failed")

        waiter = asyncio.ensure_future(transport._through(step()))
        await started.wait()
        waiter.cancel()
        await asyncio.sleep(0)  # the cancel lands on the wait, which waits again
        still = not waiter.done()
        release.set()
        (raised,) = await asyncio.gather(waiter, return_exceptions=True)
        gc.collect()
        await asyncio.sleep(0)
        return still, raised, told

    still, raised, told = bounded(main, seconds=5.0)
    assert still and isinstance(raised, RuntimeError) and told == []
