# ADR-0056: Play Control API - Runner Control Plane

Status: proposed
Date: 2026-05-27
Decision owners: @governance-maintainers
Depends on: ADR-0053 (artifact persistence), ADR-0058 (play cost tracking), ADR-0059 (Postgres state backend)
Related: ADR-0020 (invocations), ADR-0024 (session health), ADR-0025 (session lifecycle), ADR-0027 (scheduled runs), ADR-0028 (status reasons), ADR-0057 (remote sandbox execution), ADR-0060 (unified config resolution)

## Context

Governed orchestration needs an operator control plane, not only a status viewer. A play or flow
that can be observed in Studio but can only be paused, cancelled, killed, or retried from an
operator terminal has an audit gap: the intervention bypasses Studio authentication, durable actor
attribution, status reasons, and future task certificates.

The current flow engine already exposes enough structure to control. `FlowPlan` models a two-level
DAG of persistent `FlowAgent` identities and dependent `FlowOp` nodes
(`lionagi/cli/orchestrate/flow.py:332-425`). `_run_flow()` creates live persistence, records whether
the invocation is a `flow` or `play`, and maps Python outcomes into the session lifecycle vocabulary
of `completed`, `failed`, `timed_out`, `aborted`, and `cancelled`
(`lionagi/cli/orchestrate/flow.py:610-760`). While the DAG runs, operation segments and an early DAG
snapshot are persisted into `sessions.node_metadata` for Studio rendering
(`lionagi/cli/orchestrate/flow.py:1177-1290`). What is missing is the reverse channel from Studio to
the running process or remote runner.

The state model already separates workflow state from runtime state. `sessions.status` is the
runtime lifecycle and intentionally has no SQLite `CHECK` constraint so Python remains the source of
truth (`lionagi/state/schema.sql:97-168`, `lionagi/state/db.py:203-219`). `plays.status` is the show
workflow state, with values such as `pending`, `prepared`, `running_complete`, `gated`, `merged`, and
`blocked`; it is not a process control state (`lionagi/state/schema.sql:265-302`). Status history is
append-only in `status_transitions` (`lionagi/state/schema.sql:540-561`). This ADR must therefore not
add `paused` or `killed` to `plays.status`.

The current recovery path is CLI-centric. `li kill` resolves sessions, invocations, plays, and shows,
finds a PID from `node_metadata` or artifact pid files, sends `SIGTERM`, escalates to `SIGKILL`, and
persists cancellation through `StateDB.update_status()` (`lionagi/cli/kill.py:34-126`,
`lionagi/cli/kill.py:149-260`). This is useful prior art, but it is not a typed API, does not cover
pause/resume/retry, and cannot be the remote-runner abstraction.

Studio's existing middleware is not sufficient for this surface. It gates all `/api/admin/*`
requests and mutating non-admin `/api/*` requests when `LIONAGI_STUDIO_AUTH_TOKEN` is set, but it
does not gate ordinary GET routes such as future status or log endpoints
(`apps/studio/server/app.py:52-78`). Play control exposes runtime status and logs that may contain
secrets, so every endpoint in this ADR requires bearer authentication, including reads.

The rejected drafts for ADR-0056 and ADR-0057 formed a cycle: ADR-0056 depended on ADR-0057 while
ADR-0057 also depended on ADR-0056. This rewrite breaks the cycle. ADR-0056 owns the control plane:
the vocabulary, `PlayRunner` protocol, `runner_handles` table, `run_control_requests` table, control
API endpoints, and runner state machine. ADR-0057 depends on this ADR and implements concrete runner
backends behind the protocol. ADR-0057 does not define a competing execution table or state machine.

## Decision

Add a single runner control plane used by Studio, CLI recovery paths, the scheduler, local worktree
runners, and remote sandbox runners. The API is play-friendly for Studio, but the canonical runtime
target is the linked session:

```text
plays.id -> plays.session_id -> runner_handles.session_id
```

