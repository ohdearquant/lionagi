"""The served actor's HTTP transport (ADR-0021/D8): loopback only, one request per connection in one
strict form, answered in D9's shapes, streams as server-sent events, and an end that leaves nothing
running. Every test here speaks to a real socket on loopback."""

import asyncio
import contextlib
import gc
import json
import socket
from types import SimpleNamespace

import pytest

import lion1.serve as serve
from lion1 import Actor, Profile, RunRequest
from lion1.serve import Service
from tests.lionfake import unflushed_store
from tests.test_serve import FINISH, Answer, Held, actor, bounded, log_lines, turns


@contextlib.asynccontextmanager
async def serving(service: Service, host: str = "127.0.0.1", grace: float = 2.0):
    """`service` served on a free port for the block; cancelled, and seen to return, after it."""
    bound = asyncio.get_running_loop().create_future()
    task = asyncio.ensure_future(service.serve(host, 0, grace, ready=lambda h, p: bound.set_result(p)))
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
    lines = [f"{method} {path} HTTP/1.1", f"Host: {host if host is not None else f'127.0.0.1:{port}'}"]
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


def pieces(data: bytes, size: int = 7) -> list[bytes]:
    return [data[i : i + size] for i in range(0, len(data), size)]


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


SAFE = {"x-content-type-options": "nosniff", "cache-control": "no-store", "connection": "close"}


def assert_safe(fields: dict) -> None:
    assert {k: fields.get(k) for k in SAFE} == SAFE
    assert not [k for k in fields if k.startswith("access-control-")]


# --- D8: the routes ---------------------------------------------------------------------------------


async def stream_open(port: int, path: str) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, dict]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(http("GET", path, port))
    head = await reader.readuntil(b"\r\n\r\n")
    status, fields, _ = parsed(head)
    assert status == 200 and fields["content-type"] == "text/event-stream" and "content-length" not in fields
    assert_safe(fields)
    return reader, writer, fields


