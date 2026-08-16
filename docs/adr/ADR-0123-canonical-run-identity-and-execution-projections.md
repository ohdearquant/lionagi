# ADR-0123: Canonical Run identity and execution projections

- **Status**: Proposed
- **Kind**: Aspirational
- **Implementation-status**: not-started
- **Area**: orchestration
- **Date**: 2026-08-16
- **Relations**: depends on ADR-0119 (deterministic declaration and configuration substrate),
  ADR-0122 (feature boundaries and optional loading), and ADR-0058's generic atomic
  creation/transition mechanics; coordinates with ADR-0121 (authoritative
  action execution and native-agent harness); extends ADR-0113 (execution graph as the primary Run canvas)
  and ADR-0114 (executable flow definitions); prospectively amends ADR-0095 D1/D2 (terminal
  callback source and v2 envelope cutover); revisits ADR-0077 (Studio state boundary),
  ADR-0035 (persisted completion), ADR-0064 (CLI
  completion), and ADR-0090 (execution-target seams); must be
  decided before ADR-0118 freezes the target entity registry

## Context

`Run` is already one of LionAGI's most important product nouns. It is the unit listed in Studio,
opened from Fleet and the VS Code extension, resumed by operators, related to schedules, charged
for usage, and inspected for artifacts and failure evidence. There is no corresponding backend
authority. Instead, unrelated objects use the name and each supplies part of the behavior.

**P1 — a model turn is called a Run but has no durable Run identity.**
`lionagi/operations/run/run.py` implements one CLI-backed model turn. `Branch.run()` delegates to
it, and the operation emits `RunStart`, `RunEnd`, or `RunFailed`. Those signals carry no durable
`run_id`. A Session can execute many such turns, so treating each signal pair as a product Run
would create a different identity boundary from Studio and the CLI.

**P2 — the engine creates a second Run identity.** `lionagi/engines/engine.py::EngineRun` is a
mutable per-call context containing a Session, semaphore, task set, budget, deduplication set, and
callbacks. Its constructor mints a twelve-hex-character `run_id`. The CLI also persists an
`engine_runs` row with an independent lifecycle and `spec_json`. An engine is an execution
strategy inside an attempt; it is not evidence that a second user-visible attempt began.

**P3 — the CLI creates a third Run identity and lets a directory act as the record.**
`lionagi/cli/_runs.py::allocate_run()` mints a timestamp-and-random-suffix ID, creates
`~/.lionagi/runs/{run_id}/`, and writes `run.json`. `RunDir` owns manifest, checkpoint, branch,
stream, notification, and artifact paths. Some readers infer status and timestamps from the
presence and modification times of those files. A workspace is a resource used by an attempt;
its directory cannot author lifecycle truth, especially for remote targets or deleted artifacts.

**P4 — Studio's `/runs` surface is a Session projection.**
`lionagi/studio/services/runs.py::_run_row()` assigns both `run_id` and `id` from `sessions.id`,
even though the session schema already has a separate nullable `sessions.run_id` column. Detail
lookup calls `get_session(run_id)`. A Session is reusable conversational state and a Run is an
attempt. Equating them makes a resumed Session indistinguishable from the prior attempt and makes
one Run with several worker Sessions impossible to represent without nesting conventions.

**P5 — control-plane occurrences are also called runs.** `schedule_runs` owns queue, lease,
retry, dependency, concurrency, dispatch, and schedule-chain state. `engine_runs` owns engine
telemetry. Both have their own IDs and status sets. A schedule occurrence can be skipped before
execution, leased more than once, or launch a retry after an attempt has ended. A queue/control
handle therefore cannot also be the immutable identity of the execution attempt.

**P6 — frontend contracts preserve the collision rather than expose it.** Studio hand-authors a
`RunSummary` matching a Session row and a separate `RunDetail` documented as a filesystem
manifest shape, although the current detail service also reads the Session projection. The VS
Code extension hand-authors another `Run` interface. Fields such as `finished_at` versus
`ended_at`, `worker_name` versus `agent_name`, and `id` versus `run_id` survive as compatibility
aliases. UI code must join invocations, sessions, graphs, messages, artifacts, controls, and
effective health without a backend-owned projection contract.

**P7 — executable workflow compilation is owned by Studio.**
`lionagi/studio/services/workflow_compile.py` translates an authored workflow graph into an
`OperationGraph`, resolves engine definitions, validates expressions and paths, and constructs
engine operations. `workflow_run.py` then duplicates persistence setup so a request can execute
the graph. Compilation is runtime business logic. Keeping it inside the optional web application
makes Studio an execution dependency and prevents CLI, MCP, and SDK callers from sharing the same
definition and plan hashes.

**P8 — lifecycle evidence is attached to whichever proxy happened to be available.** Cost,
served-model provenance, artifact verification, cwd, graph hierarchy, terminal reasons, process
health, and callbacks are spread across Session, invocation, schedule occurrence, engine row,
manifest, and signals. This has produced impossible state combinations and races between terminal
write, verification, and notification. Adding more fields to `sessions` does not solve the
ownership error.

**P9 — ADR-0118 cannot freeze the persistence vocabulary first.** ADR-0118 currently reasons
about the existing tables, where `run` is not an entity and `session` is the closest substitute.
If its registry is frozen before this record is decided, generated schema will preserve the
semantic collision and require another structural migration immediately afterward. This record
owns the logical entities and invariants; ADR-0118 owns their physical SQLAlchemy schema,
dialect mapping, migration plan, and CRUD generation.

| Concern | Decision |
|---|---|
| Attempt boundary | D1: an Invocation is ingress; a Run is exactly one admitted execution attempt, created before compilation or provisioning. |
| Identity and lifecycle | D2: every new Run has one globally unique `run_id`; terminal outcome is immutable and finalized once. |
| State model | D3: Run, definition version, plan, operation attempt, lineage, Session link, and workspace are distinct contracts. |
| Session relation | D4: Sessions are reusable conversational state and relate to Runs many-to-many through `run_sessions`. |
| Definition and plan | D5: every Run references an immutable executable definition snapshot and an immutable compiled plan or a recorded compile failure. |
| Engine and model turns | D6: Engine is a strategy, `EngineRun` becomes `EngineRunContext`, and a model turn is an `OperationAttempt`, not another Run. |
| Workspaces | D7: `RunDir` becomes a `RunWorkspace`/`WorkspaceLease` adapter; files project Run state but never author it. |
| Scheduling and jobs | D8: schedule occurrences and job handles own admission, queue, lease, and retry control and link to Runs without becoming Runs. |
| Retry and resume | D9: retry, resume, replay, and fork always create a new Run and record typed lineage; terminal Runs are never reopened. |
| API projections | D10: one backend Run repository emits versioned summary/detail projections and generated frontend types. |
| Historical compatibility | D11: old IDs remain resolvable through explicit aliases or legacy projections; migration never invents a Run boundary absent from evidence. |
| Compiler boundary and sequencing | D12: the canonical workflow compiler lives outside Studio, and ADR-0118 target-registry freeze waits for this logical model. |
| Terminal callback cutover | D13: each Invocation freezes one-run legacy-v1 or multi-capable canonical-v2 mode before its first Run; selected facts deduplicate by terminal-fact identity. |

