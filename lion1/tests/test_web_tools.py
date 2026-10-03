"""The web tools: each provider's adapter against a recorded response with the HTTP call replaced (no
network in a default run), the failures a provider can answer with, the page reader's refusals, and
the two commands driven through a real Actor. One live test per provider runs only when its variable
is set."""

import asyncio
import http.client
import http.server
import importlib
import io
import ipaddress
import json
import os
import re
import socket
import ssl
import threading
import time
import types
import urllib.error
import urllib.parse
import urllib.request
from email.message import Message

import pytest

from hub import tools
from hub.tools.web import Brave, Hit, Jina, SearXNG, Tavily, WebError, WebFetch, WebSearch, provider_from_env
from lion1 import Actor, Kind, Profile, RunRequest
from lion1.actor import DEFAULT_HANDLERS
from lion1.spec import render_guidance
from tests.test_actor import scripted

w = importlib.import_module("hub.tools.web")  # `hub.tools.web` the attribute is the function
KEY = "k-SECRET-123"
PUBLIC = [(2, 1, 6, "", ("93.184.216.34", 443))]


class Resp:
    """What `urlopen` hands back: a body, headers, a context manager."""

    length = None  # no declared length: the body ends where it ends

    def __init__(self, body: bytes | str, ctype: str | None = "application/json"):
        self.body = body.encode() if isinstance(body, str) else body
        self.headers = Message()
        if ctype:
            self.headers["Content-Type"] = ctype

    def read(self, n: int = -1) -> bytes:
        self.asked = n  # what the caller was willing to read
        return self.body if n < 0 else self.body[:n]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Wire:
    """Replaces `_urlopen`: records every request, answers from a queue, raises what it is handed."""

    def __init__(self):
        self.requests: list[urllib.request.Request] = []
        self.answers: list = []

    def __call__(self, req, timeout, guard=False):
        self.requests.append(req)
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


@pytest.fixture
def wire(monkeypatch):
    wire = Wire()
    monkeypatch.setattr(w, "_urlopen", wire)
    return wire


@pytest.fixture
def public_dns(monkeypatch):
    monkeypatch.setattr(w.socket, "getaddrinfo", lambda *a, **k: PUBLIC)


def headers(req: urllib.request.Request) -> dict[str, str]:
    return {k.lower(): v.replace(KEY, "<key>") for k, v in req.header_items()}


def query(req: urllib.request.Request) -> dict[str, str]:
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(req.full_url).query))


def run(coro):
    return asyncio.run(coro)


def http_error(code: int, body: str = "") -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://x.example/", code, "err", Message(), io.BytesIO(body.encode()))


# --- recorded responses ----------------------------------------------------------------------------

SEARXNG_BODY = {
    "query": "asyncio taskgroup",
    "number_of_results": 0,
    "results": [
        {
            "title": "TaskGroup",
            "url": "https://docs.python.org/3/library/asyncio-task.html",
            "content": "A context manager that holds a group of tasks.",
            "publishedDate": "2024-05-01T00:00:00",
            "engine": "duckduckgo",
        },
        {"title": "No snippet", "url": "https://example.org/a", "publishedDate": None},
        {"title": "Dropped: no url", "content": "x"},
        {"title": "Third", "url": "https://example.org/b", "content": "third"},
    ],
    "answers": [],
    "suggestions": [],
}
BRAVE_BODY = {
    "type": "search",
    "query": {"original": "asyncio taskgroup", "more_results_available": True},
    "web": {
        "type": "search",
        "results": [
            {
                "title": "asyncio.TaskGroup",
                "url": "https://docs.python.org/3/library/asyncio-task.html",
                "description": "Run tasks in a group.",
                "page_age": "2024-05-01T00:00:00",
                "age": "2 years ago",
            },
            {"title": "Older", "url": "https://example.org/old", "description": "d", "age": "3 weeks ago"},
        ],
    },
}
TAVILY_BODY = {
    "query": "asyncio taskgroup",
    "results": [
        {
            "title": "TaskGroup",
            "url": "https://docs.python.org/3/library/asyncio-task.html",
            "content": "A context manager that holds a group of tasks.",
            "published_date": "Wed, 01 May 2024 00:00:00 GMT",
            "score": 0.91,
        },
        {
            "title": "Other",
            "url": "https://example.org/o",
            "content": "o",
            "published_date": None,
            "score": 0.5,
        },
    ],
    "response_time": 0.8,
}
JINA_BODY = {
    "code": 200,
    "status": 20000,
    "data": [
        {
            "title": "TaskGroup",
            "url": "https://docs.python.org/3/library/asyncio-task.html",
            "description": "A context manager that holds a group of tasks.",
            "publishedTime": "2024-05-01",
            "content": "# TaskGroup\n" + "long page text " * 200,
            "usage": {"tokens": 1000},
        },
        {
            "title": "Only content",
            "url": "https://example.org/c",
            "content": "c" * 3000,
            "date": "2024-06-01",
        },
        {"title": "Third", "url": "https://example.org/d", "description": "third"},
    ],
}
DOCS = "https://docs.python.org/3/library/asyncio-task.html"

PROVIDERS = [
    pytest.param(lambda: SearXNG("https://searx.example/"), SEARXNG_BODY, id="searxng"),
    pytest.param(lambda: Brave(KEY), BRAVE_BODY, id="brave"),
    pytest.param(lambda: Tavily(KEY), TAVILY_BODY, id="tavily"),
    pytest.param(lambda: Jina(KEY), JINA_BODY, id="jina"),
]


# --- SearXNG ---------------------------------------------------------------------------------------


def test_searxng_builds_its_request_and_parses_its_hits(wire):
    wire.answers = [Resp(json.dumps(SEARXNG_BODY))]
    hits = run(
        SearXNG("https://searx.example/").search(
            WebSearch(query="asyncio taskgroup", count=2, recency="year")
        )
    )
    (req,) = wire.requests
    assert req.get_method() == "GET"
    assert req.full_url.startswith("https://searx.example/search?")
    assert query(req) == {"q": "asyncio taskgroup", "format": "json", "time_range": "year"}
    assert headers(req) == {"accept": "application/json"}
    assert hits == [
        Hit(
            title="TaskGroup",
            url=DOCS,
            snippet="A context manager that holds a group of tasks.",
            published="2024-05-01T00:00:00",
        ),
        Hit(title="No snippet", url="https://example.org/a", snippet=""),
    ]  # trimmed to count; the entry with no url is dropped


def test_searxng_has_no_week_and_no_site_and_says_so(wire):
    p = SearXNG("https://searx.example")
    with pytest.raises(WebError, match="does not support recency"):
        run(p.search(WebSearch(query="x", recency="week")))
    with pytest.raises(WebError, match="does not support site"):
        run(p.search(WebSearch(query="x", site="docs.python.org")))
    assert wire.requests == []
    assert query(p.request(WebSearch(query="x", recency="day")))["time_range"] == "day"
    assert "time_range" not in query(p.request(WebSearch(query="x")))


def test_searxng_refuses_a_url_that_is_not_an_instance():
    with pytest.raises(WebError, match="not an http or https instance URL"):
        SearXNG("searx.example")


def test_searxng_whose_json_is_off_answers_403(wire):
    wire.answers = [http_error(403)]
    with pytest.raises(WebError, match=r"searxng: HTTP 403, the credentials or the instance's settings"):
        run(SearXNG("https://searx.example").search(WebSearch(query="x")))


# --- Brave -----------------------------------------------------------------------------------------


def test_brave_builds_its_request_and_parses_its_hits(wire):
    wire.answers = [Resp(json.dumps(BRAVE_BODY))]
    hits = run(
        Brave(KEY).search(
            WebSearch(query="asyncio taskgroup", count=7, site="docs.python.org", recency="month")
        )
    )
    (req,) = wire.requests
    assert req.get_method() == "GET"
    assert req.full_url.startswith("https://api.search.brave.com/res/v1/web/search?")
    assert query(req) == {"q": "site:docs.python.org asyncio taskgroup", "count": "7", "freshness": "pm"}
    assert headers(req) == {"accept": "application/json", "x-subscription-token": "<key>"}
    assert hits == [
        Hit(
            title="asyncio.TaskGroup",
            url=DOCS,
            snippet="Run tasks in a group.",
            published="2024-05-01T00:00:00",
        ),
        Hit(title="Older", url="https://example.org/old", snippet="d", published="3 weeks ago"),
    ]


@pytest.mark.parametrize(
    ("recency", "freshness"), [("day", "pd"), ("week", "pw"), ("month", "pm"), ("year", "py")]
)
def test_brave_maps_each_recency(recency, freshness):
    assert query(Brave(KEY).request(WebSearch(query="x", recency=recency)))["freshness"] == freshness
    assert "freshness" not in query(Brave(KEY).request(WebSearch(query="x")))
    assert query(Brave(KEY).request(WebSearch(query="x")))["q"] == "x"


