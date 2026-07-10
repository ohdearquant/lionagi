# ADR-0081: Studio execution and artifact workspace target

- **Status**: Proposed
- **Kind**: Aspirational
- **Area**: studio
- **Date**: 2026-07-09
- **Relations**: supersedes v0-0012, v0-0015, v0-0020, v0-0021, v0-0022, v0-0031, v0-0055, v0-0066; extends ADR-0077 and ADR-0080

## Context

Studio already exposes sessions, runs, invocations, shows, plays, branches, messages,
session signals, tool calls, and artifacts. The web client contains partial run,
invocation, operation-graph, show-DAG, and outcome renderers. These are useful records and
components, but they do not form one execution workspace or one evidence contract.

This aspirational ADR answers seven concrete problems.

**P1 — One execution appears under several backend identities.** A schedule firing may
join a schedule run to an invocation and one or more sessions; a show has plays and optional
sessions; a run API is a session projection. Exposing each record as a peer product noun
forces the operator to perform the joins manually (`runs.py`; `invocations.py`; `shows.py`).

**P2 — Live and historical screens can disagree about status.** Mission Control, Fleet,
History, schedule views, and editor views can independently interpret `running`, process
liveness, terminal reasons, and timestamps. One dead process can therefore appear healthy
in one surface and stale in another (`runs.py`; `state/health.py`; frontend run-status code).

**P3 — Branch messages are not automatically operation-scoped.** A branch may execute
more than one operation, while current run detail hydrates a tail window per branch.
Presenting the full branch transcript under one operation invents provenance unless
persisted message/event boundaries support the claim (`sessions.py`; `runs.py`).

**P4 — Tool audit messages are stored separately but read as a pair.** ActionRequest and
ActionResponse are different persisted messages. Current adaptation pairs them when the
request's `action_response_id` resolves, yet orphan requests and responses are diagnostic
evidence and must not disappear (`runs.py`; StateDB message schema).

**P5 — Artifacts mix queryable outcomes and optional host paths.** `artifacts.content` is
JSON while `file_path` may name a larger blob. Returning arbitrary absolute paths or reading
them directly from a renderer would bypass auth, containment, size, media-type, and hash
checks (`state/schema.sql`; `invocations.py`).

**P6 — Large histories need explicit window and partial-data semantics.** Session detail
defaults to the newest 200 messages, caps a page at 1,000, and returns a versioned cursor.
Signal streams replay from sequence zero in batches of 500. A unified workspace cannot
silently treat either window as the whole execution (`sessions.py`; `signals.py`).

**P7 — Detail renderers must not become control-plane backdoors.** Cancel, re-run, or other
mutations may be useful, but a renderer that writes StateDB or opens files directly would
bypass ADR-0078's command and authorization boundaries.

| Concern | Decision |
|---|---|
| Canonical identity | D1: Normalize public executions into a closed `ExecutionRef` and `ExecutionRecord`, keeping invocation internal. |
| Source adaptation and status | D2: Use typed adapters over current run/session/show/schedule APIs and one status oracle. |
| Selection and layout | D3: Share one URL-addressable workspace state between History and Fleet. |
| Evidence scope | D4: Project graph, timeline, messages, and signals with explicit operation/branch/partial scopes. |
| Tool-call presentation | D5: Pair correlated request/response messages while retaining orphans. |
| Artifact access | D6: Resolve content through authenticated identifier-based metadata and bounded verified preview contracts. |
| Scale and commands | D7: Window large data, cancel stale reads, and route every mutation through typed application commands. |

Out of scope:

- Creating a new universal `executions` database table before the adapter contract proves
  that one is required.
- Replacing StateDB, the session signal log, or existing domain-specific daemon endpoints.
- Claiming historical operation/message provenance the runtime did not persist.
- Selecting a graph-first layout for every run; graph is one evidence projection.
- Defining command authorization or confirmation generally; ADR-0078 and ADR-0083 own
  those protocols.

## Decision

### D1 — Closed public identity and canonical execution record