This record deliberately does **not** decide:

- the physical table DDL, indexes, dialect types, or online cutover algorithm; ADR-0118 owns
  those after this logical vocabulary is accepted;
- action permission ordering, provider-native sandbox guarantees, or approval policy; ADR-0121
  owns them and contributes immutable policy and harness snapshots to a Run;
- signal fan-out, interception ordering, retry queues, or terminal callback mechanics; ADR-0120
  owns those mechanics while this record owns the identifiers in their envelopes;
- flow scheduling algorithms, DAG layout, rank assignment, or reactive-expansion policy;
- Session message/progression representation;
- artifact blob formats or retention policy;
- a guarantee that a local Git worktree is a security sandbox; ADR-0090 already rejects that
  implication;
- automatically turning every low-level `Branch.run()` call into a durable product Run. The SDK
  may expose model-turn primitives without importing StateDB; a composition root that claims Run,
  resume, scheduling, or history semantics must bind a durable `RunRepository`.

## Decision

### D1 — Invocation is ingress; Run is one admitted attempt

The vocabulary is closed at the execution boundary:

```text
Invocation
  requested intent + caller + untrusted inputs + requested policy/target
      |
      | admission ALLOW
      v
Run
  one durable attempt under resolved definition + inputs + policy + target
      |
      +--> ExecutionPlan --> OperationAttempt(s)
      +--> Session link(s)
      +--> RunWorkspace/WorkspaceLease(s)
```

An **Invocation** begins when a caller asks LionAGI to do work. It is the admission envelope and
may be rejected, deferred, escalated for approval, deduplicated, or expanded into several
attempts. It records the requested definition, input reference, principal, ingress surface, and
request time. It is not proof that execution began.

Its decision type is `InvocationAdmissionDecision`. That top-level decision resolves enough
definition/input/policy/target information to decide whether a Run exists. It is distinct from
ADR-0121's `ActionAdmissionDecision`, which governs one tool/callable operation inside an existing
execution scope and can never create, reopen, or terminalize the parent Run by itself.

A **Run** begins only after invocation-level admission returns `ALLOW` with a resolved immutable
definition version, redacted input snapshot, policy snapshot, and target request. The Run record
is then durably created **before** any of these steps:

1. compiling the resolved definition into an executable plan;
2. provisioning a worktree, container, remote workspace, or provider session;
3. creating the first Session;
4. invoking a model, tool, MCP server, subprocess, or workflow node.

This ordering makes plan compilation, harness-policy compilation, and provision failures
observable outcomes of the admitted attempt instead of disappearing between an accepted request
and its first Session. Failure to resolve enough immutable input to make an admission decision is
an Invocation failure and creates no Run. A denied, deferred, or still-escalated Invocation has no
Run. A schedule occurrence skipped by its control policy has no Run. Once admitted, even a
failure before the first operation has a Run.

One Invocation may relate to zero Runs or to several Runs when an admitted job performs automatic
execution retries. An explicit operator resume, replay, or fork is a new Invocation and creates a
child Run linked to the prior Run. A Run belongs to exactly one Invocation. A child Run may
additionally record another Run as its lineage parent; invocation ownership and lineage answer
different questions and neither is inferred from the other.

During ADR-0095 callback migration, D13 requires any Invocation capable of producing several Runs
to select canonical-v2 callback mode before its first Run. The legacy-v1 compatibility mode is
deliberately one Invocation to at most one Run because one immutable Invocation terminal event
cannot represent several distinct Run facts.

Low-level SDK operations remain usable without an operational store. Such a call is a
`ModelTurn`/`OperationContext`, not a durable Run. An SDK composition that exposes a Run API must
receive a durable repository explicitly; an in-memory test adapter cannot claim resume or history
capability.

### D2 — Run identity is minted once and terminal state is immutable

`run_id` is the sole canonical public identity for a new attempt. It is minted by the Run service,
not by a CLI directory allocator, Engine, Session, scheduler, provider, or frontend. The chosen ID
encoding may remain time-sortable, but consumers treat it as opaque and never parse timestamps,
provider names, hierarchy, or target information from it.

The canonical lifecycle is:

```text
preparing -> queued -> running <-> paused
     |          |         |          |
     +----------+---------+----------+
                            v
         completed | failed | cancelled | timed_out
```

- `preparing` begins at durable creation and covers definition resolution, compilation, policy
  compilation, and provisioning.
- `queued` means the admitted attempt is waiting for execution capacity. Queue admission before
  Run creation remains an Invocation/Job state.
- `running` means at least one operation can execute.
- `paused` is a control state of a live attempt; it does not create another Run.
- terminal statuses are `completed`, `failed`, `cancelled`, and `timed_out`.
- `degraded`, partial, or verification-incomplete is an outcome-quality facet, not another
  terminal status. A completed Run may be degraded without being relabeled failed.
- `skipped` is not a Run terminal status: work skipped before admission remains on the Invocation,
  schedule occurrence, job, or planned operation.

The following invariants are enforced by one compare-and-set terminal transition owned by
ADR-0058's `LifecycleService`:

```text
created_at <= started_at <= ended_at                  when all are present
ended_at is null                                      while status is nonterminal
ended_at is non-null                                  when status is terminal
terminalized_at, outcome, reason, usage, and status   are written atomically
terminal Run fields                                   never transition again
```

Cancellation and timeout race with completion through the same transition. Exactly one terminal
outcome wins; later observations are appended as audit evidence and cannot rewrite the winner.
Notification, artifact verification, telemetry export, and mirror delivery occur after commit
through ADR-0120's durable-delivery plane. Their failure is recorded separately and never reopens
the Run.

`Run` is an intentional eighth lifecycle-policy-managed entity in `TargetRegistry`, in addition to
the seven current ADR-0058 policies (the six legacy status-facade values plus `dispatch`) reproduced
by ADR-0118's baseline parity gate. It is not added to the generic legacy facade.
`RunRepository` owns Run record
composition, associations, queries, and projections; it does not create a second mutation
authority. `create()` stages ADR-0058's initial-state command with the new row; `transition()` and
`finalize()` construct one typed lifecycle command and delegate to the single `LifecycleService` in
the same transaction. Direct Run-status SQL is a bypass. This extends ADR-0058 rather than
superseding its one-policy/one-service invariant.

