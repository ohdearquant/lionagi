# ADR-0107: Conclusive orphan terminal reaping

- **Status**: Proposed
- **Kind**: Aspirational (records the target state)
- **Area**: cli-surface
- **Date**: 2026-07-27
- **Relations**: extends ADR-0095, ADR-0106

## Context

The MCP background-job surface records each submitted run in
`~/.lionagi/mcp/jobs/<run_id>/job.json`. Callers do not classify the open
`status` vocabulary. They branch on two producer-owned fields:

- `terminal`: stop waiting;
- `outcome`: how the work came out, null while `terminal` is false.

Ordinary completion reaches the sidecar through the child's terminal hook.
Cancellation can instead be recovered from the lifecycle store and cached into
the sidecar. A third state has no writer today: the spawned process is gone, the
child never ran its terminal hook, and neither the sidecar nor lifecycle store
contains an end.

`lionagi/mcp/jobs.py` currently reports that state as:

```json
{
  "status": "exited",
  "terminal": false,
  "outcome": null,
  "reason_code": null,
  "possibly_orphaned": true
}
```

`job.wait` drops the run from `pending` because more waiting cannot help, but
keeps `all_terminal` false and returns it under `stopped_without_end`. A caller
that waits for `all_terminal` therefore has no successful stopping state. More
importantly, the child that normally writes the end and delivers the terminal
notice no longer exists, so a notice-only caller is never woken.

### The argument for leaving the state non-terminal

The current rule protects four real properties:

1. A PID number is not a process identity. The operating system can recycle it.
2. A failed or denied identity probe is not evidence of death.
3. `terminal` is a latch. A false positive cannot be repaired by a later read.
4. A live probe changes with observation time. Two readers of an unchanged
   record must not disagree about a latch.

Those properties reject terminalisation from a bare PID probe and reject a
purely derived terminal answer. They do not require permanent non-terminality.
The existing implementation now records process creation time and distinguishes
three findings that positively establish that this run's process is gone:

- the recorded PID held no live process at the first liveness probe;
- it disappeared between the liveness and creation-time probes;
- a live process holds the PID but its creation time differs from the recorded
  creation time.

The implementation also has one false-looking result that proves nothing:
`alive == false` with `pid_identity == "unusable_pid"` means no OS probe was
made. This case must remain advisory.

### Problems

**P1 — A run can remain non-terminal forever after its work can no longer
report an outcome.** No surviving producer can write `finished_at`, so another
wait cannot turn uncertainty into a reported result.

**P2 — `job.wait` has two stop concepts.** It stops observing
`possibly_orphaned` runs but cannot set `all_terminal`. Every caller must
interpret `stopped_without_end` as a second completion protocol.

**P3 — notification has no surviving owner.** The terminal hook both writes the
end and attempts delivery. A process that dies before the hook does neither.
Changing only `terminal` would make waiters stop while leaving the notification
consumer broken.

**P4 — the liveness vocabulary is unsafe as a terminal predicate.** The
conclusive set is currently expressed by excluding `"unusable_pid"` from the
`alive == false` cases. That is a negative discriminator over an open vocabulary:
a future inconclusive value would silently become terminal. `None` is also
overloaded, and the docstring's description of `"gone"` does not match the first
dead-PID return path.

**P5 — the sidecar write primitive is atomic but not a transaction.**
`_write_job` prevents torn JSON, but concurrent read-modify-write operations can
lose one another. A terminal hook, notification result, PID attachment, lifecycle
cache, and orphan observer must not overwrite a terminal fact or delivery result.

### Decisions

| Concern | Decision |
|---------|----------|
| How a conclusively gone unreported run becomes terminal | D1: the observer persists an idempotent orphan-reap transition before returning it as terminal |
| What evidence is conclusive | D2: one positive internal `liveness_conclusion` names conclusive process loss; unknown remains advisory |
| How the result is represented | D3: `outcome="lost"` and `reason_code="process_gone_without_outcome"` are distinct from reported failure |
| How the transition is attributed | D4: the sidecar records source, observation time, and bounded evidence |
| How concurrent writers behave | D5: every sidecar mutation uses one per-run serialization discipline and first terminal fact wins |
| What wait/list/status mean | D6: all three may trigger the same reap; a reaped run is terminal and contributes to `all_terminal` |
| How notification works | D7: the observer that wins the reap also attempts the configured terminal notice exactly as the hook would |
| What happens to existing stranded records | D8: the next conclusive observation reaps them under the same contract |