The public identity remains the skeleton's discriminated union:

```typescript
export type ExecutionRef =
  | { kind: "session"; id: string }
  | { kind: "run"; id: string }
  | { kind: "show"; id: string }
  | { kind: "schedule-run"; id: string };

export interface ExecutionSelection {
  ref: ExecutionRef;
  operationId?: string;
  artifactId?: string;
}
```

`invocation` is deliberately absent. Its id may be retained as an internal join reference,
but no public `ExecutionRef` variant, tab, filter, or label exposes it.

The normalized record is:

```typescript
export type ExecutionStatus =
  | "queued"
  | "waiting"
  | "running"
  | "completed"
  | "completed_empty"
  | "failed"
  | "timed_out"
  | "cancelled"
  | "aborted"
  | "stale"
  | "unknown";

export interface ExecutionRecord {
  ref: ExecutionRef;
  title: string;
  source: "agent" | "playbook" | "flow" | "show" | "schedule" | "engine" | "unknown";
  status: ExecutionStatus;
  rawStatus: string | null;
  effectiveHealth: string | null;
  project: string | null;
  startedAt: number | null;
  endedAt: number | null;
  updatedAt: number | null;
  reason: {
    code: string | null;
    summary: string | null;
    evidenceRefs: Array<Record<string, unknown>>;
  } | null;
  lineage: {
    parent?: ExecutionRef;
    children: ExecutionRef[];
    internalInvocationId?: string;
    sessionIds: string[];
    scheduleId?: string;
    showTopic?: string;
  };
  capabilities: {
    liveMessages: boolean;
    liveSignals: boolean;
    artifacts: boolean;
    canCancel: boolean;
    canRetry: boolean;
  };
}
```

Exact semantics:

- `ref.kind` identifies which adapter can refresh the record. It is not inferred from the
  display label.
- `run` and `session` may currently resolve to the same StateDB session id. They remain
  separate variants during compatibility migration so old URLs can retain their meaning.
- `internalInvocationId` is never formatted as a product kind; it is used to fetch child
  sessions, artifacts, and schedule failure detail.
- Missing optional lineage does not fabricate parents. It produces empty arrays/undefined
  fields and a visible partial-lineage state in the view.
- Unknown raw statuses map to `unknown`, preserve `rawStatus`, and remain visible.
- The adapter chooses stable ids from persisted records only. Array indexes, titles, and
  timestamps are not identities.

Why this way: one UI record removes storage nouns from navigation without forcing an early
database migration. The closed union also makes unsupported kinds a compile-time and
runtime error instead of an arbitrary string.

### D2 — Typed source adapters and one status oracle

Adapters consume current daemon contracts rather than query StateDB from the browser:

```typescript
export interface ExecutionAdapter<TRef extends ExecutionRef = ExecutionRef> {
  readonly kind: TRef["kind"];
  get(ref: TRef, signal: AbortSignal): Promise<ExecutionRecord | null>;
  evidence(ref: TRef, request: EvidenceRequest, signal: AbortSignal): Promise<EvidencePage>;
}

export interface ExecutionAdapterRegistry {
  session: ExecutionAdapter<{ kind: "session"; id: string }>;
  run: ExecutionAdapter<{ kind: "run"; id: string }>;
  show: ExecutionAdapter<{ kind: "show"; id: string }>;
  "schedule-run": ExecutionAdapter<{ kind: "schedule-run"; id: string }>;
}
```

The current source shapes that adapters must tolerate are explicitly different:

- `/api/runs/` returns a page of session-derived rows including `run_id`, lifecycle,
  effective health, provenance, counts, project, reasons, artifact contracts, and tags.
- `/api/runs/{id}` is a superset with branches, tail-windowed messages, graph, steps,
  cursor metadata, and evidence refs.
- `/api/invocations/{id}` joins child session summaries, structured artifacts, and the
  schedule-run exit/error fields.
- `/api/shows/{topic}` joins the show row, plays, optional session links, and authored
  filesystem content.
