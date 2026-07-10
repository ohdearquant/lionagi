# ADR-0076: Studio daemon route registry and local control plane

- **Status**: Proposed
- **Kind**: Retrospective
- **Area**: studio
- **Date**: 2026-07-09
- **Relations**: supersedes v0-0001, v0-0002, v0-0006, v0-0008, v0-0018

## Context

Studio is a FastAPI daemon in `lionagi/studio/`, not a passive database viewer. The
running application exposes reads, launches, cancellation, definition and schedule
mutation, approvals, show import, and maintenance. It also owns scheduler startup,
stale-state reconciliation, optional transcript mirroring, launch cleanup, and a
deferred WAL checkpoint. The earlier CLI-primary, browser-mostly-read-only boundary
therefore no longer describes the service.

This ADR answers five concrete problems in the shipped daemon.

**P1 — Endpoint decoration does not by itself make an endpoint public.** Service
modules call `studio_route()`, but registration happens as a Python import side effect.
Without one explicit composition root, a valid decorated handler can remain unreachable,
duplicate registration can depend on import order, and clients cannot tell which route
set is authoritative (`lionagi/studio/registry.py`; `lionagi/studio/app.py`).

**P2 — One process has three delivery shapes.** Studio must run as an API-only daemon,
serve a built Vite distribution, or sit behind the Vite development server. A catch-all
SPA route would intercept FastAPI's trailing-slash redirects, while returning the SPA for
unknown `/api/*` paths would turn API errors into HTML (`lionagi/studio/app.py`;
`lionagi/studio/cli.py`).

**P3 — Readiness depends on some lifecycle work but not all maintenance.** Stateful
routes must not observe phantom sessions left from a prior process, yet a WAL checkpoint
or optional transcript mirror must not indefinitely block liveness. Shutdown must settle
owned tasks and launched subprocesses before the scheduler stops (`lionagi/studio/app.py`).

**P4 — Loopback-first is not the same as an absent trust boundary.** A browser can reach
a loopback daemon through DNS rebinding or cross-origin requests. When a bearer token is
configured, schema and API paths expose protected information; the static SPA must still
load without attaching a token to document navigation. State-changing requests with a
body must not accept form-compatible content types that avoid a CORS preflight
(`lionagi/studio/app.py`; `lionagi/studio/config.py`).

**P5 — “SSE” names a framing mechanism, not one event protocol.** Session output,
persisted session signals, show-directory changes, and Leo turns all emit unnamed
`data:` frames, but their cursors, terminal tests, and error paths differ. Treating them
as one replayable event log would promise semantics the code does not implement
(`lionagi/studio/services/_sse.py`; `sessions.py`; `shows.py`; `leo.py`).

| Concern | Decision |
|---|---|
| Public HTTP composition | D1: Make the declarative registry plus fixed module manifest the sole API composition root. |
| API and SPA delivery | D2: Mount registered routes below `/api`, keep `/health` direct, and make SPA hosting optional. |
| Process lifecycle | D3: Reconcile before readiness, defer non-critical maintenance, and stop owned resources in order. |
| Local network boundary | D4: Apply host validation, explicit CORS, JSON-body enforcement, and optional bearer auth centrally. |
| Live transport | D5: Keep endpoint-specific fetch-compatible SSE contracts and do not imply a central event bus. |

Out of scope:

- Scheduler firing, lease, retry, and worker semantics are owned by the
  scheduling-control-plane area; this ADR records only daemon lifecycle wiring.
- StateDB schema and file/database consistency are owned by ADR-0077.
- The web client's information architecture is owned by ADR-0079 and ADR-0080.
- A durable operator-command protocol is the aspirational decision in ADR-0083.
- Network-wide identity, authorization roles, and remote exposure are not introduced
  here; the shipped boundary is a local daemon with one optional bearer secret.

## Decision

### D1 — Declarative route registry and explicit composition root

`lionagi/studio/registry.py` is the public HTTP composition root. Its shipped Python
contract is:

```python
Handler = TypeVar("Handler", bound=Callable[..., Any])
HttpMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE"]

@dataclass(frozen=True, slots=True)
class StudioRoute:
    order: int
    area: str
    path: str
    method: HttpMethod
    handler: Callable[..., Any]
    response_model: Any | None
    dependencies: tuple[Any, ...]
    status_code: int | None
    tags: tuple[str, ...]
    name: str | None
    summary: str | None
    description: str | None
    response_class: type[Response] | None
    responses: Mapping[int | str, Mapping[str, Any]] | None
    include_in_schema: bool

def studio_route(
    path: str,
    *,
    method: HttpMethod,
    area: str,
    response_model: Any | None = None,
    dependencies: Sequence[Any] = (),
    status_code: int | None = None,
    tags: Sequence[str] | None = None,
    name: str | None = None,
    summary: str | None = None,
    description: str | None = None,
    response_class: type[Response] | None = None,
    responses: Mapping[int | str, Mapping[str, Any]] | None = None,
    include_in_schema: bool = True,
) -> Callable[[Handler], Handler]: ...

def load_studio_route_modules() -> None: ...
def iter_studio_routes(*, area: str | None = None) -> tuple[StudioRoute, ...]: ...
```

