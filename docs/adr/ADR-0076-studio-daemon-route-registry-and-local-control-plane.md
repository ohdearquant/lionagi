# ADR-0076: Studio daemon route registry and local control plane

- **Status**: Accepted
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
- The conventional `~/.lionagi` profile may mirror the ambient
  `~/.claude/projects` and `~/.codex/sessions` trees by default. Any explicitly
  selected, different `LIONAGI_HOME` is isolated: Studio starts no ambient mirror
  unless `LIONAGI_STUDIO_MIRROR_CLAUDE_ROOT` and/or
  `LIONAGI_STUDIO_MIRROR_CODEX_ROOT` names a source, or
  `LIONAGI_STUDIO_MIRROR_IMPORT_AMBIENT=1` explicitly restores ambient imports.
  A custom root wins over the corresponding ambient root, and a missing root
  removes that provider from a `both` request rather than falling through to home.
- Mirror startup logs describe the provider selection and window, never source
  paths or transcript content. An unexpected task failure reports only its exception
  class at this boundary; detailed transcript-bearing exceptions are not rendered
  into the user-facing startup log.
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
- Every non-GET/HEAD/OPTIONS `/api` request must declare media type
  `application/json`; otherwise it returns HTTP 415. The rule includes bodyless action
  routes so a cross-site simple request cannot bypass preflight by omitting its body.
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

Most producers emit unnamed frames of the form `data: <JSON>\n\n`. Session-message data
frames additionally carry `id: <opaque-cursor>` so authenticated fetch clients can resume;
the endpoint contracts remain distinct:

| Endpoint | Cursor/source | Poll/heartbeat | Terminal and miss semantics |
|---|---|---|---|
| `/api/sessions/{id}/stream` | Live mode uses the same-detail opaque message ticket, then retained delivery cursors in `id:`; cursorless and v1 requests retain timestamp-based replay | Live mode shares one per-session tail: bounded drains, poll 0.5s only when caught up, heartbeat after 5s idle; legacy retains per-subscriber polling and existing hydration budgets | Preflight 404; live ticket loss or mismatch returns 409 `stream_resync_required` with a typed reason, or resync control plus close after admission; v1 invalid/foreign cursor stays 400; done requires both backlogs drained, the existing greater-than-60s terminal stability test and the final same-connection change fence |
| `/api/sessions/{id}/signals` | Live mode retains both the same-detail snapshot ticket and last-applied `signal_seq`; explicit bounded history pages fold through the detail cut; legacy starts from its supplied sequence or 0 | Live mode shares the message tail resource, bounded drains, poll 0.5s only when caught up and heartbeat after 5s idle; legacy retains per-subscriber 500-row polling | Preflight 404; missing/foreign/expired live ticket or unavailable frontier returns 409 `stream_resync_required` with a typed reason, or resync control plus close after admission; done uses the shared drained-backlog, terminal-stability and change-fence decision |
| `/api/shows/{topic}/stream` | in-memory `(mtime,size)` map over sorted files | scan every 0.5s; no heartbeat | route preflight 404; generator emits `done` on invalid/missing directory or terminal DB status after 60s without file change |
| `/api/leo/sessions/{id}/messages` | one in-memory turn, no replay cursor | no heartbeat/reconnect state | missing/expired session 404; concurrent turn 409; model error emits `error` then `done`; success emits effects, `text`, then `done` |

The 0.5-second polls, 5-second heartbeats, and 60-second stability windows are shipped
compatibility values. Their qualitative reasons are fast local feedback, keeping idle
connections visibly alive, and avoiding closure on a transient terminal write. The source
contains no measurement selecting the exact numbers.

The legacy message path uses `MAX_HYDRATED_ROWS` (currently 50,000) plus one lookahead row and separate hydration/content/scan budgets; signals default to 500 rows. New live drains use their smaller named batch limit within those existing ceilings. None of these exact limits was selected by a recorded benchmark.

Fetch-based consumers are required when bearer auth is enabled because native
`EventSource` cannot attach the authorization header. The session-message stream alone
uses an SSE `id:` field and an explicit `cursor` query parameter; there is no
`Last-Event-ID` header handling, universal cursor, common error envelope, or central replay
log.

## Amendment 1 (2026-10-26): session live streams after a detail snapshot

Proposed. Related issue: #3107.

### Context

Opening a session first reads its detail and then opens message and signal streams. Replaying the same history through those streams duplicates work. Independent polling resources also make query and connection work grow with the number of viewers. Message timestamps are not commit order, so a maximum timestamp cursor cannot safely identify membership appended after the detail snapshot.