Sessions and invocations without a play row use the same control service through session endpoints.
`plays.status` remains the show workflow state. Runtime state lives in `runner_handles.state`.
Operator intent lives in `run_control_requests`. Terminal runtime outcomes continue to update
`sessions.status` and `status_transitions` through the StateStore contract from ADR-0059.

### Canonical Vocabulary

ADR-0056 defines these names for all dependent ADRs and implementations:

```python
# lionagi/runtime/control.py

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from enum import StrEnum
from typing import Protocol
from pydantic import BaseModel, Field


class RunnerKind(StrEnum):
    LOCAL_WORKTREE = "local_worktree"
    REMOTE_SANDBOX = "remote_sandbox"
    SSH = "ssh"


class RunnerState(StrEnum):
    PENDING = "pending"
    STARTING = "starting"
    RUNNING = "running"
    PAUSING = "pausing"
    PAUSED = "paused"
    RESUMING = "resuming"
    CANCELLING = "cancelling"
    KILLING = "killing"
    RETRYING = "retrying"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    KILLED = "killed"
    LOST = "lost"


class RunControl(StrEnum):
    PAUSE = "pause"
    RESUME = "resume"
    CANCEL = "cancel"
    KILL = "kill"
    RETRY = "retry"


class ControlRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=1000)
    grace_seconds: float = Field(default=5.0, ge=0.0, le=300.0)
    cascade: bool = False
    expected_state: RunnerState | None = None
    idempotency_key: str | None = Field(default=None, max_length=128)
    metadata: dict = Field(default_factory=dict)


class RetryRequest(ControlRequest):
    from_checkpoint: str | None = Field(default=None, max_length=256)
    attempt_label: str | None = Field(default=None, max_length=128)


class RunnerHandle(BaseModel):
    session_id: str
    play_id: str | None = None
    invocation_id: str | None = None
    runner_kind: RunnerKind
    runner_ref: str | None = None  # opaque provider/local handle, never a secret
    state: RunnerState
    location_label: str
    pid: int | None = None
    process_group_id: int | None = None
    heartbeat_at: float | None = None
    artifact_root: str | None = None
    log_ref: str | None = None
    capabilities: set[RunControl] = Field(default_factory=set)
    metadata: dict = Field(default_factory=dict)


class RunnerLogLine(BaseModel):
    cursor: str
    created_at: float
    stream: str
    text: str
    metadata: dict = Field(default_factory=dict)


class ControlResponse(BaseModel):
    request_id: str
    target_type: str
    target_id: str
    session_id: str
    action: RunControl
    accepted: bool
    request_status: str
    runner_state: RunnerState
    detail: str = ""


class RuntimeStatusResponse(BaseModel):
    target_type: str
    target_id: str
    session_id: str
    play_status: str | None = None
    session_status: str
    runner_handle: RunnerHandle | None = None
    last_control_request_id: str | None = None
    cost_cents: int | None = None


class PlayRunner(Protocol):
    kind: RunnerKind

    async def start(self, request: Mapping) -> RunnerHandle: ...
    async def control(
        self,
        handle: RunnerHandle,
        action: RunControl,
        request: ControlRequest,
    ) -> RunnerHandle: ...
    async def status(self, handle: RunnerHandle) -> RunnerHandle: ...
    async def logs(
        self,
        handle: RunnerHandle,
        *,
        cursor: str | None = None,
        limit: int = 500,
    ) -> AsyncIterator[RunnerLogLine]: ...
    async def cleanup(self, handle: RunnerHandle) -> None: ...
```

`RunnerHandle`, `RunnerState`, `RunControl`, and `ControlRequest` are the only accepted vocabulary.
ADR-0057 runner implementations must import these types and must not define alternate handle/status
types, runner lifecycle tables, log tables, or a second runner-state enum.

### Runner State Machine

The state machine is intentionally small and runner-neutral:

| Current state | Allowed controls | Next state before side effect | Terminal mapping |
|---------------|------------------|-------------------------------|------------------|
| `pending`, `starting` | `cancel`, `kill` | `cancelling`, `killing` | `cancelled` |
| `running` | `pause`, `cancel`, `kill` | `pausing`, `cancelling`, `killing` | runner-specific |
| `paused` | `resume`, `cancel`, `kill` | `resuming`, `cancelling`, `killing` | runner-specific |
| `failed`, `timed_out`, `cancelled`, `killed`, `lost` | `retry` | `retrying` | new session/handle |
| `completed` | `retry` | `retrying` | new session/handle |

