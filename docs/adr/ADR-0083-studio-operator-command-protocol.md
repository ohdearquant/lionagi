# ADR-0083: Studio operator-command protocol

- **Status**: Proposed
- **Kind**: Aspirational
- **Area**: studio
- **Date**: 2026-07-09
- **Relations**: supersedes v0-0067; extends ADR-0076, ADR-0078, and ADR-0080

## Context

Studio contains a Leo prototype but no persistent browser operator dock. The prototype
proves that a LionAGI `Branch` can use read tools, emit declarative UI commands, and return
mutation proposals without directly writing Studio state. It does not provide restart
survival, replay cursors, confirmation acknowledgements, durable audit, or a frontend
protocol.

This aspirational ADR answers eight concrete problems.

**P1 — In-memory conversations disappear and evict silently.** The prototype retains at
most 50 `LeoSession` objects, expires them after two idle hours, lazily evicts on access,
and loses every Branch/history on daemon restart (`lionagi/studio/services/leo.py`).

**P2 — Current frames are endpoint-local dictionaries, not a versioned protocol.** A turn
emits `ui_command`, `proposed_action`, `text`, `error`, and `done` frames but has no
conversation/request/sequence envelope and no replay cursor (`leo.py`).

**P3 — Destination strings already drift from the shell.** The prototype accepts
`mission`, `fleet`, `designer`, `library`, `schedules`, and `system`, while the checked-in
rail does not expose Designer and the accepted ADR-0080 registry includes History. Copying
route vocabularies lets the model target nonexistent or retired spaces.

**P4 — Proposed mutation is not the same as confirmed execution.** Existing mutation tools
return endpoint strings and parameters for the browser to call. There is no target refresh,
risk class, expiry, idempotency, application-command type, or durable proof that the
operator confirmed the exact command.

**P5 — Emitting a UI command does not mean the client applied it.** Navigation, selection,
form prefill, and theme changes can be rejected by route validation, stale context, or a
disconnected browser. The backend currently has no acknowledgement path.

**P6 — Long-lived auth must not leak into URLs.** The default fetch-based SSE transport can
attach the bearer header, but future browser transports must not solve auth by placing the
daemon secret in a query string.

**P7 — Failure and restart semantics must be machine-readable.** Auth, validation,
conflict, stale context, denial, rate limit, model failure, service failure, cancellation,
and replay gaps require stable codes. A string-only error cannot support safe client
recovery.

**P8 — Mutations require an audit record, including failure to record one.** Read-only
explanation may degrade when optional observability is absent, but executing a state change
without durable proposal/confirmation/result evidence breaks the safety boundary.

### Existing prototype contract (source, not the target)

The current HTTP surface is:

```python
class _MessageBody(BaseModel):
    content: str

POST /api/leo/sessions
  -> {"id": "<uuid4>"}

POST /api/leo/sessions/{session_id}/messages
  body: {"content": "..."}
  -> text/event-stream
```

One session has `id`, lazily-created `branch`, `created_at`, `last_used_at`, and an
`asyncio.Lock`. Missing/expired ids return 404; a concurrent turn returns 409. The model
error path emits `{"type":"error","detail":...}` followed by `done`. A successful turn
emits any new-turn UI/proposal outputs, then `text`, then `done`. The lock is released when
the response generator is consumed or aborted.

Read tools call run, invocation, session, playbook, schedule, and doctor services. UI tools
return:

```json
{"ui_command":{"kind":"navigate","space":"fleet","params":{"status":"failed"}}}
{"ui_command":{"kind":"prefill_schedule","space":"schedules","params":{"name":"...","cron":"...","prompt":"...","desc":"..."}}}
```

Mutation tools only propose:

```json
{
  "proposed_action": {
    "kind": "launch_playbook | create_playbook | run_maintenance",
    "params": {},
    "description": "human text",
    "endpoint": "POST /api/..."
  }
}
```

The 50-session and two-hour bounds prevent unbounded process memory; the source records no
measurement selecting those exact numbers. They are prototype behavior, not retained target
limits.