This amendment changes only session-message and session-signal delivery. Authenticated fetch SSE, existing cursorless clients, opaque v1 composite cursors, backward message pagination, heartbeat intervals, terminal stability and hydration budgets remain compatible. Other stream endpoints, WebSockets, database schema changes and client-retention policy are outside scope.

The rejected alternative "General Studio event bus and replay log" remains rejected: this tailer is per-session, daemon-local and non-durable, so the amendment does not reopen a general event bus or durable replay log. Multi-daemon ownership and PostgreSQL ownership are outside this amendment's scope.

### Decisions

#### Same-snapshot bootstrap and explicit live mode

A detail request opts in with `live_snapshot=true`. In one short read transaction it reads the detail and returns:

```text
stream_snapshot: {version: 1, message_cursor: <opaque ticket>, signal_seq: <cut>}
```

The ticket binds session, daemon boot epoch and, for each branch, its ID, existing progression ID, captured progression length and anchor ID. An empty branch uses length zero and no anchor. A previously captured branch disappearing, or its progression pointer changing even with the same length and anchor, invalidates the frontier and requires resnapshot. A bootstrap read does not itself start a tail resource. The response contains no signal projection or reducer checkpoint.

New live subscriptions use `/sessions/{id}/stream?mode=live&cursor=...` and `/sessions/{id}/signals?mode=live&snapshot=...&after_seq=...`. Signals retain the snapshot ticket on every reconnect; sequence alone does not establish the bootstrap generation. Existing v1 and cursorless requests keep their behavior. Legacy subscriptions keep per-subscriber polling; the shared tail serves live mode only. Backward `message_next_cursor` is separate from live continuation.

New message membership follows progression append positions, including equal-timestamp/lower-ID and backdated appends. The retained live delivery order is deterministic across partial-batch reconnects. Anchor mismatch, shrink, replaced progression, foreign epoch, expiry or lost retained frontier explicitly require a new snapshot. This detects the specified frontier changes; it does not claim to detect arbitrary same-length interior rewrites. No store-incarnation record or prefix digest is introduced.

#### Explicit paginated signal history and one reducer

Add `GET /sessions/{id}/signals/page?after_seq=A&through_seq=C&limit=L`. Both positions are nonnegative and `C >= A`; limit defaults to 500 and is capped at 500. Its response is:

```text
{signals: [...], next_after_seq: <last returned seq or A>,
 through_seq: C, has_more: <boolean>}
```

Rows are ordered by sequence and restricted to `A < seq <= C`, using existing signal parsing. Existing unbounded-above signal reads retain their semantics. Authentication and file-store 404/503 behavior remain consistent with the session endpoints.

The client obtains paired detail, reads the prefix through its cut in bounded pages, folds those rows with the existing `SignalProjection`, and only then opens both live subscriptions. Empty cut zero completes without historical live replay. Normalize signal timestamps from seconds to milliseconds once. Keep the existing 2,000-event raw ring, graph-sized indexes and counters. There is no duplicate server reducer, projection schema or persisted projection. Initial prefix work transfers O(signals) rows and makes O(signals) reducer appends per detail open; this is not a bound on total client CPU. Do not publish a projection after every prefix page: publish once after the full fold, then coalesce live publications through the existing queued microtask. Index copying, activity sorting and graph reduction have separate costs; neither their CPU cost, projection memory nor whole-detail work is bounded by the raw-ring size.

#### Shared bounded tail lifetime

One tailer per session per daemon/application instance shares one reusable read resource across both channels. Scope its registry to the configured application/store lifetime; restarting the application to reconfigure its store starts a new daemon epoch. This introduces no identity record or arbitrary hot-swap detection. Cache signal-table capability and its existing table definition at activation. A changed SQLite schema cookie triggers capability revalidation: appearance, disappearance or a changed definition of `session_signals` invalidates the tail and requires resnapshot before more delivery or done; an unrelated schema change refreshes the cookie after the signal definition is confirmed unchanged. Check this in admission, drain and terminal confirmation snapshots. Drain messages, signals and terminal state in short combined transactions; finish cursors and transactions before waiting. Tail query and connection creation work must not grow with viewer count.

Named operational limits cover broker count, subscribers, queue rows/bytes, per-broker/global retained bytes and ticket/idle expiry. Initial limits are 64 brokers, 128 subscribers per broker, 128 queued entries and 8 MiB queued serialized bytes per subscriber, 16 MiB retained per broker, 64 MiB retained globally, and 300-second ticket/cache idle expiry. Retained metadata counts toward limits. These limits are not RSS guarantees or benchmark-selected values. Oversized payloads and resource admission failures have explicit outcomes rather than endless retries. A slow consumer cannot block others. Last departure, cancellation and shutdown cancel and await the tailer and close its resource; dormant metadata remains bounded and expirable.