**Out of scope.**

- Preventing every child crash. Parent reaping or a wrapper may reduce the
  frequency, but cannot cover parent-and-child loss or repair existing records.
- Automatic retry or resume. `lost` states that no outcome was reported; it does
  not prove that retrying side effects is safe.
- Closing `spawn_state="preparing"` from elapsed time. No process identity was
  durably acquired, so elapsed time alone is not conclusive.
- Changing the open `status` vocabulary into a caller-owned enum.
- Reworking the lifecycle-store orphan coordinator in ADR-0095. This ADR governs
  the MCP job sidecar and its public derived fields.

## Decision

### D1 — Conclusive observation writes the end before returning it

`status(run_id)` remains the single classification path used by `job.status`,
`job.list`, and `job.wait`. When that path finds an otherwise-unended started
job and D2 positively concludes that its process is gone, it invokes one
idempotent sidecar operation, conceptually:

```python
reap_orphan(
    run_id: str,
    *,
    finding: Literal["pid_absent", "disappeared_during_probe", "pid_recycled"],
    observed_at: str,
) -> ReapResult
```

`reap_orphan` serializes mutation, rereads the current sidecar, and writes only
if all of these remain true under the mutation guard:

1. the record exists and belongs to `run_id`;
2. `spawn_state == "started"`;
3. `finished_at is None`;
4. no terminal lifecycle end was cached;
5. the supplied finding is one of D2's three positive conclusions.

The winner persists D3 and D4 in one publication. A loser rereads and returns
the terminal fact already present. `_derive` continues to make `terminal` true
from a recorded end, not from a live observation. Thus the first reader may
cause a transition, but it never returns a terminal answer derived from an
unchanged non-terminal record. The second reader reads the durable transition.

This deliberately permits a read operation to write. `status()` already writes
when it discovers and caches a lifecycle-store end. The relevant boundary is
not HTTP-style command/query purity; it is whether terminality is a durable,
attributable fact before a caller acts on it.

The losing property is read-path purity. We choose to lose it rather than lose
stable terminality (derive-time F1), prompt waiter completion (periodic F3), or
one stopping protocol (advisory F4).

### D2 — Positive liveness conclusion, not exclusion

The implementation introduces a closed internal conclusion used for decisions:

```python
LivenessConclusion = Literal["alive", "process_gone", "unknown"]

@dataclass(frozen=True)
class ProcessLiveness:
    alive: bool
    conclusion: LivenessConclusion
    finding: str
```

Exactly these observations produce `conclusion == "process_gone"`:

| Finding | Required observation | Why conclusive |
|---------|----------------------|----------------|
| `pid_absent` | the recorded PID is askable and `_pid_alive(pid)` is false | no live process holds the number, so this run's process cannot be running |
| `disappeared_during_probe` | the PID was initially live, then the create-time probe returns no process | the process ended between two reads |
| `pid_recycled` | a live PID's creation time is readable and does not match the creation time recorded at spawn | the live process is a different identity |

The following are not conclusive:

| Observation | Conclusion |
|-------------|------------|
| PID is missing, boolean, non-numeric, or otherwise unaskable | `unknown` |
| liveness or creation-time access is denied or unreadable | `unknown` unless the independent PID-existence probe already established absence |
| a live PID has no recorded usable creation time | `alive` for waiting purposes, identity unverified |
| `spawn_state == "preparing"` | outside orphan reaping; advisory only |

No transition predicate may be written as `pid_identity != "unusable_pid"`, as
membership in a negative set, or as `not alive` alone. Only
`conclusion == "process_gone"` admits D1.

The public `alive` and `pid_identity` fields remain available with their current
meanings for compatibility. The public response additionally exposes
`liveness_conclusion`, with the three values above. The old `pid_identity`
docstring is corrected in the same change, but no existing emitted value is
silently redefined.

### D3 — A distinct `lost` outcome and reason

The persisted and returned lifecycle fields are:

```json
{
  "status": "exited",
  "terminal": true,
  "outcome": "lost",
  "reason_code": "process_gone_without_outcome",
  "possibly_orphaned": false
}
```

`outcome="lost"` means: the process identity is conclusively gone and no
authoritative work outcome was reported. It does not mean the work failed. The
work may have completed its intended effect before dying; the producer has no
evidence either way.

`outcome="failed"` remains reserved for a reported terminal status classified
as failure, including a caught spawn failure. Callers may retry `failed` under
their existing policy. They must not automatically retry `lost`, because an
unreported external side effect may already have committed.