| Concern | Decision |
|---|---|
| Persistence and concurrency | D1: Persist conversations, turns, frames, proposals, effects, and cursors in StateDB with one active turn per conversation. |
| Frame protocol | D2: Use a version-1 envelope with conversation-wide monotonic sequence and typed payloads. |
| Destinations and tools | D3: Generate destinations from ADR-0080 and expose tools as typed ADR-0078 application adapters. |
| Proposal and confirmation | D4: Execute risky commands only through expiring, refreshed, idempotent, durably audited proposals. |
| Client effects | D5: Require typed validation and acknowledgement before an emitted effect is considered applied. |
| Transport and auth | D6: Use HTTP submit/cancel/ack plus authenticated fetch SSE; allow tickets only as short-lived one-use credentials. |
| Replay, errors, and context | D7: Persist frames before delivery, replay by durable cursor, bound model context, and publish stable error codes. |
| Degraded operation | D8: Fail closed when mutation audit is unavailable while allowing visibly degraded read-only explanation. |

Out of scope:

- Replacing ordinary cockpit navigation or control; the dock is optional assistance, not a
  dependency for core operation.
- Giving the model arbitrary URLs, shell strings, SQL, filesystem paths, or raw endpoint
  invocation.
- Selecting WebSocket without a demonstrated concurrent bidirectional requirement and a
  later transport decision.
- Defining the six-space IA or application commands; ADR-0080 and ADR-0078 own them.
- Persisting a LionAGI runtime Session row as the conversation. Operator conversations are a
  distinct product record with different retention and control semantics.

## Decision

### D1 — Durable StateDB records and one active turn per conversation

The target tables are owned by StateDB schema/migrations:

```sql
CREATE TABLE studio_operator_conversations (
  id                 TEXT PRIMARY KEY,
  project            TEXT,
  title              TEXT,
  status             TEXT NOT NULL DEFAULT 'active'
                     CHECK(status IN ('active', 'archived', 'deleted')),
  next_sequence      INTEGER NOT NULL DEFAULT 1,
  active_request_id  TEXT,
  created_at         REAL NOT NULL,
  updated_at         REAL NOT NULL,
  archived_at        REAL,
  deleted_at         REAL
);

CREATE TABLE studio_operator_turns (
  request_id         TEXT PRIMARY KEY,
  conversation_id   TEXT NOT NULL REFERENCES studio_operator_conversations(id),
  instruction        TEXT NOT NULL,
  context_json       JSON NOT NULL,
  context_hash       TEXT NOT NULL,
  status             TEXT NOT NULL
                     CHECK(status IN ('queued', 'running', 'awaiting_confirmation',
                                      'completed', 'failed', 'cancelled')),
  error_code         TEXT,
  created_at         REAL NOT NULL,
  started_at         REAL,
  ended_at           REAL,
  cancel_requested_at REAL
);

CREATE TABLE studio_operator_frames (
  conversation_id   TEXT NOT NULL REFERENCES studio_operator_conversations(id),
  sequence           INTEGER NOT NULL,
  request_id         TEXT NOT NULL REFERENCES studio_operator_turns(request_id),
  frame_type         TEXT NOT NULL,
  payload_json       JSON NOT NULL,
  created_at         REAL NOT NULL,
  PRIMARY KEY(conversation_id, sequence)
);

CREATE INDEX idx_operator_frames_request
  ON studio_operator_frames(request_id, sequence);

CREATE TABLE studio_operator_proposals (
  id                 TEXT PRIMARY KEY,
  conversation_id   TEXT NOT NULL REFERENCES studio_operator_conversations(id),
  request_id         TEXT NOT NULL REFERENCES studio_operator_turns(request_id),
  command_type       TEXT NOT NULL,
  command_json       JSON NOT NULL,
  target_version     TEXT,
  risk               TEXT NOT NULL CHECK(risk IN ('mutate', 'execute', 'admin')),
  summary            TEXT NOT NULL,
  idempotency_key    TEXT NOT NULL UNIQUE,
  status             TEXT NOT NULL
                     CHECK(status IN ('pending', 'confirmed', 'executing', 'succeeded',
                                      'failed', 'expired', 'cancelled', 'conflict')),
  expires_at         REAL NOT NULL,
  confirmed_at       REAL,
  completed_at       REAL,
  result_json        JSON,
  error_code         TEXT,
  created_at         REAL NOT NULL
);

CREATE TABLE studio_operator_effects (
  id                 TEXT PRIMARY KEY,
  conversation_id   TEXT NOT NULL REFERENCES studio_operator_conversations(id),
  request_id         TEXT NOT NULL REFERENCES studio_operator_turns(request_id),
  effect_type        TEXT NOT NULL,
  effect_json        JSON NOT NULL,
  status             TEXT NOT NULL DEFAULT 'pending'
                     CHECK(status IN ('pending', 'applied', 'rejected', 'expired')),
  emitted_at         REAL NOT NULL,
  acknowledged_at    REAL,
  rejection_code     TEXT
);
```