- Schedule-run rows carry schedule id, invocation id, trigger context, action, status,
  chain parent/depth, timestamps, exit/error, and queue/lease fields.

The status oracle is a pure function:

```typescript
export interface StatusInput {
  rawStatus: string | null;
  effectiveHealth: string | null;
  processAlive: boolean | null;
  endedAt: number | null;
  scheduleEnabled?: boolean;
  scheduleRemainingRuns?: number | null;
}

export interface StatusVerdict {
  status: ExecutionStatus;
  tone: "neutral" | "active" | "success" | "warning" | "error";
  terminal: boolean;
  reason: string | null;
}

export function deriveExecutionStatus(input: StatusInput): StatusVerdict;
```

Exact semantics:

- A running row with confirmed dead process/liveness becomes `stale`; raw `running` is kept
  for diagnostics.
- Confirmed terminal failure/timed-out/cancelled/aborted states remain terminal even when
  liveness is unknown.
- `completed_empty` is distinct from successful `completed` because it carries missing-
  evidence meaning.
- A spent bounded schedule cannot project as active merely because `enabled` was not yet
  refreshed; remaining-run state participates when available.
- `processAlive=null` is unknown, not false. The oracle must not invent stale status from an
  unavailable probe.
- All Mission Control, Fleet, History, detail header, filters, and status counts consume the
  same verdict object.
- Source fetch failure produces an error/partial state; it never becomes an empty successful
  record set.

### D3 — One URL-addressable workspace shared by History and Fleet

The workspace state contract is:

```typescript
export type EvidencePane =
  | "overview"
  | "graph"
  | "timeline"
  | "messages"
  | "tools"
  | "artifacts"
  | "raw";

export interface ExecutionWorkspaceState {
  project?: string;
  statuses: ExecutionStatus[];
  sources: ExecutionRecord["source"][];
  query?: string;
  liveOnly: boolean;
  selection?: ExecutionSelection;
  pane: EvidencePane;
  messageCursor?: string;
  signalAfterSeq?: number;
}
```

Route adapters serialize this state into typed TanStack Router search parameters. History
owns the full record; Fleet applies `liveOnly=true` and an appropriate status subset but
does not define a second selection or detail model.

Exact semantics:

- Selecting an execution changes the URL and opens the master-detail pane without unloading
  the list.
- Selecting an operation or artifact extends the same selection. It never navigates to an
  independent page whose filters cannot be reconstructed.
- Invalid kind/id/pane values are rejected or normalized by the route validator.
- Back/forward restores selection, pane, project, and filters.
- A project change refreshes list and detail. An out-of-scope selection is cleared
  explicitly, as required by ADR-0080.
- Fleet and History may choose different default panes, but a shared deep link resolves to
  the same execution and evidence.
- Legacy run, show, and invocation URLs translate into this state. Invocation resolution
  selects its primary/first child session when one exists and surfaces a partial state when
  it has no child.

### D4 — Evidence projections carry explicit scope and completeness

The evidence page is discriminated:

```typescript
export type EvidenceScope =
  | { kind: "execution" }
  | { kind: "branch"; branchId: string }
  | { kind: "operation"; operationId: string; basis: "persisted" | "timestamp-fallback" };

export interface EvidencePage {
  scope: EvidenceScope;
  completeness: "complete" | "windowed" | "partial" | "unavailable";
  items: EvidenceItem[];
  nextCursor?: string;
  warnings: string[];
}

export type EvidenceItem =
  | { kind: "message"; message: MessageEvidence }
  | { kind: "tool-call"; call: ToolCallEvidence }
  | { kind: "signal"; signal: SignalEvidence }
  | { kind: "status"; transition: StatusEvidence }
  | { kind: "artifact"; artifact: ArtifactSummary };
```

Exact semantics:

- Graph is a projection of persisted session metadata and session signals. It is not a
  separate source of truth.
- Signal operation status follows the ordered lifecycle mapping already used by
  `operationGraph.ts`: queued, running, awaiting approval, succeeded, failed, escalated.
  Unknown signal kinds remain available in raw/timeline evidence but do not change the
  operation lane.