def test_brave_with_no_web_section_has_no_hits(wire):
    wire.answers = [Resp(json.dumps({"type": "search", "query": {"original": "x"}}))]
    assert run(Brave(KEY).search(WebSearch(query="x"))) == []


# --- Tavily ----------------------------------------------------------------------------------------


def test_tavily_builds_its_request_and_parses_its_hits(wire):
    wire.answers = [Resp(json.dumps(TAVILY_BODY))]
    hits = run(
        Tavily(KEY).search(
            WebSearch(query="asyncio taskgroup", count=3, site="docs.python.org", recency="week")
        )
    )
    (req,) = wire.requests
    assert req.get_method() == "POST"
    assert req.full_url == "https://api.tavily.com/search"
    assert headers(req) == {"content-type": "application/json", "authorization": "Bearer <key>"}
    assert json.loads(req.data) == {
        "query": "asyncio taskgroup",
        "max_results": 3,
        "include_published_date": True,
        "include_domains": ["docs.python.org"],
        "time_range": "week",
    }
    assert hits == [
        Hit(
            title="TaskGroup",
            url=DOCS,
            snippet="A context manager that holds a group of tasks.",
            published="Wed, 01 May 2024 00:00:00 GMT",
        ),
        Hit(title="Other", url="https://example.org/o", snippet="o"),
    ]


def test_tavily_sends_only_the_filters_it_was_given():
    body = json.loads(Tavily(KEY).request(WebSearch(query="x")).data)
    assert body == {"query": "x", "max_results": 5, "include_published_date": True}


# --- Jina ------------------------------------------------------------------------------------------


def test_jina_builds_its_request_and_parses_its_hits(wire):
    wire.answers = [Resp(json.dumps(JINA_BODY))]
    hits = run(Jina(KEY).search(WebSearch(query="asyncio taskgroup", count=2, site="docs.python.org")))
    (req,) = wire.requests
    assert req.get_method() == "GET"
    assert req.full_url.startswith("https://s.jina.ai/search?")
    assert query(req) == {"q": "asyncio taskgroup", "num": "2", "site": "docs.python.org"}
    assert headers(req) == {"accept": "application/json", "authorization": "Bearer <key>"}
    assert [h.title for h in hits] == ["TaskGroup", "Only content"]  # trimmed to count
    assert (
        hits[0].snippet == "A context manager that holds a group of tasks."
        and hits[0].published == "2024-05-01"
    )
    assert hits[1].snippet == "c" * w.SNIPPET_CHARS  # page text stands in for a missing description, cut
    assert hits[1].published == "2024-06-01"  # `date` where `publishedTime` is absent


def test_jina_has_no_recency_and_says_so(wire):
    with pytest.raises(WebError, match="jina does not support recency"):
        run(Jina(KEY).search(WebSearch(query="x", recency="day")))
    assert wire.requests == []
    assert "site" not in query(Jina(KEY).request(WebSearch(query="x")))


# --- what a provider can answer with ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "words"), [(401, "HTTP 401, the credentials"), (429, "HTTP 429, rate limited")]
)
@pytest.mark.parametrize(("make", "_body"), PROVIDERS)
def test_a_bad_key_and_a_rate_limit_are_errors_the_model_reads(wire, make, _body, code, words):
    echo = "" if make().name == "searxng" else KEY  # it takes no key
    wire.answers = [http_error(code, f'{{"detail": "no such key {echo}"}}')]
    with pytest.raises(WebError, match=words) as e:
        run(make().search(WebSearch(query="x")))
    assert KEY not in str(e.value) and ("[credential]" in str(e.value)) == bool(echo)
    assert str(e.value).startswith(make().name + ": ")


@pytest.mark.parametrize(("make", "_body"), PROVIDERS)
def test_a_timeout_in_either_of_its_two_forms(wire, make, _body):
    wire.answers = [TimeoutError("timed out"), urllib.error.URLError(TimeoutError("timed out"))]
    for _ in range(2):
        with pytest.raises(WebError, match="timed out after 20s"):
            run(make().search(WebSearch(query="x")))


@pytest.mark.parametrize(
    "make",
    [
        lambda t: SearXNG("https://searx.example/", timeout=t),
        lambda t: Brave(KEY, timeout=t),
        lambda t: Tavily(KEY, timeout=t),
        lambda t: Jina(KEY, timeout=t),
    ],
    ids=["searxng", "brave", "tavily", "jina"],
)
def test_a_search_adapter_built_with_a_timeout_gives_it_to_its_request(wire, make):
    wire.answers = [TimeoutError("timed out")]
    with pytest.raises(WebError, match="timed out after 7s"):
        run(make(7.0).search(WebSearch(query="x")))


@pytest.mark.parametrize(("make", "_body"), PROVIDERS)
def test_a_network_failure_and_a_cut_body_name_the_provider(wire, make, _body):
    wire.answers = [urllib.error.URLError("name or service not known"), ConnectionResetError("reset")]
    with pytest.raises(WebError, match="request failed: name or service not known"):
        run(make().search(WebSearch(query="x")))
    with pytest.raises(WebError, match="request failed: ConnectionResetError"):
        run(make().search(WebSearch(query="x")))


def test_a_key_of_eight_characters_is_blanked_and_a_shorter_one_is_left():
    def carrying(**header):
        return urllib.request.Request("https://p.example/", headers=header)

    assert w._credentials(carrying(Authorization="Bearer 12345678")) == (b"12345678",)
    assert w._credentials(carrying(**{"X-Subscription-Token": "12345678"})) == (b"12345678",)
    assert w._credentials(carrying(Authorization="Bearer 1234567")) == ()
    assert w._credentials(carrying(**{"X-Subscription-Token": "1234567"})) == ()


@pytest.mark.parametrize(("make", "_body"), PROVIDERS)
def test_a_provider_that_echoes_the_key_has_it_blanked_in_errors_and_hits(wire, make, _body):
    if make().name == "searxng":  # it takes no key
        return
    echoed = {"title": KEY, "url": "https://example.org/k", "content": KEY, "description": KEY}
    body = {"results": [echoed], "web": {"results": [echoed]}, "data": [echoed]}
    wire.answers = [http_error(401, f"bad token {KEY}"), Resp(json.dumps(body))]
    with pytest.raises(WebError) as e:
        run(make().search(WebSearch(query="x")))
    assert KEY not in str(e.value) and "[credential]" in str(e.value)
    (hit,) = run(make().search(WebSearch(query="x")))
    assert KEY not in hit.model_dump_json() and hit.title == "[credential]"


@pytest.mark.parametrize(("make", "_body"), PROVIDERS)
def test_a_failure_whose_text_carries_the_key_has_it_blanked(wire, make, _body):
    if make().name == "searxng":  # it takes no key
        return
    wire.answers = [
        http.client.BadStatusLine(f"HTTP/1.1 {KEY}"),
        urllib.error.URLError(f"refused for {KEY}"),
        ConnectionResetError(f"reset by {KEY}"),
    ]
    for _ in range(3):
        with pytest.raises(WebError, match="request failed") as e:
            run(make().search(WebSearch(query="x")))
        assert KEY not in str(e.value) and "[credential]" in str(e.value)


@pytest.mark.parametrize(("make", "_body"), PROVIDERS)
def test_a_provider_answer_past_the_cap_is_refused_and_never_read_whole(wire, make, _body, monkeypatch):
    monkeypatch.setattr(w, "MAX_BYTES", 100)
    wire.answers = [answer := Resp(json.dumps(_body))]
    with pytest.raises(WebError, match="the response is longer than 100 bytes"):
        run(make().search(WebSearch(query="x")))
    assert answer.asked == 101


@pytest.mark.parametrize(("make", "_body"), PROVIDERS)
def test_a_body_that_is_not_json_or_not_the_documented_shape(wire, make, _body):
    wire.answers = [
        Resp("<html>nope</html>", "text/html"),
        Resp("[1, 2]"),
        Resp(json.dumps({"results": 5, "web": 5, "data": 5})),
        Resp(json.dumps({"results": [5], "web": {"results": [5]}, "data": [5]})),
    ]
    with pytest.raises(WebError, match="the response is not JSON"):
        run(make().search(WebSearch(query="x")))
    with pytest.raises(WebError, match="a list where an object was expected"):
        run(make().search(WebSearch(query="x")))
    section = "the web section is a int, not an object" if make().name == "brave" else "results are a int"
    with pytest.raises(WebError, match=f"unexpected response, {section}"):
        run(make().search(WebSearch(query="x")))
    with pytest.raises(WebError, match="unexpected response, a result is a int, not an object"):
        run(make().search(WebSearch(query="x")))