Exact persistence semantics:

- Conversation ids, request ids, proposal ids, and effect ids are UUID strings.
- `sequence` is monotonic across the whole conversation, not reset per request. A single
  StateDB transaction increments `next_sequence` and inserts each frame, preventing
  duplicates during concurrent delivery.
- At most one turn is queued/running/awaiting confirmation per conversation.
  `active_request_id` is set with a compare-and-set; a second submission returns conflict.
- Different conversations may run concurrently subject to provider/application capacity.
- Conversation rows are not automatically expired. They remain until explicit archive or
  deletion, avoiding another silent two-hour loss contract.
- Archive makes a conversation read-only and replayable. Delete is two-stage: mark deleted,
  then an explicit maintenance policy may purge dependent rows after the documented
  recovery window.
- Daemon restart reloads rows and marks a previously `running` turn failed with
  `service_restarted` unless its provider adapter has a durable resume handle. It appends
  `error` and `done` frames; it does not pretend the turn completed.
- StateDB absence or migration failure makes the operator protocol unavailable. It does not
  fall back to ephemeral sessions under the same API.

Why this way: a durable conversation needs different identity, retention, and replay from a
runtime execution Session. Explicit deletion is more predictable than silent LRU/idle
eviction.

### D2 — Version-1 typed frame envelope

The target Python contract is:

```python
OperatorFrameType = Literal[
    "text", "tool_call", "tool_result", "ui_command", "proposal",
    "confirmation", "error", "done"
]

class OperatorFrame(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal[1] = 1
    conversation_id: UUID
    request_id: UUID
    sequence: int = Field(ge=1)
    type: OperatorFrameType
    payload: TextPayload | ToolCallPayload | ToolResultPayload | UiCommandPayload | \
             ProposalPayload | ConfirmationPayload | ErrorPayload | DonePayload
    created_at: float
```

Wire JSON uses camelCase for the three id/sequence fields as in the original skeleton:

```typescript
export interface OperatorFrame<T extends OperatorPayload = OperatorPayload> {
  version: 1;
  conversationId: string;
  requestId: string;
  sequence: number;
  type:
    | "text" | "tool_call" | "tool_result" | "ui_command"
    | "proposal" | "confirmation" | "error" | "done";
  payload: T;
  createdAt: number;
}
```

The payload discriminants are exact:

```typescript
type TextPayload = { content: string; format: "plain" | "markdown" };
type ToolCallPayload = {
  callId: string; tool: string; arguments: Record<string, unknown>; mode: "read" | "draft";
};
type ToolResultPayload = {
  callId: string; ok: boolean; result?: unknown; error?: ProtocolError;
};
type UiCommandPayload = { effect: UiEffect };
type ProposalPayload = { proposal: CommandProposal };
type ConfirmationPayload = {
  proposalId: string; state: "required" | "confirmed" | "expired" | "executed";
};
type ErrorPayload = { error: ProtocolError };
type DonePayload = {
  outcome: "completed" | "failed" | "cancelled";
  lastSequence: number;
};
```

Exact frame semantics:

- A frame is inserted durably before it is yielded to any stream.
- Stream delivery is ordered by `sequence`; reconnect may redeliver a frame after the
  client's cursor. Clients deduplicate by `(conversationId, sequence)`.
- Every accepted request ends in exactly one durable `done` frame, including cancellation,
  validation failure after acceptance, provider failure, and restart recovery.
- Submission errors that prevent creation of a turn use an ordinary HTTP application error
  and have no request/frame sequence.
- `error` may be followed only by diagnostic frames and the terminal `done`; it does not
  imply the stream transport itself failed.
- Unknown version or frame type is a protocol error. Clients do not best-effort execute
  unknown UI commands or proposals.
- Text may stream in multiple frames. Concatenation order is sequence order; frame
  boundaries are not semantic paragraphs.

### D3 — Router-derived destinations and application-service tools

The cockpit command schema is generated from ADR-0080's typed registry:

```python
class NavigateEffect(BaseModel):
    kind: Literal["navigate"] = "navigate"
    space: Literal["mission", "designer", "library", "history", "schedules", "system"]
    params: dict[str, str | int | bool | list[str]] = Field(default_factory=dict)

class SelectEffect(BaseModel):
    kind: Literal["select"] = "select"
    space: Literal["mission", "designer", "library", "history", "schedules"]
    selection: dict[str, str]

class PrefillEffect(BaseModel):
    kind: Literal["prefill"] = "prefill"
    form: Literal["schedule", "workflow", "playbook"]
    values: dict[str, JsonValue]

class ThemeEffect(BaseModel):
    kind: Literal["theme"] = "theme"
    theme: Literal["light", "dark"]

UiEffect = NavigateEffect | SelectEffect | PrefillEffect | ThemeEffect
```

Fleet is represented as `space="mission"` with a validated sub-view/search parameter; it is
not a seventh space. Every param is validated by the destination route schema before the
effect is emitted and again by the client before application.

Tools are registered by stable id with an execution class:

```python
class OperatorToolSpec(BaseModel):
    id: str
    title: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    execution_class: Literal["read", "navigate", "draft", "mutate", "execute", "admin"]
    application_operation: str
```

Exact semantics:

- Read, navigate, and draft tools may execute during the turn. Read tools call ADR-0078
  application queries; navigate/draft tools emit acknowledged effects.
- Mutate, execute, and admin tools never execute during model tool handling. They create D4
  proposals.
- No tool spec contains a raw URL, shell command, SQL statement, or file path for the model
  to choose.
- Tool input/output is Pydantic-validated with `extra="forbid"`.
- Unknown tool ids and invalid inputs emit stable validation errors; the model does not get
  a permissive arbitrary-call fallback.
- Tool results include a bounded, redacted application projection. Secrets and full host
  paths are not inserted into conversation frames.
- The model provider and model remain configurable, but provider unavailability cannot
  disable ordinary cockpit operation.

### D4 — Expiring, refreshed, idempotent, audited proposals

The proposal and confirmation contracts are:

```python
class CommandProposal(BaseModel):
    id: UUID
    command: StudioApplicationCommand
    risk: Literal["mutate", "execute", "admin"]
    summary: str
    target: ResourceVersion | None
    idempotency_key: UUID
    expires_at: float

class ConfirmProposalRequest(BaseModel):
    expected_command_hash: str
    expected_target_version: str | None = None

class ProposalResult(BaseModel):
    proposal_id: UUID
    status: Literal["succeeded", "failed", "conflict", "expired"]
    result: dict[str, JsonValue] | None = None
    error: ProtocolError | None = None
```

Exact semantics:

1. Model tool output is validated as an endpoint-independent ADR-0078 application command.
2. The coordinator computes a canonical JSON hash, stores proposal/target/risk/summary,
   allocates an idempotency key, and sets expiry to 10 minutes after creation.
3. The client renders the exact command summary and risk, then confirms by proposal id plus
   expected command hash and target version.
4. Confirmation re-reads the proposal and target. Hash mismatch, changed target, expired
   proposal, archived conversation, or non-pending status prevents execution.