Runner terminal states map to session terminal statuses as follows:

| RunnerState | `sessions.status` |
|-------------|-------------------|
| `completed` | `completed` |
| `failed` | `failed` |
| `timed_out` | `timed_out` |
| `cancelled` | `cancelled` |
| `killed` | `cancelled` with force-kill reason |
| `lost` | no automatic session terminal write until policy classifies it |

Every accepted state transition writes a `status_transitions` row with
`entity_type='runner_handle'`, `entity_id=session_id`, `source='admin'` or `source='executor'`, and a
non-null actor. Destructive terminal controls also update the linked session through
`StateStore.update_status()`.

### HTTP API

Mount the router under the existing Studio `/api` prefix. All endpoints, including `GET` endpoints,
use an explicit bearer-auth dependency and fail closed when an actor cannot be resolved.

```python
# apps/studio/server/routers/play_control.py

from fastapi import APIRouter, Depends, Query, Request

router = APIRouter(tags=["play-control"])


@router.post("/plays/{play_id}/pause", status_code=202)
async def pause_play(
    play_id: str,
    body: ControlRequest,
    actor: Actor = Depends(require_studio_actor),
) -> ControlResponse: ...


@router.post("/plays/{play_id}/resume", status_code=202)
async def resume_play(
    play_id: str,
    body: ControlRequest,
    actor: Actor = Depends(require_studio_actor),
) -> ControlResponse: ...


@router.post("/plays/{play_id}/cancel", status_code=202)
async def cancel_play(
    play_id: str,
    body: ControlRequest,
    actor: Actor = Depends(require_studio_actor),
) -> ControlResponse: ...


@router.post("/plays/{play_id}/kill", status_code=202)
async def kill_play(
    play_id: str,
    body: ControlRequest,
    actor: Actor = Depends(require_studio_actor),
) -> ControlResponse: ...


@router.post("/plays/{play_id}/retry", status_code=202)
async def retry_play(
    play_id: str,
    body: RetryRequest,
    actor: Actor = Depends(require_studio_actor),
) -> ControlResponse: ...


@router.get("/plays/{play_id}/status")
async def play_runtime_status(
    play_id: str,
    actor: Actor = Depends(require_studio_actor),
) -> RuntimeStatusResponse: ...


@router.get("/plays/{play_id}/logs")
async def read_logs(
    play_id: str,
    actor: Actor = Depends(require_studio_actor),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=500, ge=1, le=2000),
    follow: bool = Query(default=False),
) -> LogPage | StreamingResponse: ...


@router.post("/sessions/{session_id}/control/{action}", status_code=202)
async def control_session(
    session_id: str,
    action: RunControl,
    body: ControlRequest,
    actor: Actor = Depends(require_studio_actor),
) -> ControlResponse: ...


@router.get("/sessions/{session_id}/runtime")
async def session_runtime_status(
    session_id: str,
    actor: Actor = Depends(require_studio_actor),
) -> RuntimeStatusResponse: ...
```

`require_studio_actor` validates `Authorization: Bearer <token>`, derives an actor id, and rejects
requests with `401` when the token is missing or invalid. The play-control router does not rely on
the current generic middleware because GET status and log routes are sensitive.

### State Schema

ADR-0056 owns exactly two control-plane tables. The DDL below is logical; SQLite and Postgres
implement it through ADR-0059 schema management and StateStore methods.

