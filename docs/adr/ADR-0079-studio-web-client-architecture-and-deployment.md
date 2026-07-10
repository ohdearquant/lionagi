# ADR-0079: Studio web client architecture and deployment

- **Status**: Proposed
- **Kind**: Retrospective
- **Area**: studio
- **Date**: 2026-07-09
- **Relations**: supersedes v0-0001, v0-0002, v0-0005, v0-0010, v0-0013, v0-0018, v0-0034, v0-0035

## Context

The checked-in Studio web client lives under `apps/studio/frontend/`. It is a
client-rendered Vite application that talks to the Python Studio daemon. Earlier records
described a Next.js application, centralized server-state/cache libraries, and a
shadcn/Radix component system. Those packages and runtime assumptions are not present in
the shipped source.

This ADR answers six current-code problems.

**P1 — Contributors need the real build and runtime contract.** Assuming SSR, a Node
production server, or frontend API routes leads to deployment and debugging instructions
that cannot work against the Vite bundle (`package.json`; `vite.config.mts`; `main.tsx`).

**P2 — The same bundle runs with several API origins.** It may be served by the daemon,
the Vite server, a static hosted origin that talks to loopback, or a reverse proxy. A wrong
default can return the SPA's HTML for `/api/*` with status 200 or create an absolute
cross-origin trailing-slash redirect (`api.ts`; `vite.config.mts`; `studio/cli.py`).

**P3 — Browser auth must cover streams as well as ordinary fetches.** Desktop/runtime
injection can provide a bearer token, but native `EventSource` cannot attach it. JSON and
SSE need one token source and compatible error behavior (`src/lib/api.ts`).

**P4 — Component and state ownership must match installed code.** Generic primitives are
source-owned under `components/ui`; route and feature components own fetching and local
reducers. Naming uninstalled libraries as architectural commitments would direct new code
toward dependencies the application does not have (`package.json`; `components/**`).

**P5 — The route tree is in migration.** The shell exposes a cockpit subset, while entity
route files and shared redirect adapters preserve old links. If retired routes regain
feature logic, Studio will have two information architectures and two state owners
(`routes/**`; `lib/retiredRoutes.ts`).

**P6 — Public vocabulary differs from some implementation symbols.** APIs and product
labels use “playbook,” while graph editor types retain `Worker*` names. Internal type names
must not silently redefine user vocabulary (`api.ts`; `types.ts`; Library components).

| Concern | Decision |
|---|---|
| Stack and runtime | D1: Keep a React 19/TypeScript/Vite SPA with TanStack Router and no production Node requirement. |
| API origin and delivery | D2: Resolve API base explicitly across same-origin, development, hosted-static, and runtime-injected modes. |
| HTTP and SSE client | D3: Use one fetch-based bearer-aware client and fetch-based SSE parsing. |
| Components and state | D4: Keep source-owned UI primitives and feature-local data ownership until a measured cache decision. |
| Navigation compatibility | D5: Let TanStack Router own URL state and keep retired routes as redirect adapters only. |
| Product vocabulary | D6: Keep “playbook” public while treating legacy `Worker*` names as internal migration residue. |

Out of scope:

- The canonical six-space cockpit taxonomy is decided by ADR-0080.
- The shared execution workspace target is ADR-0081.
- The VS Code client is recorded separately in ADR-0082.
- This ADR does not select a future server-state cache, headless component library, SSR
  framework, or design-system migration.
- Daemon host, CORS, token, and SPA fallback enforcement are recorded in ADR-0076.

## Decision

### D1 — Client-rendered Vite SPA and checked-in dependency set

The build contract in `apps/studio/frontend/package.json` is (abridged — devDependencies and
tooling metadata omitted):

```json
{
  "private": true,
  "type": "module",
  "scripts": {
    "dev": "vite",
    "build": "tsc --noEmit && vite build",
    "start": "vite preview",
    "test": "vitest run",
    "typecheck": "tsc --noEmit",
    "e2e": "npm run build && playwright test"
  },
  "dependencies": {
    "@tanstack/react-router": "^1.170.15",
    "dagre": "^0.8.5",
    "react": "^19.2.6",
    "react-dom": "^19.2.6",
    "react-markdown": "^10.1.0",
    "reactflow": "^11.11.4",
    "remark-gfm": "^4.0.1",
    "smol-toml": "^1.7.0",
    "use-intl": "^4.13.0",
    "yaml": "^2.9.0"
  },
  "devDependencies": {
    "tailwindcss": "^3.4.3",
    "typescript": "^5.4.5",
    "vite": "^8.0.16",
    "vitest": "^4.1.8"
  }
}
```