Unknown and zero remain distinct. Missing cost is `None`, not `0.0`; an absent served-model report
is unknown, not the requested model; an absent verifier result is `not_recorded`, not success.
Approximate imported timestamps carry an explicit approximation/source marker.

The canonical terminal record separates lifecycle from acceptance and provenance:

```text
RunTerminalOutcome
  status: completed | failed | cancelled | timed_out
  acceptance: accepted | rejected | not_applicable | unknown
  quality: full | degraded | partial | indeterminate
  reason_code, evidence_refs, legacy_source?, cli_success_projection_version
```

The first legacy mapping is complete and evidence-sensitive:

| Legacy kind/status | Canonical Run projection when an attempt boundary is proven | Aggregate CLI success |
|---|---|---:|
| Session/Invocation `completed` | `completed`, `accepted`, measured quality/evidence | yes |
| Session/Invocation `completed_empty` | `completed`, `rejected`, `partial`, preserve legacy reason | no |
| Session/Invocation `failed` | `failed`, `rejected` | no |
| Session/Invocation `timed_out` | `timed_out`, `rejected` | no |
| Session/Invocation `aborted` or `cancelled` | `cancelled`, `rejected`, preserve exact legacy status/reason | no |
| Play `merged` | `completed`, `accepted`, outcome facet `merged` | yes |
| Play `gate_failed`, `escalated`, or `blocked` | `failed`, `rejected`, preserve exact outcome/reason | no |
| Play `aborted_after_finish` | `cancelled`, `rejected`, preserve exact outcome/reason | no |
| Show `completed` / `aborted` | `completed` / `cancelled` only with corroborating attempt evidence | not a v1 wait target |
| Schedule occurrence `skipped` | no Run; control-plane terminal only | no |
| Schedule occurrence `completed`, `failed`, `timed_out`, `cancelled` | never sufficient alone; a corroborated linked attempt maps by its own evidence | only the legacy schedule projection |
| Team `archived` | no Run | not a v1 wait target |

An engine row, manifest, process exit, or filesystem mtime alone is likewise insufficient. When
several sources disagree but a real attempt boundary is proven, migration preserves each
observation and imports the closed terminal outcome `status="failed"`, `acceptance="unknown"`,
`quality="indeterminate"`, and `reason_code="legacy_evidence_conflict"`; this status means the
historical import could not establish a trustworthy successful outcome, not that one legacy source
won. When the attempt boundary itself is disputed, migration creates no Run and exposes a typed
`LegacyExecutionConflict` projection for operator resolution. Versioned CLI adapters reproduce
ADR-0035 bytes and success values during migration, then switch deliberately to the canonical
projection.

### D3 — the logical execution schema has one owner per fact

The following is a logical contract, not SQL DDL. The records use ADR-0119 `Params`, `Spec`, and
`Operable` declarations; ADR-0118 materializes them for a selected store.

```text
Invocation
  invocation_id, principal, ingress, requested_definition, requested_inputs,
  requested_policy, requested_target, admission_state,
  terminal_callback_mode?: legacy_invocation_v1_single_run | canonical_run_v2,
  created_at

Run
  run_id, invocation_id, definition_version_id, status,
  resolved_input_hash, policy_snapshot_hash?, harness_plan_hash?,
  terminal_fact_id, terminal_callback_source: legacy_invocation_v1 | canonical_run_v2,
  created_at, started_at?, ended_at?, terminalized_at?, outcome?, reason?, quality?

ExecutableDefinitionVersion
  definition_version_id, definition_id, kind, version, semantic_spec,
  semantic_hash, presentation_metadata?, presentation_hash?, created_at

ExecutionPlan
  plan_id, run_id, definition_version_id, compiler_id, compiler_version,
  resolved_inputs_hash, plan_spec, plan_hash, created_at

PlanDelta
  delta_id, run_id, plan_id, ordinal, previous_delta_hash?, cause,
  compiler_id, compiler_version, delta_spec, delta_hash, active_plan_hash, created_at

OperationAttempt
  operation_attempt_id, run_id, operation_key, attempt_index, parent_attempt_id?,
  session_id?, active_plan_hash, kind, status, started_at?, ended_at?, outcome?, reason?

RunSession
  run_id, session_id, role, ordinal, access_mode, attached_revision,
  writer_lease_id?, writer_lease_expires_at?, attached_at, detached_at?

RunLineage
  child_run_id, parent_run_id, relation, created_at, reason?

RunWorkspace
  workspace_id, run_id, backend, root_uri?, artifact_uri?, lease_id?, state

RunTerminalCallbackBinding
  binding_id, run_id, invocation_callback_binding_id, terminal_fact_id, source,
  correlated_legacy_entities, binding_version, state: active | closed | released_standalone,
  frozen_at?, closed_at?

InvocationTerminalCallbackBinding
  binding_id, invocation_id, mode, selected_run_id?, selected_terminal_fact_id?,
  binding_version, state: active | closed | released_standalone, closed_at?
```

The authoritative service boundary is a repository port, not `StateDB` itself:

```python
class RunRepository(Protocol):
    async def create(self, spec: RunCreate) -> RunRecord: ...
    async def attach_plan(self, run_id: str, plan: ExecutionPlan) -> RunRecord: ...
    async def transition(self, transition: RunTransition) -> RunRecord: ...
    async def finalize(self, terminal: RunTerminalOutcome) -> RunRecord: ...
    async def attach_session(self, link: RunSessionLink) -> None: ...
    async def get(self, run_id: str) -> RunRecord | None: ...
    async def list(self, query: RunQuery) -> RunPage: ...
```

The two lifecycle-looking methods are convenience façades over an injected `LifecycleService`;
their implementations may not validate or write status independently.

`RunCreate`, resolved policy, harness plan, definition version, plan, transition, and terminal
outcome are immutable Params. Accumulating counters and in-flight runtime data are DataClass
contexts. Public HTTP/MCP DTOs are generated/materialized adapters. Store rows do not become
`Element` merely to inherit an ID.

The repository rejects unresolved `Unset` before persistence. Secret values are never embedded in
inputs, policy, plan, or harness snapshots; they are represented by redacted capability/reference
records whose resolution belongs to the authorized runtime.

### D4 — Session is reusable state related many-to-many to Run

A Session owns conversation state, branches, progressions, and messages. It may exist before a
Run, be reused by a resumed or follow-up Run, or be shared as read context. A Run may fail before
creating a Session or may coordinate several worker Sessions. Therefore both cardinalities are
many-to-many and optional:

```text
Run 0..* <---- run_sessions ----> 0..* Session
```

`run_sessions` records at least:

- a closed role such as `primary`, `worker`, `context`, or `output`;
- a stable ordinal for display, never inferred from creation timestamps;
- attach/detach times when relevant;
- the explicit Session ID.
- access mode (`exclusive_writer` or `read_only`), the attached Session revision/checkpoint, and
  the writer-lease identity/expiry when mutable access is granted.

A Run may have at most one `primary` Session, but it may have no primary Session when compilation
or provisioning fails. Operation attempts may point at the Session on which they executed.
Session does not copy Run status, terminal reason, cost, or policy. Run summaries may aggregate
Session message and branch counts through a named projection, but those aggregates are not stored
as competing lifecycle truth.

The current `sessions.run_id` column becomes a compatibility/read-model field during dual-write.
New relationships are written to `run_sessions`; after cutover the scalar column is removed or
generated as a lossy “most recent/primary Run” projection whose semantics are explicit. No new
business logic may depend on that scalar for cardinality.

At most one nonterminal Run holds the exclusive writer lease for a Session revision. A concurrent
Run must attach the Session read-only or fork a new Session/revision. Every message/progression
mutation compares the attached revision or lease token; stale writers fail with a typed conflict.
Lease expiry permits takeover only through a recorded recovery transition, never by silent
last-writer-wins.

### D5 — definitions and plans are immutable, content-addressed execution inputs

An **ExecutableDefinition** is an immutable version of what the user asked the runtime to execute:
an agent, play, flow, fanout, show play, workflow graph, coding-agent task, or inline SDK
definition. A mutable name or editor document points to a current version; a Run never points
only to the mutable name.

Every admitted Run records exactly one definition version. An ad hoc prompt or direct command is
wrapped as an immutable inline definition version rather than escaping provenance. Its semantic
hash uses ADR-0119 canonical serialization and excludes secrets.

Presentation metadata is separate from execution semantics. Canvas position, viewport, color,
and collapsed state may be versioned with the authored document but do not change the semantic
definition or plan hash. A change to executable nodes, edges, conditions, tools, policy references,
or inputs does.

An **ExecutionPlan** is the immutable result of resolving a definition for one Run's inputs,
policy, target capabilities, and compiler version. It records:

- the definition version and semantic hash;
- compiler identity and version;
- resolved, redacted input hash;
- executable graph/steps and declared outputs;
- target and capability requirements;
- the canonical plan hash.

The Run exists before the plan. Compilation success attaches one content-addressed base plan
exactly once. Compilation failure terminalizes the Run as failed and leaves `plan_id` absent while
retaining compiler and error evidence. Reactive expansion is represented only as an ordered,
append-only chain of content-addressed `PlanDelta` values. Each delta identifies its predecessor,
compiler/version and cause, and contributes to an `active_plan_hash`; every `OperationAttempt`
binds the active hash it executed. Replay applies deltas in ordinal/hash-link order and rejects a
gap, fork, duplicate ordinal, or changed predecessor. Silent in-place mutation and a second
"versioned mutable plan revision" representation are forbidden.

### D6 — Engine and model-turn contexts borrow the canonical identity

`Engine` remains an execution strategy. Its current mutable `EngineRun` is renamed
`EngineRunContext` and receives `run_id` from the caller. It may own semaphores, in-flight tasks,
budgets, deduplication, Session references, and callbacks, but it does not mint or persist a new
public ID.

The existing `engine_runs` store becomes a transitional engine-telemetry projection keyed by the
canonical `run_id`, then folds into the Run, plan, and operation-attempt entities. If an engine is
used as one node inside a larger Run, its internal context is identified by that node's
`operation_attempt_id`, not by creating a child Run. A child Run is created only when the parent
explicitly delegates an independently governed attempt with its own policy/target boundary.

Likewise, the current operation called `run()` is conceptually a `ModelTurn`. Its compatibility
method name may remain for the SDK, but internal lifecycle envelopes use the canonical `run_id`
when a Run exists and always carry an `operation_attempt_id`. Current `RunStart`, `RunEnd`, and
`RunFailed` turn signals are deprecated in favor of unambiguous turn/operation names; an adapter
may emit the old names during compatibility.

The rule for nested orchestration is:

- ordinary flow nodes, engine stages, model turns, and tool calls are OperationAttempts in the
  current Run;
- a separately admitted worker with an independent permission snapshot, execution target,
  budget, or resumable outcome is a child Run linked by `relation="delegation"`;
- object nesting, subprocess creation, or provider session creation alone never implies a Run.

### D7 — RunWorkspace is a resource projection, never lifecycle authority

`RunDir` is renamed and narrowed into `RunWorkspace`. It resolves state, checkpoint, stream, and
artifact locations for an already-created `run_id`. `WorkspaceLease` owns provisioning,
acquisition, expiry, release, and cleanup for local worktrees, containers, and remote workspaces.

```python
class RunWorkspace(Protocol):
    run_id: str
    workspace_id: str
    backend: str

    async def write_projection(self, projection: RunExport) -> None: ...
    async def write_checkpoint(self, checkpoint: Checkpoint) -> ArtifactRef: ...
    async def store_artifact(self, artifact: ArtifactInput) -> ArtifactRef: ...
```

`run.json` becomes a versioned export/cache of `RunRepository`, not a peer authority. Directory
existence, branch files, buffer files, mtimes, and artifact presence never decide Run status or
timestamps. A missing workspace does not mean a missing Run; a present workspace does not mean a
live Run. Remote URI and content-addressed artifact stores are first-class, so no canonical DTO
requires a local path.

Workspace creation happens after the Run record. Provision failure terminalizes that Run.
Workspace cleanup is post-terminal work: it records lease outcome and attention when cleanup
fails, but it cannot change the already-committed execution outcome. Filesystem import remains a
compatibility adapter governed by D11.

### D8 — schedules and jobs are control handles linked to attempts

A **ScheduleOccurrence** answers when and why a schedule fired. A **Job** or task-application
handle answers admission, dependency, queue, lease, worker ownership, dispatch, and control-plane
retry. A **Run** answers what happened during one admitted execution attempt.

The relationships are explicit:

```text
ScheduleDefinition 1 --> 0..* ScheduleOccurrence
Invocation         1 --> 0..* Job
Job                1 --> 0..* Run
ScheduleOccurrence 1 --> 0..* Run
```

- an occurrence rejected or skipped before admission has zero Runs;
- a lease retry before execution may retain the same Job and still have zero Runs;
- once execution is admitted, every actual retry receives a new `run_id`;
- a chained schedule occurrence records its occurrence parent; Run lineage records attempt
  ancestry separately;
- kill/pause/resume commands resolve a typed resource before acting and never guess whether an
  unqualified ID belongs to a Session, schedule occurrence, Job, or Run.

The physical fate and eventual rename of `schedule_runs` is an ADR-0118 migration decision. During
compatibility, its ID remains a schedule/job handle and a nullable canonical `run_id` or relation
row links it to the attempt. Its current queue status is not copied into Run terminal state.