@pytest.mark.parametrize(("make", "_body"), PROVIDERS)
def test_a_body_without_its_result_list_is_off_the_shape_not_empty(wire, make, _body):
    # a renamed or dropped field would otherwise read as a search with no results, every time
    absent = {"web": {"type": "search"}} if make().name == "brave" else {"query": "x"}
    wire.answers = [Resp(json.dumps(absent)), Resp(json.dumps({"results": None, "web": {}, "data": None}))]
    for _ in range(2):
        with pytest.raises(WebError, match=f"{make().name}: unexpected response, it has no result list"):
            run(make().search(WebSearch(query="x")))


@pytest.mark.parametrize(("make", "_body"), PROVIDERS)
def test_a_result_with_an_unusable_field_is_read_as_far_as_it_goes(wire, make, _body):
    # the body's shape is refused (the test above); inside a result, a title or snippet that is not text
    # reads as empty, a date that is not text as none, and a result with no usable link is dropped
    rows = [
        {
            "title": 5,
            "url": "https://example.org/a",
            "content": ["x"],
            "description": {},
            "published_date": 3,
            "publishedDate": 3,
            "page_age": 3,
            "age": 3,
            "publishedTime": 3,
            "date": 3,
        },
        {"title": "no link", "content": "c", "description": "c"},
        {"title": "empty link", "url": ""},
        {"title": "link that is not text", "url": 5},
        {"title": "Last", "url": "https://example.org/b", "content": "c", "description": "c"},
    ]
    wire.answers = [Resp(json.dumps({"results": rows, "web": {"results": rows}, "data": rows}))]
    assert run(make().search(WebSearch(query="x"))) == [
        Hit(title="", url="https://example.org/a", snippet=""),
        Hit(title="Last", url="https://example.org/b", snippet="c"),
    ]


def test_a_provider_redirect_is_an_error_and_never_followed(monkeypatch):
    class Moved(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            if self.path == "/search":
                self.send_response(302)
                self.send_header("Location", "/elsewhere")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):
            pass

    seen: list[str] = []
    server = http.server.HTTPServer(("127.0.0.1", 0), Moved)
    port = server.server_address[1]
    monkeypatch.setattr(urllib.request, "getproxies", dict)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/search", headers={"Authorization": f"Bearer {KEY}"}
    )
    try:
        with pytest.raises(WebError, match="p: HTTP 302"):
            w._send("p", req, timeout=5.0)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
    assert seen == ["/search"]  # the header carrying the key went nowhere else


def test_a_site_that_is_not_a_domain_is_refused():
    for site in (
        "https://docs.python.org",
        "docs.python.org/3",
        "a b",
        " ",
        "example.com?x",
        "bücher.example",
    ):
        with pytest.raises(WebError, match="bare domain"):
            Brave(KEY).request(WebSearch(query="x", site=site))
    punycode = Brave(KEY).request(WebSearch(query="x", site="xn--bcher-kva.example"))
    assert query(punycode)["q"] == "site:xn--bcher-kva.example x"  # an IDN is given as punycode


def test_a_site_is_dns_labels_none_starting_or_ending_with_a_hyphen():
    longest = ".".join(["a" * 63, "a" * 63, "a" * 63, "a" * 61])  # 253 characters, 63 to a label
    assert len(longest) == 253
    for site in ("a-b.example", "a.b", "x", "1.2.3.4", "a" * 63 + ".example", longest):
        assert query(Brave(KEY).request(WebSearch(query="x", site=site)))["q"] == f"site:{site} x"
    for site in (
        "-a.example",  # a label beginning with a hyphen, first, middle and last
        "a.-b.example",
        "example.-a",
        "a-.example",  # a label ending with one, first, middle and last
        "a.b-.example",
        "example.a-",
        "-",
        "a" * 64 + ".example",  # a label of 64
        longest + "a",  # 254 characters
        "a..example",
        "a.example.",
    ):
        with pytest.raises(WebError, match="bare domain"):
            Brave(KEY).request(WebSearch(query="x", site=site))


# --- choosing a provider ---------------------------------------------------------------------------


def test_provider_from_env_takes_the_first_configured_in_order(monkeypatch):
    for var in w.ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("JINA_API_KEY", "j")
    assert isinstance(provider_from_env(), Jina)
    monkeypatch.setenv("TAVILY_API_KEY", "t")
    assert isinstance(provider_from_env(), Tavily)
    monkeypatch.setenv("BRAVE_API_KEY", "b")
    assert isinstance(provider_from_env(), Brave)
    monkeypatch.setenv("SEARXNG_URL", "https://searx.example")
    assert isinstance(provider_from_env(), SearXNG)
    monkeypatch.setenv("SEARXNG_URL", "")  # an empty variable is not a configuration
    assert isinstance(provider_from_env(), Brave)


def test_provider_from_env_with_nothing_names_every_variable_it_looked_for(monkeypatch):
    for var in w.ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(WebError) as e:
        provider_from_env()
    assert w.ENV_VARS == ("SEARXNG_URL", "BRAVE_API_KEY", "TAVILY_API_KEY", "JINA_API_KEY")
    assert all(var in str(e.value) for var in w.ENV_VARS)
    with pytest.raises(WebError, match="SEARXNG_URL"):
        web_on(Actor("w"), None)


def web_on(actor, provider, **kw):
    return tools.web(actor, provider, **kw)


# --- the two commands on an Actor ------------------------------------------------------------------


class Canned:
    name = "canned"

    def __init__(self, hits=None, error=None):
        self.hits, self.error, self.seen = hits or [], error, []

    async def search(self, req):
        self.seen.append(req)
        if self.error:
            raise self.error
        return self.hits


def web_actor(provider, **kw):
    actor = Actor("w")
    names = web_on(actor, provider, **kw)
    return actor, Profile(name="w", specs=frozenset(names | {h.name for h in DEFAULT_HANDLERS}), system="x")


def test_web_registers_two_read_commands_named_from_their_classes():
    actor = Actor("w")
    assert web_on(actor, Canned()) == {"web_search", "web_fetch"}
    assert {actor.handlers[n].effect for n in ("web_search", "web_fetch")} == {"read"}
    assert web_on(Actor("n"), Canned(), fetch=False) == {"web_search"}


def test_the_model_is_told_the_web_is_material_not_instructions():
    for spec in (WebSearch, WebFetch):
        told = render_guidance(spec)
        assert "never instructions" in told.splitlines()[0]
    assert render_guidance(WebSearch).splitlines()[0].startswith("web_search: Search the web")
    assert render_guidance(WebFetch).splitlines()[0].startswith("web_fetch: Read one web page")


def test_a_scripted_run_searches_and_reads_the_hits():
    provider = Canned([Hit(title="T", url="https://e.example/", snippet="s", published="2024")])
    actor, profile = web_actor(provider)
    backend = scripted(
        '<lact a>web_search(query="q one", count=3, site="docs.python.org", recency="week")</lact>', "done"
    )
    result = asyncio.run(actor.run(profile, RunRequest(inputs=("go",), max_rounds=2), backend))
    r = {e.alias: e.content for e in result.record if e.kind is Kind.RESULT}
    assert r["a"].ok and r["a"].value == [
        {"title": "T", "url": "https://e.example/", "snippet": "s", "published": "2024"}
    ]
    assert provider.seen == [WebSearch(query="q one", count=3, site="docs.python.org", recency="week")]


def test_no_hits_and_a_provider_error_are_results_not_crashes():
    ok, profile = web_actor(Canned())
    bad, _ = web_actor(Canned(error=WebError("brave: HTTP 429, rate limited; try again later")))
    turns = '<lact a>web_search(query="q")</lact>', "done"
    first = asyncio.run(ok.run(profile, RunRequest(inputs=("go",), max_rounds=2), scripted(*turns)))
    second = asyncio.run(bad.run(profile, RunRequest(inputs=("go",), max_rounds=2), scripted(*turns)))
    a = {e.alias: e.content for e in first.record if e.kind is Kind.RESULT}["a"]
    b = {e.alias: e.content for e in second.record if e.kind is Kind.RESULT}["a"]
    assert a.ok and a.value == "(no results)"
    assert b.ok is False and b.error == "WebError: brave: HTTP 429, rate limited; try again later"


@pytest.mark.parametrize(("make", "_body"), PROVIDERS)
def test_a_search_whose_every_result_has_no_usable_link_is_no_results(wire, make, _body):
    rows = [{"title": "no link"}, {"title": "empty link", "url": ""}, {"title": "number", "url": 5}]
    wire.answers = [Resp(json.dumps({"results": rows, "web": {"results": rows}, "data": rows}))]
    actor, profile = web_actor(make())
    turns = '<lact a>web_search(query="q")</lact>', "done"
    result = asyncio.run(actor.run(profile, RunRequest(inputs=("go",), max_rounds=2), scripted(*turns)))
    a = {e.alias: e.content for e in result.record if e.kind is Kind.RESULT}["a"]
    assert a.ok and a.value == "(no results)"


