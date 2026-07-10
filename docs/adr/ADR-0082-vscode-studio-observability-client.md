# ADR-0082: VS Code Studio observability client

- **Status**: Proposed
- **Kind**: Retrospective
- **Area**: studio
- **Date**: 2026-07-09
- **Relations**: supersedes v0-0084; extends ADR-0076

## Context

The VS Code extension under `apps/vscode/` is a native TypeScript client of the public
Studio daemon. It does not embed the Studio SPA and does not read StateDB. It provides an
Explorer, status bar, run detail, operation-tree views, and optional lifecycle management
for a local API-only daemon.

This ADR answers six concrete problems in the shipped extension.

**P1 — Editor observability must not create a second data model.** Direct SQLite reads
would duplicate joins, lifecycle classification, auth, and live-stream behavior already
owned by the daemon (`apps/vscode/src/api/*`; `lionagi/studio/services/runs.py`).

**P2 — “Read-only” must distinguish application state from process supervision.** The
extension starts or stops a Python child, but its `StudioClient` only issues GET requests.
Calling the extension a control client because it manages a process would erase a useful
negative capability boundary (`extension.ts`; `api/client.ts`; `backend/lifecycle.ts`).

**P3 — The extension may attach or own, and ownership changes stop semantics.** A
configured URL must be attach-only; an already healthy default daemon must not be killed;
only a child spawned by the extension may be terminated (`backend/lifecycle.ts`).

**P4 — Python availability is not guaranteed in the editor environment.** The extension
may need an explicit interpreter, a workspace `.venv`, a source-checkout `uv sync`, or a
system `python3` fallback. Failed provisioning and import preflight need actionable states.

**P5 — Authenticated live output cannot use native EventSource.** Session output and
persisted signals require bearer-aware fetch streams, terminal-frame handling, and visible
transport EOF failure (`api/sse.ts`; `api/signals.ts`).

**P6 — The extension and web client independently encode the daemon contract.** Run,
project-group, invocation, auth, trailing-slash, and stream-shape changes can break the
extension even if the daemon and web app continue to work.

| Concern | Decision |
|---|---|
| Data boundary | D1: Use only Studio HTTP/SSE for application data; never read StateDB or embed the SPA. |
| Capability boundary | D2: Keep `StudioClient` GET-only; classify backend start/stop as owned-process lifecycle. |
| Daemon lifecycle | D3: Attach when configured/healthy, otherwise provision and supervise an API-only local child. |
| Live observation | D4: Parse authenticated session and signal SSE with explicit terminal and EOF behavior. |
| Native UX | D5: Prefer Explorer, status bar, commands, and purpose-built detail webviews over a general SPA webview. |
| Compatibility | D6: Treat daemon OpenAPI and shared fixtures as the client seam. |

Out of scope:

- Launching runs, editing definitions, changing schedules, approvals, maintenance, or any
  other Studio application mutation.
- Shipping Docker, Node, a frontend build, or the web SPA as an extension dependency.
- Defining the daemon's HTTP/SSE contract; ADR-0076 and ADR-0078 own it.
- Converging the VS Code visual design with the browser cockpit.

## Decision

### D1 — Native client over Studio HTTP/SSE only

The extension module tree is:

```text
apps/vscode/src/
├── extension.ts              # composition and commands
├── config.ts                 # `den.*` settings
├── statusBar.ts
├── api/
│   ├── client.ts             # GET JSON client
│   ├── types.ts              # daemon projections
│   ├── sse.ts                # session message stream
│   └── signals.ts            # persisted signal stream
├── backend/lifecycle.ts      # attach/provision/spawn/supervise
└── runs/
    ├── runsExplorer.ts
    ├── runItem.ts
    ├── runDetailPanel.ts
    ├── runTreeModel.ts
    └── runTreePanel.ts
```

The injected dependency boundary is:

```typescript
export interface StudioDeps {
  client: StudioClient;
  backend: BackendManager;
}
```

Exact semantics:

