# ADR-0057: Remote Sandbox Execution Behind PlayRunner

Status: proposed
Date: 2026-05-27
Decision owners: @governance-maintainers
Depends on: ADR-0056 (play control API), ADR-0059 (Postgres state backend), ADR-0060 (unified config resolution)
Related: ADR-0044 (tool gates), ADR-0045 (break-glass), ADR-0053 (artifact persistence), ADR-0058 (play cost tracking)

## Context

Governed orchestration needs an execution boundary that is stronger than a local developer process.
The current flow engine plans a typed DAG and persists live state, but worker execution still happens
inside the host process tree. `build_worker_branch()` sets each worker repo to an artifact directory
and then grants write access to the real project directory through `add_dir`
(`lionagi/cli/orchestrate/_orchestration.py:404-414`). That is productive for local development, but
it is not process, network, CPU, memory, or secret isolation.

The existing local sandbox primitive is a git worktree. `lionagi/tools/sandbox.py` creates a branch,
captures diffs, commits, merges, and discards worktrees (`lionagi/tools/sandbox.py:4-14`,
`lionagi/tools/sandbox.py:46-177`). Worktrees are the right local compatibility runner because they
preserve reviewable diffs and avoid repository copies. They are not a production isolation boundary:
the process still has the host user's credentials and network access unless another layer constrains
it.

Studio and run detail currently assume local state is readable from the server. The runs list reads
SQLite sessions (`apps/studio/server/services/runs.py:503-596`), but run detail still falls back to
`~/.lionagi/runs/{run_id}` manifests and branch JSON snapshots
(`apps/studio/server/services/runs.py:621-650`). Remote execution cannot require the Studio server
to read provider filesystems or SSH into sandboxes. Runner status, logs, artifacts, diffs, costs, and
cleanup outcomes must be uploaded or reported through the central control plane.

ADR-0056 now owns that control plane: `RunnerHandle`, `RunnerState`, `RunControl`,
`ControlRequest`, `PlayRunner`, `runner_handles`, `run_control_requests`, control endpoints, and the
state machine. ADR-0057 depends on ADR-0056 and implements runner backends behind the 0056 protocol.
It does not define a separate execution table, log table, runner table, status vocabulary, or
control API.

Runner configuration is also owned elsewhere. ADR-0060 defines `ResourceKind.RUNNERS` and resolves
runner files from the project/user/plugin cascade (`docs/adrs/ADR-0060-unified-config-resolution.md:44-55`,
`docs/adrs/ADR-0060-unified-config-resolution.md:101-128`). This ADR therefore must not introduce a
separate runner settings block. Playbooks and CLI flags may select a runner by name; the runner
definition itself is loaded through the ADR-0060 resource resolver.

## Decision

Implement remote and local execution as `PlayRunner` backends that import the canonical protocol and
types from ADR-0056. The first backend is `LocalWorktreeRunner`; remote backends include
`EphemeralSandboxRunner`, `PersistentWorkspaceRunner`, and `SSHRunner` as demand warrants.

```python
# lionagi/runtime/runners/base.py

from lionagi.runtime.control import (
    ControlRequest,
    PlayRunner,
    RunControl,
    RunnerHandle,
    RunnerKind,
    RunnerLogLine,
    RunnerState,
)
```

Runner implementations may use provider-specific SDK objects internally, but the only durable state
they publish is the ADR-0056 `RunnerHandle` plus `status_transitions` entries. Artifacts and durable
log chunks use ADR-0053 artifact persistence. Cost observations use ADR-0058 cost events. State
transactions use ADR-0059. Runner configuration uses ADR-0060.

### Runner Backends

`LocalWorktreeRunner` is the compatibility runner:

- Creates a git worktree with the existing sandbox mechanics.
- Runs the same `li play` or `li o flow` command inside that worktree.
- Registers and updates the ADR-0056 `runner_handles` row.
- Streams logs through `PlayRunner.logs()` and may persist bounded chunks as ADR-0053 artifacts.
- Captures diffs as ADR-0053 artifacts rather than exposing server-local paths.
- Uses local process-control helpers for pause/resume/cancel/kill.

Remote runners use the same lifecycle:

- Provision an isolated workspace from a repo URL, base ref, generated feature branch, image, and
  limits.
- Register the provider run id only as `RunnerHandle.runner_ref`.
- Inject only short-lived vendored tokens and non-secret run metadata.
- Execute the same CLI command as local runners with `LIONAGI_INVOCATION_ID`, `LIONAGI_RUN_ID`,
  `LIONAGI_SESSION_ID`, `LIONAGI_RUNNER_KIND`, and `LIONAGI_STATE_API_URL`.