async def drained(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> list:
    """What a stream sent to its close."""
    body = await reader.read()
    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()
    return frames(body)


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
            got["events"] = await drained(*(await stream_open(port, f"/runs/{sid}/events"))[:2])
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
            got["talk"] = await drained(*(await stream_open(port, f"/runs/{talk}/events"))[:2])
            ids = [talk]
            for control in ("stop", "cancel"):
                ids.append((await call(port, "POST", "/runs", {"offer": "talk", "inputs": ["hi"]}))[1]["id"])
                await backend.next()
                got[control] = await call(port, "POST", f"/runs/{ids[-1]}/{control}", {})
                await drained(*(await stream_open(port, f"/runs/{ids[-1]}/events"))[:2])
        got["every"] = await drained(*every[:2])  # open until the server ended
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


D9_STATUS = {  # D9's table, written out here so a change to the service's own table cannot pass
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
            await drained(*(await stream_open(port, f"/runs/{ended}/events"))[:2])
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
                await drained(*(await stream_open(port, f"/runs/{sid}/events"))[:2])
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
    assert serve._STATUS == D9_STATUS
    for code, (status, doc) in got.items():
        assert status == D9_STATUS[code] and doc["error"] == code and doc["reason"], (code, doc)
        assert list(doc) == ["error", "reason"]
    assert set(got) == set(serve._STATUS) - {"timeout", "starting"}  # those two have tests of their own
    assert got["refused"][1]["reason"] == "RuntimeError: no plan today"


def serve_row(sid: str) -> dict:
    return {"kind": "SERVE", "version": 1, "id": sid, "offer": "task", "resumed": None, "replayed": 0}


# --- C10, D8: the raw request -----------------------------------------------------------------------

HOST = b"Host: 127.0.0.1:{p}\r\n"
JSON = b"Content-Type: application/json\r\n"


def post(body: bytes, *, path: bytes = b"/runs", length: bytes | None = None, kind: bytes = JSON) -> bytes:
    size = str(len(body)).encode() if length is None else length
    return b"POST " + path + b" HTTP/1.1\r\n" + HOST + kind + b"Content-Length: " + size + b"\r\n\r\n" + body


START = b'{"offer": "p"}'
LONG = "start=" + "9" * 5000  # past the digits `int` reads from a string
REFUSED = [
    ("HTTP/1.0", b"GET / HTTP/1.0\r\n" + HOST + b"\r\n", "bad_request"),
    ("another method", b"PUT / HTTP/1.1\r\n" + HOST + b"\r\n", "bad_request"),
    ("a preflight", b"OPTIONS / HTTP/1.1\r\n" + HOST + b"Origin: http://evil.example\r\n\r\n", "bad_request"),
    ("a method in lower case", b"get / HTTP/1.1\r\n" + HOST + b"\r\n", "bad_request"),
    ("the absolute form", b"GET http://127.0.0.1:{p}/ HTTP/1.1\r\n" + HOST + b"\r\n", "bad_request"),
    ("the asterisk form", b"GET * HTTP/1.1\r\n" + HOST + b"\r\n", "bad_request"),
    ("a space in the target", b"GET /a b HTTP/1.1\r\n" + HOST + b"\r\n", "bad_request"),
    ("two spaces", b"GET  / HTTP/1.1\r\n" + HOST + b"\r\n", "bad_request"),
    ("a broken escape", b"GET /%zz HTTP/1.1\r\n" + HOST + b"\r\n", "bad_request"),
    ("a fragment", b"GET /#top HTTP/1.1\r\n" + HOST + b"\r\n", "bad_request"),
    ("no version", b"GET /\r\n" + HOST + b"\r\n", "bad_request"),
    ("bare line feeds", b"GET / HTTP/1.1\nHost: 127.0.0.1:{p}\n\n", "bad_request"),
    ("one bare line feed", b"GET / HTTP/1.1\r\nHost: 127.0.0.1:{p}\nX-A: b\r\n\r\n", "bad_request"),
    ("a bare carriage return", b"GET / HTTP/1.1\r\nHost: 127.0.0.1:{p}\rX-A: b\r\n\r\n", "bad_request"),
    ("a space before the colon", b"GET / HTTP/1.1\r\nHost : 127.0.0.1:{p}\r\n\r\n", "bad_request"),
    ("a folded line", b"GET / HTTP/1.1\r\n" + HOST + b"X-A: one\r\n two\r\n\r\n", "bad_request"),
    ("a line folded by a tab", b"GET / HTTP/1.1\r\n" + HOST + b"X-A: one\r\n\ttwo\r\n\r\n", "bad_request"),
    ("a control character", b"GET / HTTP/1.1\r\n" + HOST + b"X-A: a\x01b\r\n\r\n", "bad_request"),
    ("a tab in a value", b"GET / HTTP/1.1\r\n" + HOST + b"X-A: a\tb\r\n\r\n", "bad_request"),
    ("a space in a name", b"GET / HTTP/1.1\r\n" + HOST + b"X A: b\r\n\r\n", "bad_request"),
    ("no colon", b"GET / HTTP/1.1\r\n" + HOST + b"Garbage\r\n\r\n", "bad_request"),
    ("an empty name", b"GET / HTTP/1.1\r\n" + HOST + b": b\r\n\r\n", "bad_request"),
    ("a byte past ASCII", b"GET / HTTP/1.1\r\n" + HOST + "X-A: é\r\n\r\n".encode(), "bad_request"),
    ("no Host", b"GET / HTTP/1.1\r\nX-A: b\r\n\r\n", "bad_request"),
    ("no header at all", b"GET / HTTP/1.1\r\n\r\n", "bad_request"),
    ("two Hosts", b"GET / HTTP/1.1\r\n" + HOST + HOST + b"\r\n", "bad_request"),
    (
        "two Origins",
        b"GET / HTTP/1.1\r\n" + HOST + b"Origin: http://127.0.0.1:{p}\r\n" * 2 + b"\r\n",
        "bad_request",
    ),
    ("two Content-Types", post(START).replace(JSON, JSON + JSON), "bad_request"),
    (
        "two lengths",
        post(START).replace(b"Content-Length", b"Content-Length: 14\r\ncontent-length"),
        "bad_request",
    ),
    ("Transfer-Encoding", post(START).replace(JSON, JSON + b"Transfer-Encoding: chunked\r\n"), "bad_request"),
    (
        "transfer-encoding",
        post(START).replace(JSON, JSON + b"transfer-encoding: identity\r\n"),
        "bad_request",
    ),
    ("Expect", post(START).replace(JSON, JSON + b"Expect: 100-continue\r\n"), "bad_request"),
    ("a GET with a length", b"GET /runs HTTP/1.1\r\n" + HOST + b"Content-Length: 2\r\n\r\n{}", "bad_request"),
    (
        "a GET with a bad length",
        b"GET /runs HTTP/1.1\r\n" + HOST + b"Content-Length: x\r\n\r\n",
        "bad_request",
    ),
    ("a POST with no length", post(START).replace(b"Content-Length: 14\r\n", b""), "bad_request"),
    ("a signed length", post(START, length=b"+14"), "bad_request"),
    ("a hex length", post(START, length=b"0xe"), "bad_request"),
    ("an empty length", post(START, length=b""), "bad_request"),
    ("a length past 1 MiB", post(b"", length=b"1048577"), "too_large"),
    ("bytes that are not UTF-8", post(b'{"offer": "\xff"}'), "bad_request"),
    ("bytes that are not JSON", post(b"offer=p"), "bad_request"),
    ("an empty body", post(b""), "bad_request"),
    ("a JSON array", post(b"[]"), "bad_request"),
    ("a key twice", post(b'{"offer": "p", "offer": "p"}'), "bad_request"),
    ("NaN", post(b'{"offer": "p", "bounds": {"time_budget": NaN}}'), "bad_request"),
    ("Infinity", post(b'{"offer": "p", "bounds": {"cost_budget": -Infinity}}'), "bad_request"),
    (
        "a parameter on the type",
        post(START, kind=b"Content-Type: application/json; charset=utf-8\r\n"),
        "unsupported_type",
    ),
    ("no type", post(START, kind=b""), "unsupported_type"),
    ("another type", post(START, kind=b"Content-Type: text/plain\r\n"), "unsupported_type"),
    ("a head past 16 KiB", b"GET / HTTP/1.1\r\n" + HOST + b"X-A: " + b"a" * 16384, "too_large"),
    ("a head one byte past 16 KiB", b"GET / HTTP/1.1\r\n" + HOST + b"X-A: {pad1}\r\n\r\n", "too_large"),
    ("65 header fields", b"GET / HTTP/1.1\r\n" + HOST + b"X-A: b\r\n" * 64 + b"\r\n", "too_large"),
    ("a length of 5,000 digits", post(START, length=b"9" * 5000), "too_large"),
    (
        "a GET with a length of 5,000 digits",
        b"GET / HTTP/1.1\r\n" + HOST + b"Content-Length: " + b"9" * 5000 + b"\r\n\r\n",
        "bad_request",
    ),
    (
        "a number of 5,000 digits in the body",
        post(b'{"offer": "p", "bounds": {"max_turns": ' + b"9" * 5000 + b"}}"),
        "bad_request",
    ),
]
# the refusals that only the body decides; every other one is decided by the head
BY_BODY = {
    "bytes that are not UTF-8",
    "bytes that are not JSON",
    "a JSON array",
    "a key twice",
    "NaN",
    "Infinity",
    "a number of 5,000 digits in the body",
}
assert BY_BODY <= {name for name, *_ in REFUSED}
# only the bytes that arrive with the head show a body where none may be: sent in pieces, what follows
# the head is never read, as a second request never is
WHOLE_ONLY = [
    ("a GET with a body", b"GET /runs HTTP/1.1\r\n" + HOST + b"\r\n{}", "bad_request"),
    ("bytes past the length", post(START + b"x", length=b"14"), "bad_request"),
]


def with_port(raw: bytes, port: int) -> bytes:
    """`raw` for this port; `{pad}` filled so the head is exactly 16 KiB, `{pad1}` so it is a byte past."""
    data = raw.replace(b"{p}", str(port).encode())
    for mark, size in ((b"{pad}", serve._HEAD), (b"{pad1}", serve._HEAD + 1)):
        if mark in data:
            data = data.replace(mark, b"a" * (size - len(data) + len(mark)))
            assert len(data) == size
    return data


def until_refused(data: bytes, by_body: bool) -> list[bytes]:
    """`data` in pieces up to the byte at which the server can refuse it, and nothing past it: bytes that
    reach a connection the server has closed reset it, and a reset can take the answer with it."""
    end, lf = data.find(b"\r\n\r\n"), serve._BARE_LF.search(data)
    if lf and (end < 0 or lf.start() < end):
        data = data[: lf.end()]
    elif end < 0:
        data = data[: serve._HEAD]
    elif not by_body:
        data = data[: end + 4]
    return pieces(data, max(7, len(data) // 40))


async def finish(messages):
    return FINISH


@pytest.mark.parametrize("split", ["whole", "in pieces"])
@pytest.mark.parametrize("name, raw, code", REFUSED, ids=[c[0] for c in REFUSED])
def test_a_request_outside_d8s_form_is_answered_with_its_error_and_never_dispatched(
    name, raw, code, split, store
):
    a = Actor("http-raw", store=store)
    service = Service(a)
    service.offer(Profile("p"), finish, RunRequest(emits=(Answer,)))

    async def main():
        async with serving(service) as port:
            data = with_port(raw, port)
            return await ask(
                port, data if split == "whole" else until_refused(data, name in BY_BODY), pause=0.002
            )

    status, fields, body = bounded(main)
    assert (status, json.loads(body)["error"]) == (D9_STATUS[code], code) and service.runs == {}
    assert_safe(fields)


def test_a_head_the_standard_librarys_parser_refuses_is_a_bad_request(monkeypatch, store):
    """D8's own field cap sits under the parser's, so the parser's refusal is reached here by lowering
    the parser's: whatever it refuses is answered, never left to end the connection's task."""
    monkeypatch.setattr(serve.http.client, "_MAXHEADERS", 1)
    a = Actor("http-parser-refuses", store=store)
    service = Service(a)
    service.offer(Profile("p"), finish, RunRequest(emits=(Answer,)))

    async def main():
        async with serving(service) as port:
            return await ask(port, with_port(b"GET / HTTP/1.1\r\n" + HOST + b"X-A: b\r\n\r\n", port))

    status, fields, body = bounded(main)
    doc = json.loads(body)
    assert (status, doc["error"]) == (400, "bad_request") and doc["reason"].startswith(
        "the headers do not parse"
    )
    assert_safe(fields)


@pytest.mark.parametrize("name, raw, code", WHOLE_ONLY, ids=[c[0] for c in WHOLE_ONLY])
def test_a_body_that_arrives_with_the_head_where_none_may_be_is_refused(name, raw, code, store):
    test_a_request_outside_d8s_form_is_answered_with_its_error_and_never_dispatched(
        name, raw, code, "whole", store
    )


ACCEPTED = [
    ("a GET with a length of 0", b"GET / HTTP/1.1\r\n" + HOST + b"Content-Length: 0\r\n\r\n", 200),
    ("the type in capitals", post(START, kind=b"Content-Type: APPLICATION/JSON\r\n"), 201),
    ("spaces around a value", post(START).replace(b"Content-Length: 14", b"Content-Length:   14  "), 201),
    ("a head of exactly 16 KiB", b"GET / HTTP/1.1\r\n" + HOST + b"X-A: {pad}\r\n\r\n", 200),
    ("64 header fields", b"GET / HTTP/1.1\r\n" + HOST + b"X-A: b\r\n" * 63 + b"\r\n", 200),
    ("a length under 5,000 zeros", post(START, length=b"0" * 5000 + b"14"), 201),
    (
        "a GET with a length of 5,000 zeros",
        b"GET / HTTP/1.1\r\n" + HOST + b"Content-Length: " + b"0" * 5000 + b"\r\n\r\n",
        200,
    ),
]


@pytest.mark.parametrize("split", ["whole", "in pieces"])
@pytest.mark.parametrize("name, raw, status", ACCEPTED, ids=[c[0] for c in ACCEPTED])
def test_a_request_in_d8s_form_is_dispatched_however_its_bytes_arrive(name, raw, status, split, store):
    a = Actor("http-raw-ok", store=store)
    service = Service(a)
    service.offer(Profile("p"), finish, RunRequest(emits=(Answer,)))

    async def main():
        async with serving(service) as port:
            data = with_port(raw, port)
            return await ask(port, data if split == "whole" else pieces(data, 1000), pause=0.002)

    got, fields, body = bounded(main)
    assert got == status, body
    assert_safe(fields)


def test_a_body_nested_past_the_recursion_limit_is_a_bad_request(no_collection, store):
    a = Actor("http-deep", store=store)
    service = Service(a)
    service.offer(Profile("p"), finish)

    async def main():
        async with serving(service) as port:
            return await ask(port, with_port(post(b"[" * 100_000), port))

    status, fields, body = bounded(main)
    assert status == 400 and json.loads(body)["error"] == "bad_request" and service.runs == {}


async def phase_reached(service: Service, sid: str, phase: str) -> None:
    sub = service.events(sid)
    try:
        async for name, data in sub:
            if name == "state" and data["phase"] == phase:
                return
    finally:
        sub.close()
    raise AssertionError(f"the stream of {sid} closed before {phase}")


def test_a_route_outside_the_table_or_a_query_where_none_is_taken_is_refused():
    a, _ = actor("http-edges")
    service = Service(a)
    service.offer(Profile("p"), finish, RunRequest(emits=(Answer,)))

    async def main():
        got = {}
        async with serving(service) as port:
            sid = (await call(port, "POST", "/runs", {"offer": "p"}))[1]["id"]
            await drained(*(await stream_open(port, f"/runs/{sid}/events"))[:2])
            for path in ("/?a=1", "/runs?start=1", "/runs?", f"/runs/{sid}?x", f"/runs/{sid}/events?x"):
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
            for path in ("/runs/", f"/runs/{sid}/nope", "//", "/runs/x/y/z", "/events/", "/run"):
                got[path] = await call(port, "GET", path)
            got["POST /"] = await call(port, "POST", "/", {})
            got["POST record"] = await call(port, "POST", f"/runs/{sid}/record", {})
            got["GET input"] = await call(port, "GET", f"/runs/{sid}/input")
            got["stop with a body"] = await call(port, "POST", f"/runs/{sid}/stop", {"now": True})
            for body in ({}, {"text": "x", "extra": 1}, {"text": "x", "once": None}):
                got[f"input {json.dumps(body)}"] = await call(port, "POST", f"/runs/{sid}/input", body)
            got["record 3"] = await call(port, "GET", f"/runs/{sid}/record?start=3")
        return sid, got

    sid, got = bounded(main)
    rows = len(service.record(sid)["rows"])
    assert rows == 3, "the end of the record is the last start a read takes"
    want = {
        **dict.fromkeys(["/?a=1", "/runs?start=1", "/runs?", f"/runs/{sid}?x", f"/runs/{sid}/events?x"], 400),
        **dict.fromkeys(["start=x", "foo=1", "start=", "start=1&start=2", "start=-1", "start=4"], 400),
        **dict.fromkeys([LONG, "start=" + "1" * 19], 400),  # a start `int` would refuse to read is no start
        "/runs/": 404,
        f"/runs/{sid}/nope": 404,
        "//": 404,
        "/runs/x/y/z": 404,
        "/events/": 404,
        "/run": 404,
        "POST /": 405,
        "POST record": 405,
        "GET input": 405,
        "stop with a body": 400,
        "input {}": 400,
        'input {"text": "x", "extra": 1}': 400,
        'input {"text": "x", "once": null}': 400,
    }
    assert {k: v[0] for k, v in got.items() if k != "record 3"} == want
    assert got["/runs/"][1]["error"] == "unknown_run" and got["//"][1]["error"] == "not_found"
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
                ):
                    raw = http(method, path, port, body, headers=headers, host=host.replace("{p}", str(port)))
                    status, fields, data = await ask(port, raw, host=bind)
                    assert_safe(fields)
                    got.append((host, origin, path, method, status, json.loads(data).get("error")))
        return got

    got = bounded(main)
    for host, origin, admitted in cases:
        answers = {(m, p): (s, e) for h, o, p, m, s, e in got if (h, o) == (host, origin)}
        if admitted:
            assert answers == {
                ("GET", "/"): (200, None),
                ("GET", "/runs"): (200, None),
                ("POST", "/runs"): (201, None),
            }
        else:
            assert set(answers.values()) == {(403, "forbidden")}, (host, origin, answers)
    assert len(service.runs) == sum(admitted for *_, admitted in cases)


@pytest.mark.parametrize("peer", ["192.0.2.7", "10.0.0.1", "::ffff:192.0.2.7", "", "not an address"])
def test_a_peer_off_loopback_is_forbidden_before_any_byte_of_its_request_is_read(peer, monkeypatch, store):
    a = Actor("http-peer", store=store)
    service = Service(a)
    service.offer(Profile("p"), finish, RunRequest(emits=(Answer,)))
    seen = []
    monkeypatch.setattr(serve, "_peer", lambda writer: seen.append(writer) or peer)

    async def main():
        async with serving(service) as port:
            status, _, body = await ask(port, [])  # it sends nothing: the answer needs none of it
            return status, json.loads(body)

    assert bounded(main) == (403, {"error": "forbidden", "reason": "the peer is not loopback"})
    assert service.runs == {} and len(seen) == 1


def test_the_peer_is_read_from_the_connection_and_only_a_loopback_address_passes():
    assert serve._peer(SimpleNamespace(get_extra_info={"peername": ("127.0.0.1", 5)}.get)) == "127.0.0.1"
    assert serve._peer(SimpleNamespace(get_extra_info={}.get)) == ""
    assert [serve._loopback(h) for h in ("127.0.0.1", "127.9.9.9", "::1", "192.0.2.7", "", "x")] == [
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
    binds = []

    async def recorder(*args, **kwargs):
        binds.append(args)

    monkeypatch.setattr(serve.asyncio, "start_server", recorder)
    with pytest.raises(ValueError, match="binds 127.0.0.1 or ::1, nothing else"):
        bounded(lambda: service.serve(host, 0))
    assert binds == [] and service.port is None


# --- C10: the deadline ------------------------------------------------------------------------------


DEADLINE = [  # what is sent, and in how many pieces 0.05 seconds apart: all of it well before the deadline
    ("a half head", b"GET / HTTP/1.1\r\nHo", 1),
    ("a head sent slowly", b"GET / HTTP/1.1\r\nHost: 127.0.0.1:{p}\r\nX-A: b", 6),
    ("a short body", post(b'{"offer"', length=b"14"), 1),
    ("a body sent slowly", post(b'{"offer": "p"', length=b"14"), 6),
]


@pytest.mark.parametrize("name, raw, count", DEADLINE, ids=[c[0] for c in DEADLINE])
def test_a_request_not_whole_by_the_deadline_is_answered_timeout_however_its_bytes_arrive(
    name, raw, count, monkeypatch, store
):
    monkeypatch.setattr(serve, "_DEADLINE", 0.6)
    a = Actor("http-deadline", store=store)
    service = Service(a)
    service.offer(Profile("p"), finish, RunRequest(emits=(Answer,)))

    async def main():
        async with serving(service) as port:
            data = with_port(raw, port)
            loop = asyncio.get_running_loop()
            began = loop.time()
            got = await ask(port, pieces(data, -(-len(data) // count)), pause=0.05)
            return got, loop.time() - began

    (status, fields, body), took = bounded(main)
    assert (status, json.loads(body)["error"]) == (408, "timeout") and service.runs == {}
    assert_safe(fields)
    # one deadline from the first byte: a deadline per read would answer 0.6 seconds after the last
    assert 0.6 <= took < 0.85


@pytest.mark.parametrize("sent", [b"", b"GET / HTTP/1.1\r\nHo", post(b'{"of', length=b"14")])
def test_a_peer_that_closes_before_its_request_is_whole_is_never_answered(sent, store):
    a = Actor("http-eof", store=store)
    service = Service(a)
    service.offer(Profile("p"), finish, RunRequest(emits=(Answer,)))

    async def main():
        async with serving(service) as port:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(with_port(sent, port))
            writer.write_eof()
            got = await reader.read()
            writer.close()
            await writer.wait_closed()
            await asyncio.sleep(0)
            return got, set(service._conns)

    assert bounded(main) == (b"", set()) and service.runs == {}


def test_a_head_that_parses_with_a_defect_is_refused(monkeypatch):
    defect = SimpleNamespace(defects=["a defect the parser found"])
    monkeypatch.setattr(serve.http.client, "parse_headers", lambda fp: defect)
    with pytest.raises(serve.ServeError, match="parse with a defect") as e:
        serve._head(b"GET / HTTP/1.1\r\nHost: 127.0.0.1:1")
    assert e.value.code == "bad_request"


# --- C10, D10: the caps -----------------------------------------------------------------------------


def test_a_connection_past_the_cap_is_refused_busy_and_the_next_after_one_closes_is_not(monkeypatch, store):
    monkeypatch.setattr(serve, "_CONNECTIONS", 1)
    a = Actor("http-conns", store=store)
    service = Service(a)
    service.offer(Profile("p"), finish, RunRequest(emits=(Answer,)))

    async def main():
        async with serving(service) as port:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            head = http("GET", "/", port)
            writer.write(head[:10])  # held open, its request half sent
            await writer.drain()
            while not service._conns:
                await asyncio.sleep(0)
            past = await ask(port, [])  # the peer sends nothing, so its close carries no reset
            writer.write(head[10:])
            first = parsed(await reader.read())
            writer.close()
            await writer.wait_closed()
            while service._conns:
                await asyncio.sleep(0)
            after = await call(port, "GET", "/")
        return past, first, after

    past, first, after = bounded(main)
    assert (past[0], json.loads(past[2])) == (503, {"error": "busy", "reason": "1 connections are open"})
    assert_safe(past[1])
    assert first[0] == 200 and after == (200, service.card())


def test_a_stream_past_the_subscriber_cap_is_refused_busy(monkeypatch, store):
    monkeypatch.setattr(serve, "_SUBSCRIBERS", 1)
    a = Actor("http-subs", store=store)
    service = Service(a)
    service.offer(Profile("p"), finish, RunRequest(emits=(Answer,)))

    async def main():
        async with serving(service) as port:
            first = await stream_open(port, "/events")
            past = await call(port, "GET", "/events")
        return past, await drained(*first[:2])

    past, frames_ = bounded(main)
    assert past == (503, {"error": "busy", "reason": "1 streams are open, the most this service holds"})
    assert frames_ == [("runs", {"runs": []})] and service._subs == set()


def test_a_request_read_whole_after_the_end_began_is_refused_busy(store):
    a = Actor("http-closing", store=store)
    service = Service(a)
    service.offer(Profile("p"), finish, RunRequest(emits=(Answer,)))

    async def main():
        async with serving(service) as port:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            raw = with_port(post(START), port)
            writer.write(raw[:20])
            await writer.drain()
            while not service._conns:
                await asyncio.sleep(0)
            service._closing = True  # the end began between the read and the dispatch
            writer.write(raw[20:])
            got = parsed(await reader.read())
            writer.close()
            await writer.wait_closed()
            return got

    status, fields, body = bounded(main)
    assert (status, json.loads(body)) == (503, {"error": "busy", "reason": "the service is ending"})
    assert service.runs == {}


def test_a_start_while_the_service_ends_is_refused_busy():
    a, gates = actor("http-ending-start")
    service = Service(a)
    service.offer(Profile("work"), turns('<lact a1>wait(key="g")</lact>', FINISH))

    async def main():
        served = asyncio.ensure_future(service.serve("127.0.0.1", 0, grace=5.0))
        while service.port is None:
            await asyncio.sleep(0)
        await service.start({"offer": "work"})
        while "g" not in gates:  # the command is in flight
            await asyncio.sleep(0)
        served.cancel()
        while not service._closing:
            await asyncio.sleep(0)
        with pytest.raises(serve.ServeError) as refused:
            await service.start({"offer": "work"})
        gates["g"].set()  # the command ends, and the stopped run with it, inside the grace
        await asyncio.wait({served})
        return refused.value

    refused = bounded(main)
    assert (refused.code, refused.reason) == ("busy", "the service is ending")
    assert [s["outcome"] for s in service.runs_state()["runs"]] == [{"kind": "Stopped", "turn": 2}]


def test_a_connection_that_arrives_while_the_service_ends_is_cut_and_never_held(store):
    service = Service(Actor("http-late", store=store))
    service._closing = True
    cut = []
    service._accept(None, SimpleNamespace(transport=SimpleNamespace(abort=lambda: cut.append(True))))
    assert cut == [True] and service._conns == set()


# --- D12: the stream --------------------------------------------------------------------------------


def test_a_stream_sends_a_comment_line_each_time_its_clock_steps_and_frames_in_between(store):
    a = Actor("http-ping", store=store)
    service = Service(a)
    service.offer(Profile("p"), finish, RunRequest(emits=(Answer,)))
    ticks: asyncio.Queue = asyncio.Queue()
    asked: list[float] = []

    async def clock(seconds):
        asked.append(seconds)
        await ticks.get()

    service._sleep = clock

    async def main():
        async with serving(service) as port:
            reader, writer, _ = await stream_open(port, "/events")
            first = await reader.readuntil(b"\n\n")
            ticks.put_nowait(None)
            ping = await reader.readuntil(b"\n\n")
            sid = (await call(port, "POST", "/runs", {"offer": "p"}))[1]["id"]
            await phase_reached(service, sid, "ended")
            ticks.put_nowait(None)
        rest = await drained(reader, writer)
        return first, ping, sid, rest

    first, ping, sid, rest = bounded(main)
    assert frames(first) == [("runs", {"runs": []})] and ping == b": ping\n\n"
    assert set(asked) == {serve._PING} and len(asked) >= 2
    assert (None, None) in rest and [d["phase"] for n, d in rest if n == "state"] == ["working", "ended"]
    assert rest[[n for n, _ in rest].index("state")][1]["id"] == sid


# --- C10, C11, D10: writes are bounded, and the end ---------------------------------------------------


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


def test_a_stream_whose_reader_stalls_is_cut_at_the_write_bound_and_the_run_never_waits(monkeypatch):
    monkeypatch.setattr(serve, "_WRITE", 0.2)
    a = Actor("http-stall", store=unflushed_store())  # BIG is 8 MB: see `unflushed_store`
    service = Service(a)
    backend = Held()
    service.offer(Profile("talk"), backend, converse=True)

    async def main():
        async with serving(service) as port:
            sid = (await call(port, "POST", "/runs", {"offer": "talk", "inputs": ["hi"]}))[1]["id"]
            reader, writer = await stalled(port, f"/runs/{sid}/events")
            loop = asyncio.get_running_loop()
            await backend.answer(BIG)
            await phase_reached(service, sid, "waiting")
            waited = loop.time()
            while service._subs:
                await asyncio.sleep(0.01)
            cut = loop.time() - waited
            state = service.end(sid)
            await phase_reached(service, sid, "ended")
            writer.transport.abort()
            return cut, state

    cut, state = bounded(main)
    assert cut < 2.0 and state["closed"] is True
    assert service.state(state["id"])["outcome"] == {"kind": "Stopped", "turn": 2}
    assert service._conns == set()


def test_ending_the_service_refuses_admission_cuts_each_connection_and_ends_each_run(tmp_path, monkeypatch):
    """An idle stream, a stalled writer, a half-read request and a run mid-command: the end cuts the
    connections first, so no stream takes a final state, then ends the runs and writes their END rows."""
    monkeypatch.setattr(serve, "_WRITE", 1.0)
    a, gates = actor("http-end")
    a.store = unflushed_store()  # BIG is 8 MB: see `unflushed_store`
    subscriptions = list(a.bus._subs)
    service = Service(a, dir=tmp_path)
    backend = Held()
    service.offer(Profile("work"), turns('<lact a1>wait(key="g")</lact>', FINISH))
    service.offer(Profile("talk"), backend, converse=True)

    async def main():
        loop = asyncio.get_running_loop()
        # no `ready`: the port is read off the service
        served = asyncio.ensure_future(service.serve("127.0.0.1", 0, grace=0.2))
        while service.port is None:
            await asyncio.sleep(0)
        port = service.port
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
        while len(service._conns) < 3:
            await asyncio.sleep(0)
        began = loop.time()
        served.cancel()
        await asyncio.sleep(0.05)
        served.cancel()  # a second cancel while it ends: the end still runs to its close
        await asyncio.wait({served})
        took = loop.time() - began
        unanswered = await half[0].read()
        half[1].close()
        stall[1].transport.abort()
        seen = await drained(*idle[:2])
        try:
            await asyncio.open_connection("127.0.0.1", port)
        except ConnectionRefusedError:
            refused = True
        return served.cancelled(), took, unanswered, seen, refused, talk, work

    cancelled, took, unanswered, seen, refused, talk, work = bounded(main)
    assert cancelled and refused and unanswered == b""
    assert 1.0 <= took < 4.0  # the stalled write held the end for its bound, and no longer
    assert service._conns == set() and service._subs == set() and service._all == set()
    assert a.bus._subs == subscriptions and service._by_run == {}
    assert {sid: service.state(sid)["phase"] for sid in (talk, work)} == {talk: "ended", work: "aborted"}
    assert service.state(talk)["outcome"] == {"kind": "Stopped", "turn": 2}
    for sid in (talk, work):
        assert log_lines(tmp_path / f"{sid}.jsonl")[-1] == {"kind": "END", "state": service.state(sid)}
    # the idle stream was cut before its run ended: its one frame is the state it opened with
    assert [(n, d["phase"]) for n, d in seen] == [("state", "working")]


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

        waiter = asyncio.ensure_future(serve._through(step()))
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


@pytest.mark.parametrize("step", ["write", "close"])
def test_a_write_or_a_close_a_peer_never_takes_ends_at_its_bound(step, monkeypatch):
    """A peer that takes nothing holds no connection past the bound: the write raises, the close cuts."""
    monkeypatch.setattr(serve, "_WRITE", 0.05)

    async def main():
        never = asyncio.get_running_loop().create_future()
        cut = []

        async def held():
            await never

        writer = SimpleNamespace(
            write=lambda data: None,
            drain=held,
            close=lambda: None,
            wait_closed=held,
            transport=SimpleNamespace(abort=lambda: cut.append(True)),
        )
        begun = asyncio.get_running_loop().time()
        try:
            await (serve._write(writer, b"x") if step == "write" else serve._close(writer))
            raised = None
        except TimeoutError as e:
            raised = type(e)
        took = asyncio.get_running_loop().time() - begun
        never.cancel()
        return raised, cut, took

    raised, cut, took = bounded(main, seconds=2.0)
    assert (raised, cut) == ((TimeoutError, []) if step == "write" else (None, [True]))
    assert 0.05 <= took < 1.0


@pytest.mark.parametrize("step", ["write", "close"])
def test_a_cancel_that_lands_as_a_bounded_write_finishes_is_never_lost(step):
    """The step a write's drain, or a close, finishes in is the step a shutdown's cancel may land in:
    the cancel still ends the connection, or the end would wait on it forever."""

    async def main():
        gate = asyncio.get_running_loop().create_future()
        cut = []

        async def held():
            await gate

        writer = SimpleNamespace(
            write=lambda data: None,
            drain=held,
            close=lambda: None,
            wait_closed=held,
            transport=SimpleNamespace(abort=lambda: cut.append(True)),
        )
        task = asyncio.ensure_future(serve._write(writer, b"x") if step == "write" else serve._close(writer))
        await asyncio.sleep(0)  # the step is awaiting its peer
        gate.set_result(None)
        task.cancel()
        await asyncio.wait({task})
        return task.cancelled(), cut

    assert bounded(main) == (True, [True] if step == "close" else [])