5. In one StateDB transaction, pending becomes confirmed/executing and a durable audit event
   is appended before the application command is called.
6. Repeated confirmation with the same idempotency key returns the stored terminal result or
   current executing state; it never executes twice.
7. The application result/failure and final proposal status are persisted, then emitted as
   confirmation/tool-result frames.

Ten minutes bounds staleness while allowing a human to inspect the proposal. It is a
conservative initial safety value, not a measured optimum; changing it is a protocol/config
decision and must remain visible in the proposal.

Proposal cancellation and expiry are terminal. They do not mutate state. A new model turn
must create a new proposal with a new hash and idempotency key.

### D5 — Declarative effects require client acknowledgement

The acknowledgement endpoint accepts:

```python
class AcknowledgeEffectRequest(BaseModel):
    status: Literal["applied", "rejected"]
    client_route: str | None = None
    rejection_code: Literal[
        "unsupported", "invalid_params", "stale_context", "not_visible", "client_error"
    ] | None = None
```

Exact semantics:

- Emitting `ui_command` inserts a `studio_operator_effects` row and frame with status
  pending. The backend never calls it applied at emission time.
- The client validates the effect against its registry and current context, attempts it, and
  POSTs one acknowledgement.
- `applied` requires the resulting route/selection/theme to match the validated command.
- `rejected` requires a stable rejection code. Optional human detail is diagnostic only.
- A repeated identical acknowledgement is idempotent. A contradictory second
  acknowledgement returns conflict.
- Disconnect leaves the effect pending. Replay shows it again, but the client checks local
  effect id history before applying and then acknowledges, preventing duplicate navigation
  or form overwrite.
- Effects expire when their turn/proposal context is no longer valid. An expired effect is
  not applied on reconnect.
- A turn may finish while an informational navigation effect is pending, but the text must
  say it was requested rather than completed. A workflow requiring effect completion emits
  a confirmation frame only after acknowledgement.

### D6 — HTTP commands and authenticated fetch SSE

The version-1 HTTP surface is:

```text
POST   /api/operator/conversations
GET    /api/operator/conversations/{id}
DELETE /api/operator/conversations/{id}
POST   /api/operator/conversations/{id}/turns
POST   /api/operator/conversations/{id}/requests/{request_id}/cancel
GET    /api/operator/conversations/{id}/stream?after_sequence=N
POST   /api/operator/conversations/{id}/effects/{effect_id}/ack
POST   /api/operator/conversations/{id}/proposals/{proposal_id}/confirm
POST   /api/operator/stream-tickets
```

Turn submission body is:

```python
class OperatorTurnRequest(BaseModel):
    instruction: str = Field(min_length=1, max_length=32_768)
    context: OperatorContextSnapshot
    expected_last_sequence: int = Field(ge=0)

class OperatorContextSnapshot(BaseModel):
    project: str | None = None
    space: Literal["mission", "designer", "library", "history", "schedules", "system"]
    route: str
    selection: dict[str, str] | None = None
    filters: dict[str, JsonValue] = Field(default_factory=dict)
```

The 32 KiB instruction cap bounds persistence and prompt abuse while remaining far above an
ordinary operator instruction. It is a conservative initial bound, not a measured optimum.

Exact transport semantics:

- Create/submit/cancel/ack/confirm are ordinary authenticated JSON requests.
- Submit returns HTTP 202 with `{conversationId, requestId, acceptedSequence}`.
- Stream uses fetch SSE with the bearer header and unnamed `data:` JSON frames. The cursor
  is `after_sequence`; frames returned have strictly greater sequence.
- If `after_sequence` is beyond the current tail, the stream waits for new frames. If it is
  older than retained data after an explicit purge, the server emits/returns `replay_gap`
  with the earliest available cursor; it never silently starts later.
- Cancellation is idempotent. Queued work becomes cancelled immediately; running provider
  work receives best-effort cancellation and ultimately emits `done(cancelled)` or a
  failure explaining why cancellation could not stop side effects already completed.