- Multiple `depends_on` edges are preserved. Layout uses all known predecessors and guards
  cycles rather than claiming the underlying execution graph is cyclic.
- Operation-scoped messages require persisted operation/message association or explicit
  start/end event boundaries.
- When only timestamps are available, the pane labels the basis
  `timestamp-fallback`, states the window, and warns that concurrent operations can overlap.
- When neither association nor a safe interval exists, messages remain branch-scoped. The
  UI does not relabel them as operation-scoped.
- A session message response advertises `windowed` when a next cursor or a server limit
  proves the view is incomplete. It shows full counts from `message_stats`, not the page
  length.
- An empty complete page renders “no evidence recorded.” An unavailable source renders an
  error/partial diagnostic. These states are not interchangeable.

The current message defaults—200 newest messages and maximum 1,000 per request—remain
server compatibility values. The workspace consumes them; it does not claim those numbers
are a complete transcript.

### D5 — Correlated tool messages render as one unit without losing audit rows

The target presentation model is:

```typescript
export interface ToolCallEvidence {
  requestId: string;
  responseId: string | null;
  operationId: string | null;
  function: string;
  arguments: Record<string, unknown>;
  output: unknown;
  status: "pending" | "ok" | "error" | "orphan-request" | "orphan-response";
  exitCode: number | null;
  requestedAt: number | null;
  respondedAt: number | null;
  rawMessageIds: string[];
}
```

Exact semantics:

- An ActionRequest with an `action_response_id` matching an ActionResponse becomes one
  rendered call and retains both persisted ids in `rawMessageIds`.
- A request with no response remains visible as `pending` while execution is live and
  `orphan-request` after the evidence source is terminal/complete.
- An ActionResponse not referenced by a visible request remains `orphan-response`; it is not
  dropped merely because the normal pairing direction is absent.
- Tool status uses structured response/error/exit fields when available. Text keyword
  inference is a labeled compatibility fallback, not authoritative failure classification.
- Repeated function names do not correlate calls. Only persisted ids do.
- Arguments and output are escaped/bounded in the default view; raw evidence remains
  inspectable under an explicit raw pane.

This keeps a readable unit while preserving the audit fact that request and response are
separate messages.

### D6 — Authenticated identifier-based artifact resolution

The browser never receives an arbitrary readable host path as a content URL. It first uses
artifact metadata, then an authenticated identifier endpoint implemented through ADR-0078:

```python
class ArtifactContentQuery(ContractModel):
    artifact_id: str
    mode: Literal["metadata", "preview", "download"] = "metadata"
    max_bytes: int = Field(default=262_144, ge=1, le=1_048_576)

class ArtifactContentResult(ContractModel):
    artifact_id: str
    kind: str
    name: str
    display_path: str | None
    media_type: str | None
    sha256: str | None
    size_bytes: int | None
    content_json: dict[str, object]
    preview_base64: str | None
    preview_truncated: bool
    verified: bool
```

Exact semantics:

- Metadata lookup happens in StateDB by `artifact_id`. Missing metadata is 404.
- Structured `content` is returned independently of file availability.
- File-backed content is addressable only through the artifact id. The service resolves a
  stored relative path beneath an approved root and rejects absolute paths, traversal,
  containment escape, and unsafe symlink resolution.
- Preview reads no more than `max_bytes`, reports truncation, and permits only a reviewed
  safe media-type set. Unknown/binary types remain download-only or unavailable.
- When an expected SHA-256 exists, mismatch fails with conflict/integrity error; unverified
  bytes are not displayed as trusted output.
- Authentication and application authorization apply to metadata, preview, and download.
  A static SPA path is never used for artifact bytes.
- Missing file with present metadata yields a visible unavailable-content result; it does
  not erase the artifact record.
- Absolute host paths are never serialized to clients. `display_path` is relative and
  informational.

The 256 KiB default and 1 MiB preview cap match ADR-0078's target. They protect browser and
daemon memory, but are admitted conservative defaults rather than measured optima.