- Upload status, heartbeat, logs, artifacts, and diffs through authenticated runner-ingest
  endpoints.
- Revoke tokens and clean up provider resources during terminal cleanup.

### Runner Registry and Configuration

Add `lionagi/runtime/runner_registry.py`. It resolves runner names through ADR-0060:

```python
from pathlib import Path
from typing import Any

from lionagi.config_resolution import ResourceKind, resolve_resource
from lionagi.runtime.control import PlayRunner


def load_runner_config(name: str, *, cwd: Path | None = None) -> dict[str, Any]:
    location = resolve_resource(ResourceKind.RUNNERS, name, cwd=cwd)
    if location is None:
        raise RunnerConfigError(f"runner {name!r} was not found")
    return parse_runner_yaml(location.path)


def build_runner(name: str, *, cwd: Path | None = None) -> PlayRunner:
    config = load_runner_config(name, cwd=cwd)
    return build_runner_from_config(config)
```

Canonical runner files live in the ADR-0060 cascade:

```yaml
# .lionagi/runners/remote_sandbox.yaml
kind: remote_sandbox
image: ghcr.io/lionagi/lionagi-sandbox:latest
limits:
  timeout_s: 2400
  cpu_count: 4
  memory_mb: 8192
  disk_mb: 20480
network:
  mode: allowlist
  hosts:
    - github.com
    - api.openai.com
credential_policy:
  state_token_ttl_s: 900
  model_broker: required
  source_control: branch_scoped
cleanup:
  retain_failed_s: 86400
  retain_success_s: 3600
```

Playbooks may select the runner by name without embedding secrets:

```yaml
name: guarded-review
runner: remote_sandbox
runner_limits:
  timeout_s: 2400
  cpu_count: 4
  memory_mb: 8192
```

CLI flags may override the runner name:

```bash
li play guarded-review --runner remote_sandbox
li o flow "review the PR" --runner local_worktree
```

If no runner is selected, the compatibility default is `local_worktree`. Any default beyond that is a
resolved resource/default owned by ADR-0060, not a new settings schema in this ADR.

### State Model

ADR-0057 adds no tables. Runner backends use:

| Concern | Owner | Storage |
|---------|-------|---------|
| Current runner location/state | ADR-0056 | `runner_handles` |
| Operator intent | ADR-0056 | `run_control_requests` |
| State history | ADR-0028 / ADR-0056 | `status_transitions` with `entity_type='runner_handle'` |
| Artifacts and diffs | ADR-0053 | immutable `artifacts` rows |
| Durable log chunks | ADR-0053 | `artifacts.kind='runner_log_chunk'` |
| Cost | ADR-0058 | integer-cent `cost_events` |
| Config | ADR-0060 | `ResourceKind.RUNNERS` cascade |

Runner-specific metadata, including provider workspace ids, upload token hashes, cleanup status, and
network policy snapshots, is stored in `runner_handles.metadata_json`. Secrets are never stored there.

### Runner Ingest API

Remote sandboxes do not receive database credentials. They write through narrow authenticated ingest
endpoints. These endpoints are not a second control plane; they only update ADR-0056 runner handles,
ADR-0028 status transitions, and ADR-0053 artifacts.

```python
# apps/studio/server/routers/runner_ingest.py

@router.post("/runner-ingest/{session_id}/heartbeat", status_code=204)
async def heartbeat(
    session_id: str,
    body: HeartbeatRequest,
    token: RunnerToken = Depends(require_runner_token),
) -> None: ...


@router.post("/runner-ingest/{session_id}/status", status_code=202)
async def runner_status(
    session_id: str,
    body: RunnerStatusUpdate,
    token: RunnerToken = Depends(require_runner_token),
) -> None: ...


@router.post("/runner-ingest/{session_id}/logs", status_code=202)
async def runner_logs(
    session_id: str,
    body: RunnerLogUpload,
    token: RunnerToken = Depends(require_runner_token),
) -> UploadAck: ...


@router.post("/runner-ingest/{session_id}/artifacts", status_code=202)
async def runner_artifact(
    session_id: str,
    body: RunnerArtifactUpload,
    token: RunnerToken = Depends(require_runner_token),
) -> UploadAck: ...
```

Required behavior:

- The bearer token is bound to exactly one `session_id` and `runner_ref`.
- `logs` uploads are idempotent on `(session_id, stream, seq)`; duplicates return the existing ack.
- Log uploads have a body-size cap and total-run cap from runner config.
- Artifact uploads compute SHA-256 server-side and persist through ADR-0053. Provider paths are
  metadata only.