def test_a_search_provider_given_to_web_is_called_as_it_is_and_owes_its_own_failures():
    actor, profile = web_actor(Canned(error=RuntimeError("boom")))
    turns = '<lact a>web_search(query="q")</lact>', "done"
    result = asyncio.run(actor.run(profile, RunRequest(inputs=("go",), max_rounds=2), scripted(*turns)))
    a = {e.alias: e.content for e in result.record if e.kind is Kind.RESULT}["a"]
    # the runtime's failed result for what it raised: not a `WebError`, no provider name, nothing blanked
    assert a.ok is False and a.error == "RuntimeError: boom"


def test_a_scripted_fetch_reads_a_page_and_a_private_address_is_a_failed_result(wire, monkeypatch):
    real = w.socket.getaddrinfo
    monkeypatch.setattr(
        w.socket,
        "getaddrinfo",
        lambda host, *a, **k: PUBLIC if host == "pages.example" else real(host, *a, **k),
    )
    wire.answers = [Resp("<html><body><p>Hello <b>web</b></p></body></html>", "text/html; charset=utf-8")]
    actor, profile = web_actor(Canned())
    backend = scripted(
        '<lact p>web_fetch(url="https://pages.example/a")</lact>'
        '<lact q>web_fetch(url="http://169.254.169.254/latest/meta-data/")</lact>',
        "done",
    )
    result = asyncio.run(actor.run(profile, RunRequest(inputs=("go",), max_rounds=2), backend))
    r = {e.alias: e.content for e in result.record if e.kind is Kind.RESULT}
    assert r["p"].ok and r["p"].value == "Hello web"
    assert r["q"].ok is False and "not a public address" in r["q"].error
    assert len(wire.requests) == 1  # the private address never reached the wire


# --- WebFetch --------------------------------------------------------------------------------------


def test_a_self_closed_script_or_a_stray_end_tag_leaves_nothing_hidden_on_the_page():
    # `<script/>` opens a script in HTML; a `</script>` inside a template closed the template's skip
    assert w.html_text("<script/>hidden js</script><p>shown</p>") == "shown"
    assert w.html_text("<style/>hidden css</style><p>shown</p>") == "shown"
    # what follows `<script/>` is raw text to its end tag, so a tag spelled inside it opens nothing
    assert w.html_text('<script/>const s = "<script/>";</script><p>visible</p>') == "visible"
    assert w.html_text('<style/>p::after {content: "<style/>"}</style><p>visible</p>') == "visible"
    assert w.html_text("<template><div></script>hidden</div></template><p>shown</p>") == "shown"
    assert w.html_text("<template/><p>hidden</p></template><p>shown</p>") == "shown"  # opened, not raw
    assert w.html_text("<p>a</p><svg/><p>b</p>") == "a\nb"  # svg is foreign content: `/>` closes it


def test_a_tag_spelled_inside_a_noscript_opens_nothing_so_its_end_tag_ends_it():
    assert w.html_text("<noscript><noscript></noscript><p>visible</p>") == "visible"
    assert w.html_text("<noscript><p>hidden</p><script></noscript><p>shown</p>") == "shown"
    assert w.html_text("<noscript/>hidden</noscript><p>shown</p>") == "shown"  # `/>` opens it


def test_html_becomes_text_without_scripts_styles_or_markup():
    page = (
        "<html><head><title>T</title><style>p {color: red}</style></head><body>"
        "<script>var x = '<p>no</p>';</script><h1>Head</h1><p>one   two</p>"
        "<ul><li>a</li><li>b &amp; c</li></ul>"
        "<noscript>enable js</noscript><p>end</p></body></html>"
    )
    assert w.html_text(page) == "T\nHead\none two\na\nb & c\nend"


def test_fetch_caps_the_text_at_max_chars(wire, public_dns):
    wire.answers = [Resp("x" * 500, "text/plain")]
    out = w._fetch("https://pages.example/long", 100)
    assert out == "x" * 100 + "\n[cut at 100 characters]"
    (req,) = wire.requests
    assert req.get_method() == "GET" and req.full_url == "https://pages.example/long"


def test_fetch_caps_what_it_reads_whatever_max_chars_asks(wire, public_dns, monkeypatch):
    monkeypatch.setattr(w, "MAX_BYTES", 50)
    wire.answers = [page := Resp(b"y" * 500, "text/plain; charset=latin-1")]
    assert w._fetch("https://pages.example/huge", 10_000) == "y" * 50 + "\n[cut at 50 characters]"
    assert page.asked == 51  # read to one byte past the cap, never the whole body


def test_fetch_leaves_a_short_page_alone_and_reads_json_and_charsets(wire, public_dns):
    wire.answers = [
        Resp("caf\xe9".encode("latin-1"), "text/plain; charset=latin-1"),
        Resp('{"a": 1}', "application/json"),
    ]
    assert w._fetch("https://pages.example/a", 100) == "café"
    assert w._fetch("https://pages.example/b", 100) == '{"a": 1}'
    wire.answers = [Resp("ok", "text/plain; charset=no-such-codec")]
    assert w._fetch("https://pages.example/c", 100) == "ok"


@pytest.mark.parametrize(
    "ctype", ["image/png", "application/pdf", "application/octet-stream", "video/mp4", None]
)
def test_fetch_refuses_what_is_not_text(wire, public_dns, ctype):
    wire.answers = [Resp(b"\x89PNG", ctype)]
    with pytest.raises(WebError, match="reads text pages"):
        w._fetch("https://pages.example/file", 100)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://127.1.2.3:8080/admin",
        "http://10.0.0.5/",
        "http://192.168.1.1/",
        "http://172.16.0.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://0.0.0.0/",
        "http://100.64.0.1/",
        "http://[::1]/",
        "http://[fe80::1]/",
        "http://[fd00::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://[::127.0.0.1]/",  # v4-compatible: reads as public, carries loopback
        "http://[64:ff9b::7f00:1]/",  # NAT64: the gateway would deliver it to 127.0.0.1
        "http://[64:ff9b::a9fe:a9fe]/",  # NAT64 carrying 169.254.169.254
        "http://[::ffff:10.0.0.1]/",
        "http://[::ffff:0:7f00:1]/",  # IPv4-translated: reads as public, carries loopback
        "http://[::ffff:0:a9fe:a9fe]/",  # IPv4-translated carrying 169.254.169.254
    ],
)
def test_fetch_refuses_loopback_link_local_and_private_addresses(wire, url):
    with pytest.raises(WebError, match="not a public address"):
        w._fetch(url, 100)
    assert wire.requests == []


def test_a_v6_form_that_carries_a_public_v4_address_is_judged_by_that_address(wire):
    """An IPv6-only network reaches IPv4 sites through NAT64, so the form is not refused as such."""
    wire.answers = [Resp("ok", "text/plain"), Resp("ok", "text/plain")]
    assert w._fetch("http://[64:ff9b::5db8:d822]/", 100) == "ok"  # carries 93.184.216.34
    assert w._fetch("http://[::ffff:0:5db8:d822]/", 100) == "ok"  # IPv4-translated, the same address
    assert w._carried_v4(w.ipaddress.IPv6Address("2001:db8::1")) is None
    assert w._carried_v4(w.ipaddress.IPv6Address("::1")) is None