`reason_code="process_gone_without_outcome"` names why the result is lost. It is
not a failure reason and must never be mapped to `_outcome_for`. The status
`"exited"` remains an open, displayable producer value; callers continue to
branch on `terminal`, `outcome`, and `reason_code`, not on that string.

### D4 — Attribution is durable and additive

The winning transition writes:

```json
{
  "finished_at": "2026-07-27T23:42:10.123456+00:00",
  "terminal_source": "mcp_orphan_reaper",
  "terminal_evidence": {
    "kind": "process_identity_conclusively_gone",
    "finding": "pid_recycled"
  }
}
```

`finished_at` is the time the end was established and recorded, not a claim
about the unknown instant at which the process exited. It satisfies the
existing recorded-end latch and answers when the transition was made.

`terminal_source` answers what made the transition. Existing hook-produced,
kill/lifecycle-cached, and spawn-failure records also populate this additive
field when written or next rewritten:

| Source | `terminal_source` |
|--------|-------------------|
| child terminal hook | `cli_terminal_hook` |
| lifecycle end cached by observer | `lifecycle_cache` |
| producer-caught spawn failure | `spawn_failure` |
| conclusive orphan observer | `mcp_orphan_reaper` |

`terminal_evidence` is deliberately bounded. It records the classifier version
if versioning is introduced and the named finding; it does not copy argv,
environment, logs, notification payloads, or secrets. PID and creation time
remain in their existing top-level fields and need not be duplicated.

No existing field can answer both what and when without changing its meaning:
`finished_at` can carry when, but `status` must remain open and
`reason_code` explains the run result rather than the authority that wrote it.
The additive `terminal_source` is therefore required.

### D5 — Sidecar mutations are serialized and terminal is first-writer-wins

Atomic `os.replace` remains the publication primitive, but all sidecar
read-modify-write mutations share a per-run serialization mechanism. The
critical section includes reread, invariant check, merge, and replace.

The implementation may use the repository's supported per-file locking
primitive or an equivalent compare-and-retry mechanism. It may not protect only
the orphan writer: PID attachment, spawn failure, terminal hook,
`record_notify_delivery`, lifecycle caching, and orphan reaping must participate
in the same discipline or prove non-overlapping field merges.

Within the guard:

- an existing `finished_at` wins and is never replaced;
- a reported hook or lifecycle end that arrived first keeps its reported
  `status`, `outcome`, and `reason_code`;
- an orphan reap that arrived first remains `lost`; a later child-hook process
  cannot overwrite it, but its delivery attempt may fill an absent
  `notify_delivery`;
- notification updates merge only `notify_delivery` and cannot roll terminal
  fields backward;
- PID attachment cannot replace any terminal or notification field.

This rule makes concurrent observers idempotent and prevents a late stale write
from un-terminalising a record.

### D6 — Status, list, and wait share the terminal contract

All three public readers resolve through `status()` and therefore may perform
D1 once:

| Consumer | Existing branch | Required behavior |
|----------|-----------------|-------------------|
| `job.status` | displays and returns lifecycle fields | returns the durable `lost` terminal state and attribution fields |
| `job.list` | calls `status()` per entry | may reap; includes `terminal`, `outcome`, `reason_code`, `finished_at`, and `terminal_source` |
| `job.wait` | polls `status()` and computes set fields | returns reaped entries as terminal; they are neither `pending` nor `stopped_without_end` |

`all_terminal` means every valid requested run has a durable terminal end,
including `outcome="lost"`. It does not mean every run succeeded or reported an
outcome. A caller determines aggregate success from each entry's `outcome`.

`stopped_without_end` remains additive compatibility output for inconclusive
advisory cases only. A positively conclusive orphan is reaped before wait
aggregation and never appears there. `timed_out` remains true only when a
non-terminal run remains worth polling.

### D7 — The reap winner owns terminal notification

The transition winner attempts the same configured terminal delivery that the
child hook would have attempted, substituting:

```json
{
  "run_id": "<run_id>",
  "status": "exited",
  "label": "<label-or-kind>",
  "target": "<configured-target>",
  "sender": "<configured-sender>"
}
```

Delivery happens after the terminal record is durably published and outside the
sidecar mutation lock. Its result is merged back through D5 as
`notify_delivery`. A delivery failure cannot undo or recategorize the terminal
transition.

