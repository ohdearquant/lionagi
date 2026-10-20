# ADR-0135: Standard-library HTTP transport for endpoints

- **Status**: Proposed
- **Kind**: Aspirational
- **Implementation-status**: implemented (D1-D7 land with this record)
- **Area**: service-providers
- **Date**: 2026-10-20
- **Relations**: replaces the transport ADR-0027 describes for `Endpoint` (its one-shot status,
  resilience and stream bullets now name this client); keeps ADR-0029's admission, deadline and
  resilience policy unchanged and renames the helpers its P5 cites; keeps ADR-0052's scripted
  endpoint and changes only the error type it raises; leaves ADR-0126's account routing unchanged

**Instruction.** This record serves an instruction the project's maintainer gave on 2026-10-20:
remove `aiohttp` and `msgspec` from the base dependencies to reduce the supply-chain surface, with
no deprecation cycle, since no known caller configures the endpoint's client beyond its headers.

Purpose: every HTTP request lionagi makes to a model provider goes through one client that the
project owns and that depends only on the standard library and `anyio`, so a vulnerability in a
third-party HTTP stack is no longer a lionagi release.

## Context

`Endpoint` (`lionagi/service/connections/endpoint.py`) opened an `aiohttp.ClientSession` per call
and per stream, with `aiohttp.ClientTimeout(total=config.timeout)` and whatever
`EndpointConfig.client_kwargs` held. Three provider modules built `aiohttp.FormData` bodies, two
overrode the `aiohttp`-named transport helpers, the retry layers named `aiohttp.ClientError`, and
the scripted test endpoint built `aiohttp.ClientResponseError` objects to look realistic.

**P1. The HTTP stack was the largest third-party surface in a base install.** `aiohttp` brought
nine packages with it into the 40-package closure of `uv export --no-dev`: `aiohappyeyeballs`,
`aiosignal`, `async-timeout`, `attrs`, `frozenlist`, `multidict`, `propcache`, `yarl`, plus
`aiohttp` itself; `msgspec` was the tenth. Its floor in `pyproject.toml` was raised for security
advisories three times in 2026 (3.11.0 to 3.13.3 in February, 3.13.0 to 3.14.1 in June, 3.14.1 to
3.14.3 in August), each one a release that every user had to take. The latest raise named three
advisories: two in WebSocket code lionagi never calls, and one in the C response parser that every
request did use. lionagi uses none of `aiohttp`'s WebSocket, server, cookie jar, or cross-call
connection pooling.

**P2. `msgspec` was a hard dependency of an optional feature.** The structural helpers
(`lionagi/ln/_hash.py`, `_structural.py`, `_to_list.py`, `_async_call.py`) accept a `msgspec.Struct`
when one is passed. Each site already imported `msgspec` under `try`/`except ImportError` or only
after seeing a `Struct`, so the package was installed for every user and needed by none who do not
pass one.

**P3. Redirects were followed by default.** `ClientSession.request` follows redirects unless told
not to. The endpoint's address guard (`_assert_ssrf_safe_url`) checks the configured URL once, so a
redirect could take a request, its body and its authorization header to an address the guard never
saw. Only the Lattice adapter turned redirects off.

**P4. A cancelled or timed-out request must free what it holds.** The replacement does its blocking
work in worker threads (D2). A thread blocked on a socket does not notice the task that waits on it
being cancelled, so the client has to end the socket operation itself, including after
`http.client` has handed the socket from the connection to the response (it does this whenever the
server says it will close the connection).