- `StudioClient` obtains its base URL from `BackendManager` and its optional token from
  extension configuration.
- Every application record comes from `/api/*`; `/health` is used only for process
  discovery/supervision.
- No extension module imports a Python package, SQLite library, StateDB path, or browser
  Studio bundle.
- A purpose-built webview may render detail, but it receives messages from extension code;
  it is not granted a separate daemon client or arbitrary host-file access.
- When the backend is not running, the Explorer clears its cached grouping and returns no
  rows rather than displaying stale data as current.

Why this way: the daemon already owns lifecycle and read adaptation. Reusing it keeps the
editor from becoming a second persistence consumer while preserving a native VS Code UX.

### D2 — GET-only application client and negative capability

The shipped JSON client contract is:

```typescript
export class StudioApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly detail: string,
  );
}

export interface ListRunsOptions {
  page?: number;
  per_page?: number;
  status?: string;
  playbook?: string;
  project?: string;
  project_null?: boolean;
}

export class StudioClient {
  constructor(
    getBaseUrl: () => string,
    getToken: () => string | undefined,
  );

  listRuns(opts?: ListRunsOptions): Promise<RunsPage>;
  listProjectGroups(): Promise<ProjectGroupsPage>;
  getRun(runId: string): Promise<Run>;
  getInvocation(invocationId: string): Promise<InvocationDetail>;
}
```

All four public methods call the private request helper with method `GET`. The paths are:

```text
/api/runs/                     # slash retained, optional query
/api/runs/projects
/api/runs/{runId}
/api/invocations/{encodedId}
```

Exact semantics:

- Every request sends `Content-Type: application/json` and adds the bearer header when
  configured. GETs have no body.
- Non-2xx responses attempt to parse `detail`; failure falls back to `statusText`, then
  throws `StudioApiError(status, detail)`.
- JSON response parsing errors propagate; they are not converted into empty pages.
- Run ids are currently interpolated directly in `getRun`; invocation ids are encoded.
  Contract tests must cover ids accepted by the daemon so this difference cannot become an
  injection/path bug.
- The client exposes no POST, PUT, PATCH, or DELETE application method.
- `den.startBackend` and `den.stopBackend` call `BackendManager`; they do not hit `/api`
  mutation routes.

This negative capability is architectural. Adding launch, schedule, definition, approval,
or maintenance methods requires a new decision, not an opportunistic helper.

### D3 — Attach/provision/spawn lifecycle with ownership tracking

The configuration contract is:

```typescript
den.url: string = ""
den.pythonPath: string = ""
den.port: number = 8765
den.host: string = "127.0.0.1"
den.autoStart: boolean = true
den.authToken: string = ""

export type BackendState = "stopped" | "starting" | "running" | "error";
```

Interpreter resolution order is:

1. Explicit `den.pythonPath`, with import preflight.
2. Existing workspace `.venv` Python, with preflight and optional `uv sync --extra studio
   --no-dev` repair.
3. Source checkout with `pyproject.toml` and `uv`, provisioned into `.venv` using the same
   sync command.
4. System `python3`, with import preflight.

Every spawn executes `python -m lionagi.studio`. It propagates host, port, and a non-empty
token through `LIONAGI_STUDIO_*` environment variables. It does not build or mount a
frontend.

Exact discovery and ownership semantics:

- A non-empty `den.url` is attach-only. The manager probes it; unreachable configuration
  becomes `error` and does not fall back to spawning another daemon.
- With no configured URL, the default host/port is probed before spawning. A healthy daemon
  is attached as unmanaged.
- `stop()` detaches from unmanaged backends and explicitly reports they remain running.
- `stop()` terminates only the child stored as owned. It first sends the platform default
  termination and escalates to `SIGKILL` after 3 seconds if the child remains.
- An epoch counter prevents a slow in-flight start from resurrecting state after a newer
  start/stop.
- A spawn that loses an address-in-use race re-probes the target; if another daemon is
  healthy, it attaches instead of reporting failure.