The configured command is already recorded on the sidecar for per-run
overrides. Resolution must also preserve the same project/global settings
semantics the hook would have used. The observer must not invent a second
notification configuration path.

Concurrent readers can race after the transition and before delivery. Only the
reader whose guarded operation returns `won_transition=true` may initiate the
delivery. Other readers return the durable terminal state. Crash after the
terminal write but before delivery remains visible as absent
`notify_delivery`; notification is best-effort, and ADR-0106 D9 still requires
reconciliation by reading state.

This is tension with the ideal that a terminal transition and its wake-up are
indivisible. They are not indivisible today either: the terminal hook records
first and delivers second. The mitigation is attribution plus a durable,
inspectable delivery outcome, not a false exactly-once promise.

### D8 — Existing stranded records are reaped on their next observation

There is no bulk migration. An existing record with `spawn_state="started"`,
no recorded end, and a D2 positive finding is closed by the next
`job.status`, `job.list`, or `job.wait` call. It receives current observation
time and `terminal_source="mcp_orphan_reaper"`.

An existing record with an unusable PID, inconclusive access, or
`spawn_state="preparing"` remains non-terminal. The change fails toward
advisory, as the current implementation does.

This retroactive behavior is safe because the transition records exactly what
the new observer established. It does not rewrite historical records merely
because they are old.

## Consumers audit

| Consumer / contract | Current dependency | Result under this ADR | Same-PR change |
|---------------------|--------------------|-----------------------|----------------|
| `job.status` | canonical source of `terminal` and `outcome` | safe after D1-D5; gains `lost`, attribution, and liveness conclusion | implementation and public schema/docs |
| `job.list` | delegates to `status()` but omits orphan and delivery detail | safe to terminalise; projection must include `terminal_source` so attribution is not hidden | projection and tests |
| `job.wait` | stops polling advisory orphans but keeps `all_terminal=false` | simplifies to one durable stop condition for conclusive cases | aggregation, compatibility field, and tests |
| terminal notice / `notify_delivery` | child hook owns both transition and delivery | **breaks if only terminality changes**; dead child cannot notify | D7 observer delivery and failure-path tests |
| lifecycle-store cancellation cache | a read may cache a terminal lifecycle end | safe; its recorded end wins races and gains source attribution | guarded merge and tests |
| submit PID attachment | late write rereads to preserve hook terminality | unsafe under an added concurrent observer without D5 | shared mutation discipline and race tests |
| schedule runs | MCP jobs may wrap flow/play work, but scheduler lifecycle is authoritative in StateDB | no scheduler status is inferred or rewritten; only the MCP sidecar closes | cross-surface test that scheduler-reported ends still win |
| dispatch/outbox | not used by MCP terminal-hook delivery | unchanged; D7 does not enqueue a Studio-dependent dispatch | no behavior change |
| external machine consumers | branch on `terminal` and `outcome`, status opaque | must accept additive outcome `lost`; must not treat non-success as automatically retryable | ADR-0106 contract/docs and compatibility test |

The notification row is the breaking consumer the design must address. A patch
that changes `_derive` or `all_terminal` without D7 is incomplete.

## Consequences

### Positive

- Every conclusive run end eventually becomes one durable terminal fact.
- Two readers cannot disagree about a latch on an unchanged record.
- `job.wait` regains one stopping protocol and can return `all_terminal`.
- Callers can distinguish reported failure from missing outcome.
- Existing stranded runs are repaired without a bulk migration or daemon.
- Notification has a surviving owner and an inspectable delivery result.

### Costs and accepted tensions

- Read paths can mutate a sidecar and execute a configured notification after a
  transition. This is the principal cost of choosing F2.
- Sidecar mutation must gain real concurrency control; atomic replace alone is
  insufficient.
- A read can now incur one bounded notification attempt after the durable write.
- `lost` expands the public `outcome` vocabulary. Consumers that incorrectly
  treat it as a boolean success/failure must be corrected.
- The actual process exit time remains unknown. `finished_at` records when loss
  was established.
- Best-effort notification retains a crash gap after terminal persistence.

### Constraint tensions

| Constraint | Tension | Resolution |
|------------|---------|------------|
| distinct terminal outcome | expands a public producer vocabulary | additive `lost`, documented and never mapped to failed |
| conclusive identity only | OS observations occur at read time | only the positive closed conclusion can write; unknown stays advisory |
| consumers audit | notification is not repaired by terminality alone | D7 makes it part of the same PR |
| attributable transition | existing records lack a writer field | additive `terminal_source` plus `finished_at` and bounded evidence |