| Concern | Decision |
|---------|----------|
| Dependencies | D1: the base install drops `aiohttp` and `msgspec`; `msgspec` stays accepted when installed |
| Transport | D2: `http.client` per request, blocking steps in `anyio` worker threads under a per-loop limiter |
| Request semantics | D3: one deadline for the whole request; no redirects, no environment proxies, identity encoding |
| Ending a request | D4: cancel, timeout and close shut the socket down; a request stopped before sending is never sent |
| Errors | D5: `HTTPStatusError` for statuses, `TransportError` for the connection, `TimeoutError` for the deadline |
| Client options | D6: `client_kwargs` accepts `headers` only; anything else is a `TypeError` |
| Names and bodies | D7: `_call_http` / `_stream_http`, `MultipartForm`, and an in-repo test server |

Out of scope:

- Connection reuse across calls. Each call already opened its own session before this record, so no
  pool is lost; a pool is DEFERRED (see Alternatives).
- HTTP/2. Neither the old nor the new client speaks it.
- The `sandbox` extra. `daytona` still depends on `aiohttp` through `aiohttp-retry`; a user who
  installs that extra gets it. The base install and every other extra do not.
- Agentic (subprocess) providers. They make no HTTP requests (ADR-0030).

## Decision

### D1. The base install drops `aiohttp` and `msgspec`

`pyproject.toml` lists neither in `dependencies`. The development group lists `msgspec>=0.20.0`, so
the tests that pass a `Struct` through the structural helpers keep running. Measured on the lock
this record ships with: `uv export --no-dev` lists 30 packages, against 40 before; the ten removed
are the ones P1 and P2 name, and none were added.

Exact semantics:

- `import lionagi` and every endpoint call and stream work with neither package installed.
- With `msgspec` installed, a `Struct` is hashed, listed, compared and accepted as model-like exactly
  as before; the import sites are unchanged.
- `li doctor` no longer reports `aiohttp` among the core dependencies.

### D2. The transport is `http.client` in worker threads

The client lives in `lionagi/service/connections/_http.py`:

```python
MAX_REQUESTS_IN_FLIGHT = 256

class HTTPSession:
    def __init__(self, *, timeout: float | None = None, headers: Any = None): ...
    async def __aenter__(self) -> HTTPSession: ...
    async def __aexit__(self, *exc_info: object) -> None: ...   # closes open responses
    def close(self) -> None: ...
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
    ) -> HTTPResponse: ...

class HTTPResponse:
    status: int
    reason: str
    headers: http.client.HTTPMessage
    method: str
    url: str
    closed: bool
    async def read(self) -> bytes: ...
    async def text(self, encoding: str = "utf-8") -> str: ...
    async def json(self) -> Any: ...
    async def iter_chunks(self) -> AsyncIterator[bytes]: ...   # pieces as they arrive
    def close(self) -> None: ...
    release = close
```

Each `request` opens one `HTTPConnection`, or an `HTTPSConnection` whose TLS context is
`ssl.create_default_context()` (one context per process, reused), and the connection is closed when
its response is read, released, or its session closes. Connecting and sending, waiting for the status
line, and each body read are separate blocking steps; each runs through
`anyio.to_thread.run_sync(..., abandon_on_cancel=True, limiter=...)`.

Exact semantics:

- The limiter is an `anyio.CapacityLimiter(MAX_REQUESTS_IN_FLIGHT)` kept in an `anyio` `RunVar`,
  so each event loop has its own and HTTP steps never take slots from the default thread limiter
  that other code waits on.
- A step waiting for a slot is waiting inside its request's deadline (D3).
- `anyio` releases a slot as soon as the waiting task is cancelled, while an abandoned thread runs
  on; D4 is what makes that thread end promptly.
- `iter_chunks` yields what each `read1(65536)` returns. A piece can end inside a line or inside a
  UTF-8 character; the endpoint's stream parser decodes incrementally (`codecs` incremental decoder)
  and splits lines itself.
- `json()` decodes the body as JSON without checking `Content-Type`. A 200 response is therefore
  decoded whatever type it declares: a JSON body served as `text/plain` now succeeds, and a body
  that is not JSON raises `json.JSONDecodeError` (a `ValueError`), which no retry path retries.
  `aiohttp`'s `json()` raised `ContentTypeError` for any type other than `application/...json`, and
  as a `ClientError` that was retried by the native path. The endpoint's error-body read for a
  non-200 status already caught any failure and falls back to a message without the body.