The contract is the dependency roles, not a promise to freeze every patch version. React
mounts one `RouterProvider` into `#root`; TanStack Router's generated route tree is created
by the Vite plugin. There is no SSR entry point, server component graph, Node request
handler, or frontend API route.

The source ownership tree is:

```text
src/
├── main.tsx                 # locale setup, preload failure guard, router mount
├── routeTree.gen.ts         # generated TanStack route tree
├── routes/                  # URL adapters and top-level page composition
├── lib/
│   ├── api.ts               # daemon JSON/SSE client
│   ├── types.ts             # client response/view types
│   ├── retiredRoutes.ts     # compatibility redirects
│   └── operationGraph.ts    # signal-to-operation projection
└── components/
    ├── ui/                  # generic source-owned primitives
    ├── shell/               # rail/topbar/footer/palette/layout
    └── <feature>/           # mission, fleet, history, library, schedules, ...
```

Exact semantics:

- `npm run build` type-checks before Vite emits `dist/`; a type failure produces no
  successful build.
- The router is entirely client-side. Deep-link serving therefore depends on ADR-0076's
  SPA fallback or an equivalent static-host rewrite.
- `vite:preloadError` triggers at most one guarded reload; a repeated broken chunk is
  allowed to surface rather than forming a reload loop (`preloadReload.ts`).
- Locale direction and language are applied before first React paint; the root component
  validates locale choices against the `LOCALES` registry.
- The production artifact is static HTML/JS/CSS. A Node runtime is required to build or run
  Vite preview, not to serve the built application in daemon-hosted production.

### D2 — Ordered API-base resolution and supported delivery shapes

The browser-facing runtime injection contract is:

```typescript
declare global {
  interface Window {
    __STUDIO_API_BASE__?: string;
    __STUDIO_AUTH_TOKEN__?: string;
  }
}

export function resolveApiBase(): string {
  // 1. non-empty window.__STUDIO_API_BASE__
  // 2. non-empty import.meta.env.VITE_STUDIO_API_BASE
  // 3. ports 3000/5173 -> same protocol/hostname, port 8765
  // 4. any other browser origin -> "" (same origin)
  // 5. no window (tests/SSR compatibility) -> http://localhost:8765
}
```

`API_BASE` is computed once at module evaluation. Empty configured strings are treated as
unset. The supported delivery matrix is:

| Shape | Frontend origin | API base rule | Process shape |
|---|---|---|---|
| API-only daemon | no bundled UI | client supplied externally | one Python process |
| Daemon-hosted `dist/` | daemon origin | `""` same-origin | one Python process after build |
| Vite development | port 3000 or 5173 | browser fallback to daemon port 8765; Vite also proxies `/api` | Vite + Python |
| Static hosted UI | hosted origin | build injects `window.__STUDIO_API_BASE__`, default `http://127.0.0.1:8765` on hosted builds | static host + local Python |
| Reverse proxy/container | proxy origin | `""` same-origin | topology chosen by deployer |

Vite preview proxies `/api` to the configured daemon so production-build tests exercise a
single browser origin. `STUDIO_API_URL` overrides the proxy target; otherwise it uses
`127.0.0.1` and the e2e/default port. The hosted build injector runs only when its hosted
build marker is present, so ordinary `vite build` remains same-origin.

Exact error semantics in `fetchJson()`:

- Network/CORS/DNS failure reports a connectivity failure and rethrows.
- Non-2xx parses a string `detail` when possible and throws it; otherwise it reports the
  HTTP status.
- HTTP 204 returns `undefined` without JSON parsing.
- A non-JSON empty body returns `undefined`.
- A non-JSON HTML document produces an explicit “API base likely unconfigured” error,
  covering static hosts that rewrite unknown `/api/*` requests to `index.html` with 200.
- Other non-JSON text is parsed as JSON and may throw normally.