```sql
CREATE TABLE IF NOT EXISTS runner_handles (
  session_id        TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
  play_id           TEXT REFERENCES plays(id) ON DELETE SET NULL,
  invocation_id     TEXT REFERENCES invocations(id) ON DELETE SET NULL,
  runner_kind       TEXT NOT NULL,
  runner_ref        TEXT,
  state             TEXT NOT NULL,
  location_label    TEXT NOT NULL,
  pid               INTEGER,
  process_group_id  INTEGER,
  heartbeat_at      REAL,
  artifact_root     TEXT,
  log_ref           TEXT,
  capabilities_json JSON NOT NULL DEFAULT '[]',
  metadata_json     JSON NOT NULL DEFAULT '{}',
  state_updated_at  REAL NOT NULL,
  created_at        REAL NOT NULL,
  updated_at        REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_runner_handles_play
  ON runner_handles(play_id) WHERE play_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_runner_handles_invocation
  ON runner_handles(invocation_id) WHERE invocation_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_runner_handles_state
  ON runner_handles(state, updated_at DESC);

CREATE TABLE IF NOT EXISTS run_control_requests (
  id                  TEXT PRIMARY KEY,
  target_type         TEXT NOT NULL,
  target_id           TEXT NOT NULL,
  resolved_session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  action              TEXT NOT NULL,
  request_status      TEXT NOT NULL,
  requested_by        TEXT NOT NULL,
  reason              TEXT NOT NULL,
  idempotency_key     TEXT,
  expected_state      TEXT,
  grace_seconds       REAL NOT NULL DEFAULT 5.0,
  cascade             INTEGER NOT NULL DEFAULT 0,
  runner_kind         TEXT,
  runner_ref          TEXT,
  created_at          REAL NOT NULL,
  claimed_at          REAL,
  completed_at        REAL,
  error               TEXT,
  metadata_json       JSON NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_run_control_requests_target
  ON run_control_requests(target_type, target_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_run_control_requests_session
  ON run_control_requests(resolved_session_id, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_run_control_requests_idempotency
  ON run_control_requests(target_type, target_id, action, idempotency_key)
  WHERE idempotency_key IS NOT NULL;
```

No log table is added here. The logs endpoint reads through `PlayRunner.logs()` and may persist
bounded log chunks as ADR-0053 artifacts when a runner supports upload. Log content is always served
through authenticated endpoints and never exposed by absolute server paths.

### StateStore Contract

All state access goes through ADR-0059's `StateStore`. Control-plane code must not call `db.db` or
use backend-specific lock statements. Atomicity is expressed through the transaction abstraction:

```python
# lionagi/state/store.py additions

from contextlib import AbstractAsyncContextManager
from typing import Protocol

class StateStore(Protocol):
    def transaction(self) -> AbstractAsyncContextManager["StateTxn"]: ...

    async def upsert_runner_handle(self, handle: RunnerHandle) -> None: ...
    async def get_runner_handle(self, session_id: str) -> RunnerHandle | None: ...
    async def get_runner_handle_for_play(self, play_id: str) -> RunnerHandle | None: ...
    async def list_runner_controls(
        self,
        session_id: str,
        *,
        limit: int = 50,
    ) -> list[dict]: ...


class StateTxn(Protocol):
    async def create_control_request(
        self,
        *,
        target_type: str,
        target_id: str,
        resolved_session_id: str,
        action: RunControl,
        requested_by: str,
        reason: str,
        idempotency_key: str | None,
        expected_state: RunnerState | None,
        grace_seconds: float,
        cascade: bool,
        metadata: dict | None = None,
    ) -> dict: ...

    async def claim_control_request(self, request_id: str) -> dict: ...
    async def complete_control_request(
        self,
        request_id: str,
        *,
        request_status: str,
        error: str | None = None,
        metadata: dict | None = None,
    ) -> None: ...

    async def transition_runner_state(
        self,
        session_id: str,
        *,
        new_state: RunnerState,
        reason_code: str,
        reason_summary: str,
        actor: str,
        source: str,
        control_request_id: str | None = None,
        metadata: dict | None = None,
    ) -> RunnerHandle: ...
```

A control request is created and the runner handle is moved to an in-progress state in one
transaction before the runner side effect is attempted. If the runner call fails, the request is
marked `failed`; the linked session is not moved to a terminal status unless the failure is itself
the observed terminal runtime outcome.

### Coupling and Testability