- Status updates validate monotonic transitions against the ADR-0056 state machine.
- Cross-session writes, expired tokens, revoked tokens, replayed tokens with mismatched sequence
  metadata, and oversized uploads are rejected and audited.

### Credential Security

Full API keys, broad GitHub tokens, SSH private keys, and provider credentials must never be passed
into arbitrary-code sandboxes. Runners receive only short-lived, scope-limited tokens bound to a
single session and runner handle. Security invariants:

- Sandboxes receive opaque tokens, not host API keys.
- Tokens are scoped to specific operations (state writes, model access, source reads).
- Token TTLs are short (minutes, not hours) and renewed only by valid heartbeats.
- Model access uses brokered tokens where possible — the sandbox never holds provider keys directly.
- Source-control access is branch-scoped via deploy keys or fine-grained tokens.
- Revocation is durable and checked on every request.

### Coupling and Testability

The runner layer has six primary components: runner registry, LocalWorktreeRunner, remote provider
runners, credential vending, runner ingest router, and StateStore. Directed dependencies are registry
-> ADR-0060 resolver, runners -> ADR-0056 protocol, runners -> credential vending, ingest router ->
StateStore, ingest router -> ADR-0053 artifacts, and runners -> StateStore for handle updates.
`κ = 6 / (6 * 5) = 0.20`, under the 0.3 target. Testability target `τ > 0.8` is met with fake
providers, local worktree integration tests, runner-ingest auth tests, and opt-in provider tests.

## Implementation

### Phase 0 - Consume ADR-0056 Contracts (250-400 LOC)

- Add `lionagi/runtime/runners/base.py` that imports the 0056 protocol and re-exports no duplicate
  vocabulary. Estimate: 40 LOC.
- Add `lionagi/runtime/runner_registry.py` backed by ADR-0060 `ResourceKind.RUNNERS`. Estimate:
  140 LOC.
- Add `FakeRunner` and shared runner contract tests. Estimate: 180 LOC.
- Remove any draft references to alternate runner state models. Estimate: 30 LOC.

Exit criteria: a fake runner can start, report status, stream logs, accept controls, and update the
0056 handle model without any 0057-owned schema.

### Phase 1 - LocalWorktreeRunner (350-550 LOC)

- Implement `LocalWorktreeRunner` using `lionagi/tools/sandbox.py` for worktree creation, diff,
  merge, and cleanup. Estimate: 260 LOC.
- Run the existing CLI command in the worktree with the correct run/session/invocation environment.
  Estimate: 100 LOC.
- Persist diffs and bounded logs through ADR-0053 artifacts. Estimate: 100 LOC.
- Share local pause/resume/cancel/kill behavior with ADR-0056's process-control module. Estimate:
  80 LOC.

Exit criteria: `li play NAME --runner local_worktree` registers a `RunnerHandle`, streams logs,
captures a diff artifact, supports cancel/kill, and cleans up according to the runner config.

### Phase 2 - Credential Vending and Runner Ingest (450-750 LOC)

- Add `apps/studio/server/services/runner_credentials.py` for token issuance, hash storage,
  verification, renewal, and revocation. Estimate: 220 LOC.
- Add `apps/studio/server/routers/runner_ingest.py` with heartbeat, status, logs, and artifact
  upload endpoints. Estimate: 220 LOC.
- Add body-size limits, monotonic log sequence checks, SHA-256 artifact verification, and negative
  tests for cross-session writes. Estimate: 180 LOC.
- Add broker-token plumbing for model access where the configured provider supports it. Estimate:
  120 LOC.

Exit criteria: a fake remote runner can run without database credentials or host API keys and can
upload status, logs, and artifacts using only scoped tokens.

### Phase 3 - Remote Providers

Remote runner implementations follow the same `PlayRunner` contract as `LocalWorktreeRunner`.
Provider-specific SDK usage is internal to each runner; the only durable state they publish is the
ADR-0056 `RunnerHandle`. Providers are added incrementally as demand warrants — each must pass the
same contract suite as `LocalWorktreeRunner`.

Exit criteria: at least one ephemeral runner and one persistent workspace runner pass the shared
contract suite.

### Phase 4 - Governance Hardening (350-650 LOC)

- Bind runner start/control/cleanup events to evidence records when that path is active. Estimate:
  200 LOC.
- Enforce break-glass revocation by cancelling or preventing resume for affected handles. Estimate:
  180 LOC.
- Add cost hooks for ADR-0058 using integer cents. Estimate: 120 LOC.
- Add adversarial tests for secret logging, token replay, provider cleanup failure, stale heartbeat,
  artifact tampering, and network allowlist bypass. Estimate: 300 LOC.

## Security