**Why this way.** Provider traffic is a few long requests per conversation, so a thread per request
in flight costs little next to the request, and 256 is far above what one process runs at once while
still bounding threads if a caller fans out without limit. `http.client` is the standard library's
HTTP/1.1 implementation, maintained with the interpreter and covered by its security releases. The
comparison with the other candidates is under Alternatives.

### D3. One deadline per request; no redirects, proxies or compression

Exact semantics:

- `HTTPSession(timeout=t)` bounds each request from its start until its response is read, a
  streamed body included, which is what `aiohttp.ClientTimeout(total=t)` bounded. Each blocking step
  runs under `anyio.fail_after` with the time left, and the socket's own timeout is set to the same
  remainder before each step. `timeout=None` sets no bound.
- A 3xx response is returned like any other status; the endpoint then treats it as a non-200
  failure. `allow_redirects=True` raises `ValueError`. The Lattice adapter still passes
  `allow_redirects=False`, which is accepted.
- `HTTP_PROXY`, `HTTPS_PROXY` and `NO_PROXY` are not read (`aiohttp` read them only with
  `trust_env=True`, which the endpoint never set).
- The request carries `Accept-Encoding: identity`, so bodies arrive uncompressed and no
  decompressor is involved.
- Default headers: `User-Agent: lionagi/<version>`; `Content-Type: application/json` for a `json=`
  body unless the caller set one; `Content-Length` for any body. A later header source replaces an
  earlier one of the same name in any case (session headers, then request headers).
- `params` are URL-encoded and appended to any query the URL already has.
- `json=` together with `data=`, and a URL that is not `http` or `https` with a host, raise
  `ValueError` before any connection is opened.

**Why this way.** Not following redirects closes P3 for every provider at once instead of one
adapter at a time; a provider that moves an endpoint is configured at its new URL. Identity encoding
trades transfer size on large bodies for having no decompression code in the path. `aiohttp` asked
for `gzip, deflate` by default.

### D4. Cancel, timeout and close end the connection

The `_Exchange` object owns one connection and the socket it opened:

- `stop()` marks the exchange stopped. If no step is running it closes the response and the
  connection now. If a step is running it shuts the socket down (`SHUT_RDWR`), which makes the
  blocked read or write return; the thread running the step then closes everything.
- The exchange keeps its own reference to the socket, taken right after connecting, because
  `http.client` clears `HTTPConnection.sock` once a response says the server will close the
  connection. Without that reference a cancelled read under `Connection: close` would block until
  the server sent something.
- A stop that arrives while the connection is still being opened is seen before anything is sent:
  `send()` checks after `connect()` returns and raises `TransportError("the request was stopped")`.
  The connect itself runs until it completes or its socket timeout expires.
- A cancelled task, an expired deadline, and a step that fails all call `stop()`. `HTTPResponse`
  `close()` and `HTTPSession.close()` call it too, so the peer sees the connection end when the
  caller is done with it, not when the objects are collected.

Tests (`tests/service/connections/test_http_client.py`) run the timeout, cancel and close cases
against a local server that answers once with `Connection: close` and once without it, and require
the server to see the connection end; the stop-while-connecting case runs against a stand-in
connection that records whether a request was sent.

### D5. Errors

```python
class TransportError(ConnectionError): ...

class HTTPStatusError(APIClientError):
    def __init__(
        self,
        message: str,
        *,
        status: int,
        headers: Mapping[str, str] | None = None,
        method: str = "",
        url: str = "",
    ): ...
    # .status and .status_code carry the status; .headers is a plain dict
```

Exact semantics:

- Apart from `ValueError` and `TypeError` for arguments it refuses (D3, D6, D7), the client raises
  `TransportError` or `TimeoutError`, and lets a cancellation through. A refused or reset
  connection, a TLS failure, and a reply that is not HTTP become `TransportError` naming the method
  and URL, with the original exception as `__cause__`.
- An expired deadline raises `TimeoutError("<METHOD> <url> did not complete within <t> s")`.
- The endpoint raises `HTTPStatusError` for a status it treats as a failure, where it raised
  `aiohttp.ClientResponseError`: 429 and 5xx as retryable, other non-200 statuses wrapped in the
  existing non-retryable sentinel, and a 200 body that names an error under the status it names.
- The endpoint's native retry path (no `RetryConfig`) retries `HTTPStatusError`,
  `TransportError`, `TimeoutError` and `asyncio.TimeoutError`, where it retried
  `aiohttp.ClientError` and `asyncio.TimeoutError`. `RetryConfig`'s default retry exceptions name
  `APIClientError` (which `HTTPStatusError` is) and `ConnectionError` (which `TransportError` is).
- The Anthropic adapter's routed path, which caught `aiohttp.ClientError`, catches
  `ConnectionError`.
- The scripted endpoint (`lionagi/testing/_endpoint.py`, ADR-0052) raises `HTTPStatusError` with
  status 429, 500 or 400 for its rate-limit, server and bad-request errors.

**Why this way.** Keeping the status error under `APIClientError` puts it under the one base class
the resilience layer already uses, and making the transport error a `ConnectionError` puts it where
the standard library and most callers already look for network failures. Callers that read
`.status` and `.headers` keep working; `.request_info` and `.history` are gone.

### D6. `client_kwargs` accepts only `headers`

`Endpoint._create_http_session` passes `EndpointConfig.client_kwargs` to `HTTPSession` after
`check_client_options` has checked it:

```python
def check_client_options(options: Mapping[str, Any]) -> None:
    """Refuse a client option this client does not have: it takes only ``headers``."""
```

Any other key (`connector`, `trust_env`, `cookies`, `auth`, ...) raises
`TypeError("client_kwargs supports only 'headers'; got ...")` when the session is created, that is,
on the first call or stream. Request keyword arguments that were forwarded to `aiohttp` (`ssl=`,
`proxy=`, `timeout=`, ...) now raise `TypeError` from `HTTPSession.request`. The account router's
existing check (`lionagi/service/accounts/_router.py`) already admitted only headers on routed calls.

**Why this way.** Ignoring an option silently would change what a configured request does without
saying so; a `TypeError` naming the key says it at the first use. There is no deprecation window: the
maintainer judged that no caller configures the client beyond headers, and a window would mean
keeping `aiohttp` installed for it, which is what this record removes.

### D7. Names, form bodies, and the test server

- `Endpoint._call_aiohttp` is `Endpoint._call_http`, and `_stream_aiohttp` is `_stream_http`, with
  the same parameters. Subclasses that override them (Lattice, Anthropic) override the new names.
- `MultipartForm` replaces `aiohttp.FormData` in the OpenAI audio and image adapters and the Groq
  transcription adapter:

  ```python
  class MultipartForm:
      boundary: str                     # uuid4 hex
      content_type: str                 # "multipart/form-data; boundary=<boundary>"
      def add_field(self, name: str, value: Any, *, filename: str | None = None,
                    content_type: str | None = None) -> None: ...
      def encode(self) -> bytes: ...
  ```

  A value is `str`, bytes, or a file object read once when the body is built; anything else raises
  `TypeError`. A value that contains the boundary raises `ValueError`. Names and filenames are
  written as browsers write them (RFC 7578 section 4.2 allows UTF-8 there): UTF-8 as is, with `"`, CR
  and LF percent-encoded, so a name cannot end its parameter or add a header. `aiohttp` percent-
  encoded every filename character outside the unreserved set and backslash-escaped names, so a
  filename with a space or a non-ASCII character now reaches the server as written. Bodies are still built inside each attempt, as ADR-0027 records, because a file object is
  consumed by the first send.
