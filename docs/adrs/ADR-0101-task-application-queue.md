# ADR-0101: Task-Application Surface & Durable Queue

**Status**: Accepted
**Date**: 2026-07-08

Activates: ADR-0061 (universal scheduler — queue columns, lease engine, concurrency
policy), ADR-0062 (state-machine spec — lifecycle statuses, entity-agnostic
`transition()`), building on shipped ADR-0092 slice 1 (CAS core: `transitions.py` +
dispatch outbox).
Related: ADR-0063/ADR-0065 (task board — an operator UI projection that consumes this
entity's transition events, not a competing execution entity), ADR-0027
(schedule_runs), ADR-0098 (resident engine — a warm, long-lived process is a natural
worker in this ADR's pull model; its in-process task-source seam is complementary),
ADR-0102 (workflow library registry — composes at the `library_ref` seam).

## Context

Today every orchestration entry point has a different shape and none of them is a
durable queue:

- Schedules are trigger-anchored: a "run once" still requires a stored schedule row,
  and `schedule_runs.schedule_id` is `NOT NULL` with `ON DELETE CASCADE`
  (`lionagi/state/schema.sql`), so no run can exist without a parent schedule.
- Ad-hoc submission (`POST /api/launches/`) admits via an in-memory
  `asyncio.Semaphore` (`lionagi/studio/services/launches.py`) — no pending state, no
  durability; a capacity reject is just "retry later", and queued intent does not
  survive a daemon restart.
- The generalized lifecycle and queue mechanics are already designed in ADR-0061
  (queue columns `queued_at`, `leased_by`, `lease_expires_at`, `concurrency_key`,
  lease-based workers, concurrency policy) and ADR-0062 (statuses `queued ·
  waiting_dependency · running · retry_wait · completed · failed · timed_out ·
  cancelled · skipped`, entity-agnostic `transition()` with idempotency dedup) — both
  Proposed and unbuilt before this ADR: its acceptance activates them (see the
  Activation section), while their implementation remains pending. A working slice of the engine shipped with ADR-0092:
  `lionagi/state/transitions.py` (guarded CAS + `status_transitions` audit) currently
  scoped to `_ENTITY_TABLES = {"dispatch": "dispatch_outbox"}`, with
  `lionagi/dispatch/outbox.py` proving claim/lease/backoff/dead-letter in production.
- No worker/seat registry exists anywhere in the tree, and every launcher hardcodes a
  local subprocess spawn even though `SandboxBackend`'s `ExecutionTarget.kind`
  already includes `daytona`/`remote_agent` (`lionagi/tools/sandbox_backend.py`).

Goal: ALL orchestration submitted as a task application — one shape
(submit → queue → claim → execute → terminal) for every action kind, portable to
remote execution later without the submitter changing.

## Decision

### D1. The frozen submit shape

`TaskApplication` (Pydantic, the contract every binding shares):

```text
action_kind        # existing vocab: agent | flow | fanout | play | flow_yaml
                   # (launcher also accepts engine, and playbook as a legacy
                   # alias for play); this ADR pair ADDS workflow for
                   # registry-resolved definitions (ADR-0102) — a CHECK widen,
                   # not a rename of any existing kind
library_ref        # "namespace/name@version" | null   (seam into ADR-0102)
required_capabilities: list[str]                       (D4)
execution_target   # host | local_worktree | daytona | remote_agent | process
args               # action-kind-specific payload (existing action_args shape)
idempotency_key    # optional, per ADR-0062 dedup
```

Bindings, all of this one shape: `li task submit` (primary), in-process
`submit_task(TaskApplication(...))`, and later `POST /api/tasks` (HTTP is a later
equivalent binding, not the thing frozen first).

### D2. The task entity = ADR-0061/0062's run entity, decoupled from schedules

Generalize `schedule_runs` per ADR-0061's own written migration, plus the decoupling
this ADR adds:

- Apply ADR-0061's columns: `queued_at`, `leased_by`, `lease_expires_at`,
  `concurrency_key` (+ indexes as written there).
- Make `schedule_id` NULLABLE. An ad-hoc task application = a row with
  `schedule_id NULL`.
- Widen the status CHECK to the ADR-0062 lifecycle (the current CHECK lacks `queued`
  and its siblings).
- Register the entity in `transitions._ENTITY_TABLES` so ALL status movement routes
  through the ADR-0062 `transition()` + `status_transitions` audit. A second CAS
  implementation anywhere is a defect.
- New columns: `required_capabilities JSON`, `execution_target TEXT`,
  `library_ref TEXT`, `library_content_hash TEXT` (provenance, seam into ADR-0102).

Migration note: the NULLABLE change and CHECK widen require a SQLite table rebuild
(12-step ALTER). The migration MUST take a pre-rebuild backup of the state database
(file copy alongside, timestamped) before the rebuild transaction, and the migration
section of the implementing PR must document the rollback path: restore the backup
file and reinstall the prior schema version via `schema_meta`. The rebuild runs once,
versioned through `schema_meta`.

Schedules become triggers that ENQUEUE tasks: a schedule fire writes a `queued` row
(it already writes the row; it now starts `queued`, not `running`).
`POST /api/launches` and `li schedule trigger --once` write durable `queued` rows
instead of taking a semaphore slot; admission control = ADR-0061 concurrency policy,
not a per-daemon semaphore.

### D3. Execution = pull model; remote is additive

A worker claims a `queued` task iff it matches (D4); claim = CAS lease per ADR-0061
(`leased_by`, `lease_expires_at`); the claiming worker resolves `execution_target` to
a `SandboxBackend` and executes. v1 ships ONE local worker (the Studio daemon engine)
claiming everything it can serve. A remote worker later — including a warm resident
engine per ADR-0098 — is purely additive: it advertises its capabilities and claims
matching tasks; the submit path and the frozen `TaskApplication` shape never change.

### D4. Capability matching (minimal, first-class)

- Task rows carry `required_capabilities` (flat tokens, optionally inherited from the
  library definition's metadata at submit).
- NEW `workers` table (the only genuinely new table; no worker registry exists
  anywhere in the tree): `worker_id PK`, `advertised_capabilities JSON`,
  `execution_targets JSON`, `last_heartbeat_at`, `leased_run_id NULL`. A stale
  heartbeat (> TTL) makes a worker ineligible for NEW claims; in-flight leases still
  recover via `lease_expires_at` (unchanged ADR-0061 semantics). Rule: heartbeat TTL
  gates assignment eligibility only; lease expiry alone governs recovery of in-flight
  work.
- Each capability token has a class (small declarative config map, not a policy
  engine):
  - `eligibility` (default): subset-match routing filter.
  - `serialization` (e.g. an exclusive-GPU token): sets the task's `concurrency_key`
    to a host-scoped key (`{host}:{token}`); ADR-0061 concurrency admission then
    queues at most one such task per host. The queue ORDERS advisorily; a host-level
    advisory lock (e.g. an OS flock on a well-known lock file) that the worker takes
    before touching the resource stays AUTHORITATIVE. The queue never arbitrates
    machine locks.
  - `affinity` (e.g. a warmed-cache token): soft preference among eligible workers;
    never blocks progress.
- Match rule: worker W claims task R iff R's eligibility∪serialization tokens ⊆
  W.advertised_capabilities AND R.execution_target ∈ W.execution_targets; prefer
  affinity matches.
- Acceptance case (Lean toolchain probe task): a probe task declaring
  `["lean-toolchain", "warmed-cache"]` routes to a worker with the toolchain,
  preferring one with a warm cache, and runs concurrently with unrelated work; a
  GPU-bound benchmark declaring `["gpu-exclusive"]` serializes per host via
  `concurrency_key` with the host lock as backstop — two such tasks never execute
  concurrently.

## Alternatives rejected

- New first-class queue subsystem (parallel table + own worker loop + own claim
  protocol): duplicates ADR-0061/0062 + the shipped outbox → permanent dual-CAS
  maintenance, doubled schema-parity burden. Rejected.
- Thin separate `tasks` table instead of generalizing `schedule_runs`: avoids the
  table rebuild but creates a second run-shaped entity next to the one ADR-0061
  already wrote its migration against, and splits what ADR-0063/0065 project over.
  Rejected — the rebuild is a bounded one-time cost.
- `execution_target` selection baked into the submit path: hardcodes routing at
  submit time; every new target becomes submit-path surgery plus a change to the
  frozen contract. Rejected in favor of target-as-capability + pull.
- Capability-as-authority (grant/attenuation/revocation): out of scope; a capability
  here is a scheduling-match label only.

## Scope fence (v1 MUST NOT contain)

No parallel CAS engine or second status path; no remote worker implementation (the
protocol is merely shaped so one is additive); no external broker (the state database with
guarded CAS is the queue); no bin-packing, cost-aware placement, preemption, or
priorities beyond ADR-0061's overlap + concurrency policy; no capability
grant/attenuation; no queue arbitration of machine locks; no removal of `li schedule`
triggers; no HTTP-first contract freeze.

## Verify by

1. A Lean toolchain probe task (ordinary + gpu-exclusive variants) runs
   submit → queue → claim → terminal.
2. Two `gpu-exclusive` tasks never execute concurrently (queue key + host lock).
3. A `schedule_id NULL` task survives daemon restart still `queued` (durability).
4. A downstream CLI consumer submits one real task via `li task submit`; the
   `TaskApplication` shape is unchanged end-to-end.
5. Schema parity: `schema.sql` + `schema_meta.py` + the engine-schema and
   route-registry test expectations all updated in the same PR.
6. The migration takes the pre-rebuild backup and the rollback path is documented.

## Activation of ADR-0061 / ADR-0062

Acceptance of this ADR activates ADR-0061 and ADR-0062 as designed: both flip to
Accepted in the same change that lands this document, each with an activation note
pointing here. Amendments to their designs, if any emerge during implementation, land
as amendments to those ADRs — not as a fresh scheme. ADR-0063 remains Proposed and is
not affected.