#### Overflow, reconnect and resnapshot

Reserve a control path independent of payload-queue capacity. Overflow or invalid retained state sends `resync_required`, a typed reason and a conservative starting marker, then closes the subscription. Message controls carry `resume_cursor`; signal controls carry `resume_after_seq`. Before streaming starts, return HTTP 409 with `detail.code="stream_resync_required"` and a reason. Reasons include overflow, expired, foreign_epoch, frontier_unavailable, anchor_mismatch, frontier_shrunk and resource_limit. V1 invalid cursors retain their existing 400 behavior.

Server markers are advisory; enqueueing or yielding does not acknowledge browser application. The client keeps its last successfully applied cursor/sequence and advances only after its consumer returns normally. Heartbeat, done and resync controls never advance it. A rejected live request never silently becomes legacy replay.

Either channel's resync stops both streams, coalesces one new paired detail request, folds its bounded signal pages and resubscribes. Visit and stream-generation guards discard stale responses, callbacks and older-page requests, including A-to-B-to-A navigation. Install the fresh authoritative message window, totals and backward cursor together. Compatibility is unproven for an old-only row: this opaque response does not expose a cross-snapshot progression-identity witness, and equal counts, anchor IDs or overlapping message IDs do not prove that progression P was not replaced by Q. Therefore this client retains no old-only rows across a paired resnapshot, including overflow, expiry and epoch recovery. Every prior row remains reachable by paging from the fresh backward cursor. Overlap consists only of rows actually returned in the fresh authoritative page, in that page's progression order; it cannot validate an unseen old prefix. A P-to-Q swap with the same length and anchor cannot preserve a stale old prefix through this rule. No server compatibility witness is added.

The fresh backward cursor starts immediately before that installed window. Older pages prepend their authoritative progression-ordered rows, deduplicate overlap by message ID, and preserve the already accepted newer live suffix and current totals; they never sort by timestamps. Thus a gap larger than the detail window remains reachable in order instead of being stitched ahead of a disjoint stale prefix. Backward history and ordinary terminal refreshes cannot rebase live cursors. An older server lacking the optional snapshot field may use temporary legacy compatibility after detail installation; malformed or unsupported explicit snapshots fail visibly. Automatic resnapshot is limited to one attempt without successfully accepted current-generation data; repeated failure surfaces an error instead of looping on resource limits or an expired bootstrap.

#### Terminal fence

Database ingestion is drained only when both message memberships and persisted signals have been consumed through the tail's frontiers. Done also requires the existing terminal status stable for more than 60 seconds. On the same read-only tailer connection, read `PRAGMA data_version` before the combined drain/state snapshot, end the transaction and read a fresh final marker. If unchanged, that final read is the terminal decision's linearization point.

A changed marker requires positive session-specific confirmation, rather than database-wide quiet. Open a fresh short read transaction on that same connection; its first contributing read pins a new SQLite snapshot. Confirm that the session still exists with the same terminal status and `updated_at`, every branch ID/progression ID/length/anchor equals the ingested frontiers, with the confirmation enumerating the session's complete current branch set in its snapshot and a branch absent from the ingested frontiers meaning drain again, and no persisted signal has sequence above the ingested signal frontier. Revalidate schema capability as above. If these predicates hold, the confirmation snapshot is the terminal decision's linearization point, even while unrelated sessions keep committing. If this session's facts differ, drain again; disappearance or invalid frontiers require resnapshot. An unequal marker is never accepted without this positive confirmation, and no retry count substitutes for it.

Database ingestion and subscriber delivery are separate. Marking ingestion complete lets each subscriber drain its own retained payload suffix on that channel before yielding normal `done`. A slow subscriber does not delay another subscriber's suffix or done; a joining subscriber receives its admitted retained suffix before done too. Only resync may preempt queued payloads. Neither terminal linearization rule promises anything about later writes. Never compare markers across connections or expose them as tickets. The tailer does not write through this connection. No read transaction persists across idle waits. SQLite documents the [connection-local marker](https://www.sqlite.org/pragma.html#pragma_data_version) and [retained transaction snapshots](https://www.sqlite.org/lang_transaction.html).

### Failure modes and acceptance tests