# `ipaddress`'s ranges of addresses that are not global, and the exceptions inside them, as 3.11 had
# them before 3.11.10 and as 3.11.16, 3.12.14, 3.13.15 and 3.14.7 have them. `is_global` is the
# opposite of membership, save in 100.64.0.0/10, which is neither.
TABLES = {
    "before-3.11.10": (
        (
            *("0.0.0.0/8", "10.0.0.0/8", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/29"),
            *("192.0.0.170/31", "192.0.2.0/24", "192.168.0.0/16", "198.18.0.0/15", "198.51.100.0/24"),
            *("203.0.113.0/24", "240.0.0.0/4", "255.255.255.255/32", "::1/128", "::/128", "::ffff:0:0/96"),
            *("100::/64", "2001::/23", "2001:2::/48", "2001:db8::/32", "2001:10::/28", "fc00::/7"),
            "fe80::/10",
        ),
        (),
    ),
    "3.11.16": (
        (
            *("0.0.0.0/8", "10.0.0.0/8", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24"),
            *("192.0.0.170/31", "192.0.2.0/24", "192.168.0.0/16", "198.18.0.0/15", "198.51.100.0/24"),
            *("203.0.113.0/24", "240.0.0.0/4", "255.255.255.255/32", "::1/128", "::/128", "::ffff:0:0/96"),
            *("64:ff9b:1::/48", "100::/64", "2001::/23", "2001:db8::/32", "2002::/16", "3fff::/20"),
            *("fc00::/7", "fe80::/10"),
        ),
        (
            *("192.0.0.9/32", "192.0.0.10/32", "2001:1::1/128", "2001:1::2/128", "2001:3::/32"),
            *("2001:4:112::/48", "2001:20::/28", "2001:30::/28"),
        ),
    ),
}


def use_table(mp: pytest.MonkeyPatch, name: str) -> None:
    """`ipaddress` answers `is_global` from the named table, whatever interpreter runs the test."""
    private, exceptions = ([ipaddress.ip_network(n) for n in nets] for nets in TABLES[name])
    shared = ipaddress.ip_network("100.64.0.0/10")

    def is_global(a) -> bool:
        listed = any(a in n for n in private) and not any(a in n for n in exceptions)
        return a not in shared and not listed

    mp.setattr(ipaddress.IPv4Address, "is_global", property(is_global))
    mp.setattr(ipaddress.IPv6Address, "is_global", property(is_global))


@pytest.mark.parametrize("table", [None, "before-3.11.10"], ids=["this-interpreter", "before-3.11.10"])
def test_the_local_use_translation_prefix_is_refused_whatever_the_interpreter_calls_it(
    wire, monkeypatch, table
):
    if table:
        use_table(monkeypatch, table)
        assert ipaddress.ip_address("64:ff9b:1::7f00:1").is_global  # what those releases answered
    wire.answers = [Resp("ok", "text/plain")]
    with pytest.raises(WebError, match="not a public address"):
        w._fetch("http://[64:ff9b:1::7f00:1]/", 100)  # 64:ff9b:1::/48 translates to a local network's v4
    assert wire.requests == []
    assert w._fetch("http://[64:ff9b::5db8:d822]/", 100) == "ok"  # the well-known prefix, a public v4


def test_every_address_gets_one_verdict_whichever_table_the_interpreter_has(monkeypatch):
    """The tables are compared at the first and the last address of every range either names, and one
    past each end: between two of those points neither table changes its answer."""
    points = set()
    for net in (ipaddress.ip_network(n) for nets in TABLES.values() for part in nets for n in part):
        points |= {net.network_address, net.broadcast_address}
        if int(net.network_address):
            points.add(net.network_address - 1)
        if int(net.broadcast_address) < 2**net.max_prefixlen - 1:
            points.add(net.broadcast_address + 1)
    monkeypatch.setattr(w.socket, "getaddrinfo", lambda host, port, **k: [(0, 1, 6, "", (host, port))])

    def refused(ip) -> bool:
        try:
            w._public_addresses(str(ip), 80)
        except WebError:
            return True
        return False

    seen = []
    for name in TABLES:
        with monkeypatch.context() as m:
            use_table(m, name)
            seen.append(({ip: ip.is_global for ip in points}, {ip: refused(ip) for ip in points}))
    (old_global, old), (new_global, new) = seen
    disputed = {ip for ip in points if old_global[ip] != new_global[ip]}
    assert disputed >= set(
        map(ipaddress.ip_address, ("192.0.0.8", "64:ff9b:1::", "2002::", "3fff::", "2001:3::"))
    )
    assert {ip for ip in points if old[ip] != new[ip]} == set()  # the tables disagree; the check does not
    assert any(new.values()) and not all(new.values())


def test_fetch_refuses_a_name_that_resolves_to_a_private_address(wire, monkeypatch):
    mixed = PUBLIC + [(2, 1, 6, "", ("10.1.2.3", 80))]  # one bad answer among good ones is enough
    monkeypatch.setattr(w.socket, "getaddrinfo", lambda *a, **k: mixed)
    with pytest.raises(WebError, match="'internal.example'.*10.1.2.3"):
        w._fetch("http://internal.example/", 100)
    assert wire.requests == []


def test_fetch_refuses_other_schemes_and_hosts_that_do_not_resolve(wire, monkeypatch):
    for url in ("file:///etc/passwd", "ftp://pages.example/a", "/etc/passwd", "https:///nohost"):
        with pytest.raises(WebError, match="http and https|has no host"):
            w._fetch(url, 100)

    def unresolvable(*a, **k):
        raise w.socket.gaierror(8, "nodename nor servname provided")

    monkeypatch.setattr(w.socket, "getaddrinfo", unresolvable)
    with pytest.raises(WebError, match="cannot resolve 'nowhere.example'"):
        w._fetch("https://nowhere.example/", 100)
    assert wire.requests == []


def test_a_redirect_is_followed_only_to_a_public_address(public_dns, monkeypatch):
    req = urllib.request.Request("https://pages.example/a")
    handler = w._Redirects(True)
    moved = handler.redirect_request(req, io.BytesIO(), 302, "Found", Message(), "https://pages.example/b")
    assert moved is not None and moved.full_url == "https://pages.example/b"
    for target in ("http://169.254.169.254/", "http://localhost.invalid/", "file:///etc/passwd"):
        monkeypatch.setattr(w.socket, "getaddrinfo", lambda host, *a, **k: [(2, 1, 6, "", ("127.0.0.1", 80))])
        with pytest.raises(WebError):
            handler.redirect_request(req, io.BytesIO(), 302, "Found", Message(), target)


def test_fetch_errors_are_failed_results_that_name_the_cause(wire, public_dns):
    wire.answers = [http_error(404), TimeoutError("x"), urllib.error.URLError("refused"), OSError("reset")]
    for words in (
        "HTTP 404",
        "timed out after 20s",
        "request failed: refused",
        "request failed: OSError: reset",
    ):
        with pytest.raises(WebError, match=words):
            w._fetch("https://pages.example/a", 100)


def test_a_status_that_arrives_after_the_deadline_is_a_timeout(public_dns, monkeypatch):
    def late(req, timeout, guard=False):
        w._current.clock._expire()  # the timer fired while the head was read; the status still came back
        raise http_error(404)

    monkeypatch.setattr(w, "_urlopen", late)
    with pytest.raises(WebError, match="web_fetch: timed out after 20s"):
        w._fetch("https://pages.example/a", 100)


@pytest.mark.parametrize(
    "url",
    [
        "http://[::1/",
        "http://[",
        "http://a]b/",
        f"http://pages.example{chr(0xFF03)}x/",  # fullwidth number sign: reads as `#` once normalized
        "http://pages.example:80a/",
        "http://pages.example:99999/",
    ],
    ids=[
        "open-bracket",
        "lone-bracket",
        "close-bracket",
        "normalized-netloc",
        "port-not-a-number",
        "port-range",
    ],
)
def test_a_url_the_parser_refuses_is_a_failed_result_that_names_it(wire, url):
    with pytest.raises(WebError, match=r"web_fetch: .*" + re.escape(url)):
        w._fetch(url, 100)
    assert wire.requests == []


# --- the connection: a direct fetch dials an address that was checked ---------------------------------


class Dialed:
    """Stands in for `socket.socket`: records what it is asked to dial and refuses the connection."""

    made: list["Dialed"] = []

    def __init__(self, family, kind, proto):
        self.address, self.timeout, self.closed = None, None, False
        Dialed.made.append(self)

    def settimeout(self, timeout):
        self.timeout = timeout

    def connect(self, address):
        self.address = address
        raise ConnectionRefusedError("refused")

    def close(self):
        self.closed = True


@pytest.fixture
def dialed(monkeypatch):
    Dialed.made = []
    monkeypatch.setattr(w.socket, "socket", Dialed)
    monkeypatch.setattr(urllib.request, "getproxies", dict)
    return Dialed


def test_the_connection_dials_the_address_that_was_checked_and_never_the_name(dialed, public_dns):
    with pytest.raises(ConnectionRefusedError):
        w._connect_public("pages.example", 443, 5.0)
    assert [(s.address, s.timeout, s.closed) for s in dialed.made] == [(("93.184.216.34", 443), 5.0, True)]


def test_a_name_whose_answer_changes_after_the_check_is_refused_at_the_connection(dialed, monkeypatch):
    answers = iter([PUBLIC, [(2, 1, 6, "", ("127.0.0.1", 80))]])  # the check, then the connection
    monkeypatch.setattr(w.socket, "getaddrinfo", lambda *a, **k: next(answers))
    with pytest.raises(WebError, match="resolves to 127.0.0.1, not a public address"):
        w._fetch("http://rebind.example/", 100)
    assert dialed.made == [] and next(answers, None) is None  # both answers were asked for, nothing dialed


def test_a_direct_fetch_follows_a_redirect_through_checked_connections(monkeypatch):
    class Page(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append((self.path, self.headers["Host"]))
            if self.path == "/a":
                self.send_response(302)
                self.send_header("Location", "/b")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            body = b"the page"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    seen: list[tuple[str, str]] = []
    resolved: list[str] = []
    server = http.server.HTTPServer(("127.0.0.1", 0), Page)
    port = server.server_address[1]

    def here(host, port):  # the one thing replaced: which address the name is said to have
        resolved.append(host)
        return [(2, 1, 6, "", ("127.0.0.1", port))]

    monkeypatch.setattr(w, "_public_addresses", here)
    monkeypatch.setattr(urllib.request, "getproxies", dict)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert w._fetch(f"http://pages.example:{port}/a", 100, timeout=5.0) == "the page"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
    assert seen == [("/a", f"pages.example:{port}"), ("/b", f"pages.example:{port}")]
    assert resolved == ["pages.example"] * 4  # each hop: once for the check, once for its connection


def test_a_body_that_ends_short_of_its_declared_length_is_a_cut_body_not_an_answer(monkeypatch):
    class Short(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # declares 1,000 bytes, sends a valid JSON object of 15, and hangs up
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", "1000")
            self.end_headers()
            self.wfile.write(b'{"results": []}')

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Short)
    port = server.server_address[1]
    monkeypatch.setattr(w, "_public_addresses", lambda host, port: [(2, 1, 6, "", ("127.0.0.1", port))])
    monkeypatch.setattr(urllib.request, "getproxies", dict)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    search = urllib.request.Request(f"http://127.0.0.1:{port}/search")
    try:
        with pytest.raises(WebError, match=r"p: request failed: IncompleteRead: .*15 bytes read, 985 more"):
            w._send("p", search, timeout=5.0)
        with pytest.raises(WebError, match=r"web_fetch: request failed: IncompleteRead: .*15 bytes read"):
            w._fetch(f"http://pages.example:{port}/", 100, timeout=5.0)
        monkeypatch.setattr(w, "MAX_BYTES", 5)  # the cap reached first is the cap, not a cut body
        with pytest.raises(WebError, match="p: the response is longer than 5 bytes"):
            w._send("p", search, timeout=5.0)
        assert w._fetch(f"http://pages.example:{port}/", 100, timeout=5.0) == '{"res\n[cut at 5 characters]'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def test_the_tls_connection_verifies_the_name_it_was_asked_for(monkeypatch):
    context = w._tls()
    assert context.verify_mode is ssl.CERT_REQUIRED and context.check_hostname

    class Wrapped:
        def __init__(self, sock, name, handshake_on_connect):
            self.made = (sock, name, handshake_on_connect)
            self.shook = False

        def do_handshake(self):
            self.shook = True

    class Wraps:
        def wrap_socket(self, sock, server_hostname, do_handshake_on_connect=True):
            return Wrapped(sock, server_hostname, do_handshake_on_connect)

    monkeypatch.setattr(w, "_connect_public", lambda host, port, timeout: ("raw", host, port, timeout))
    monkeypatch.setattr(w, "_tls", Wraps)
    conn = w._PinnedTLS("pages.example", timeout=5.0, context=context)
    conn.connect()
    # the handshake runs after the wrap, once the request's clock can see the socket
    assert conn.sock.made == (("raw", "pages.example", 443, 5.0), "pages.example", False) and conn.sock.shook


def test_a_timed_tls_connection_is_watched_from_its_dial_and_before_its_handshake(monkeypatch):
    # a provider's or a proxy's: the CONNECT exchange and the handshake both read from the socket
    events: list[tuple] = []

    class Raw:
        def settimeout(self, timeout):
            events.append(("timeout", timeout))

        def connect(self, address):
            events.append(("connect", address))

        def setsockopt(self, *args):
            pass

    class Wrapped:
        def do_handshake(self):
            events.append(("handshake",))

    raw, wrapped = Raw(), Wrapped()

    class Wraps:
        verify_mode, check_hostname = ssl.CERT_REQUIRED, True

        def wrap_socket(self, sock, server_hostname, do_handshake_on_connect=True):
            events.append(("wrap", sock, server_hostname, do_handshake_on_connect))
            return wrapped

    monkeypatch.setattr(w.socket, "socket", lambda *args: raw)
    monkeypatch.setattr(w.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("192.0.2.1", 3128))])
    monkeypatch.setattr(w, "_watch", lambda sock: events.append(("watch", sock)))
    conn = w._TimedTLS("proxy.example", 3128, timeout=5.0, context=Wraps())
    conn.set_tunnel("search.example", 443)
    monkeypatch.setattr(conn, "_tunnel", lambda: events.append(("tunnel",)))
    conn.connect()
    assert events == [
        ("watch", raw),  # before it dials
        ("timeout", 5.0),
        ("connect", ("192.0.2.1", 3128)),
        ("tunnel",),
        ("wrap", raw, "search.example", False),
        ("watch", wrapped),
        ("handshake",),
    ]


# the proxy's dial: `_dial` tries one address at a time through `_connect`, each under the request's clock


@pytest.fixture
def fake_time(monkeypatch):
    """What `_Clock` and `_left` read as the time, moved by the test."""
    now = [100.0]
    monkeypatch.setattr(w, "time", types.SimpleNamespace(monotonic=lambda: now[0]))
    return now


THREE = [(2, 1, 6, "", (f"192.0.2.{n}", 3128)) for n in (1, 2, 3)]


def test_each_address_of_a_proxy_is_dialed_with_what_is_left_of_the_deadline(fake_time, monkeypatch):
    tried: list[tuple] = []

    def connect(info, timeout, source_address=None):
        tried.append((info[4][0], timeout))
        fake_time[0] += 5  # an address that takes five seconds to refuse
        raise ConnectionRefusedError("refused")

    monkeypatch.setattr(w.socket, "getaddrinfo", lambda *a, **k: THREE)
    monkeypatch.setattr(w, "_connect", connect)
    with w._Clock(10), pytest.raises(TimeoutError, match="deadline passed"):
        w._dial(("proxy.example", 3128), 20.0)
    # the second try gets the remainder, not the 10 s the first had; at zero the third is never dialed
    assert tried == [("192.0.2.1", 10.0), ("192.0.2.2", 5.0)]


def test_a_dial_whose_resolution_used_the_deadline_dials_nothing(fake_time, monkeypatch):
    def resolve(*args, **kwargs):
        fake_time[0] += 10  # the name took the whole request
        return THREE

    monkeypatch.setattr(w.socket, "getaddrinfo", resolve)
    monkeypatch.setattr(w, "_connect", lambda *a: pytest.fail("dialed past the deadline"))
    with w._Clock(10), pytest.raises(TimeoutError, match="deadline passed"):
        w._dial(("proxy.example", 3128), 20.0)


def test_a_dial_goes_on_to_the_next_address_and_ends_with_the_last_refusal(monkeypatch):
    tried: list[str] = []

    def second_answers(info, timeout, source_address=None):
        tried.append(info[4][0])
        if info[4][0] == "192.0.2.2":
            return "connected"
        raise ConnectionRefusedError(info[4][0])

    def none_answers(info, timeout, source_address=None):
        raise ConnectionRefusedError(info[4][0])

    monkeypatch.setattr(w.socket, "getaddrinfo", lambda *a, **k: THREE)
    monkeypatch.setattr(w, "_connect", second_answers)
    assert w._dial(("proxy.example", 3128), 20.0) == "connected" and tried == ["192.0.2.1", "192.0.2.2"]
    monkeypatch.setattr(w, "_connect", none_answers)
    with pytest.raises(ConnectionRefusedError, match="192.0.2.3"):
        w._dial(("proxy.example", 3128), 20.0)
    monkeypatch.setattr(w.socket, "getaddrinfo", lambda *a, **k: [])
    with pytest.raises(OSError, match="'proxy.example' resolves to no address"):
        w._dial(("proxy.example", 3128), 20.0)


def test_a_socket_is_watched_before_it_connects_and_closed_when_it_does_not(monkeypatch):
    events: list[tuple] = []

    class Raw:
        def __init__(self, family, kind, proto):
            events.append(("socket", family))

        def settimeout(self, timeout):
            events.append(("timeout", timeout))

        def bind(self, address):
            events.append(("bind", address))

        def connect(self, address):
            events.append(("connect", address))
            if address[0] == "192.0.2.1":
                raise ConnectionRefusedError("refused")

        def close(self):
            events.append(("close",))

    answers = [(2, 1, 6, "", ("192.0.2.1", 3128)), (10, 1, 6, "", ("2001:db8::1", 3128, 0, 0))]
    monkeypatch.setattr(w.socket, "socket", Raw)
    monkeypatch.setattr(w.socket, "getaddrinfo", lambda *a, **k: answers)
    monkeypatch.setattr(w, "_watch", lambda sock: events.append(("watch", type(sock))))
    sock = w._dial(("proxy.example", 3128), 5.0, ("198.51.100.7", 0))
    assert isinstance(sock, Raw)
    assert events == [
        ("socket", 2),
        ("watch", Raw),
        ("bind", ("198.51.100.7", 0)),
        ("timeout", 5.0),  # read last before the connect
        ("connect", ("192.0.2.1", 3128)),
        ("close",),
        ("socket", 10),
        ("watch", Raw),
        ("bind", ("198.51.100.7", 0)),
        ("timeout", 5.0),
        ("connect", ("2001:db8::1", 3128, 0, 0)),
    ]


def test_a_direct_socket_is_watched_before_it_connects_and_closed_when_it_does_not(monkeypatch):
    events: list[tuple] = []

    class Raw:
        def __init__(self, family, kind, proto):
            events.append(("socket", family))

        def settimeout(self, timeout):
            events.append(("timeout", timeout))

        def connect(self, address):
            events.append(("connect", address))
            if address[0] == "192.0.2.1":
                raise ConnectionRefusedError("refused")

        def close(self):
            events.append(("close",))

    answers = [(2, 1, 6, "", ("192.0.2.1", 443)), (10, 1, 6, "", ("2001:db8::1", 443, 0, 0))]
    monkeypatch.setattr(w, "_public_addresses", lambda host, port: answers)
    monkeypatch.setattr(w.socket, "socket", Raw)
    monkeypatch.setattr(w, "_watch", lambda sock: events.append(("watch", type(sock))))
    assert isinstance(w._connect_public("pages.example", 443, 5.0), Raw)
    assert events == [
        ("socket", 2),
        ("watch", Raw),
        ("timeout", 5.0),  # read last before the connect
        ("connect", ("192.0.2.1", 443)),
        ("close",),
        ("socket", 10),
        ("watch", Raw),
        ("timeout", 5.0),
        ("connect", ("2001:db8::1", 443, 0, 0)),
    ]


@pytest.fixture
def stepping(fake_time, monkeypatch):
    """Sockets that move the fake clock: `before` seconds pass as one is made, after the dial read what
    was left; `during` pass inside its connect, which then returns after the clock's shutdown missed it."""
    step = types.SimpleNamespace(before=0.0, during=0.0, made=[])

    class Sock:
        def __init__(self, family, kind, proto):
            fake_time[0] += step.before
            self.timeout = self.address = None
            self.closed, self.sent = False, []
            step.made.append(self)

        def settimeout(self, timeout):
            self.timeout = timeout

        def connect(self, address):
            self.address = address
            fake_time[0] += step.during

        def setsockopt(self, *args):
            pass

        def do_handshake(self):
            pass

        def sendall(self, data):
            self.sent.append(data)

        def shutdown(self, how):
            pass

        def close(self):
            self.closed = True

    monkeypatch.setattr(w.socket, "socket", Sock)
    monkeypatch.setattr(w.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("192.0.2.1", 80))])
    return step