The fixed manifest contains 22 service modules:

```text
casts, runs, engine_runs, definitions, agents, playbooks, shows, skills,
plugins, teams, invocations, launches, projects, engine_defs, workflow_defs,
sessions, run_tags, leo, approvals, admin, schedules, stats
```

All names are relative to `lionagi.studio.services`. `app._mount_studio_routes()`
imports that manifest, iterates registration order, and calls
`application.add_api_route(f"/api{route.path}", ...)`. Importing the current app
produces 96 decorated `/api` routes. That number demonstrates the surface size; it
is not a compatibility constant.

Exact semantics:

- Registration appends in decorator execution order; `iter_studio_routes()` sorts by
  the captured integer `order` and returns an immutable tuple.
- When `tags is None`, the route receives `(area,)`; an explicit empty sequence means
  no tag.
- The deduplication key is `(path, method, fn.__module__, fn.__qualname__)`. Repeating
  that same registration raises `ValueError`; two different handlers can still claim
  the same method/path and must be caught by route-contract verification.
- Import caching makes repeated manifest imports harmless for already-loaded modules.
  `_reset_registry()` exists for tests, not runtime hot reload.
- A decorated handler in an unlisted module is not mounted. This is deliberate: public
  reachability requires both declaration and inclusion at the composition root.
- The manifest order controls registration order. Literal routes that might otherwise
  be captured by parameter routes must be registered first within their service.

Why this way: one registry makes metadata enumerable without introducing a parallel
`APIRouter` hierarchy per small service. The fixed manifest also makes public inclusion
an explicit reviewable act. The tradeoff is manual composition, so a missing manifest
entry is a realistic failure mode rather than an impossible state.

### D2 — `/api` boundary, direct health, and optional SPA mount

The application factory is the stable construction seam:

```python
def create_app() -> FastAPI: ...

app = create_app()
```

`create_app()` installs the lifespan, exception translation, central middleware,
registered API routes, direct `GET /health`, optional assets, and final middleware
order. `LionError` becomes `{"detail": exc.message}` with the error's status code.
Health returns exactly:

```json
{"status": "ok"}
```

SPA discovery and fallback use:

```python
def _resolve_frontend_dist() -> Path | None: ...
def _mount_spa(application: FastAPI, dist: Path) -> None: ...
```

Exact semantics:

- If `LIONAGI_STUDIO_FRONTEND_DIST` is unset, the daemon is API-only.
- If it is set but `<dist>/index.html` does not exist, the daemon remains API-only.
- If `<dist>/assets` exists, it is mounted at `/assets` with `StaticFiles`.
- An unmatched `/api` or `/api/*` request remains JSON 404; it never receives the SPA.
- An unmatched non-API `GET` or `HEAD` receives `index.html` with `no-store`,
  `no-cache`, and `must-revalidate`; any other method remains JSON 404.
- The fallback is a 404 exception handler, not `/{full_path:path}`. This preserves
  FastAPI's trailing-slash redirect behavior for routes such as `/api/runs/`.
- `python -m lionagi.studio` starts Uvicorn with `HOST` and `STUDIO_PORT`.

The shipped defaults in `lionagi/studio/config.py` are port `8765` and host
`127.0.0.1`. The port is inherited from the existing Studio surface; no separate
rationale is recorded beyond compatibility. Loopback is the safety-oriented default.

### D3 — Ordered daemon lifecycle

The lifespan contract is:

```text
startup:
  scheduler.start()
  run_startup_reconciliation()       # readiness gate
  optional Claude mirror task
  create _startup_warmup task         # WAL checkpoint, not a readiness gate
  yield ready

shutdown:
  settle/cancel warmup task
  stop mirror task
  shutdown_launches()
  scheduler.stop()
```

Exact semantics:

- Scheduler startup happens before reconciliation so the daemon owns one initialized
  scheduler throughout its ready lifetime.
- Reconciliation is awaited before `yield`; stateful API reads do not race the repair
  of stale session and invocation rows.