The design has five primary components: Studio router, control service, StateStore, PlayRunner
registry, and runner implementations. Required dependencies are router -> service, service ->
StateStore, service -> PlayRunner, runner -> StateStore for heartbeat/state updates, and runner ->
artifact persistence for log/artifact upload. With five components and five directed dependencies,
`κ = 5 / (5 * 4) = 0.25`, under the 0.3 target. Testability target `τ > 0.8` is met by fake runners,
StateStore contract tests, FastAPI auth tests, and local process-control integration tests.

## Implementation

### Phase 0 - Contract and Schema (MVP foundation, 300-450 LOC)

- Add `lionagi/runtime/control.py` for the enums, Pydantic models, state-machine validation, and
  `PlayRunner` protocol. Estimate: 180 LOC.
- Add `runner_handles` and `run_control_requests` to `lionagi/state/schema.sql` and the Postgres DDL
  in ADR-0059's implementation. Estimate: 90 LOC.
- Extend `StateStore` and `StateDB` facade methods for runner handles, control requests, and runner
  state transitions. Estimate: 170 LOC.
- Extend status reason validation so `entity_type='runner_handle'` transitions are valid. Estimate:
  30 LOC.

### Phase 1 - Local Control Behavior (350-550 LOC)

- Register a `RunnerHandle` when `start_live_persist()` creates a session row in
  `lionagi/cli/orchestrate/_orchestration.py`. Include PID, process group id, artifact root, and a
  server-local `log_ref`; do not expose absolute paths through API responses. Estimate: 50 LOC.
- Extract reusable PID/process-group utilities from `lionagi/cli/kill.py` into
  `lionagi/runtime/process_control.py`, then update `li kill` to share them. Estimate: 120 LOC net.
- Implement `LocalWorktreeRunner` status, logs, pause, resume, cancel, kill, and cleanup using the
  0056 protocol. Estimate: 260 LOC.
- Add a fake runner for deterministic unit and API tests. Estimate: 80 LOC.

### Phase 2 - Studio API (300-500 LOC)

- Add `apps/studio/server/auth.py` with `require_studio_actor`; it must authenticate all play-control
  routes, including status and logs. Estimate: 80 LOC.
- Add `apps/studio/server/services/play_control.py` to resolve play/session/invocation targets,
  create/claim/complete control requests, call the runner, and write status transitions. Estimate:
  260 LOC.
- Add `apps/studio/server/routers/play_control.py` and mount it from `apps/studio/server/app.py`.
  Estimate: 130 LOC.
- Include runtime summaries in runs/show detail responses without requiring unauthenticated
  waterfall calls. Estimate: 70 LOC.

### Phase 3 - UI and Retry (350-700 LOC)

- Add runner badges and valid control buttons to run and show-play pages. Estimate: 250 LOC.
- Add typed API client methods and response types. Estimate: 120 LOC.
- Implement retry only for terminal playbook-backed sessions whose launch metadata is complete.
  Retry creates a new session and a new `RunnerHandle`; it does not mutate the old terminal handle.
  Estimate: 250 LOC.

### Phase 4 - Remote Runner Integration (owned by ADR-0057, 100-200 LOC in this ADR)

- Wire ADR-0057 runner backends into the `PlayRunner` registry without changing this API contract.
- Ensure remote runner state, logs, and artifacts write through 0056/0053/0058 contracts rather than
  provider-specific Studio routes.

## Security

- Every endpoint in this ADR requires bearer authentication, including `GET /status` and
  `GET /logs`. The route dependency must not rely on the current generic middleware because that
  middleware does not protect non-admin GET routes.
- `requested_by` is non-null for accepted controls. MVP actor identity is the bearer token subject,
  or a stable operator id bound to that token by configuration. If the actor cannot be resolved, the
  API returns `401` or `403` and does not create a control request.
- Logs may contain prompts, tool output, environment names, or secrets. Log responses are
  authenticated, bounded by `limit`, marked `Cache-Control: no-store`, and redacted when redaction
  hooks are configured.