class Unwrapped:
    """A TLS context that wraps nothing, so what a request sends is what the socket stub records."""

    verify_mode, check_hostname = ssl.CERT_REQUIRED, True

    def wrap_socket(self, sock, server_hostname, do_handshake_on_connect=True):
        return sock


def timed(https: bool):
    """A provider's connection of either kind."""
    if not https:
        return w._Timed("search.example", 80, timeout=20.0)
    return w._TimedTLS("search.example", 443, timeout=20.0, context=Unwrapped())


@pytest.mark.parametrize("https", [False, True], ids=["http", "https"])
def test_a_connect_that_returns_after_the_deadline_is_closed_and_no_request_follows(stepping, https):
    stepping.during = 11.0  # the 10 s request's connect comes back a second late
    conn = timed(https)
    with w._Clock(10), pytest.raises(TimeoutError, match="deadline passed"):
        conn.request("GET", "/")
    (sock,) = stepping.made
    assert sock.address == ("192.0.2.1", 80) and sock.closed and sock.sent == []


@pytest.mark.parametrize("https", [False, True], ids=["http", "https"])
def test_a_dial_paused_before_its_connect_connects_with_what_is_left_then(stepping, https):
    stepping.before = 4.0  # paused after the dial read its 10 s, before the connect
    with w._Clock(10):
        timed(https).connect()
    (sock,) = stepping.made
    assert sock.address == ("192.0.2.1", 80) and sock.timeout == 6.0 and not sock.closed


