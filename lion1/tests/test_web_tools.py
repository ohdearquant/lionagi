"""The web tools: each provider's adapter against a recorded response with the HTTP call replaced (no
network in a default run), the failures a provider can answer with, the page reader's refusals, and
the two commands driven through a real Actor. One live test per provider runs only when its variable
is set."""

import asyncio
import importlib
import io
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from email.message import Message

import pytest

from hub import tools
from hub.tools.web import Brave, Hit, Jina, SearXNG, Tavily, WebError, WebFetch, WebSearch, provider_from_env
from lionagi import Actor, Kind, Profile, RunRequest
from lionagi.actor import DEFAULT_HANDLERS
from lionagi.spec import render_guidance
from tests.test_actor import scripted

w = importlib.import_module("hub.tools.web")  # `hub.tools.web` the attribute is the function
KEY = "k-SECRET-123"
PUBLIC = [(2, 1, 6, "", ("93.184.216.34", 443))]


class Resp:
    """What `urlopen` hands back: a body, headers, a context manager."""

    def __init__(self, body: bytes | str, ctype: str | None = "application/json"):
        self.body = body.encode() if isinstance(body, str) else body
        self.headers = Message()
        if ctype:
            self.headers["Content-Type"] = ctype

    def read(self, n: int = -1) -> bytes:
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
            "date": "2024-05-01",
            "content": "# TaskGroup\n" + "long page text " * 200,
            "usage": {"tokens": 1000},
        },
        {"title": "Only content", "url": "https://example.org/c", "content": "c" * 3000},
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
    wire.answers = [http_error(code, '{"detail": "no"}')]
    with pytest.raises(WebError, match=words) as e:
        run(make().search(WebSearch(query="x")))
    assert KEY not in str(e.value)
    assert str(e.value).startswith(make().name + ": ")


@pytest.mark.parametrize(("make", "_body"), PROVIDERS)
def test_a_timeout_in_either_of_its_two_forms(wire, make, _body):
    wire.answers = [TimeoutError("timed out"), urllib.error.URLError(TimeoutError("timed out"))]
    for _ in range(2):
        with pytest.raises(WebError, match="timed out after 20s"):
            run(make().search(WebSearch(query="x")))


@pytest.mark.parametrize(("make", "_body"), PROVIDERS)
def test_a_network_failure_and_a_cut_body_name_the_provider(wire, make, _body):
    wire.answers = [urllib.error.URLError("name or service not known"), ConnectionResetError("reset")]
    with pytest.raises(WebError, match="request failed: name or service not known"):
        run(make().search(WebSearch(query="x")))
    with pytest.raises(WebError, match="request failed: ConnectionResetError"):
        run(make().search(WebSearch(query="x")))


@pytest.mark.parametrize(("make", "_body"), PROVIDERS)
def test_a_body_that_is_not_json_or_not_the_documented_shape(wire, make, _body):
    wire.answers = [
        Resp("<html>nope</html>", "text/html"),
        Resp("[1, 2]"),
        Resp(json.dumps({"results": 5, "web": 5, "data": 5})),
    ]
    with pytest.raises(WebError, match="the response is not JSON"):
        run(make().search(WebSearch(query="x")))
    with pytest.raises(WebError, match="a list where an object was expected"):
        run(make().search(WebSearch(query="x")))
    if make().name != "brave":  # Brave's `web` that is not an object reads as no results
        with pytest.raises(WebError, match="results are a int, not a list"):
            run(make().search(WebSearch(query="x")))


def test_a_provider_redirect_is_an_error_and_never_followed():
    req = urllib.request.Request("https://api.example/search", headers={"Authorization": "Bearer x"})
    assert (
        w._Redirects(False).redirect_request(req, None, 302, "Found", {}, "https://elsewhere.example/")
        is None
    )


def test_a_site_that_is_not_a_domain_is_refused():
    for site in ("https://docs.python.org", "docs.python.org/3", "a b", " "):
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
    wire.answers = [Resp(b"y" * 500, "text/plain; charset=latin-1")]
    assert w._fetch("https://pages.example/huge", 10_000) == "y" * 50 + "\n[cut at 50 characters]"


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
        "http://[::ffff:10.0.0.1]/",
    ],
)
def test_fetch_refuses_loopback_link_local_and_private_addresses(wire, url):
    with pytest.raises(WebError, match="not a public address"):
        w._fetch(url, 100)
    assert wire.requests == []


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