### D9 — resume, retry, replay, fork, and delegation create lineage, not mutation

A terminal Run is never reopened. Any operation that executes again creates a new Run and records
one typed lineage edge:

```text
resume      continue from checkpoint and optionally reuse the same Session
retry       repeat after a failed/timed-out/cancelled attempt
replay      intentionally execute the same definition and inputs again
fork        execute from prior state with changed inputs, policy, definition, or target
delegation  independently governed child attempt created by a parent Run
```

`RunLineage(child_run_id, parent_run_id, relation)` is immutable, acyclic, and unique for the
child's immediate parent. A cached `root_run_id` may be emitted as a derived projection but does
not replace the edges. The child stores its own definition, plan, policy, harness, target, usage,
artifacts, and terminal outcome even when some hashes match the parent.

Session reuse is an explicit option. Resume may attach the same Session, create a forked Session,
or start without one according to the definition contract. Reusing a Session never reuses the
parent `run_id`. Checkpoints are immutable artifact references with source Run and operation IDs;
copying checkpoint bytes does not transfer identity.

### D10 — one repository produces backend- and frontend-owned projections

The canonical application service queries `RunRepository` and its related repositories, then
emits two versioned read models from the same declared schema:

```text
RunSummaryProjection
  identity, definition label/kind, lifecycle, health, time, usage,
  invocation/lineage references, aggregate counts, project/tags

RunDetailProjection
  RunSummaryProjection + definition/plan references, policy/harness disclosure,
  operation graph and attempts, Session links, messages projection,
  artifacts/verifications, workspaces, control capabilities, audit reasons
```

Summary and detail share an identical root Run projection; detail is an extension, not a
filesystem-shaped sibling. Lifecycle status comes from Run. Process/workspace health remains a
nullable live diagnostic and never overwrites status. Counts identify their source and may be
eventually consistent; monetary cost retains unknown-versus-zero semantics.

Python wire models are materialized from the declared contracts and TypeScript clients for Studio
and VS Code are generated from the versioned API schema. Hand-authored duplicate `RunSummary`,
`RunDetail`, and VS Code `Run` field definitions are deleted after equivalence tests. The frontend
may derive presentation state, but it does not reinterpret file mtimes, Session status, or engine
status as Run lifecycle.

The target routes are typed by resource:

```text
GET /api/v2/runs/{run_id}
GET /api/v2/runs/{run_id}/operations
GET /api/v2/runs/{run_id}/sessions
GET /api/v2/sessions/{session_id}
GET /api/v2/schedule-occurrences/{occurrence_id}
```

The current `/api/runs` and MCP/CLI/VS Code surfaces remain compatibility adapters until their
clients migrate. A control response always returns the canonical `run_id` plus any Invocation,
Job, occurrence, Session, and lineage references; no layer calls all of them `id` without a type.

### D11 — preserve historical access without fabricating history

Migration applies an evidence rule: **a historical Run is created only when the source records an
actual attempt boundary and stable Run ID**. The importer records source kind, source identifier,
schema version, approximation flags, and the immutable raw evidence hash.

| Historical evidence | Migration behavior |
|---|---|
| explicit CLI `run.json` with stable `run_id` and a correlated attempt boundary | import a legacy-source Run; preserve unknown fields and approximation markers |
| explicit engine row correlated to an already-imported Run | attach engine telemetry; do not mint a second Run |
| Session with a trustworthy explicit `run_id` and corroborating attempt record | attach through `run_sessions` |
| Session without a trustworthy attempt boundary | keep as Session and expose a legacy Session projection; create no Run |
| `schedule_runs` row | keep as occurrence/job compatibility record and link only when an actual Run is evidenced |
| directory, branch file, buffer, or mtime alone | expose as legacy file evidence; do not infer lifecycle or mint a Run |

Compatibility resolution uses a typed alias registry or explicit legacy adapter. New foreign keys
never point at an alias. Exact canonical Run IDs win only in the Run namespace; ambiguous
unqualified IDs return a typed ambiguity error instead of search-order behavior.

The v1 `/api/runs/{id}` adapter may continue accepting legacy Session IDs while clients migrate.
Its response must disclose `identity_kind`, `canonical_run_id` when one exists, and the legacy
source ID. Any retained `run_id=<session-id>` field is explicitly a v1 compatibility alias and is
never persisted as a canonical Run. The v2 contract does not repeat that fiction.

Historical `started_at` or `ended_at` inferred from a filesystem mtime is marked approximate and
never silently promoted into a canonical measured timestamp. Missing definition, policy, target,
served-model, cost, plan, or outcome data remains unknown. The migration is allowed to preserve
fewer Runs than the old UI displayed; it is not allowed to invent one Run per Session merely to
keep a count unchanged.

### D12 — workflow compilation leaves Studio, and Run precedes schema freeze

The canonical compiler is an SDK/orchestration application service below all composition roots.
It accepts an immutable `ExecutableDefinitionVersion`, resolved inputs, policy/target capability
descriptions, and explicit definition resolvers; it returns an `ExecutionPlan` or a typed compile
error. It imports no Studio, CLI, MCP, StateDB, provider implementation, or frontend module.

Studio owns canvas editing and maps its authoring document into the canonical definition contract.
Node positions and other presentation metadata remain in a separate projection. CLI, MCP, SDK,
and Studio all call the same compiler, so the same definition/input/compiler tuple produces the
same plan hash regardless of ingress surface. State and provider access occurs through injected
ports, never imports hidden inside the compiler.

Acceptance of this record is a prerequisite for freezing ADR-0118's **target** entity registry,
not for capturing or compiling its legacy-baseline snapshot. ADR-0118's target logical entity
vocabulary must include the accepted Run model and must stop using the present table population
as proof that Run is not an entity. Implementation is coordinated as follows:

1. ADR-0123 defines logical records, identity, cardinality, and invariants.
2. ADR-0119 supplies deterministic declarations and hashes.
3. ADR-0058 supplies the already-accepted entity-agnostic initialization, transition, CAS, and
   callback mechanics; its Run policy and production creation wiring land only after the Run table
   in step 6 exists.
4. ADR-0121 supplies versioned policy/harness/evidence references when that execution profile is
   enabled. ADR-0123 stores those references opaquely and does not depend on ActionExecutor in
   order to define Run identity or support a low-level SDK operation.
5. ADR-0122 supplies legal package edges and optional adapter loading.
6. ADR-0118 registers and materializes the persistence entities and migration; Gate 1 then
   registers the Run lifecycle policy and wires RunRepository creation through ADR-0058 before
   dual-write.

No implementation issue may freeze physical names or delete a compatibility path until these
records agree on that sequence.

### D13 — Invocation callback mode selects one public source per Run during cutover