- Start waits up to 30 seconds for health. It probes every 400 ms and aborts when the spawn
  fails or the start generation is superseded.
- Supervisor reconciliation runs every 8 seconds. Two consecutive misses change a running
  backend to `error`; a later healthy probe restores `running`.
- Configured/existing/start-path probes use 2.5-second attempts with two retries; the recurring
  reconciliation health probe uses 2.5 seconds with one retry; the general helper defaults to
  1.5 seconds and zero retries. Import preflight is killed after 5 seconds. Provisioning is
  killed after 180 seconds.

The time values are shipped operating constants. Their reasons are bounded startup,
avoiding port races, hysteresis against one slow health response, and bounding package
installation/import checks. The source contains no measurements selecting the exact values.

Failure semantics:

- Missing imports produce an actionable install/configuration message before a doomed
  spawn when preflight applies.
- Provision failure and spawn failure transition to `error`; output is retained in the
  extension output channel.
- An owned child dying after readiness triggers reconciliation. If the port remains healthy,
  the manager does not flap to error.
- `dispose()` clears the supervisor, invokes ownership-aware stop, and disposes emitters and
  output resources.

### D4 — Authenticated session and signal streams

The two shipped stream signatures are:

```typescript
export async function streamSession(
  baseUrl: string,
  sessionId: string,
  token: string | undefined,
  onEvent: (event: StudioEvent) => void,
  signal: AbortSignal,
): Promise<void>;

export async function streamSignals(
  baseUrl: string,
  sessionId: string,
  token: string | undefined,
  onEvent: (event: SignalStreamEvent) => void,
  signal: AbortSignal,
): Promise<void>;
```

Their public event types are:

```typescript
type StudioEvent = { type: "heartbeat" } | { type: "done" } | MessageEvent;
type SignalStreamEvent =
  | { type: "heartbeat" }
  | { type: "done" }
  | {
      id: string;
      session_id: string;
      seq: number;
      kind: string;
      op_id: string;
      ts: number;
      payload: Record<string, unknown>;
    };
```

Exact semantics:

- Both send `Accept: text/event-stream`, `Cache-Control: no-cache`, and optional bearer
  authorization.
- Session ids are URL-encoded.
- Non-2xx or a missing response body throws before frame parsing.
- Frames split on double newlines. Multiple `data:` lines are joined and parsed as JSON.
- Malformed JSON frames are ignored; other valid event objects are delivered in arrival
  order.
- Heartbeats are visible to the caller type but ignored by current detail rendering.
- Explicit `{type:"done"}` returns normally.
- Transport EOF before `done` throws a visible stream-closed error. An AbortSignal-driven
  close is handled by the caller and does not show a spurious error after retarget/dispose.
- These functions do not reconnect or resume. The signals endpoint replays persisted rows
  from seq zero on each new connection; the session message stream uses its server-local
  timestamp cursor per connection.

The missing reconnect/resume contract is a current delta, not an implied guarantee.

### D5 — Native Explorer and bounded purpose-built webviews

The Explorer groups runs under a pinned Active band and project buckets. The key budgets
are:

```typescript
const POLL_INTERVAL_MS = 4_000;
const PAGE_SIZE = 50;
const ACTIVE_LIMIT = 100;
```

Exact semantics:

- The root loads project counts and active runs concurrently with `Promise.allSettled`.
  Active-band failure does not blank the archive; project-group failure does.
- The active query requests status `running`, page 1, up to 100 rows, then sorts newest
  first. It does not page active rows, so more than 100 current sessions are not shown in
  the pinned band.
- Project groups start with 50 rows and grow one page at a time. Unassigned runs use the
  explicit `project_null` query.
- Polling runs only while the view is visible, the backend is running, and active work is
  known. Loading completion, not merely refresh request, starts polling to avoid the stale-
  empty bootstrap race.
- The four-second poll, page 50, and active cap 100 are inherited responsiveness/resource
  bounds. No recorded measurement selects the exact values.
- One run-detail webview is reused and retargeted; clicking multiple runs does not create
  unbounded panels.