- The default browser uses the bearer header. It does not need a ticket.
- If a transport cannot attach headers, `POST /stream-tickets` returns a random one-use
  credential valid for 60 seconds and scoped to one conversation/cursor. The stored server
  value is hashed; redemption invalidates it; it is never the long-lived bearer token.
- Sixty seconds is a short connection-establishment window chosen to limit URL/log exposure;
  no measurement selects exactly 60 seconds.
- WebSocket is not part of version 1. A later ADR must prove a concurrent bidirectional need
  and specify auth, heartbeat, backpressure, acknowledgement, and replay.

### D7 — Durable replay, bounded context, and stable errors

Conversation replay and model context are separate:

- The client may page every retained frame from StateDB.
- The model receives the current instruction plus the newest complete turns that fit both a
  64-frame and 128-KiB serialized-content cap. System/tool schemas are budgeted separately.
- Frames are added newest-to-oldest by complete turn; the compiler never truncates half a
  tool call/result pair. If one current instruction exceeds the instruction cap it is
  rejected before turn creation.
- The 64-frame/128-KiB limits bound provider input and latency while retaining substantial
  local context. They are conservative initial budgets with no recorded production
  calibration; the persisted full history remains available to the client.
- Context compilation records the included sequence range and hash in the turn's
  `context_json`, making a model answer traceable to its snapshot.

Stable error codes are:

```typescript
export type OperatorErrorCode =
  | "auth_required"
  | "validation"
  | "not_found"
  | "denied"
  | "conflict"
  | "stale_context"
  | "rate_limited"
  | "model_failure"
  | "service_failure"
  | "service_restarted"
  | "audit_unavailable"
  | "replay_gap"
  | "cancelled"
  | "protocol_version";

export interface ProtocolError {
  code: OperatorErrorCode;
  message: string;
  retryable: boolean;
  retryAfterMs?: number;
  details?: Record<string, unknown>;
}
```

Exact semantics:

- HTTP auth failure occurs before a turn and uses the daemon error response.
- Turn-level validation/model/service failures append typed error plus done frames.
- Conflict and stale context are not automatically retried; the client refreshes first.
- Rate limiting, when returned by the configured application/provider gate, includes
  `retryAfterMs`; this ADR standardizes the error contract but does not invent a global
  fixed request quota.
- Provider retry is allowed only when the error says `retryable` and no mutate/execute/admin
  proposal was confirmed as part of that attempt.
- Client reconnect repeats reads by cursor and never repeats turn submission automatically.

### D8 — Mutation audit fails closed; read-only explanation may degrade visibly

The mutation audit reuses StateDB's append-only `admin_events` table with
`action="studio.operator.command"`, `target_id=<proposal id>`, and this exact `details`
payload:

```json
{
  "conversation_id": "uuid",
  "request_id": "uuid",
  "proposal_id": "uuid",
  "command_type": "stable application command id",
  "command_hash": "sha256 hex",
  "target": {"kind": "resource kind", "id": "resource id", "version": "opaque"},
  "risk": "mutate | execute | admin",
  "idempotency_key": "uuid",
  "decision": "confirmed | denied | expired | executed | failed | indeterminate",
  "result": {},
  "error_code": null,
  "confirmed_at": 0.0,
  "completed_at": null
}
```

Under the current single-token local boundary, `admin_events.actor` is the stable value
`"studio_operator"`; the bearer token does not identify an individual and is never stored.
If the daemon later gains a richer principal contract, that boundary may supply a stable
principal id, but it never comes from model output. The audit row is written through StateDB
in the D4 ordering. Events are append-only: confirmation and terminal result are separate
rows with the same `target_id` and idempotency key, preserving the transition trail.

Exact semantics:

- If proposal or audit persistence is unavailable, mutate/execute/admin tools return
  `audit_unavailable`; no application command is invoked.
- If the pre-execution audit succeeds but final-result persistence fails, the command result
  is not retried automatically. The proposal remains `executing`/indeterminate and the
  idempotency key blocks duplicate execution until reconciliation determines the outcome.