- The API accepts only the `RunControl` enum. It never accepts arbitrary signal names, shell
  commands, filesystem paths, provider command strings, or provider credentials from the browser.
- `runner_ref` is opaque and non-secret. API keys, SSH private keys, provider tokens, signed upload
  URLs, and full bearer tokens are never stored in `runner_handles`.
- Local PID control must guard against PID reuse where practical by checking process group,
  executable metadata, and heartbeat freshness before signaling. Evidence records include PID,
  process group id, runner kind, request id, actor, and timestamps.
- `kill` is privileged and destructive. Production UI must require a non-empty reason and visually
  separate force kill from graceful cancel.
- Control requests are append-only except for claim/completion fields. Destructive terminal controls
  must update the linked session through `StateStore.update_status()` and write a runner-handle
  transition.
- Remote credential issuance is specified by ADR-0057, but this ADR sets the boundary: sandboxes do
  not receive full API keys through `ControlRequest`, `RunnerHandle`, or any Studio request body.

## Migration

1. Apply the additive schema migration for `runner_handles` and `run_control_requests`. Existing
   `sessions`, `plays`, `invocations`, `artifacts`, and `status_transitions` rows remain valid.
2. Do not backfill terminal sessions. Their runtime state is derived from `sessions.status`.
3. For currently running sessions, attempt best-effort handle creation from `sessions.node_metadata`
   and known pid-file locations. If no live process can be verified, no handle is created and status
   endpoints report `runner_state='lost'`.
4. Register handles for all new CLI flow/play sessions when `start_live_persist()` creates the
   session row.
5. Keep `li kill` behavior for one release, then move it onto the same process-control and
   StateStore methods so terminal and Studio actions produce equivalent evidence.
6. Hide Studio controls unless a handle exists and the runner reports the requested capability.
7. Retry remains disabled until launch reconstruction is implemented. The endpoint may return `409`
   for unsupported targets during Phase 2.

## Testing

- Unit-test enum parsing, state-machine transition rules, `ControlRequest` validation, and invalid
  action rejection.
- Migration-test databases created before this ADR; existing play and session statuses must survive.
- StateStore contract-test idempotency under duplicate `idempotency_key` values and concurrent
  control attempts.
- FastAPI-test every endpoint with no token, bad token, and valid token. Reads and writes must both
  require auth.
- Local runner tests with a controlled long-running subprocess must cover pause, resume, cancel,
  kill, log paging, and lost PID detection.
- Verify `status_transitions` rows for runner state changes and terminal session cancellation.
- Fake-runner API tests must prove the router and service do not depend on POSIX signal behavior.

## Alternatives Considered

### Extend `plays.status` With Runtime States

Rejected. `plays.status` is a show workflow state, not a runner lifecycle. Mixing gate/merge states
with runtime states makes a paused-but-gated play impossible to represent and forces schema churn.

### Make ADR-0057 Own Execution State

Rejected. That was the source of the circular dependency and duplicate control plane. ADR-0056 owns
runner handles, control requests, and the state machine. ADR-0057 consumes those contracts.

### Shell Out From Studio to `li kill`

Rejected as the control plane. It is useful implementation reuse for local process control, but it
does not support pause/resume/retry, remote runners, idempotent browser requests, or durable actor
attribution before side effects.

### Router Sends Signals Directly

Rejected. Direct signal calls make the fastest local demo, but they lose durable operator intent and
make concurrent requests, retries, and remote runners hard to reason about.

## Consequences

Positive:

- Studio becomes an authenticated, auditable orchestration control plane.
- ADR-0056 and ADR-0057 have a one-way dependency: ADR-0057 depends on this ADR.
- Local and remote runners share one vocabulary and API contract.
- Existing show-play workflow semantics remain intact.
- State access is backend-neutral through ADR-0059.

Negative:

- Runner handles add a projection that must be kept in sync with session lifecycle.
- Local pause/resume is POSIX-oriented until cooperative checkpoints or provider-specific support
  are added.
- Retry is intentionally delayed because faithful retry requires persisted launch metadata,
  checkpoint policy, cost attribution, and artifact contract reuse.