- Retarget aborts the old stream before changing the selected run. The old stream's failure
  cannot post an error into the new run.
- Terminal runs fetch detail, merge it with list metadata, show structured failure reasons,
  and flatten `steps[].messages[]`. Live runs consume session SSE.
- Operation-tree views consume persisted signal SSE through extension code. Webviews do not
  receive the auth token or fetch daemon data themselves.

### D6 — Shared daemon contract is the compatibility seam

The extension's `Run`, `RunsPage`, `ProjectGroupsPage`, `InvocationDetail`, session event,
and signal event interfaces are projections of daemon payloads, not independent domain
models. Compatibility therefore follows ADR-0078 D5:

- OpenAPI and route snapshots pin method/path/shape/auth.
- Recorded SSE fixtures pin heartbeat, data, terminal, malformed frame, EOF, and auth
  behavior.
- The VS Code serializers/parsers and web parsers run against the same fixtures.
- Additive optional fields are tolerated. Removing or changing required fields, route
  slashes, auth, or terminal behavior is versioned before an extension release consumes it.
- A negative capability test inspects the public `StudioClient` surface and exercises its
  request calls to prove only GET methods are emitted.

## Consequences

- The editor gets native local observability without a second datastore or SPA hosting
  requirement.
- Attach versus own is explicit, preventing the extension from killing a daemon it did not
  spawn.
- Provisioning makes source-checkout use more accessible but adds interpreter, package
  manager, timeout, and health-race failure modes.
- GET-only scope reduces application mutation risk. Reversing D2 would be a product and
  authorization expansion, not a small API addition.
- Fetch streams support bearer auth and visible EOF failure but currently require the caller
  to reconnect manually.
- Shared contract fixtures add release work but prevent one of two clients from silently
  drifting.

## Current-vs-ideal delta

| # | Delta | Size | Issue |
|---|---|---|---|
| 1 | Add a contract test suite that runs the VS Code serializers and SSE parsers against the daemon's current OpenAPI examples and recorded stream fixtures. | M | (filled at issue-open time) |
| 2 | Add a negative capability test proving the extension client has no methods for launch, definition, schedule, approval, maintenance, or other mutation endpoints. | S | (filled at issue-open time) |
| 3 | Define retry, backoff, resume, and terminal behavior for dropped session and signal streams; a transport EOF must produce a visible recoverable state rather than a frozen view. | M | (filled at issue-open time) |
| 4 | Version or compatibility-gate daemon response and stream changes before publishing an extension release that depends on them. | S | (filled at issue-open time) |

## Alternatives considered

### Direct SQLite/StateDB reads from the extension

This would remove the daemon dependency and permit rich local queries. It lost because the
extension would need schema migrations, query joins, lifecycle health logic, filesystem
policy, and live observation. It would also bypass daemon auth and diverge from the web
read model.

### Embed the Studio SPA in a general webview

This would maximize browser/editor parity and reuse feature components. It lost because the
extension would inherit the frontend build and cockpit navigation while still requiring the
daemon. Native Explorer and focused panels integrate better with editor workflows and keep
the deployment API-only.

### Make the extension a launch/control client

Adding POST methods would let users launch or cancel work without leaving VS Code. It lost
because the current product has a clear observer boundary and no editor-specific
confirmation/authorization design. Process start/stop is sufficient for local availability.

### Always spawn a private daemon

This would simplify ownership and configuration. It lost because an existing daemon may
already hold the port and serve the browser, and a configured remote/local URL reflects user
intent. Attach-first avoids duplicate state processes and address races.

### Require Docker for lifecycle management

Docker could make dependencies reproducible and isolate the daemon. It lost because the
extension should work in a Python source checkout or installed environment, including
editor hosts where Docker is absent. The API-only process needs no frontend container.

### Native EventSource for streams

It offers automatic reconnect. It lost because the extension must attach the bearer header
and requires explicit EOF/error behavior. Fetch streams keep authentication and cancellation
under extension control.