## Alternatives considered

### F1 — Derive terminality directly from conclusive liveness

This is the smallest code change and would close waiters immediately. It loses
the strongest current invariant: two observations of unchanged bytes may
disagree about a terminal latch. Attribution would also be synthetic unless a
second write were added, at which point it becomes F2. Rejected.

### F3 — A separate reaper

A single scheduled or startup sweep keeps reads pure and gives the transition
one owner. It loses promptness: a waiter remains stranded until the component
runs, and plain CLI/MCP use must not require Studio or another daemon for
correctness. A maintenance sweep may later call the same D1 primitive, but it
is not the only owner. Rejected as the primary mechanism.

### F4 — Keep non-terminal and make the advisory state loud

This preserves every existing latch invariant and avoids mutation. `job.wait`
already approximates it with `stopped_without_end`. It requires every consumer
to implement a second stop condition, leaves `all_terminal` permanently false,
and cannot wake notice-only consumers. The new positive identity evidence makes
that permanent ambiguity unnecessary. Rejected.

### F5 — Guarantee that the child or parent records every end

A parent reaper or wrapper reduces the number of stranded jobs and should be
considered independently. It cannot handle parent-and-child loss and cannot
repair records already stranded when the change ships. Rejected as sufficient;
compatible as defense in depth.

### Persist `failed` with an orphan reason

ADR-0095 uses canonical failed/blocked lifecycle states for its StateDB
coordinator. The MCP machine contract has an explicit `outcome` dimension and
the binding requirement here is to distinguish no report from reported
failure. Persisting `outcome="failed"` would invite automatic retry of work
whose external effects are unknown. Rejected for this sidecar contract.

## Implementation fences

### MAY

- Reuse one pure liveness classifier across `status`, `kill`, and future sweep
  paths, provided only its positive `process_gone` conclusion admits reaping.
- Add a maintenance sweep that calls the same guarded `reap_orphan` operation.
- Preserve `stopped_without_end` for backward-compatible reporting of genuinely
  inconclusive stopped-looking records.
- Use locking or compare-and-retry for sidecar mutation if the chosen mechanism
  proves the D5 interleavings.

### MAY NOT

- Terminalise from `not alive`, elapsed time, missing PID, denied access, or a
  negative test against an open finding vocabulary.
- Return `terminal=true` before the durable transition is published.
- Map `lost` to `failed`, success, cancellation, or automatic retry.
- Overwrite an earlier reported terminal end with an inferred lost outcome.
- Let notification delivery happen under the sidecar mutation lock or roll
  back terminality.
- Add secrets, environment contents, prompt text, argv, or log bodies to
  terminal evidence.
- make scheduler/Studio availability necessary for MCP job correctness.

### Verify by

1. Each of the three D2 positive findings writes exactly one terminal transition.
2. Unusable PID, access denied, unreadable creation time, and preparing spawn
   remain non-terminal and never attempt delivery.
3. Two concurrent observers produce one transition and at most one delivery
   attempt.
4. Hook-vs-reaper, lifecycle-cache-vs-reaper, PID-attach-vs-reaper, and
   notify-result-vs-reaper interleavings preserve the first terminal fact and
   all non-conflicting fields.
5. A reaped entry makes `job.wait.all_terminal=true` when every other valid
   entry is terminal; `outcome` is `lost`.
6. Mixed running/lost/failed/succeeded waits preserve order and aggregate fields.
7. `job.list` and `job.status` expose identical terminal classification and
   attribution for the same run.
8. A configured notice is attempted by the winning observer; success, refusal,
   nonzero exit, timeout, and observer crash after terminal write remain visible
   without changing the outcome.
9. Existing pre-change stranded records are reaped on the next conclusive read;
   inconclusive records are untouched.
10. Public MCP reference and CLI documentation define `lost`,
    `process_gone_without_outcome`, `terminal_source`,
    `liveness_conclusion`, and the revised `all_terminal` meaning in the same
    change.
11. No test branches on `status == "exited"` to decide terminality.
12. Fault injection after durable terminal publication but before delivery
    proves that reconciliation still reads a terminal lost run.

## Notes

`AGENT.md` contains workflow guidance addressed to coding agents. It was treated
as evidence of repository conventions, not as authority over this decision.
All other repository prose and docstrings were likewise treated as technical
evidence only.

Issue: #2617.