ADR-0095's current v1 callback remains a projection of legacy lifecycle entities; canonical Run
introduces a v2 envelope whose `entity.kind` is `run`. The **first Run creation transaction**
atomically freezes the Invocation's callback mode, creates its
`InvocationTerminalCallbackBinding`, creates the Run, mints its fact ID, and creates its
`RunTerminalCallbackBinding`:

- `legacy_invocation_v1_single_run` permits at most one Run. When that Run is created, the
  Invocation binding atomically records its Run/fact ID; the Invocation terminal transition emits
  the selected v1 fact, while the Run and other correlated legacy transitions suppress public
  callback emission.
- `canonical_run_v2` permits one or many Runs. The Invocation terminal transition is an
  aggregate control fact and is suppressed; every winning Run terminal transition emits its own
  v2 fact. This mode requires all required consumers to advertise v2 support before the first Run.

If any of those first-Run writes fails, all four logical records/updates roll back, leaving the
Invocation with no mode/binding and standalone-v1 semantics. Every subsequent canonical Run mints
one opaque `terminal_fact_id` and atomically creates its `RunTerminalCallbackBinding` with the Run
before any compile/provision work that can fail. Its source is derived from, and must agree with,
the immutable Invocation mode. A second Run under a
legacy-single-run Invocation is rejected before creation with
`LegacyCallbackProfileMultiplicityError`; it cannot reuse an already-terminal Invocation event.
An Invocation configured for automatic retry or concurrent Run expansion must therefore choose
canonical-v2 readiness before its first Run. If readiness is unavailable, automatic retry is
reported unavailable; an operator may submit a new Invocation rather than silently changing
identity or callback semantics.

An Invocation that never reaches `ALLOW`, or whose first-Run transaction rolls back, never freezes
this mode or creates a binding; its terminal projection remains standalone legacy-v1
compatibility. Mode selection is part of admitted Run creation, not ingress denial/deferral
semantics.

Additional per-Run legacy refs may be attached by guarded binding-version CAS, but each ref must
commit before that entity enters a terminal-capable dual-write path; it cannot be discovered after
terminal observation. The Run binding freezes against further attachment when finalization starts.
A unique active-binding constraint applies to those per-Run legacy refs, not to the shared
Invocation binding. Read-only or historical RunSession links remain many-to-many but have no
callback authority. Runs created before these bindings exist remain standalone legacy behavior and
do not enter canonical dual-write. Historical imports emit neither source.

Closing a binding retains its immutable ledger marker; absence of an active row is never treated as
proof that the entity was never canonical-bound. A later standalone reclassification requires an
audited `StandaloneLegacyRelease` tied to the entity generation and every prior binding. Without
that release, a late or recovery transition stages attention/no emit. Binding/decision retention
is coupled to the source transition and terminal-delivery retention gates in ADR-0095.

This creation order covers failures before Session creation: legacy-single-run mode emits through
its selected Invocation, while canonical-v2 mode emits through its Run transition and suppresses
the one aggregate Invocation transition regardless of Run cardinality. Compile failure,
provisioning failure, and cancellation while `preparing` are acceptance fixtures for both modes.

Every bound terminal command carries the exact Run- or Invocation-binding ID **and expected
binding version** into ADR-0058's
`TerminalProjectionParticipant`. Inside the owner transaction, that participant validates the
binding kind/version, entity membership, Invocation mode, optional canonical Run ID, fact ID,
selected source, and transition role, then stages an immutable emit/suppress decision beside `status_transitions`. LifecycleService
always stages lifecycle audit; after commit it offers the registry only a decision selecting the
current transition. It never infers binding from the lossy `sessions.run_id`, RunSession search
order, timestamps, or route aliases. Missing, stale, multiply-active, or ambiguous binding in a
canonical cohort stages typed attention and emits nothing; it cannot fall back to standalone v1.

The v2 envelope uses the canonical Run `status_transitions.id` as `event_id`, carries the Run's
separately minted durable `terminal_fact_id`,
`schema="lionagi.run-terminal"`, `schema_version=2`, `entity={kind:"run", id:run_id}`, and retains
typed nullable legacy correlation. Reconciliation and delivery acknowledgement use
`(terminal_fact_id, consumer)`, independent of wire serialization or retry. There is no generic
v2-to-v1 downgrade: v1 cannot represent `entity.kind="run"`, and a Run need not have one unique
legacy entity. A rollout may select `canonical_run_v2` only after every required consumer for that
Invocation cohort advertises v2 support; otherwise it may use
`legacy_invocation_v1_single_run` only when admission guarantees one Run. Optional consumers
that are not v2-ready receive no fabricated legacy event. A source retry therefore redelivers the
same fact instead of creating a second logical callback.

The cutover gate injects terminal races for every legacy entity correlation and proves: one winning
Run CAS; audit rows on both sides where dual-write requires them; one and only one selected public
source; stable terminal-fact identity across retries; required-consumer v2 readiness; and no
callback when an imported record is merely materialized. It includes one legacy entity linked to
multiple historical/read-only Runs and proves only the exact active callback binding participates.
It also covers a one-Run legacy Invocation, attempted second legacy Run, sequential automatic
retry, and concurrent multi-Run expansion; the latter two succeed only in canonical-v2 mode.
Only after that matrix passes may the default for new Runs become
`canonical_run_v2`.

## Consequences

### Positive

- Studio, CLI, MCP, VS Code, schedules, and SDK orchestration can point to one attempt ID.
- A Session can be reused across retries and resumes without rewriting history, while a Run can
  coordinate many Sessions without nesting ad hoc JSON.
- Compile and provision failures become visible, queryable Run outcomes.
- Engine, scheduling, workspace, and model-turn state stop competing for the word `run`.
- Policy, target, compiler, served-model, cwd, cost, artifacts, and verification evidence attach
  to the attempt where they can be audited together.
- Frontend types derive from backend contracts, removing stale filesystem/Session distinctions.
- The workflow compiler becomes usable without Studio and respects ADR-0122 optional boundaries.
- ADR-0118 can generate one coherent physical schema instead of canonizing the current collision.

### Costs and risks

- This is a structural migration across CLI, StateDB, Studio, frontend, VS Code, engines,
  scheduling, lifecycle callbacks, and resume paths; dual-write and equivalence periods are
  required.
- A many-to-many Run/Session relation makes some current queries more explicit and may require
  indexes and aggregation read models.
- Keeping old IDs resolvable while refusing to invent historical Runs produces a temporary union
  of canonical and legacy projections.
- A Run record created before compilation increases the number of short failed attempts. That is
  intended evidence, but retention and UI filters must account for it.
- Generated clients require a release process and schema-compatibility checks.
- Separating schedule/job retry from execution retry will reveal code that currently assumes one
  status machine; those call sites must choose an owner rather than receive a compatibility
  default.

