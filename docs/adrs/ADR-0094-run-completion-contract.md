# ADR-0094: Run Completion Contract and Machine-Consumable Orchestration

**Status**: Proposed
**Date**: 2026-07-05

## Context

Every lionagi run — an agent session, a play, a flow, a scheduled job — already persists a
durable record: a status, a machine-readable terminal reason code (ADR-0029), and an artifact
verification verdict. This state layer is the strongest property of the orchestration stack:
a run's outcome can be answered from a queryable row rather than inferred from a process exit
code or a log tail.

Programmatic consumers cannot yet consume that record through a stable surface. A harness that
launches a run and needs to act on its completion today triangulates: poll an output file,
watch a directory, re-invoke a status command and parse a human-oriented table, or read the
state database directly. Each of those channels can disagree with the others, and none of them
carries the full answer (terminal status AND reason AND where the artifacts are) in one place.
The existing wait facility (`li monitor run`) covers scheduled runs only, and its output line
carries status and exit code but neither the reason code nor an artifact location. The result
is a recurring failure class: a consumer treats "process ended" or "status says completed" as
"work is done and evidence exists", when the record itself knows better (`completed_empty`,
`failed.missing_artifact`).

This ADR defines the one machine contract for run completion, the output discipline that makes
it parseable, and the integrity invariants the state layer must uphold for the contract to be
trustworthy.

## Decision

Introduce a single purpose-built completion verb, **`li wait <id>...`**, as the only
machine-contract-bearing completion surface. It blocks until each named run reaches a terminal
state and emits exactly one stable, versioned line per run on stdout. Existing observation
commands (`li monitor`, `li monitor run`, status subcommands) are unchanged; they remain human
dashboards and carry no machine contract.

### 1. State-as-truth

The run record is the source of truth for outcome. A run's completion is defined by its
persisted terminal status, its terminal reason code, and its artifact verdict — never by
process exit alone. Orchestration built on the record beats process-exit signaling because the
record distinguishes outcomes the exit code cannot (completed with verified evidence, completed
empty, failed with missing artifact, cancelled externally).

### 2. The completion contract

`li wait <id>...` accepts one or more run identifiers of any kind (agent session, play, flow
run, scheduled run), resolves each to its record, blocks until terminal, and emits per run one
tab-delimited line on stdout:

```text
<run_id>\tstatus=<terminal_status>\treason=<reason_code>\tartifact_dir=<run_dir>\texit_code=<n>
```

This grammar is **frozen** by this ADR:

- `run_id` — the canonical identifier the run was invoked with.
- `status` — the persisted terminal status.
- `reason` — a code drawn from the existing closed reason vocabulary
  (`lionagi/state/reasons.py`, `VALID_REASON_CODES`). No new outcome strings may be invented at
  the CLI layer. Because the reason surfaces the artifact verifier's verdict,
  `run.completed_empty.no_evidence` and `run.failed.missing_artifact` are distinct, parseable
  outcomes — "terminal" and "evidence exists" stop being conflated.