List paths whose daemon route is slash-terminated include the slash in the client. This
avoids an absolute redirect whose target can be cross-origin when UI and daemon differ.

### D3 — One bearer-aware fetch layer for JSON and SSE

Token resolution is runtime-only:

```typescript
export function resolveAuthToken(): string | undefined {
  return window.__STUDIO_AUTH_TOKEN__ || undefined;
}

async function fetchJson<T>(path: string, init?: RequestInit): Promise<T>;

function sseSubscribe(
  path: string,
  onData: (data: string) => void,
): () => void;
```

`fetchJson()` attaches `Authorization: Bearer <token>` when the token exists and otherwise
preserves caller headers. Mutation helpers explicitly set `Content-Type: application/json`
and stringify their bodies, matching ADR-0076's body guard.

The SSE helper uses `fetch` plus `ReadableStream`, not native `EventSource`. Exact semantics:

- It sends `Accept: text/event-stream` and the same optional bearer header as JSON calls.
- It parses unnamed frames separated by `\n\n`, joins multiple `data:` lines, and passes
  the payload string to the endpoint-specific consumer.
- The returned closer sets a closed flag and aborts the request.
- EOF or network error reconnects after 2,000 ms unless closed.
- The endpoint consumer is responsible for JSON parsing and closing on its `done` frame.
- Parser errors thrown by the consumer are not converted into a durable cursor or replay.
- There is no `Last-Event-ID`; reconnect starts according to the endpoint's server contract.

The two-second reconnect delay is a shipped inherited value. Its qualitative purpose is to
avoid a tight retry loop while keeping local recovery responsive; no recorded measurement
selects exactly two seconds.

### D4 — Source-owned primitives and feature-local data state

The generic component boundary is `src/components/ui`. It contains buttons, badges,
fields, modal/split-pane/stacked-list primitives, status and timestamp renderers, toast,
Markdown, skeleton, and related components. `components/shell` owns application chrome.
Feature directories compose those primitives and own domain-specific loading, selection,
reducers, and error states.

The dependency manifest does not include shadcn, Radix, TanStack Query, or Zustand. This
absence is load-bearing: those libraries are not available by architectural implication.

Exact ownership semantics:

- Generic role/name/state/keyboard behavior belongs in `components/ui`; feature-specific
  API calls and domain labels do not.
- Routes own validated search-parameter contracts and page-level composition.
- Operational features currently call functions from `lib/api.ts` and use React state,
  effects, reducers, or `Promise.allSettled`; there is no central server-state cache.
- A feature may keep a specialized reducer, as Mission Control and Fleet do. It must not
  claim cross-feature cache invalidation unless it implements it.
- Shared interactive primitives require explicit accessibility behavior because no
  installed headless component library supplies it.

This decision records the current system. It does not reject a future cache or headless
library; it requires an evidence-based ADR against this baseline.

### D5 — TanStack Router owns navigation state; retired routes do not

Routes are file-based and generate `routeTree.gen.ts`. Search validators reduce arbitrary
input into typed route state. For example, Fleet preserves primitive legacy filters while
normalizing the selected session `s`.

The redirect target contract is:

```typescript
export type RetiredRedirectTo =
  | "/fleet"
  | "/library"
  | "/schedules"
  | "/system";

export interface RetiredRedirectTarget<TTo extends RetiredRedirectTo> {
  to: TTo;
  search: Record<string, string | number | boolean | Array<string | number | boolean>>;
}
```

Exact semantics:

- Retired search preservation keeps non-empty strings, numbers, booleans, and non-empty
  arrays of those primitives; it drops objects, functions, empty strings, and nullish data.
- Explicit redirect overrides win over preserved search.
- Retired invocation detail fetches the invocation, accepts `?s=` only when it names one of
  its child sessions, otherwise selects the first child, and targets Fleet. Zero-child
  invocations preserve their invocation id.
- A failed invocation fetch rejects to the route error component; it is not silently
  converted into a generic fallback.
- Re-clicking an active rail space dispatches `studio:toggle-pane` instead of navigating.
- The current rail exposes Mission Control, Library, Schedules, and System; Fleet is a
  separate route shown as a Mission Control tab and command. ADR-0080 records the accepted
  six-space baseline and treats this as implementation drift.