### D7 — Windowing, cancellation, partial state, and command separation

Large execution views obey these budgets:

```text
message page: server default 200, hard maximum 1000
signal replay read: server batch 500
artifact preview: default 256 KiB, hard maximum 1 MiB
list/history rendering: windowed or virtualized; no unbounded all-history DOM
```

Exact semantics:

- Every selection/filter change aborts obsolete fetches and streams with `AbortController`.
  Results from an old selection cannot overwrite the active detail.
- “Load older” follows the opaque server cursor. The client never manufactures cursor
  anchors or changes message limit mid-cursor.
- Stream EOF before an explicit terminal frame becomes a recoverable transport error, not a
  silently frozen “live” state.
- Partial failures are per projection: artifact failure need not hide messages, and signal
  failure need not erase persisted run metadata. The header shows degraded/partial status.
- Macro graphs and long timelines render incrementally or virtually; empty sources avoid
  allocating graph layouts.
- A renderer receives data and raises typed intents only. It does not import StateDB, read a
  host path, or call mutation endpoints directly.
- Cancel/retry/other controls call ADR-0078 application commands. They show the target,
  current status, risk, and explicit confirmation when required, then refresh the record.
- A stale target or optimistic conflict is shown as conflict and forces refresh; the UI does
  not repeat the mutation against old state automatically.

## Consequences

- Operators get one causal workspace from execution identity through graph, messages, tool
  calls, status reasons, and work products.
- History and Fleet can share adapters without forcing heterogeneous records into a new
  universal table.
- Provenance claims become honest: operation-scoped evidence names its persisted or fallback
  basis, and branch-scoped history stays branch-scoped when boundaries are absent.
- Artifact content becomes auditable and bounded. Some existing absolute-path records will
  be metadata-only until migrated to approved relative references and hashes.
- Adapters and completeness states add code, but they localize heterogeneity that is
  currently spread through routes and components.
- Reversing D1 after URLs and saved links depend on `ExecutionRef` is costly; adding an
  adapter kind requires an explicit union and compatibility update.
- The workspace cannot manufacture old boundaries. Some historical executions will remain
  partial forever, which is preferable to false precision.

## Alternatives considered

### Preserve separate detail pages for every backend noun

This would minimize adapters and keep each service response close to its table. It lost
because status, selection, lineage, and artifact behavior would remain duplicated, and one
schedule execution would still require cross-page correlation.

### Create an `executions` table first

A universal row could normalize identity and make History queries straightforward. It lost
because the UI contract and joins are not stable enough to justify a migration, and a table
does not by itself solve evidence scope, tool pairing, or artifact safety. Adapters let the
model prove itself first.

### Make every view graph-first

Graphs are excellent for multi-operation DAGs and causal fan-out. They lost as the primary
layout because single-agent transcripts, artifact-heavy investigations, and linear schedule
runs are better served by master-detail and timeline evidence. Graph remains a projection.

### Treat timestamps as authoritative operation boundaries

This would let historical branch messages appear under graph nodes without runtime changes.
It lost because concurrent operations overlap and clock/order gaps make the attribution
ambiguous. A labeled fallback is permitted; silent authoritative attribution is not.

### Collapse ActionRequest and ActionResponse in storage

One persisted tool-call record would simplify rendering. It lost because the messages are
separate audit events with independent timestamps and orphan diagnostics. Presentation pairs
them while preserving both ids.

### Return artifact file paths and let the browser fetch them

This is simple and matches existing `file_path` metadata. It lost because paths expose host
layout and bypass daemon authentication, containment, size, media-type, and hash checks.
Identifier-based resolution keeps the daemon in the trust path.

### Add a central event bus for the workspace

A unified bus could feed graph, timeline, status, and artifacts live. It lost because
persisted StateDB rows and endpoint streams already provide bounded sources; a bus would add
ordering, retention, and replay decisions without fixing historical data. The workspace
normalizes at the adapter layer instead.