- Read-only tools may continue when their own data source is available. Frames and dock UI
  show degraded durability/observability; they do not imply the conversation is fully
  persisted when it is not.
- Navigation/draft effects require durable effect/frame persistence. If that persistence is
  unavailable, the model may describe the intended action in text but the backend does not
  emit an untracked effect.
- Ordinary cockpit navigation and typed application commands remain usable without the
  operator dock. Model/provider failure never disables them.

For the target seven-component subgraph—dock, HTTP/SSE adapter, coordinator, tool registry,
application services, conversation store, and audit/effect bridge—with eight intended direct
dependencies, `κ = 8/(7×6) = 0.19`, below 0.3. Direct dock→StateDB or model→endpoint edges
would increase coupling and violate the boundary.

Testability target is `τ ≥ 0.85` using a scripted model, fake application adapters, fake
clock/store, frame-replay tests, restart recovery, effect acknowledgement, stale target,
expiry, idempotency, audit-failure, EOF, and cancellation tests.

## Consequences

- The operator dock becomes durable and replayable rather than an ephemeral daemon demo.
- Typed destinations, tools, proposals, and effects keep model output inside the same
  application and IA boundaries as ordinary UI actions.
- StateDB gains five protocol tables and explicit migration/recovery work. Sequence
  allocation, provider cancellation, and indeterminate audit outcomes add state-machine
  complexity.
- Confirmation is slower than calling a model-authored endpoint directly. That delay buys
  refreshed targets, explicit human intent, audit, and idempotency.
- Clients must acknowledge effects and persist cursors locally; a simple text-only client
  may ignore effects but must render them as unapplied.
- Full history remains available to the operator while model context stays bounded.
- Reversing the version-1 envelope after clients persist cursors is expensive and requires a
  version negotiation/migration decision.

## Alternatives considered

### Keep the ephemeral Leo sessions and current SSE dictionaries

This is already small, bounded, and safe from direct mutation. It lost because restart,
silent LRU/idle eviction, missing replay, and absent acknowledgements make it unsuitable for
a persistent cockpit capability. The prototype remains an implementation source, not the
target contract.

### Require WebSocket for version 1

One duplex connection could carry turns, cancellation, confirmations, effects, and frames.
It lost because HTTP commands plus authenticated fetch SSE already meet ordered server-to-
client delivery and explicit client writes. WebSocket would front-load ticketing, heartbeat,
backpressure, and replay without a demonstrated simultaneous duplex requirement.

### Free-form model-authored URLs or CLI commands

This would make tool coverage broad and easy to extend. It lost because it bypasses typed
application validation, enables shell/endpoint injection, makes risk classification
unreliable, and cannot guarantee confirmation refers to the executed command.

### Browser executes the proposed endpoint after confirmation

The current prototype points the browser at endpoint strings. This keeps the backend model
agent read-only. It lost as the durable target because proposal, confirmation, target
refresh, idempotency, audit, and result become split across two uncoordinated callers. The
coordinator executes a typed application command only after durable confirmation.

### Treat emitted UI commands as applied

This would remove acknowledgement endpoints and reduce latency. It lost because route
validation, disconnect, stale selection, and unsupported clients are observable rejection
paths. The backend must distinguish requested from applied effects.

### Reuse runtime `sessions` for operator conversations

This would reuse message persistence and existing APIs. It lost because runtime sessions
represent executions/branches, while operator conversations need proposals, effects,
replay sequences, explicit retention, and one-active-turn semantics. Overloading the noun
would create incompatible lifecycle rules in one table.

### Auto-expire conversations after the prototype's two hours

Automatic expiry bounds storage and mirrors current behavior. It lost because a persistent
operator dock promises restart/history continuity; silently deleting a conversation based
on idle time violates that promise. Explicit archive/delete keeps storage policy visible.

### Continue mutation when audit storage is unavailable

This maximizes availability. It lost because the inability to prove what was proposed,
confirmed, and executed is precisely the unsafe state for natural-language mutation.
Read-only explanation may degrade; state changes fail closed.