- The WAL checkpoint runs in a named background task. Any failure is logged and does
  not make startup fail.
- Optional transcript mirroring is controlled by
  `LIONAGI_STUDIO_MIRROR_CLAUDE` (default enabled), starts with a default `24h`
  catch-up window, and polls every `5` seconds. These values are inherited operating
  defaults; the source records bounded catch-up and live tailing as the reasons, but
  no measurement selecting exactly 24 hours or 5 seconds.
- Mirror shutdown signals its stop event and waits up to 10 seconds, then cancels the
  task. Ten seconds is a backstop inherited from the implementation; no recorded
  measurement justifies the exact value.
- An unexpectedly failed mirror is logged by a done callback and does not stop Studio.
- Warmup is cancelled and awaited on shutdown so no task is left pending.
- Owned launches stop before the scheduler. Resources the daemon did not launch are
  not claimed by this lifecycle.

The split is intentional: reconciliation changes the truth read by API handlers and
therefore gates readiness; checkpointing is maintenance and does not.

### D4 — Central local security and request boundary

The middleware execution order for an incoming request is fixed by Starlette's LIFO
wrapping:

```text
Host validation
  → CORS
    → JSON content-type / CSRF guard
      → optional bearer-token gate
        → route handler
```

The relevant configuration fields and defaults are:

```python
STUDIO_PORT = int(env.get("LIONAGI_STUDIO_PORT", "8765"))
HOST = env.get("LIONAGI_STUDIO_HOST", "127.0.0.1")
CORS_ORIGINS = configured comma-separated origins or [
    "http://localhost:5173",
    "http://localhost:3000",
    "http://localhost:3765",
    "https://lion-studio.khive.ai",
]
```

Exact semantics:

- Host validation runs first, including for preflight. It accepts `localhost`,
  `127.0.0.1`, `::1`, and a configured non-wildcard bind host. Malformed authorities,
  unlisted hosts, suffix tricks, and unbracketed IPv6 fail with HTTP 400.
- CORS allowed methods are derived after all routes and optional mounts exist. `OPTIONS`
  is always added. This avoids silently omitting FastAPI-generated `HEAD` methods.
- With no `LIONAGI_STUDIO_AUTH_TOKEN`, requests are not bearer-gated. Startup emits a
  warning, with a stronger warning for unauthenticated `0.0.0.0`.
- With a token, every `/api` and `/api/*` path plus `/openapi.json`, `/docs`, `/redoc`,
  and `/docs/oauth2-redirect` requires the exact `Authorization: Bearer <token>` value.
  Failure returns `{"detail":"Unauthorized"}` and HTTP 401.
- `/health` remains public. Non-API `GET` and `HEAD` requests other than schema/docs
  remain public so the SPA document and hashed assets can load.
- `OPTIONS` passes the inner guards because valid-host preflight is answered by CORS.
- A non-GET/HEAD/OPTIONS `/api` request with a non-empty body must declare media type
  `application/json`; otherwise it returns HTTP 415. Bodyless mutation requests do not
  acquire a synthetic content-type requirement.
- The rule is central. Individual services cannot opt out by omitting a dependency.

This is defense in depth for the shipped local daemon; it is not a claim that one static
bearer secret supplies user-level authorization.

### D5 — Endpoint-specific SSE over authenticated fetch

The shared transport helper is deliberately small:

```python
SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}

def sse_response(generator: AsyncGenerator[str]) -> StreamingResponse:
    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )
```

All current producers emit unnamed frames of the form `data: <JSON>\n\n`, but their
contracts are distinct:

| Endpoint | Cursor/source | Poll/heartbeat | Terminal and miss semantics |
|---|---|---|---|
| `/api/sessions/{id}/stream` | local `after_ts`, messages after timestamp | poll 0.5s; heartbeat after 5s idle | preflight 404; `done` only after terminal status is stable more than 60s |
| `/api/sessions/{id}/signals` | persisted per-session `seq`, starting at 0 | rows limited to 500 per poll; poll 0.5s; heartbeat after 5s | preflight 404; same 60s stable terminal test |
| `/api/shows/{topic}/stream` | in-memory `(mtime,size)` map over sorted files | scan every 0.5s; no heartbeat | route preflight 404; generator emits `done` on invalid/missing directory or terminal DB status after 60s without file change |
| `/api/leo/sessions/{id}/messages` | one in-memory turn, no replay cursor | no heartbeat/reconnect state | missing/expired session 404; concurrent turn 409; model error emits `error` then `done`; success emits effects, `text`, then `done` |