- `tests/_local_http.py` is a small asyncio HTTP/1.1 server used by the wire-level tests: routes with
  `{name}` patterns, JSON and streamed (chunked) responses, a redirect response, multipart parsing,
  and `announce_close` to answer with or without `Connection: close`.

## Consequences

- A base install is 30 packages. A vulnerability in `aiohttp` or its dependencies no longer reaches a
  user who has not installed the `sandbox` extra.
- HTTP behaviour is now lionagi's to maintain: about 450 lines in `_http.py` and the tests that pin
  them. A defect in chunked decoding or TLS is `http.client`'s and arrives with interpreter updates;
  a defect in deadlines, cancellation or headers is ours.
- Each request in flight holds a worker thread, at most 256 per event loop. A process that runs more
  requests than that at once queues them, and the queueing counts against their deadlines.
- A request stopped while its TCP or TLS connection is being opened leaves its thread in `connect()`
  until the connect finishes or times out; nothing is sent on that connection.
- Responses arrive uncompressed. Large non-streamed JSON responses cost more bytes on the wire than
  under `aiohttp`, which asked for gzip.
- Code that configured `client_kwargs` beyond headers, passed `aiohttp` request options, caught
  `aiohttp` exceptions, or overrode `_call_aiohttp` / `_stream_aiohttp` breaks at upgrade. The
  CHANGELOG's upgrade notes list each.
- Reversing D1 means re-adding the dependency and an adapter from `aiohttp` to the D5 errors; the
  endpoint code would not need to change shape again.

## Alternatives considered

- **Keep `aiohttp` and keep raising its floor.** No code change, and connection handling stays with a
  widely used library. It lost on P1: three floor raises in one year for features lionagi never calls,
  each a forced upgrade for every user, and nine transitive packages that exist only to support it.
- **`httpx`.** A maintained client with an `anyio`-based async transport, timeouts, and no thread per
  request. It would add `httpx`, `httpcore` and `h11` (none are in the base closure today), so it moves
  the surface rather than removing it, and its release cadence would again set ours.
- **`requests` or `urllib3`.** Both are already in the base closure, but only because `tiktoken`
  depends on `requests`. Building on a package that is present only through another package's
  dependency list ties the transport to that list. Both are blocking clients, so they would need the
  same worker-thread wrapping as `http.client` while adding a dependency to declare.
- **An HTTP/1.1 client on `asyncio` streams.** No threads, and cancellation is native. It means
  writing and maintaining a response parser (status line, headers, chunked bodies, trailers) and TLS
  handling on two backends, since lionagi runs on both asyncio and trio through `anyio`. That is the
  code with the most security exposure, and `http.client` already has it.
- **A deprecation cycle with `aiohttp` as an optional extra.** It would keep existing
  `client_kwargs` working for a release. It lost on the maintainer's call (D6), and because two
  transports would double the test surface for a configuration nobody is known to use.
- **Connection reuse across calls (DEFERRED).** A pool keyed by scheme, host and port, with idle
  connections checked before reuse, would save a TCP and TLS handshake per call to the same
  provider. Before this record each call opened its own `ClientSession`, so there was no reuse to
  keep. The design, when measured latency asks for it: a per-loop pool beside the limiter, an idle
  timeout under the provider's keep-alive, a connection returned only after its body is fully read,
  and never reused after a stop.

## Notes

- The test files that exercised `aiohttp` objects were converted one for one: at conversion each
  file collected the same number of tests as before, and the only changed test IDs were the eleven
  whose names said `aiohttp`. New tests were added afterwards, for the client and for a UTF-8
  character split across two pieces of a streamed body.
- `.github/workflows/ci.yml` keeps `aiohttp` in the list of modules a bare `import lionagi` must not
  load; it still guards the `sandbox` extra.