- A 100,000-message opening does not replay historical messages through the default live subscription. Report admission, explicit signal-prefix and steady-state work separately.
- A second writer commits between detail and subscription: ordinary, tied, backdated and new-branch appends arrive once with correct totals. Cover empty history and deterministic reconnects.
- Paginated prefix folding equals the existing full reducer, including evicted unnamed activity, later name correlation, spawn/escalation, gates, lane resets and duplicates. Verify one timestamp normalization and no new retention policy.
- Overflow and epoch recovery after a gap larger than the detail window install a fresh backward cursor; multiple older pages restore progression order, including backdated and tied rows, while new live rows remain present. Count-only or anchor-only compatibility never retains an old-only prefix after a progression replacement.
- Twenty viewers share one tail resource, with zero per-tick connection creation after activation and O(1) tail reads per session interval. Disclose remaining progression-JSON scan costs.
- Force row/byte overflow, resource limits, stale/foreign tickets, daemon or reconfigured-store restart and failed resnapshot. Distinguish applied positions from enqueued markers, prove coalesced recovery and preserve legacy behavior.
- Exercise commit-between-empty-read-and-terminal, both backlogs, final-fence changes, continuous unrelated-session commits during every fence attempt, own-session commits during confirmation, queued suffix delivery before done, a terminal-time join, cancellation and last-subscriber cleanup. Negative controls must detect removed guards.
- Activate without a signal table, create it and append a signal without restarting, and require resnapshot. An unrelated schema change must not invalidate an unchanged signal capability indefinitely.

### D5 table edit

Replace the two session rows with these rows; other endpoint rows remain unchanged.

| Endpoint | Cursor/source | Poll/heartbeat | Terminal and miss semantics |
|---|---|---|---|
| `/api/sessions/{id}/stream` | Live mode uses the same-detail opaque message ticket, then retained delivery cursors in `id:`; cursorless and v1 requests retain timestamp-based replay | Live mode shares one per-session tail: bounded drains, poll 0.5s only when caught up, heartbeat after 5s idle; legacy retains per-subscriber polling and existing hydration budgets | Preflight 404; live ticket loss or mismatch returns 409 `stream_resync_required` with a typed reason, or resync control plus close after admission; v1 invalid/foreign cursor stays 400; done requires both backlogs drained, the existing greater-than-60s terminal stability test and the final same-connection change fence |
| `/api/sessions/{id}/signals` | Live mode retains both the same-detail snapshot ticket and last-applied `signal_seq`; explicit bounded history pages fold through the detail cut; legacy starts from its supplied sequence or 0 | Live mode shares the message tail resource, bounded drains, poll 0.5s only when caught up and heartbeat after 5s idle; legacy retains per-subscriber 500-row polling | Preflight 404; missing/foreign/expired live ticket or unavailable frontier returns 409 `stream_resync_required` with a typed reason, or resync control plus close after admission; done uses the shared drained-backlog, terminal-stability and change-fence decision |

Replace the base paragraph claiming a 500-row message cap: the legacy message path uses `MAX_HYDRATED_ROWS` (currently 50,000) plus one lookahead row and separate hydration/content/scan budgets; signals default to 500 rows. New live drains use their smaller named batch limit within those existing ceilings. None of these exact limits was selected by a recorded benchmark.

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
| 2 | Define a shared SSE frame envelope, error and terminal frames, a stable replay cursor (composite where timestamps can collide), bounded batch semantics, and reconnect rules for session, signal, show, and Leo streams while preserving endpoint-specific payloads; acceptance covers same-timestamp inserts, reconnect after a partial batch, explicit truncation, prompt cancellation cleanup, and shared web/VS Code behavior fixtures. | M | (filled at issue-open time) |
| 3 | Decide whether filesystem show import is a browser-admin mutation or a CLI maintenance operation, then enforce one policy with matching authorization and confirmation behavior. | S | (filled at issue-open time) |
| 4 | Document and test API-only, daemon-hosted SPA, Vite development, and container/reverse-proxy modes against the same host, CORS, token, and API-base rules. | S | (filled at issue-open time) |
| 5 | Separate stream-subscription lifetime from per-poll database setup and bound per-viewer work; acceptance: idle viewers do not open a new database connection or repeat schema discovery every 500 ms, terminal checks use bounded/shared reads, disconnect closes promptly, and a repeatable benchmark records query and connection growth as viewer count rises. | M | (filled at issue-open time) |
| 6 | Put a measured bound and observable phase/progress on startup reconciliation while preserving D3's correctness gate; acceptance: a seeded large database reaches ready within the agreed budget, only non-authoritative maintenance is deferred, and no stale execution row is exposed as healthy before reconciliation completes. | M | (filled at issue-open time) |

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