The 0.5-second polls, 5-second heartbeats, and 60-second stability windows are shipped
compatibility values. Their qualitative reasons are fast local feedback, keeping idle
connections visibly alive, and avoiding closure on a transient terminal write. The source
contains no measurement selecting the exact numbers. The signal batch cap of 500 bounds
each database read while allowing replay to advance over repeated polls; no recorded
measurement selects exactly 500.

Fetch-based consumers are required when bearer auth is enabled because native
`EventSource` cannot attach the authorization header. There is no `id:` field,
`Last-Event-ID` handling, universal cursor, common error envelope, or central replay log.

## Consequences

- The daemon has one enumerable composition root and one central request boundary.
  Adding a public service requires a decorated handler and a manifest decision.
- API-only, daemon-hosted SPA, and Vite-development modes share the same FastAPI API.
- Stateful readers see reconciled lifecycle data at readiness, while optional mirror and
  checkpoint failures degrade observability or maintenance rather than availability.
- A contributor changing a route path, trailing slash, response model, or stream frame
  changes a compatibility surface consumed by both web and VS Code clients.
- Reversing D1 requires rebuilding enumeration, ordering, and deduplication around another
  router composition mechanism. Reversing D4 requires a replacement central trust boundary;
  service-local checks are not equivalent.
- Manual manifest maintenance can omit a service. The present dedup key also cannot reject
  two different handlers for the same method/path.
- Tokenless mode is intentionally possible and becomes unsafe if the bind boundary expands.
- Endpoint-specific SSE keeps each service simple but makes reconnect and terminal behavior
  client knowledge. A shared envelope is current debt, not a shipped guarantee.

## Current-vs-ideal delta

| # | Delta | Size | Issue |
|---|---|---|---|
| 1 | Generate and verify the mounted Studio route manifest and OpenAPI snapshot in CI; fail when an intended public service is absent or an existing method/path changes without an explicit compatibility update. | S | (filled at issue-open time) |
| 2 | Define a shared SSE frame envelope, error frame, terminal frame, and reconnect-cursor rule for session, signal, show, and Leo streams while preserving endpoint-specific payloads. | M | (filled at issue-open time) |
| 3 | Decide whether filesystem show import is a browser-admin mutation or a CLI maintenance operation, then enforce one policy with matching authorization and confirmation behavior. | S | (filled at issue-open time) |
| 4 | Document and test API-only, daemon-hosted SPA, Vite development, and container/reverse-proxy modes against the same host, CORS, token, and API-base rules. | S | (filled at issue-open time) |

## Alternatives considered

### One `APIRouter` per service

This would use familiar FastAPI composition and make each module mountable independently.
It lost because the shipped services are already small, metadata-rich decorated handlers;
adding routers would create a second inclusion and prefix mechanism without eliminating the
need for an application-level manifest. It would also make current registry enumeration a
migration requirement rather than a simplification.

### Filesystem or package auto-discovery

Scanning every `services/*.py` module would prevent a forgotten manifest entry and ease
extension. It lost because import becomes publication: a helper or unfinished service could
be exposed merely by existing. Explicit publication is safer for a control plane, provided
CI addresses omission risk.

### Read-only Studio with mutations left to the CLI

This would narrow the browser trust boundary and preserve the early CLI-primary shape. It
lost because launches, cancellation, definition and schedule mutation, approvals, show
import, and maintenance are already shipped API responsibilities. Calling the daemon
read-only would hide rather than remove those contracts.

### Catch-all SPA route

`/{full_path:path}` is the conventional SPA fallback and is simpler than an exception
handler. It lost because it captures `/api` paths before FastAPI can perform trailing-slash
redirects. The 404 handler preserves route resolution and keeps API misses JSON.

### Require bearer auth for every path

This would give one simple rule. It lost because browser document navigation and asset
loads do not carry the runtime bearer header; protecting the shell would make authenticated
mode unloadable. The chosen split keeps data under guarded `/api` while public static bytes
contain no application state.

### General Studio event bus and replay log

A central taxonomy could unify reconnect, observability, and cross-entity subscriptions.
It would also require a durable event model, cursor policy, retention, and compatibility
rules that no current cross-entity use case demonstrates. It lost to bounded per-endpoint
streams; the smaller shared-envelope delta remains justified by the two current clients.

### WebSocket as the default live transport

A duplex socket would support server and client events on one connection. Current session,
signal, and show consumers are server-to-client only, and authenticated fetch already
supports them. WebSocket therefore adds heartbeat, ticketing, and replay work without
removing the need for ordinary HTTP commands. ADR-0083 may revisit transport only for a
demonstrated operator-command need.