- All runner-ingest endpoints require bearer authentication with vendored runner tokens. All
  operator-facing status, log, and control endpoints remain authenticated by ADR-0056.
- Sandboxes never receive full API keys, broad source-control credentials, SSH private keys, provider
  tokens, database credentials, or the host process environment wholesale.
- Logs are sensitive. Runner implementations must redact configured secret values before upload,
  ingest responses use `Cache-Control: no-store`, and log artifacts are never served through
  unauthenticated URLs.
- Remote runners must not receive local project `add_dir` access. They receive a clean clone or
  provider workspace on a generated branch.
- Network policy is deny-by-default unless a runner config explicitly allowlists hosts. Model APIs,
  source control, and package mirrors are added only when the play requires them.
- Artifact upload verifies server-computed SHA-256 and size before insertion. Provider file paths are
  treated as untrusted metadata and must not appear as absolute server paths in Studio responses.
- Token TTL and revocation are enforcement points, not documentation. Cancel, kill, cleanup,
  break-glass, and expiry make subsequent ingest/broker requests fail closed.
- Cleanup failure is visible. A runner may reach a terminal session state while leaving
  `runner_handles.metadata_json.cleanup` with follow-up status for the operator.

## Migration

1. Delete the rejected draft's separate execution/log table concepts from implementation plans. No
   database migration creates those tables.
2. Add runner config files under the ADR-0060 cascade, for example `.lionagi/runners/local_worktree.yaml`.
   Do not add a new runner block to settings.
3. Existing `li play` and `li o flow` behavior remains local. When no runner is selected, use
   `local_worktree` compatibility mode after ADR-0056 handle registration exists.
4. Existing run detail continues to read legacy filesystem manifests for old local runs. New remote
   runs must render from `runner_handles`, `status_transitions`, and ADR-0053 artifacts.
5. Existing schedules that launch plays may continue spawning local subprocesses until the scheduler
   is wired to the runner registry. Once migrated, scheduled plays select runners the same way CLI
   plays do.
6. Any existing environment allowlist examples that pass model-provider or source-control API keys
   into sandboxes must be replaced with token vending or broker access.

## Testing

- Contract-test `FakeRunner`, `LocalWorktreeRunner`, and each remote runner with the same start,
  status, logs, control, artifact, and cleanup scenarios.
- Integration-test `LocalWorktreeRunner` in a temporary git repository: create worktree, run a short
  play, write an artifact, capture diff, cancel a long-running run, and clean up.
- API-test runner ingest with valid token, missing token, expired token, revoked token, wrong
  session, wrong scope, duplicate log sequence, oversized body, and replay attempt.
- Security-test that raw provider keys and broad source-control tokens are absent from sandbox
  environment, logs, artifacts, and `runner_handles.metadata_json`.
- Migration-test that no 0057-owned tables are created and existing local runs remain visible.
- Provider tests are opt-in behind environment flags and must use disposable workspaces.

## Alternatives Considered

### Keep Local Worktrees Only

Accepted as the MVP runner, rejected as the final architecture. Worktrees provide good diffs and
fast local workflows, but they do not isolate process, network, resources, or inherited secrets.

### Provider-Specific Studio Routes

Rejected. Provider-specific routes would leak remote-provider semantics into Studio and duplicate
status, logs, artifacts, and control behavior. The `PlayRunner` protocol keeps providers behind
one contract.

### 0057-Owned Execution Tables

Rejected. A separate execution or log model recreates the circular dependency and competes with
ADR-0056. Runner backends use `runner_handles`, `run_control_requests`, `status_transitions`, and
ADR-0053 artifacts.

### Direct Database Access From Sandboxes

Rejected as the default. It widens credential scope and couples provider network policy to database
topology. Self-hosted runners may opt into narrow database credentials later, but the default is
authenticated runner ingest with scoped tokens.

### Kubernetes Runner First

Deferred. Kubernetes is credible for larger self-hosted deployments, but it adds operational
assumptions not yet present in lionagi. It can be added later as another `PlayRunner` backend.

## Consequences

Positive:

- ADR-0057 now depends one-way on ADR-0056 and does not define a competing control plane.
- Local and remote execution share one runner contract.
- Remote sandboxes can reduce filesystem, process, network, and credential blast radius.
- Runner config participates in ADR-0060 instead of adding resolver drift.
- Token vending makes secret scope explicit and revocable.

Negative:

- Remote execution adds provider cost, cleanup, token issuance, and network-policy complexity.
- Some model/source providers may not support the desired scoped-token semantics; those integrations
  must use a broker or remain unavailable to remote sandboxes by default.
- Logs and artifacts become ingestion workloads that need quotas and backpressure.