## Migration plan

### Gate 0 — approve contracts and capture current behavior

Before production changes:

1. obtain architectural approval for ADR-0119, ADR-0122, and this record, and reconcile its
   policy/harness reference boundary with ADR-0121;
2. accept and verify ADR-0058's entity-agnostic mechanics: typed initialization/transition APIs,
   atomic row-plus-audit creation semantics in its test adapter, guarded companion-field patch/CAS,
   and the entity-agnostic `TerminalProjectionParticipant` seam. Gate 0 does not register a Run policy, wire a production
   Run creation path, or require a Run table before this ADR defines those contracts;
3. amend ADR-0118 so target-registry freeze depends on the accepted Run vocabulary while its
   legacy-baseline compiler remains independently testable;
4. capture fixtures for agent, play, flow, fanout, show-play, workflow, engine, schedule, resume,
   crash, cancellation, timeout, partial/degraded, and pre-Session failure paths;
5. inventory every source and consumer of `run_id`, `session_id`, `engine_runs.id`,
   `schedule_runs.id`, `RunDir`, `run.json`, and `/runs/{id}`;
6. characterize current URL/CLI/MCP/VS Code prefix and ambiguity behavior;
7. freeze no new `sessions` lifecycle columns unless they are an explicit compatibility
   projection.

### Phase 1 — introduce logical contracts and repository ports

- Define immutable Invocation, Run, definition-version, plan, operation-attempt, Session-link,
  lineage, workspace, transition, and projection contracts using ADR-0119.
- Define `RunRepository`, definition repository/compiler, workspace, and projection ports below
  CLI/Studio/MCP.
- Generate Python/OpenAPI/TypeScript compatibility fixtures without routing production traffic.
- Add forbidden-import tests proving the contracts and compiler do not import optional adapters or
  composition roots.

### Gate 1 — materialize the target store before durable traffic

ADR-0118 must compile the complete `TargetSchemaManifest`, prove its authored target change set,
apply the entire preapproved per-variant migration plan, re-introspect the store as
`ReadyReadWrite`, then register the accepted Run policy and wire RunRepository creation through
ADR-0058 `initialize_in_transaction` against that materialized table. The exact callback-binding
store/unique active-owner constraint and projection participant also land here. Atomic initial row/audit,
companion-field terminal CAS, and D13 callback-source fixtures must pass; only then is the target
eight-policy projection activated (while the legacy facade remains six) before any
composition root dual-writes canonical Runs. An arbitrary Run-table subset of one target is not a
writable schema epoch. Contract objects, in-memory repository tests, API schema generation, and
shadow projections may land earlier; durable writes may not target partial tables or fall back to
stuffing the new model into legacy Session columns.

### Phase 2 — create canonical Runs and dual-write evidence

- At each composition root, admit an Invocation and create the Run before compile/provision.
- Write canonical Run transitions alongside existing Session/manifest/engine records.
- Shadow-read new summary/detail projections and compare normalized semantics with current API
  fixtures.
- Record every divergence with source ownership; do not make the new projection copy an old
  contradiction merely to reach byte equality.

### Phase 3 — normalize execution internals

- Introduce `run_sessions` and move Session attachment behind the Run service.
- Rename `EngineRun` to `EngineRunContext`, inject canonical IDs, and stop engine ID minting.
- Record model turns, flow nodes, engine stages, and tool invocations as OperationAttempts.
- Add canonical Run and operation IDs to observation/delivery envelopes through ADR-0120.
- Replace `RunDir` lifecycle writes with `RunWorkspace` projections and WorkspaceLease evidence.

### Phase 4 — move compilation and migrate projections

- Move the workflow compiler below Studio and adapt the Designer authoring schema to the canonical
  definition contract.
- Route Studio, CLI, MCP, and SDK execution through the same definition and plan service.
- Serve versioned Run summary/detail projections and generated Studio/VS Code clients.
- Keep v1 aliases and legacy Session projections while telemetry proves client migration.

### Phase 5 — separate control handles and lineage

- Link schedule occurrences and jobs to canonical Runs without sharing identities or status
  machines.
- Make retry/resume/replay/fork/delegation create new Runs and persist typed lineage.
- Migrate control, notification, artifact verification, attention, and audit consumers to
  canonical references.
- Reject ambiguous unqualified control IDs; preserve explicit legacy aliases read-only.

### Phase 6 — migrate evidence and delete competing authorities

- Import only historical attempts that pass D11's evidence matrix.
- Mark approximate/unknown fields and retain reversible source mappings.
- Switch the Run API and new-Run callback-source default to the canonical repository only after
  D13's suppression/deduplication matrix passes; existing Runs retain their frozen source.
- Remove lifecycle inference from files, Session-as-Run projection code, engine-owned IDs,
  hand-authored frontend Run contracts, duplicate workflow persistence setup, and obsolete schema
  fields after parity and rollback gates pass.
- Let ADR-0118 generate the final physical schema and delete transitional dual-write columns and
  views only after deployed compatibility telemetry reaches the agreed threshold.

## Acceptance and merge gates

The implementation is complete only when all of the following are automated:

1. **Boundary matrix:** agent, play, flow, fanout, show-play, workflow, coding-agent, and scheduled
   execution each create exactly one root Run per admitted attempt; ordinary model turns and
   engine stages create OperationAttempts, not Runs.
2. **Pre-execution failure:** plan compilation, harness-policy compilation, and
   workspace-provision failures leave a queryable failed Run with no invented Session or plan;
   failures before an admissible immutable RunSpec remain Invocation failures.
3. **Terminal race:** completion, cancellation, timeout, and crash recovery racing concurrently
   commit exactly one terminal outcome; repeated finalization is either idempotent for the same
   evidence or a typed conflict.
4. **Terminal immutability:** property tests reject every terminal-to-* transition and mutations to
   final outcome, reason, usage, policy, plan, and end time.
5. **Session cardinality:** one Run can attach multiple Sessions; one Session can attach to parent
   and resumed Runs; a pre-Session failure attaches none; list/detail queries return stable roles
   and ordinals.
6. **Lineage:** retry, resume, replay, fork, and delegation mint distinct IDs, preserve immutable
   parents, reject cycles, and expose the same lineage in Python, HTTP, MCP, Studio, and VS Code.
7. **Engine identity:** `EngineRunContext` never mints a public ID, and engine telemetry correlates
   through canonical Run/operation IDs.
8. **Workspace independence:** deleting, moving, or failing cleanup of a workspace cannot change
   Run status; remote workspaces require no local path; manifest exports round-trip without
   becoming authoritative.
9. **Control separation:** schedule/job lease retries before execution create no Run; execution
   retries create new Runs; occurrence/job status cannot overwrite Run status.