- `artifact_dir` — the run directory (the directory containing the run's manifest). Consumers
  resolve specific artifact paths from the manifest inside it. The wait verb never hard-codes
  any run kind's internal artifact layout.
- `exit_code` — the numeric exit status recorded for the run, when the kind records one.

Normative requirements on the implementation:

- The verb MUST apply uniformly across run kinds via a per-kind resolver and per-kind terminal
  predicate, each derived from that kind's own persisted status set. Terminal-state definitions
  live with the record schema, not in the CLI.
- Chain-following MUST be kind-aware: scheduler success/failure chains apply to scheduled runs
  only. Other run kinds have no such chains and none may be synthesized for them.
- The existing `li monitor run` output MUST remain byte-identical for its current scope. No
  fields may be added to its line; the contract lives on `li wait` alone.
- The verb is poll-backed now and subscription-compatible later: a future push-backed mode
  (e.g. `--subscribe` over the dispatch layer, ADR-0092) MUST emit the identical line, making
  the poll-to-push migration invisible to consumers. Freezing the grammar here is what makes
  that migration safe.
- Internally the CLI verb is a thin shim over a reusable waiting core
  (`wait_for_terminal(ids) -> outcomes`), so other surfaces can consume completion without
  shelling out.

Acceptance for the implementing change: a consumer blocks on a play run id and an agent
session id (neither a scheduled run) and receives the terminal line with a correct reason code
and a resolvable `artifact_dir`; a completed-empty run yields
`reason=run.completed_empty.no_evidence` rather than a bare `status=completed`; scheduled-run
waiting through the existing command is byte-identical to before.

### 3. Machine-consumer output mode

Stdout is the contract; stderr is for humans. Under a machine-output mode (a flag and an
equivalent environment variable), diagnostic warnings, progress lines, provider deprecation
notices, and other advisory output MUST be suppressed or routed to stderr so that a
programmatic consumer reading stdout receives contract lines and nothing else. `li wait`
launches with this discipline; other commands adopt it as they are touched.

### 4. Integrity floor

The contract is only as trustworthy as the record. The following are gating invariants, not
ranked backlog items — the completion verb must not ship without them holding:

- **Terminal states are write-protected.** The state layer currently exposes a transition
  validator that no write path calls, so an errant writer can move a run from a terminal
  status back to `running`, silently corrupting the record the contract reports. Observed
  consequences of this class include terminal statuses oscillating when two writers with
  different views reconcile against each other.

  **Proposal (decision requested at gate): enforce at write, do not delete.** Transition
  validation moves into the single database write path for status changes: a disallowed
  transition (any terminal → non-terminal, or any pair outside the declared transition table)
  is rejected loudly at the write, and every rejection is recorded. Deliberate operational
  repair remains possible through an explicit override that records actor and justification in
  the existing status-transition audit trail. Rationale: deleting the dead validator would keep
  the invariant unenforced and merely remove the misleading API; advisory validation is the
  worst of both worlds (cost of the API, protection of neither). Enforcement at the write
  chokepoint is small, testable, and turns the crown-jewel claim ("the record is truthful")
  from a convention into a property.
- **Status writes are race-free at the storage layer.** Concurrent writers must not be able to
  interleave a stale status over a newer terminal one; the write path must guard on the
  current status it believes it is transitioning from.

### 5. Compatibility

Additive only. The new verb introduces no breaking change to any existing command, output
format, or persisted schema consumer. Runs produced before this ADR are waitable if their
records carry a terminal status; missing reason codes surface as an explicit unknown rather
than an invented value.

## Consequences

**Positive**

- One greppable, documented surface (`li wait`) retires file-drop polling, log-tail scraping,
  and direct state-database reads as completion channels.
- False "done" becomes structurally detectable: the reason code carries the artifact verdict,
  so a consumer cannot mistake `completed_empty` for verified completion without ignoring the
  contract.
- The frozen grammar decouples the contract from both the human dashboard (free to keep
  evolving) and the future push transport (free to land later without a consumer migration).
- Enforced transitions convert the state layer's headline property from "true in practice"
  to "true by construction".

**Negative**

- A new top-level verb to document and maintain, overlapping in mechanism (not in contract)
  with the scheduled-run wait facility.
- Poll-backed waiting holds a process open per waiter until the push mode exists.
- Write-path transition enforcement can reject writes from existing code paths that relied on
  being able to overwrite terminal states; those paths must be found and fixed as part of the
  implementing change, which widens its test surface.

## Alternatives Considered

| Alternative | Why Rejected |
|-------------|--------------|
| Extend `li monitor run` with reason and artifact fields | Adding fields to an already-parsed line breaks existing consumers; overloads one command with three per-kind terminal/child models; couples a stable contract to a fast-evolving human dashboard. Its polling machinery is reused; its surface is not. |
| Event subscription over the dispatch layer (ADR-0092) now | Right long-term shape, wrong dependency today: dispatch is an early-stage subsystem and gating completion on it is schedule risk. Kept as the named migration target; the frozen line grammar makes the swap invisible. |
| Library helper only, no CLI verb | The consumers are shell harnesses; a Python-first helper serves nobody without the shim, and the shim is the verb. Retained as internal structure (thin CLI over a reusable core), not as the surface decision. |
| Delete the unused transition validator | Removes a misleading API but leaves terminal states writable — the record stays corruptible and the contract untrustworthy. Enforcement at the write is comparably small and buys the invariant. |
| One omnibus integration document (contract + all operational recipes) | Every recipe edit would re-open review of the frozen contract. The contract changes rarely and is gated; per-surface recipes iterate freely (see References). |

## References

- ADR-0029 — artifact verification verdicts and reason codes on the run record.
- ADR-0092 — durable dispatch outbox; named migration target for a subscription-backed wait.
- `lionagi/state/reasons.py` — the closed reason-code vocabulary (`VALID_REASON_CODES`).
- Recipe docs (iterate without re-gating this ADR): `docs/cookbook/reliable-review-runs.md`,
  `docs/cookbook/reliable-multi-leg-runs.md`, `docs/cookbook/reliable-recurring-runs.md`,
  `docs/cookbook/reliable-artifact-production.md`.