- Retired route files may parse and redirect. They must not become a second implementation
  owner after target parity exists.

### D6 — Public “playbook” terminology survives legacy graph names

Daemon paths, Library labels, creation/editing helpers, and filters use “playbook.” The
client still contains `WorkerGraph`, `WorkerStepNode`, `WorkerLinkEdge`, `WorkerFormData`,
and helpers such as `getWorkerGraph()`.

Exact semantics:

- Public labels and new API fields use `playbook`.
- Existing `Worker*` symbols describe an internal graph/editor shape; they are not a public
  noun or permission to add “worker” filters and routes.
- Playbook payload detection treats non-empty `steps` or `links` as graph format; otherwise
  it treats the definition as declarative.
- Plugin and skill discovery remain Library capabilities backed by daemon GET endpoints.
- Renaming legacy types is a mechanical cleanup only if serialized field names and public
  paths remain unchanged.

## Consequences

- The production client is a static bundle and can be served by Python or a static/reverse-
  proxy topology without a Node application server.
- API-base resolution supports local development and hosted-local operation, but a wrong
  injection is a deployment failure visible to every request.
- JSON and streams share token injection. Endpoint-specific SSE reconnect semantics remain
  a client responsibility until ADR-0076's envelope delta is implemented.
- The small runtime dependency set keeps source code inspectable, while source-owned
  components require accessibility and keyboard tests the team cannot outsource to a
  headless library.
- Without a central server-state cache, coordinated invalidation is local to each feature.
  Reversing D4 requires measuring that pain and migrating feature consumers deliberately.
- Redirects preserve old links while the cockpit consolidates. Their maintenance cost is
  acceptable only while they stay adapters rather than duplicate pages.

## Current-vs-ideal delta

| # | Delta | Size | Issue |
|---|---|---|---|
| 1 | Publish a current component ownership catalog for `components/ui`, shell components, and feature components, with role/name/state/keyboard accessibility tests for every shared interactive primitive. | M | (filled at issue-open time) |
| 2 | Add shared daemon-contract tests for the web and VS Code clients covering response shapes, auth, trailing-slash redirects, and SSE parsing/reconnection. | M | (filled at issue-open time) |
| 3 | Reduce retired route files to redirect-only adapters after Fleet, History, Library, Schedules, and System have parity; remove duplicated entity-page behavior. | L | (filled at issue-open time) |
| 4 | Define cache invalidation and freshness behavior for operational screens before adding a server-state library; acceptance must show one status change propagating consistently to every visible consumer. | M | (filled at issue-open time) |

## Alternatives considered

### Next.js with SSR and frontend API routes

SSR could improve first render and colocate a server-side API facade. It lost because the
product reads a local daemon, the current bundle is fully client-rendered, and production
can be served as static files by the Python process. Reintroducing Next.js would add a
runtime and deployment boundary without a demonstrated server-rendering requirement.

### Browser talks only to a same-origin proxy

This would remove CORS and mixed-origin redirect concerns. It lost as a universal rule
because the hosted-static product intentionally talks from a hosted origin to the
operator's loopback daemon, and API-only clients have no frontend proxy. Same-origin remains
the ordinary bundled/reverse-proxy default.

### Native `EventSource`

It supplies automatic SSE reconnect and a mature parser. It lost because the browser API
cannot attach the runtime bearer header. Fetch streams preserve auth parity with ordinary
requests, at the cost of maintaining reconnect and framing code.

### TanStack Query plus Zustand immediately

A query cache could centralize freshness, deduplicate reads, and simplify mutation
invalidation; a client store could coordinate shell state. They lost as current decisions
because neither is installed and no cache/freshness contract has been measured. Adding them
before defining invalidation would relocate ambiguity into library calls.

### shadcn/Radix component ownership

Headless primitives could improve accessibility baselines and reduce custom interaction
code. They lost as a retrospective claim because the dependency and directory model are
absent. Re-adoption requires comparing migration cost and accessibility evidence against
the source-owned baseline.

### Preserve every entity page indefinitely

Independent pages maximize directness and reduce consolidation work. They lost because
route-per-storage-noun navigation is the problem ADR-0080 resolves. Permanent URLs survive
as redirects; permanent duplicate implementation owners do not.