10. **Projection parity:** summary fields are identical between list and detail roots, API schemas
    generate both frontend clients, and no handwritten frontend Run field list remains.
11. **Compiler determinism:** the same definition, inputs, policy/target capability description,
    and compiler version produce the same plan/hash across CLI, MCP, Studio, and SDK and across
    `PYTHONHASHSEED` values.
12. **Import boundary:** importing the minimal SDK and compiler loads no Studio, CLI, MCP,
    SQLAlchemy, aiosqlite, or concrete provider module; optional adapters fail with the typed
    missing-feature contract from ADR-0122.
13. **Historical truth:** fixtures for Session-only, manifest-only, correlated, engine-only, and
    schedule-only history produce exactly D11's results and never invent timestamps, costs,
    models, outcomes, plans, or Run IDs.
14. **Evidence fidelity:** unknown cost differs from zero; requested model differs from served
    model; verifier `not_recorded` differs from success; cwd and policy/target snapshots identify
    their source.
15. **No bypass:** every route or command offering Run history, resume, retry, kill, pause, or
    steering resolves a typed canonical or legacy reference through the application service; no
    caller joins raw tables or searches the filesystem to decide identity.
16. **Schema order:** ADR-0118's `LegacyBaselineRegistry` generates SQLAlchemy `MetaData` that
    reproduces pinned production `MetaData` exactly; divergent authored/deployed forms remain
    separate `LegacyPhysicalVariant` fixtures.
    The distinct `TargetSchemaManifest` contains accepted logical Run records. Declaration diff
    must equal `AuthoredTargetChangeSet`; a live physical diff is only a candidate and may execute
    only when its recognized-variant plan digest equals the preapproved `AuthoredMigrationPlan`.
17. **One lifecycle writer:** RunRepository transition/finalize calls reach LifecycleService once;
    direct status SQL and an independently evaluated Run policy fail static/runtime bypass tests.
18. **Legacy outcome matrix:** every ADR-0035 kind/status, plus uncorrelated manifest, engine, and
    schedule-only fixtures, maps exactly to the table above with stable CLI success and no invented
    Run.
19. **Session writer exclusion:** two nonterminal Runs cannot hold writable attachment to the same
    Session revision; read-only sharing, explicit fork, stale revision, lease expiry, and recovery
    each have deterministic tests.
20. **Reactive replay:** base plan plus ordered deltas yields the same active hash across processes;
    missing, reordered, forked, or mutated deltas fail, and each OperationAttempt retains the hash
    it actually used.
21. **Callback source cutover:** legacy-source, canonical-v2-source, in-flight rollout, consumer
    retry, terminal race, pre-Session compile/provision failure, and historical-import fixtures
    prove a Run emits through at most one
    public source, deduplicates by version-independent terminal-fact identity, refuses a canonical
    cohort until required consumers support v2, and never loses either authoritative transition
    audit. One-Run legacy, rejected second legacy Run, sequential automatic retry, and concurrent
    expansion prove that every 1→N Invocation uses canonical-v2 mode before its first Run.

## Issue relationships and implementation slicing

This ADR is the decision umbrella, not a request to merge the entire migration as one change.
Existing issues should be rescaled into these owned slices:

- **Run lifecycle authority and transition correctness:** #2068, #2077, #3018, #3201, #3202,
  #3204.
- **Cost, outcome, provenance, and machine-checkable evidence:** #2232, #2389, #2390, #3112,
  #3118, #3127, #3128, #3189, and the Run-projection half of cwd provenance in #3197.
- **Run/Session reachability, hierarchy, graph, and operation attempts:** #2582, #3051, #3111,
  #3119.
- **Scheduler audit and control-handle separation:** #3134.
- **Liveness, terminal races, crash/orphan disposition, and post-terminal attention:** #2535,
  #2576, #2658, #2748, #2755, #2979.

Related issues remain owned by adjacent records rather than being silently absorbed here:

- notification signature and delivery ordering (#2969, #2978) follow ADR-0120;
- MCP/tool enforcement provenance (#2664, #2956, #3026), the collection/enforcement half of cwd
  provenance (#3197), provider recovery (#2932, #2935), and escalation semantics (#2888, #2924)
  coordinate with ADR-0121;
- child-process cancellation (#3129) remains a standalone runtime-correctness fix and does not
  block the identity decision;
- legacy stop semantics (#2578) remain compatibility work until typed control resolution ships.

After approval, the first implementation issues are: contract/fixture gate; repository and
terminal CAS; Run/Session association; engine/model-turn normalization; workflow compiler move;
projection and generated-client cutover; schedule/job link separation; lineage/resume cutover;
historical import; and deletion of competing authorities. Each issue must name its shadow-read or
compatibility gate and the exact old authority it deletes.

## Alternatives considered

### A1 — keep Session as Run

Rejected. It makes Session reuse and multi-Session orchestration contradictory, cannot represent a
failure before Session creation, and continues to attach attempt policy/outcome to conversational
state.

### A2 — use Invocation as Run

Rejected. Denied or deferred ingress is not execution, and one Invocation may create multiple
retry/resume attempts. Combining them either loses rejected-request audit or forces terminal Runs
to reopen.

### A3 — use `schedule_runs` or a generic Job row as Run

Rejected. Queue, dependency, lease, dispatch, and pre-admission retry state has a different
lifecycle and can exist without execution. A generic control handle would preserve the collision
under a less specific name.

### A4 — use EngineRun as the canonical Run

Rejected. Not every attempt uses an Engine, an Engine can be one node inside a larger attempt,
and its current context owns mutable execution mechanics rather than durable product identity.

### A5 — keep the filesystem manifest authoritative

Rejected. It excludes remote/no-workspace execution, cannot provide transactional terminal
updates or normalized queries, and encourages mtime/status inference. The manifest remains a
valuable portable export and recovery artifact.

### A6 — allocate a new Run for every model turn or graph node

Rejected. That produces an unusably fine identity, breaks the product-level graph, and turns
ordinary nested execution into accidental orchestration lineage. Turns and nodes are
OperationAttempts unless separately admitted under an independent governance boundary.

### A7 — backfill one Run per historical Session

Rejected. It produces tidy counts by inventing boundaries, plans, and outcomes the system did not
record. Compatibility projections are less aesthetically uniform and more truthful.

### A8 — move the Studio compiler into CLI

Rejected. CLI is another composition root and optional surface. The compiler belongs below both,
accepts ports, and emits a domain plan without web, command, store, or provider dependencies.

### A9 — freeze ADR-0118's target registry to the current tables, then add Run later

Rejected. A legacy-baseline compiler is useful and required for compatibility proof, but treating
that baseline as the target spends the schema migration budget preserving a known conceptual error
and forces an immediate second registry/schema migration. Logical ownership must precede target
generation.