@pytest.mark.parametrize("https", [False, True], ids=["http", "https"])
def test_a_dial_paused_to_the_deadline_never_connects(stepping, https):
    stepping.before = 10.0
    with w._Clock(10), pytest.raises(TimeoutError, match="deadline passed"):
        timed(https).connect()
    (sock,) = stepping.made
    assert sock.address is None and sock.closed


# the same three over a page's own connection, which dials the one answer that was checked

PAGE = {False: ("pages.example", 80), True: ("pages.example", 443)}


@pytest.fixture
def pinned(stepping, monkeypatch):
    monkeypatch.setattr(w, "_public_addresses", lambda host, port: [(2, 1, 6, "", ("192.0.2.1", port))])
    monkeypatch.setattr(w, "_tls", Unwrapped)
    return stepping


def direct(https: bool):
    """A page's connection of either kind."""
    if not https:
        return w._Pinned(*PAGE[False], timeout=20.0)
    return w._PinnedTLS(*PAGE[True], timeout=20.0, context=Unwrapped())


@pytest.mark.parametrize("https", [False, True], ids=["http", "https"])
def test_a_direct_connect_that_returns_after_the_deadline_is_closed_and_no_request_follows(pinned, https):
    pinned.during = 11.0  # the 10 s request's connect comes back a second late
    with w._Clock(10), pytest.raises(TimeoutError, match="deadline passed"):
        direct(https).request("GET", "/")
    (sock,) = pinned.made
    assert sock.address == ("192.0.2.1", PAGE[https][1]) and sock.closed and sock.sent == []


@pytest.mark.parametrize("https", [False, True], ids=["http", "https"])
def test_a_direct_connect_paused_before_it_connects_connects_with_what_is_left_then(pinned, https):
    pinned.before = 4.0  # paused after the dial read its 10 s, before the connect
    with w._Clock(10):
        direct(https).connect()
    (sock,) = pinned.made
    assert sock.address == ("192.0.2.1", PAGE[https][1]) and sock.timeout == 6.0 and not sock.closed


@pytest.mark.parametrize("https", [False, True], ids=["http", "https"])
def test_a_direct_connect_paused_to_the_deadline_never_connects(pinned, https):
    pinned.before = 10.0
    with w._Clock(10), pytest.raises(TimeoutError, match="deadline passed"):
        direct(https).connect()
    (sock,) = pinned.made
    assert sock.address is None and sock.closed


class Seen(http.server.BaseHTTPRequestHandler):
    """A local server that records the request line's target and answers with one line."""

    seen: list[str] = []

    def do_GET(self):
        Seen.seen.append(self.path)
        body = b"answered"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def local():
    Seen.seen = []
    server = http.server.HTTPServer(("127.0.0.1", 0), Seen)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()
    thread.join(5)


def test_behind_an_environment_proxy_the_request_is_the_proxys_to_open(local, monkeypatch):
    # the proxy is dialed by its own name, loopback here, and is handed the whole URL to resolve itself
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {"http": f"http://127.0.0.1:{local}"})
    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda host: False)
    monkeypatch.setattr(w, "_public_addresses", lambda host, port: PUBLIC)
    assert w._fetch("http://pages.example/a", 100, timeout=5.0) == "answered"
    assert Seen.seen == ["http://pages.example/a"]


def test_a_host_on_the_proxy_bypass_list_is_dialed_at_a_checked_address(local, monkeypatch):
    resolved: list[str] = []

    def here(host, port):
        resolved.append(host)
        return [(2, 1, 6, "", ("127.0.0.1", port))]

    monkeypatch.setattr(urllib.request, "getproxies", lambda: {"http": "http://proxy.invalid:3128"})
    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda host: True)
    monkeypatch.setattr(w, "_public_addresses", here)
    assert w._fetch(f"http://pages.example:{local}/a", 100, timeout=5.0) == "answered"
    assert Seen.seen == ["/a"] and resolved == ["pages.example"] * 2  # the check, then the connection


@pytest.mark.parametrize("first", ["proxied", "direct"])
def test_proxy_settings_that_change_between_two_readings_open_no_unchecked_connection(
    local, monkeypatch, first
):
    # the settings were read once to choose the transport and again to route the request: a change in
    # between sent a request by name, on a connection nothing had checked, to wherever the name pointed
    proxy = {"http": "http://proxy.invalid:3128"}
    readings = iter([proxy, {}] if first == "proxied" else [{}, proxy])
    answers = iter([[(2, 1, 6, "", ("93.184.216.34", local))]])  # the check; after it the name is loopback

    def resolve(host, *a, **k):
        if host == "proxy.invalid":
            raise socket.gaierror(8, "no such host")
        return next(answers, [(2, 1, 6, "", ("127.0.0.1", local))])

    monkeypatch.setattr(urllib.request, "getproxies", lambda: next(readings, {}))
    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda host: False)
    monkeypatch.setattr(w.socket, "getaddrinfo", resolve)
    with pytest.raises(WebError):
        w._fetch(f"http://rebind.example:{local}/", 100, timeout=5.0)
    assert Seen.seen == []


def test_the_dial_is_chosen_from_what_the_proxy_handler_did_to_the_request(monkeypatch):
    picked = []
    monkeypatch.setattr(
        urllib.request.AbstractHTTPHandler, "do_open", lambda self, cls, req, **kw: picked.append(cls)
    )
    for pinned in (True, False):
        for url, handler in (
            ("http://pages.example/", w._HTTP(pinned)),
            ("https://pages.example/", w._HTTPS(pinned)),
        ):
            direct, proxied = urllib.request.Request(url), urllib.request.Request(url)
            proxied.set_proxy("proxy.example:3128", "http")
            assert not w._via_proxy(direct) and w._via_proxy(proxied)
            for req in (direct, proxied):
                getattr(handler, f"{req.type}_open")(req)
    # a page fetch the proxy did not take is pinned; a provider's connection and a proxy's are timed only
    assert picked == [w._Pinned, w._Timed, w._PinnedTLS, w._TimedTLS] + [
        w._Timed,
        w._Timed,
        w._TimedTLS,
        w._TimedTLS,
    ]


# --- the deadline: one for the whole request, however its bytes arrive ---------------------------------


class Drip(http.server.BaseHTTPRequestHandler):
    """Answers a byte at a time, each well inside a socket timeout: in the body, or before the headers end."""

    in_headers = False
    status = b"200 OK"

    def do_GET(self):
        self.wfile.write(b"HTTP/1.1 " + Drip.status + b"\r\n")
        if not Drip.in_headers:
            self.wfile.write(b"Content-Type: text/plain\r\nContent-Length: 1000\r\n\r\n")
        for _ in range(200):  # 10 s at most, if nothing cuts it
            try:
                self.wfile.write(b"x")
                self.wfile.flush()
            except OSError:  # the client shut the socket
                return
            time.sleep(0.05)

    do_CONNECT = do_GET  # as a proxy: CONNECT answered, then the rest of its answer a byte at a time

    def log_message(self, *args):
        pass


@pytest.fixture
def drip(monkeypatch):
    monkeypatch.setattr(urllib.request, "getproxies", dict)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Drip)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    Drip.in_headers, Drip.status = False, b"200 OK"
    server.shutdown()
    server.server_close()
    thread.join(5)


def test_a_provider_that_sends_a_byte_now_and_then_is_cut_at_the_deadline(drip):
    began = time.monotonic()
    with pytest.raises(WebError, match="p: timed out after 0.4s"):
        w._send("p", urllib.request.Request(f"http://127.0.0.1:{drip}/"), timeout=0.4)
    assert time.monotonic() - began < 2


def test_an_error_whose_detail_outlasts_the_deadline_is_a_timeout(drip):
    Drip.status = b"401 Unauthorized"  # the status in time, its body a byte at a time
    began = time.monotonic()
    with pytest.raises(WebError, match="p: timed out after 0.4s"):
        w._send("p", urllib.request.Request(f"http://127.0.0.1:{drip}/"), timeout=0.4)
    assert time.monotonic() - began < 2


def test_a_proxy_that_answers_connect_a_byte_now_and_then_is_cut_at_the_deadline(drip, monkeypatch):
    Drip.in_headers = True  # `200`, then a header line that never ends
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {"https": f"http://127.0.0.1:{drip}"})
    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda host: False)
    began = time.monotonic()
    with pytest.raises(WebError, match="p: timed out after 0.4s"):
        w._send("p", urllib.request.Request("https://search.example/"), timeout=0.4)
    assert time.monotonic() - began < 2


@pytest.mark.parametrize(
    ("url", "proxy"),
    [
        ("http://search.example/", None),
        ("http://search.example/", "http://proxy.example:3128"),
        ("https://search.example/", "http://proxy.example:3128"),
    ],
    ids=["direct-http", "proxy-http", "proxy-https"],
)
def test_a_name_with_several_stalled_addresses_is_dialed_within_the_deadline(monkeypatch, url, proxy):
    # five addresses, each refusing after 0.15 s: the clock's 0.5 s ends the third try's wait, and no try
    # may be given a timeout that reaches past it
    overruns: list[float] = []

    class Stalled:
        def __init__(self, family, kind, proto):
            self.timeout = None

        def settimeout(self, timeout):
            self.timeout = timeout

        def connect(self, address):
            overruns.append(time.monotonic() + self.timeout - w._current.clock.deadline)
            time.sleep(min(self.timeout, 0.15))
            raise ConnectionRefusedError("refused") if self.timeout > 0.15 else TimeoutError("timed out")

        def shutdown(self, how):
            pass

        def close(self):
            pass

    five = [(2, 1, 6, "", (f"192.0.2.{n}", 3128)) for n in range(1, 6)]
    monkeypatch.setattr(w.socket, "socket", Stalled)
    monkeypatch.setattr(w.socket, "getaddrinfo", lambda *a, **k: five)
    monkeypatch.setattr(
        urllib.request, "getproxies", lambda: {"http": proxy, "https": proxy} if proxy else {}
    )
    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda host: False)
    began = time.monotonic()
    with pytest.raises(WebError, match="p: timed out after 0.5s"):
        w._send("p", urllib.request.Request(url), timeout=0.5)
    assert time.monotonic() - began < 1.0
    assert len(overruns) >= 3 and max(overruns) < 0.075  # each try ends by the deadline, at the latest


@pytest.mark.parametrize("in_headers", [False, True], ids=["body", "headers"])
def test_a_page_that_sends_a_byte_now_and_then_is_cut_at_the_deadline(drip, monkeypatch, in_headers):
    Drip.in_headers = in_headers
    monkeypatch.setattr(w, "_public_addresses", lambda host, port: [(2, 1, 6, "", ("127.0.0.1", port))])
    began = time.monotonic()
    with pytest.raises(WebError, match="web_fetch: timed out after 0.4s"):
        w._fetch(f"http://pages.example:{drip}/", 100, timeout=0.4)
    assert time.monotonic() - began < 2


def test_past_the_deadline_no_socket_is_opened_and_one_watched_late_is_shut(monkeypatch):
    with w._Clock(5) as clock:  # a socket handed to a clock after its deadline is shut at once
        clock._expire()
        mine, theirs = socket.socketpair()
        clock.watch(mine)
        theirs.settimeout(2)
        assert theirs.recv(1) == b""
        mine.close()
        theirs.close()
    server = socket.create_server(("127.0.0.1", 0))
    port = server.getsockname()[1]
    dialed: list[tuple] = []

    class Late(socket.socket):  # connects, but only once the request's deadline has passed
        def connect(self, address):
            dialed.append(address)
            time.sleep(0.1)
            super().connect(address)

    def resolve(host, port):
        if host == "slow.example":
            time.sleep(0.1)  # the dial's own resolution outlasts the deadline
        return [(2, 1, 6, "", ("127.0.0.1", port))]

    monkeypatch.setattr(w, "_public_addresses", resolve)
    monkeypatch.setattr(w.socket, "socket", Late)
    try:
        for pinned in (w._Pinned("slow.example", port, timeout=5.0), w._PinnedTLS("slow.example", port)):
            with w._Clock(0.05), pytest.raises(TimeoutError):
                pinned.connect()
        assert dialed == []  # the deadline passed during the resolution: nothing was dialed
        late = w._Pinned("late.example", port, timeout=5.0)
        with w._Clock(0.05), pytest.raises(TimeoutError, match="deadline passed"):
            late.connect()  # it connects, but after the deadline: closed, never handed back
        assert late.sock is None
        peer, _ = server.accept()
        peer.settimeout(2)
        assert peer.recv(1) == b""  # the connection that arrived late was closed
        peer.close()
    finally:
        server.close()
    assert w._left(5.0) == 5.0  # outside a request, a socket keeps its own timeout


# --- live: one per provider, only when its variable is set -----------------------------------------


def live(provider: w.SearchProvider) -> None:
    hits = run(provider.search(WebSearch(query="python asyncio taskgroup documentation", count=3)))
    assert hits and all(h.url.startswith("http") and h.title for h in hits)


@pytest.mark.skipif(not os.environ.get("SEARXNG_URL"), reason="SEARXNG_URL is not set")
def test_live_searxng():
    live(SearXNG(os.environ["SEARXNG_URL"]))


@pytest.mark.skipif(not os.environ.get("BRAVE_API_KEY"), reason="BRAVE_API_KEY is not set")
def test_live_brave():
    live(Brave(os.environ["BRAVE_API_KEY"]))


@pytest.mark.skipif(not os.environ.get("TAVILY_API_KEY"), reason="TAVILY_API_KEY is not set")
def test_live_tavily():
    live(Tavily(os.environ["TAVILY_API_KEY"]))


@pytest.mark.skipif(not os.environ.get("JINA_API_KEY"), reason="JINA_API_KEY is not set")
def test_live_jina():
    live(Jina(os.environ["JINA_API_KEY"]))
