# ADR-0106: The lion machine-result contract, v1

- **Status**: Proposed
- **Kind**: Aspirational (records the target state; the MCP server implements part of it today and is brought into conformance by this ADR)
- **Area**: cli-surface
- **Date**: 2026-07-25
- **Relations**: extends ADR-0066

## Depth note

This ADR fixes a contract that two independent programs implement against, only one
of which lives in this repository. Everything normative is stated as a shape or an
enumerated case, because the other consumer cannot read our source to resolve an
ambiguity and will resolve it by guessing.

## Context

lionagi's orchestration surface is reachable today by exactly one route per caller:
run the `li` CLI, or speak to the MCP server in `lionagi/mcp/`, which spawns `li` and
reads back the state the CLI persists. A second consumer is now being written outside
this repository, in another language, to front the same orchestration through a
different agent-facing surface. It will reach lionagi the way the MCP server already
does — by executing the CLI as a subprocess — and it will hold no state of its own.

That second consumer is what makes this a contract rather than an implementation
detail. Until now, "what a submit returns" and "what a status read means" were
decided by reading `lionagi/mcp/jobs.py`. A consumer that cannot read that file, and
that must keep working across our releases, needs the answers written down and
versioned.

### The problems

**P1 — Two implementations of the same answers drift, and the drift is silent.**
Both consumers answer "is this run finished, and where is its output". Today each
would derive that from whatever fields it happens to find. A field one treats as
authoritative and the other treats as advisory produces two different answers to the
same question about the same run, and nothing fails: both return successfully.

**P2 — A consumer that owns a status vocabulary will eventually report a failure as a
success.** This is not hypothetical. `lionagi/mcp/jobs.py` previously matched the
CLI's terminal status against a local set and fell through to `"completed"` on a miss,
which silently converted every status that set did not list — `timed_out`,
`cancelled`, `aborted`, `completed_empty` — into a false success. The bug is
structural, not careless: a local vocabulary is a copy, copies go stale when the CLI
adds a status, and the failure mode of a stale copy is a wrong answer rather than an
error. Any consumer given the freedom to classify will re-acquire this bug.

**P2b — But "did it end" is not "did it succeed", and publishing only the first
recreates P2 one level up.** `completed_empty` is terminal and is not a success.
A consumer told to branch on a terminal flag, and forbidden from reading status, has
no way to distinguish them and will either invent the forbidden vocabulary or treat
every terminal run as done-and-fine. The producer therefore has to publish both facts,
not one.

**P3 — The reading consumer holds no state, so every question must be answerable from
lionagi's store alone.** The external consumer's own process may restart between a
submit and the status read that follows it. If any part of the answer lives only in
the consumer's memory, the answer changes across that restart, which makes a run's
recorded history depend on the health of a process that is not running it.

**P4 — A subprocess consumer has exactly two channels and they are not
interchangeable.** stdout carries the machine result; stderr carries diagnostics.
Today the CLI's human-facing surfaces mix progress, warnings and results across both,
because the reader was a person. A consumer that parses stderr, or that parses stdout
containing an unannounced progress line, breaks on any change to logging.

**P5 — A consumer built against one version of the contract will be run against
another, and the substitution can happen while it is alive.** lionagi releases
independently of its consumers. Worse, the consumer's own binary is replaced routinely
during operation, and an absolute path pins the path, not the build behind it. A
version established once at startup is a claim about a file that may since have been
replaced.

**P6 — Absence and failure look identical in a naive shape.** "No artifacts" and "the
artifacts directory could not be read" both compress to an empty list. "Not finished"
and "finished with no output" both compress to a null. A consumer cannot tell a fact
from a failure to establish one, and the safe reading is not knowable from the value.

**P7 — A broken lionagi installation is not a failed run, and the subprocess boundary
erases the difference.** A consumer sees a nonzero exit and no result. If the
environment cannot start `li` at all, attributing that to the submitted work is how a
four-hour outage gets recorded as a series of crashed agents.

**P8 — A push notification cannot be the only way a consumer learns a run ended.**
Delivery can fail, and the record of its failure lives in the state a consumer that is
not polling is by definition not reading. Nothing wakes it to learn that nothing will
wake it. A consumer restart across a successful delivery loses it the same way.

### Decisions

| Concern | Decision |
|---------|----------|
| What the contract covers and who owns it | D1: lionagi owns the contract; it is a CLI-level machine surface, not the MCP server's Python API |
| Version negotiation | D2: `contract_version` is an integer, checked on every envelope, not only at a handshake |
| The envelope every machine call returns | D3: one envelope shape — `ok`, `contract_version`, `data`, `error` |
| Run status | D4: `status` is opaque and verbatim; the producer also publishes `terminal` and `outcome`, on every status-bearing response |
| Submit | D5: submit returns a handle; the spawn phase is recorded rather than inferred from a missing pid |
| Reads | D6: one lifecycle authority for every path; liveness is advisory; in v1 an orphaned run stays non-terminal |
| Distinguishing absence from failure | D7: every read-derived field carries its own availability and reason |
| Process-level faults | D8: a valid envelope is authoritative; exit status is the transport-level answer, with a defined precedence |
| Terminal notification | D9: a notification is a prompt to read state, never proof and never the only path |
| Bounded observation | D10: `wait` is bounded, returns partial results, and takes ADR-0066 D6 with two marked extensions and one clause D6 leaves open decided here |

Out of scope:

- **The verb set khive exposes.** The pack curates which verbs it surfaces and under
  what names; that is its decision, not ours. This ADR fixes what a verb *returns*.
- **How the consumer reaches the binary** (sandboxing, per-call timeout budgets).
  Owned by the consumer; see D1 on why we nonetheless state a requirement about the
  binary path.
- **The MCP tool surface itself.** ADR-0066 decides that. This ADR constrains what
  those tools return, which is a strictly smaller question, and D10 takes ADR-0066 D6
  as written rather than re-deciding it, marking its two additions as additions and
  saying plainly where D6 underdetermines a rule that an implementation must nonetheless
  settle.
- **Hosted / multi-tenant concerns.** No tenancy appears in this contract; a hosted
  join is built above it, not inside it.
- **Playbook and flow semantics.** What a flow *does* is unchanged here.

## Decision

### D1 — lionagi owns the contract, and it is a CLI-level surface

The contract is a property of the `li` executable, not of `lionagi.mcp`. Both the
in-repo MCP server and any external consumer obtain results the same way: by running a
CLI command that emits a machine result on stdout.

**The contract surface.** Machine mode is explicit, never inferred:

```text
li <command> ... --machine
```

Under `--machine`, a command writes exactly one JSON object to stdout and nothing else,
sends all human-facing output to stderr, and does not colour, paginate, or prompt.

**Why a CLI flag rather than the MCP server's Python API.** The external consumer is
not written in Python and cannot import our modules. Making the MCP server's functions
the contract would mean the external consumer's contract is a re-implementation of the
Python surface in another language, which is exactly the drift P1 describes. Making
the CLI the contract gives both consumers the same artifact, and it makes our own MCP
server a consumer of the contract rather than its definition — which is the only way
we find out when we break it.

**A requirement we place on consumers, despite D1's ownership split.** The consumer
must invoke the binary by absolute path and never through `PATH` resolution. This is
stated here rather than left to each consumer, because a consumer that resolves `li` on
`PATH` may execute a different installation from the one it validated against. Note
that this bounds *which path* is executed and not *which build is behind it*; D2 covers
the remainder.

### D2 — Version is an integer, and it is checked on every envelope

```text
li handshake --machine
```

```json
{
  "ok": true,
  "contract_version": 1,
  "data": {
    "contract_version": 1,
    "min_supported_version": 1,
    "implementation": "lionagi",
    "implementation_version": "0.30.2",
    "module": "/abs/path/to/lionagi/cli"
  },
  "error": null
}
```

**Exact semantics.**

- `contract_version` is a single integer, incremented only on a change that could break
  a conforming consumer.
- The handshake governs **registration**: a consumer performs it once at
  initialisation and refuses to register any verb if the version is outside the range
  it understands. It does not register and then fail per call, because a surface that
  appears and then errors is indistinguishable, to the agent using it, from a surface
  whose underlying work failed.
- **Every envelope repeats `contract_version`, and a consumer validates it before
  decoding `data`.** The handshake is not sufficient on its own: the binary at the
  pinned absolute path is replaced during normal operation, so a consumer that
  handshook v1 can have its next call answered by v2. On mismatch the consumer stops
  trusting the surface — it degrades or unregisters — rather than decoding v2 data
  under v1 assumptions.
- `min_supported_version` is the oldest contract this implementation still honours, so
  a dropped old contract is diagnosable rather than mysterious.
- `implementation_version` and `module` are diagnostic. They answer "which build is
  talking" for a human reading a report; a consumer that branches on
  `implementation_version` instead of `contract_version` has re-invented version
  sniffing and will break on a patch release.

**What "additive changes are free" does and does not mean.** A new optional field, a
new value in an open vocabulary, or a new command does not increment the version, and
a consumer must ignore fields it does not recognise. But *ignoring a field is only safe
when omitting it preserves every v1 meaning.* A field that qualifies an existing value
— an encoding marker beside `console.text`, a redaction flag, a partial-result marker
— changes what the existing field means to a reader that drops it, and a v1 consumer
would present encoded or partial content as the complete result. Such a field is a
breaking change and increments the version, however additive it looks. The test is not
"is the field new" but "does a v1 consumer that ignores it still hold only true
beliefs".

**Why an integer and not semver.** A consumer's only real decision is "can I speak this
or not". A version with three components invites partial-compatibility logic, which is
another copy of our rules living in code we do not control.

### D3 — One envelope, for every machine call that reaches the dispatcher

```json
{ "ok": true,  "contract_version": 1, "data": { }, "error": null }
```

```json
{
  "ok": false,
  "contract_version": 1,
  "data": null,
  "error": { "kind": "not_found", "message": "no job with id ...", "detail": null }
}
```

**Exact semantics.**

- `ok` is the only field a consumer inspects to decide request success. It is not
  derived from `error` being null, from `data` being non-empty, or from the exit status.
- Exactly one of `data` / `error` is non-null. Both null, or both set, is malformed and
  the consumer treats it as a contract violation rather than guessing.
- `error.kind` is drawn from a closed set: `not_found`, `invalid_input`, `conflict`,
  `unavailable`, `internal`. A consumer may branch on it; new kinds require a version
  increment, which is what makes branching safe. This is deliberately the opposite of
  D4's open vocabulary, because `error.kind` describes *our* refusal to answer, which
  we control, while run status describes the outcome of foreign work, which we do not.
- `error.message` is for a human and is never parsed. `error.detail` is an optional
  object whose contents are not contractual.
- The envelope is emitted even when the command fails, because a failure producing no
  JSON is indistinguishable from a crash.
- **The scope of "every call" is every request that reaches the machine dispatcher.** A
  failure before that point cannot emit an envelope, because the code that builds one has
  not loaded and the contract version it would carry is not knowable. Those cases are
  enumerated in D8 and are diagnosed by exit status alone; a consumer must not classify them
  as malformed output. Stating this here rather than only in D8 matters because a
  cross-language consumer implementing D3 literally would turn the intended environment
  diagnosis into a protocol violation, which is the loudest possible way to report the one
  fault that has nothing to do with the request.

### D4 — `status` is opaque; `terminal` and `outcome` are the producer's derivations

```json
{
  "run_id": "20260725T175010-97f334",
  "kind": "agent",
  "label": "example",
  "status": "completed_empty",
  "terminal": true,
  "outcome": "failed",
  "reason_code": "no_artifacts",
  "alive": false,
  "pid": 41234,
  "submitted_at": "2026-07-25T17:50:10.412331+00:00",
  "finished_at": "2026-07-25T17:51:28.636751+00:00"
}
```

**Exact semantics.**

- `status` is an **open vocabulary**. lionagi may add values in any release without
  incrementing `contract_version`. A consumer records and displays it verbatim and
  **must not** map it onto a local set. The values in use at v1 are `running`,
  `completed`, `completed_empty`, `failed`, `timed_out`, `aborted`, `cancelled`,
  `killed`, `exited`, `unknown` — informative, not exhaustive; a consumer treating this
  list as closed is non-conforming.
- `terminal` answers **"stop waiting"**. True when and only when the run reached an end
  state, derived by lionagi from a recorded end — `finished_at`, a producer-written spawn
  failure (D5) — never by matching `status` against a set, and never computed by a reader.
  In v1 those are the only two sources; the deferred reconciler in D6 would be a third.
  (2026-08-03: ADR-0107 has since implemented that reconciler — its orphan reaper is now
  the third source.)
- `outcome` answers **"did the work come out right"**. It is a **closed** vocabulary,
  `succeeded | failed | cancelled | indeterminate`, and it is **`null` whenever `terminal`
  is false**. (`cancelled` added by the 2026-08-03 erratum carried by ADR-0110 D6.
  Timing, recorded because D2 makes it matter: this text froze on 2026-07-25 with three
  values; the wire began emitting `cancelled` hours later that same day and has shipped
  it since. That unversioned expansion violated D2 when it happened; this
  correction documents the wire as it ships and moves no version, because the breaking
  event was the 2026-07-25 code change, not the text catching up to it. ADR-0107's Notes
  recorded the drift.)
  Stated against `terminal` rather than against "the run is still going", because v1 has a
  state that is neither: an orphan has stopped and is still not terminal (D6), and a rule
  phrased around being in flight would leave that case undefined. Being closed, `outcome`
  may be branched on, and a new value costs a version increment. It is the producer's derivation, on the same principle as `terminal`:
  the party that owns the status vocabulary is the only party that can classify it without
  holding a copy.
- Both are needed because they are different questions and their answers diverge.
  `completed_empty` is `terminal: true, outcome: "failed"`. A consumer given only
  `terminal` must either invent the forbidden vocabulary or call every finished run a
  success, which is P2 recreated one level up (P2b).
- **`indeterminate` was reserved at freeze and is emitted today.** (2026-08-03: ADR-0107
  implemented the producer this paragraph deferred — its orphan reaper writes
  `indeterminate` with an attributable reason; the reservation strategy below worked as
  intended.) It is the value for a run that can be established to have ended but whose
  result cannot be established, and the component that produces it is the reconciler D6
  deferred and ADR-0107 then implemented. It was defined before any path emitted it, and
  deliberately: `outcome` is a closed vocabulary that consumers branch on, so
  adding a third value later would be a breaking change under D2 and would cost a contract
  version. Defining an unused value costs one sentence; introducing it in v2 costs every
  consumer an upgrade. A two-valued field would also force `failed` for the unknowable case,
  which asserts a fact from a failure to establish one — the defect D7's availability
  wrapper exists to prevent, smuggled back in through a field small enough to look like a
  boolean.
- `reason_code` is a short machine-readable qualifier for a terminal outcome, drawn
  from the same cross-kind lifecycle resolver ADR-0066 D6 uses. It is advisory: a
  consumer may surface it, and must not need it to decide `outcome`.
- `alive` reports process liveness at read time and is advisory; see D6.
- **Every status-bearing response carries all three of `status`, `terminal` and
  `outcome`** — status, wait entries, list rows, and the submit handle alike. A summary
  that carries `status` without the derivations hands the consumer the open vocabulary and
  nothing to classify it with, which is P2 again at whichever verb was left out. A response
  that genuinely cannot carry them must be documented as a handle that the consumer resolves
  through a status read before reporting any outcome.

**Why derivations rather than a closed status set.** This is P2 as a design rule. If
`terminal` and `outcome` did not exist, every consumer would need a set of terminal
statuses and a set of successful ones, both copies of ours, and a status added here
would silently become "not terminal" or "not a success" there. Publishing the
derivation instead of the inputs keeps the classification with the only party that
always has the current vocabulary.

### D5 — Submit returns a handle, and the spawn phase is recorded

```text
li agent --machine --detach [...]
li orchestrate flow --machine --detach [...]
li orchestrate fanout --machine --detach [...]
```

```json
{
  "ok": true,
  "contract_version": 1,
  "data": {
    "run_id": "20260725T175010-97f334",
    "pid": 41234,
    "status": "running",
    "terminal": false,
    "outcome": null,
    "log": "/abs/path/to/console.log"
  },
  "error": null
}
```

**Exact semantics.**

- Submit returns as soon as the child is spawned. It does not wait and reports nothing
  about the work itself.
- `run_id` is assigned before the child starts, so the id is known without polling.
- The child is fully detached: its own session and process group. This is required
  rather than incidental, because a consumer's daemon is expected to be replaced during
  a run's lifetime, and a job parented to it would die at every upgrade. Detachment
  happens in-process at spawn; there is no reliance on a `setsid` executable, which
  does not exist on every supported platform.
- **Input validation happens before anything is created on disk.** Argument-vector
  size, unreadable prompt files, and malformed parameters are rejected while the
  request still has no trace.
- **Spawn failure is a separate case and is not preventable.** The job record must be
  written before `Popen`, so the child's terminal hook always finds a record to mark;
  therefore a spawn that fails after that write leaves a record claiming `running` with
  no process that can ever terminalise it. The executable can be replaced with a
  non-executable file, the working directory can vanish, and descriptor limits can be
  exhausted, all after validation passed.
- **The spawn phase is recorded, never inferred.** The record carries `spawn_state`, and
  it rides writes that already have to happen, so no new failure mode is introduced:
  `"preparing"` in the pre-spawn write, `"started"` in the write that attaches the pid,
  `"failed"` when the producer catches the spawn error. On a spawn failure the producer
  also writes the terminal record — `terminal: true`, `outcome: "failed"`, a reason
  naming the spawn failure — and the envelope reports `ok: false`.
- **A reader must not infer spawn failure from a missing pid.** Between the pre-spawn
  write and the pid attachment, a perfectly healthy child is on disk with no pid, so a rule
  keyed on pid absence reports a run that is starting normally as terminally failed. That is
  worse than the ghost record it would be trying to remove, because a false terminal is one
  a consumer acts on. `spawn_state: "preparing"` is reported as exactly that: non-terminal,
  `outcome: null`, with no claim about the spawn's fate.
- **A `preparing` record that has gone stale is surfaced, not resolved.** Its age is
  reported, and it may be flagged as possibly orphaned, but staleness is advisory and never
  a terminal transition — a loaded machine and a dead spawn look identical from the record,
  and a bound chosen to tell them apart is a guess with a consumer belief riding on it.
  **In v1 it stays non-terminal, permanently**, and that is the contract rather than an
  omission from it; see D6.
- An earlier revision made both this and the orphan case in D6 **reader-derived**, on the
  reasoning that a fact a reader can compute should never depend on a write that can fail.
  That reasoning is right about *presentation* and wrong about *lifecycle*. A reader
  computing a lifecycle transition manufactures a durable fact out of a local observation,
  which is how one reader comes to disagree with another about the same unchanged record.
  The narrow form that survives: **a reader may derive a presentation field that is a
  deterministic function of durable authoritative facts; only the lifecycle authority may
  make a lifecycle transition.**
- Long instruction text is passed to the child in a file, written before the spawn, so
  editing it afterwards cannot change what an already-submitted run executes.

**Why submit carries no result.** Any result field on submit would be a second place
where run state lives, and it would be the one place a consumer could read without a
subsequent call — which is exactly the state P3 forbids it from holding.

### D6 — One lifecycle authority; liveness is advisory beside it

```text
li job status <run_id> --machine
li job output <run_id> --machine [--tail-chars N]
li job list --machine [--limit N] [--status S]
li job kill <run_id> --machine
```

**Exact semantics.**

- **Durable lifecycle state is authoritative.** Every terminal outcome comes from the
  persisted record, so a consumer restart between submit and read changes nothing.
  This is the operational form of P3 and is what lets the consumer be stateless.
- **Process liveness is a separate, advisory observation**, reported as `alive`. It is
  not part of the durable answer and must never override a recorded terminal outcome or
  be used to establish success. Liveness is derived by probing a pid, and a pid can be
  reused, denied, or already reaped, so two readers of the same record can legitimately
  observe different liveness. The earlier framing of reads as answerable from "the
  store alone" was an overclaim: the store alone settles the lifecycle, and liveness is
  extra information carried beside it with its own reliability.
- A run whose recorded pid is gone with no terminal recorded is an **orphan**. It is
  reported as `status: "exited"`, `terminal: false`, `outcome: null`, with `alive: false`
  and an advisory flag that it may be orphaned. In v1 it never becomes terminal.
- **Liveness may not be the fact that establishes terminality**, and an earlier revision
  had it both ways: this decision declares the pid probe advisory because a pid can be
  reused or denied, and the next paragraph then derived a terminal outcome from that same
  probe. A bare `kill(pid, 0)` identifies neither the process incarnation nor the run that
  owns it, so a reused pid makes a dead run look alive, and the same unchanged record reads
  differently from two hosts. A lifecycle transition resting on that is a transition that
  depends on who asked.
- **In v1, nothing terminalises an orphan. It stays non-terminal indefinitely.** This is a
  decision, not a gap, and it is stated here so that a consumer can plan for it rather than
  discover it. A run whose process died without its terminal hook running, and a run whose
  producer died before it ever spawned, both remain `terminal: false` for as long as their
  records exist. A consumer's bounded wait will keep returning them as pending until its own
  window closes; what it does then is the consumer's policy, and this contract does not
  pretend to make that decision for it.
- **Why not simply specify the reconciler here.** Two earlier revisions tried, in opposite
  directions, and both failed for the same underlying reason: terminalising a run you did
  not run requires evidence that a run *ended*, and neither a missing pid nor an
  unresponsive one is that evidence. Doing it properly needs a durable process-incarnation
  identity, a host boundary, and a fenced ownership protocol so two reconcilers cannot race.
  That is a distributed-systems protocol with its own failure modes, not a paragraph in a
  result contract. Naming an owner without specifying when its transition is *valid* moves
  the ambiguity from readers into an unspecified component and lets two conforming
  implementations disagree about whether the same run is over — which is the divergence P1
  describes, one level further in. The target shape is written out in the deferred section
  below.
- **The cost is real and is the right one to accept.** A visible stall is worse than
  nothing and better than every reader independently inventing a terminal state from an
  observation it cannot trust. A false pending wastes a consumer's time; a false terminal
  gets acted on.
- `kill` signals the run's whole process group. It reports what it did rather than
  raising, because "already exited" is an ordinary answer to a kill request. It reports
  **signal delivery**, not process death and not lifecycle terminality; a consumer that
  needs the run to be terminal observes it with D10 afterwards.
- `list` is ordered newest first, bounded by `limit`, and says so when truncated.

### D7 — Nothing read from disk is reported as a bare null or empty list

This is P6 made structural. Any field whose value is derived from a read that can fail
carries its own availability:

```json
{ "available": true,  "value": [],   "reason_code": null,        "detail": null }
{ "available": false, "value": null, "reason_code": "unreadable", "detail": "permission denied" }
```

**Exact semantics.**

- `available: true` with an empty value means, definitively, that there is none.
- `available: false` means lionagi could not establish the value; `reason_code` is a
  short machine-readable qualifier and `detail` is for a human.
- A consumer must not collapse the two, and specifically must not render
  `available: false` as "none" — absence of evidence presented as evidence of absence.
- **The wrapper applies to every read-derived field**, not only the obvious two. At v1
  that is: the artifacts listing, the console text, the run manifest, and the
  notification-delivery outcome. Each of these is currently collapsed to `None` or `[]`
  on a missing file, an unreadable file, and a malformed file alike, which makes a
  not-yet-created manifest and a corrupt one indistinguishable.
- It does **not** apply to fields that are intrinsically optional and whose absence is
  unambiguous, such as `label`. Wrapping those adds ceremony without removing an
  ambiguity.
- `console` additionally carries `truncated`, because a tail displayed as a whole log
  turns a partial record into an apparently complete one.

### D8 — A valid envelope is authoritative; the exit status answers at the transport level

The two signals answer different questions, and the precedence between them is stated
rather than left to the reader:

| Situation | Exit | Envelope | Consumer reads |
|-----------|------|----------|----------------|
| Request handled, succeeded or refused | 0 | present, `ok` true or false | the envelope |
| Environment cannot run the command | 78 | absent | environment fault; nothing executed |
| No envelope for any other reason | any | absent | transport fault |

**Exact semantics.**

- **A well-formed envelope whose `contract_version` the consumer accepts is
  authoritative, and the command exits 0 whenever one is emitted** — including for
  `ok: false`. A structured refusal is a successfully handled request; encoding the
  refusal a second time in the exit status creates two answers to one question, which
  is the ambiguity D3 exists to remove.
- **78 (`EX_CONFIG`)** is returned when a required module cannot be imported and no run
  has been allocated — that is, when nothing was executed. It is never returned once a
  run exists, because a run that started and then failed is a failed run, and reporting
  it as an environment fault sends the consumer away from durable state it should read.
  No envelope accompanies it, because the failure happens before the code that would
  emit one.
- **Any other exit without a parseable envelope is a transport fault**: the process
  could not be launched at all, it was killed by a signal, it timed out under the
  consumer's own bound, or stdout did not parse. These are distinct from both of the
  above and the consumer must not attribute them to the submitted work. In particular a
  launch failure cannot return 78, because no child existed to return anything.
- A consumer that sees a valid envelope ignores the exit status. A consumer that sees
  no valid envelope uses the exit status, and only then.

### D9 — A notification is a prompt to read state, never proof and never the only path

On reaching a terminal status, a run records that status and, when a delivery command
is configured, sends a terminal notice.

**Exact semantics.**

- **The notice carries no authority.** It says "go read the state", nothing more. A
  consumer that treats receipt as proof of a terminal outcome, or of a successful one,
  is non-conforming. This matches ADR-0066 D6, which already states that notify and
  bounded wait are complementary rather than competing.
- **A consumer must never rely on the notice as its only discovery path.** Delivery can
  fail, and the record of that failure lives in state a non-polling consumer is not
  reading, so nothing wakes it to learn that nothing will wake it (P8). A consumer
  restart across a successful delivery loses it identically. Reconciliation is D10's
  bounded wait, or a poll; the notice is an optimisation over that floor, not a
  replacement for it. That floor is bounded observation and not eventual resolution: an
  orphan under D6 sends no notice, because it never reaches a terminal status, and it does
  not resolve under polling either. A consumer that reads this decision as "the poll always
  gets there in the end" has the right fallback and the wrong stopping condition, which is
  what the consumer obligation on an unresolved bounded wait is for. D10 names such a run
  in the result rather than leaving it among the ids still pending, so the stopping
  condition is reachable without a timer; deciding what to do once it is reached is still
  the consumer's.
- The outcome of the delivery attempt is recorded and surfaced on `status` under D7's
  availability shape. `attempted: false` means no delivery was configured, which is a
  valid configuration and not a failure — and is a different fact from a delivery that
  was attempted and failed.
- Artifacts are written before the notice is sent, so a consumer woken by the notice
  finds the outputs already present.

### D10 — Bounded observation, taken from ADR-0066 D6, extended in two places and completed in one

`wait` takes ids, a maximum wait, and a poll interval; both numbers are clamped to
documented bounds and the effective values are echoed back. `0` is a legal snapshot
request.

**Taken unchanged from ADR-0066 D6**, which decides them for the MCP surface:

- Every successful call returns one entry per requested id, in input order, carrying
  that id's kind, `status`, whether it is `terminal`, and its `reason_code`, plus
  `all_terminal`, `timed_out`, and the list of ids still pending. A bare boolean is
  rejected: mixed outcomes are the normal case, and collapsing them forces the
  follow-up poll the call existed to replace.
- **Expiry is not an error.** A timed-out wait means the observation window closed. It
  reports what was learned, so completed ids are not discarded and a retry is safe.
- Unknown or ambiguous ids are per-id errors inside the result and never prevent the
  other ids from being observed.
- Lifecycle state is the single authority for these answers, not the MCP job sidecar,
  because a universal verb cannot pick its source of truth based on who submitted the
  work; the same id would then answer differently depending on provenance.
- **That authority is the same one `status` reads**, and this is load-bearing rather than
  incidental. An earlier revision let `status` apply a reader rule that terminalised an
  orphan while `wait` resolved the same run through lifecycle state, which still said
  running. The consumer was told by one conforming call that the run had definitely ended
  and by another that it must keep waiting, for the same id, at the same moment. Two
  normative source-of-truth rules for one question is the defect; which of the two answers
  is nicer is not the point. Every status-bearing path in this contract resolves through one
  authority, and an orphan is terminal on all of them or on none.

**Three additions this ADR makes, which ADR-0066 does not state.** They are marked as
extensions rather than folded into the list above, because presenting a new decision as an
existing one tells every reader there is nothing left to reconcile, which is the most
effective way to prevent it being reconciled:

- **Observing does not touch the run.** A wait that times out, is signalled, or whose
  caller disconnects leaves the durable run exactly as it was. Cancelling an observation is
  not cancelling the work. ADR-0066 D6 is silent on signal and disconnect, so an MCP
  implementer reading only that ADR could let request cancellation propagate into the
  operation while an external consumer assumes it cannot — the two surfaces then behave
  differently after the identical event.
- **Every entry carries `outcome`** as well as `terminal`, per D4. ADR-0066 D6's entry
  contract lists kind, status, terminality and reason code, so a conforming ADR-0066
  implementation would omit the field this contract requires for reporting a result.
- **An id that waiting cannot resolve does not hold the window open, and the producer pays
  a floor for it.** A run whose process is gone with no end recorded has stopped, and both
  writers of an end are past it, so further polling cannot change its answer. Such ids are
  returned in their own list, `stopped_without_end`, rather than in `pending`, and the call
  stops re-observing once every remaining id is either terminal or in that list. It is a
  separate list and not a per-id error, because observing them succeeded. Nothing about the
  record changes: the entry stays non-terminal with a null outcome, and a run that does
  record an end afterwards is classified terminal by the next observation exactly as
  before. `all_terminal` stays false while any id is in the list, because a run that stopped
  without recording an end is not a completed one.

  **The window was doing two jobs, and the second one stays here.** Its duration bounded the
  observation and also rate-limited a caller that keeps re-asking about a run which never
  resolves. Dropping such ids from `pending` removes the second, and a consumer looping
  until `all_terminal` with no backoff of its own would turn a visible stall into a hot
  loop, which is worse than the stall it removes. So a call that would return without having
  waited at all, while at least one id is in `stopped_without_end`, first sleeps one poll
  interval — bounded by whatever is left of the window — and observes once more. The trigger
  is a property of the observation the call is about to return, not of any history, so it
  applies on a first observation as much as a later one.

  This is a floor on the call, not a charge added to it. A call that already waited on a
  running id has met it and pays nothing extra, and `max_wait=0` is untaxed by construction,
  having no window to spend, so it remains the documented snapshot. The cost no shape avoids
  is a caller joining an already-finished batch beside one stopped id: it pays at most one
  poll interval, once per call. That is accepted deliberately rather than engineered around.
  It is bounded and small, the hazard it replaces is unbounded spin at a shared boundary,
  and that asymmetry decides it without a measurement of how often either case occurs —
  which neither party to the decision had. The extra observation is not wasted either, since
  it is exactly the interval in which a slow end-writer finishes.

  **Why the producer and not the consumer.** Both were live options and the choice is not
  obvious. Pacing enforced here is enforced once for every client, including clients written
  before this rule and clients we do not control; a documented duty to back off is honoured
  only by the clients that read it. The decisive evidence is local: this ADR's own consumer
  obligation 8 was silently disarmed by a code change while its text sat unchanged. A
  boundary whose safety depends on N independent clients continuing to behave is not a
  boundary.

The first two extensions need a forward amendment to ADR-0066 D6 to keep the two documents
in agreement; until that lands, this ADR is the stricter of the two on them and an
implementation satisfying it also satisfies ADR-0066.

**The third is a definition of something ADR-0066 D6 leaves open**, and it is the clause to
read carefully. D6 states the result as the entries plus `all_terminal`, `timed_out`, and
the list of ids still pending. It names that key and nowhere says which ids qualify for it,
so it does not by itself decide where a run that stopped without an end belongs. A reading
is available on which it does: D6 names a per-id error channel for ids that could not be
observed, and naming one exclusion can be read as ruling out others. That reading is
recorded here because it was argued seriously, not because it is adopted. It is not
adopted — the error channel is for ids observation could not resolve, while a stopped id
was observed and classified, so naming that channel does not settle the pending rule by
exclusion. The honest conclusion is that D6 underdetermines this, and an underdetermined
clause is settled by amending it rather than by either document assuming its own reading.
The forward amendment therefore states the partition outright: `pending`,
`stopped_without_end` and terminal are disjoint and exhaustive over every observed id.
That is the invariant this contract's implementation already tests, so the amendment
records a rule that is enforced rather than adding one that is not.

**Why this is taken up rather than deferred.** An earlier draft deferred bounded wait on
the grounds that timeout, signal and disconnect had no v1 answer. That was wrong about
timeout, which ADR-0066 D6 decides outright, and deferring on that basis would have left two
accepted documents directing opposite implementations — the MCP surface exposing a bounded
wait while an external consumer polled — which is precisely the divergence P1 describes,
produced by our own paperwork rather than by anyone's mistake. It was also wrong in the
opposite direction once corrected: the first revision claimed ADR-0066 answered all three,
when it answers one. Both errors ran the same way, toward reading a document as settling
more than it says, in whichever direction removed the obstacle in front of the draft.

**DEFERRED: the orphan reconciler.** The component that would terminalise a run whose
process died without recording a terminal, or whose producer died before spawning. It is
deferred rather than dropped because v1's answer — those runs stay non-terminal forever —
is a real cost that should eventually be paid off. What it needs, so that whoever builds it
starts from the constraints rather than rediscovering them:

- A durable **process-incarnation identity** written at spawn, not a bare pid. A pid is
  reused, so "pid 41234 is gone" does not establish that *this run's* process is gone. Boot
  identity plus process start time is the usual portable-enough pair.
- A stated **host boundary**. The same record read from two machines must not yield two
  answers, so a reconciler has to know which runs it is entitled to judge.
- A **fenced ownership protocol** — a lease or compare-and-set — so two reconcilers cannot
  both decide, and a stalled one cannot wake up and overwrite a newer decision.
- An explicit **eligibility predicate per spawn phase**. `started` and `preparing` are
  different problems: a `preparing` record has no child at all, so there is no incarnation
  to prove absent, and it needs a durable producer-attempt identity to be resolvable in
  principle. A reconciler specified only for `started` cannot resolve `preparing`, and
  claiming otherwise was the defect that produced this deferral.
- A decision, when it is designed, about platforms where that identity cannot be
  obtained. The constraint to carry into it is that leaving the run pending beats
  improvising weaker evidence, since weaker evidence is what the first three attempts at
  this were made of.

Everything in this list is a constraint on a future design, not a requirement on a v1
implementation. Nothing here is normative: a v1 implementation has no reconciler, and the
list exists so that whoever builds one starts from the constraints rather than
rediscovering them. Adding it later is additive under D2 only because `outcome`'s
vocabulary and the terminal fields are defined now, which is why they are.

**DEFERRED: usage and cost accounting** — tokens, duration, and whatever else a metered
consumer needs to bill a run. Not v1, and recorded here at the external consumer's request
so that it is not designed ad hoc when it is needed. Its shape is already constrained: an
additive block under D7's availability semantics, so that a failure to read the meter is
reported as unavailable and never as a zero. A metering read that fails silently to zero is
the same defect as an unreadable artifacts directory reported as "no artifacts", with the
difference that this one shows up on an invoice.

**DEFERRED: a `--machine` stream mode emitting progress events as JSON lines.** Would
let a consumer show live progress rather than a spinner. Deferred because JSON Lines is
a different framing and lifecycle protocol, and it breaks D1's "exactly one JSON object
on stdout" invariant, which is the single property that makes the current shape
trivially parseable. It should not be weakened until a consumer genuinely needs it.

## Consumer obligations

A contract that only constrains the producer is half a contract. Everything below is
required of a conforming consumer, and each one exists because the corresponding
producer-side decision is defeated without it. They are collected here rather than left
scattered through the decisions, because a consumer author needs the list of what is
being asked of them in one place.

1. **Validate `contract_version` on every envelope before decoding `data`** (D2). A
   handshake governs registration only, and the binary at a pinned path is replaced
   during normal operation.
2. **Ignore unrecognised fields** (D2). Additive change is only free if the other side
   actually tolerates it.
3. **Never map `status` onto a local set** (D4). Record and display it verbatim; branch
   on `terminal` and `outcome`.
4. **Branch on `outcome` totally, including `indeterminate`** (D4). All four cases —
   `null`, `succeeded`, `failed`, `indeterminate` — need a defined behaviour. This is the
   obligation that makes D4's reservation of `indeterminate` worth anything: the value is
   defined now so that a reconciler can be added later without a version increment, and
   that additivity is real only if consumers already have somewhere to put it. No v1 path
   emits it, so a consumer cannot find the missing branch by testing against a producer,
   and a two-way branch written against v1 behaviour looks complete for as long as v1 is
   what it talks to. Test the branch against a hand-written envelope.
5. **Never render `available: false` as "none"** (D7). Absence of evidence is not
   evidence of absence, and the whole wrapper exists to stop those sharing an encoding.
6. **Check the exit status before parsing stdout, and do not parse it at all on 78**
   (D8). Attributing an environment fault to the submitted work is the misattribution
   this contract was written to remove.
7. **Never treat a terminal notice as proof, or as the only discovery path** (D9).
   Delivery can fail, and the record of that failure lives in state a non-polling
   consumer is not reading.
8. **Have a defined policy for a run a bounded wait does not resolve** (D10, D6). Two
   shapes reach you and both need the policy: an id still in `pending` when the window
   expires, and an id in `stopped_without_end`, which comes back at once and comes back
   every time. This is the obligation created by v1's liveness choice, and it is the one
   most likely to be skipped, because most runs resolve and the case looks like an edge.
   It is not an edge: v1 states that nothing terminalises an orphan, so a consumer that
   runs long enough **will** meet a run that never resolves, and this contract does not
   supply the policy for it. Give up after N attempts, escalate to a human, mark it
   abandoned in your own store — any of those is conforming. Having no policy is not,
   because the failure mode is a consumer that waits forever on a run nobody will ever
   finish, or three integrators each inventing a different timeout behaviour, which is
   the divergence removed from the specification arriving back through the consumers.

   On pacing, this obligation describes rather than requires. A `stopped_without_end` id
   does not consume the window, so the window is not backpressure for it — the producer
   spends a one-interval floor instead (D10), and the correctness of the boundary does not
   depend on you. A backoff of your own is still recommended, because a floor set by the
   poll interval is a floor and not a policy, and only you know how often re-asking about
   an unresolvable run is worth anything.

## Consequences

**Easier.** A second consumer can be written without reading lionagi's source, and
tested against a documented shape. Our own MCP server becomes a consumer of the same
contract, so a change that breaks external consumers breaks ours too, in our own test
suite, before release.

**Harder.** `--machine` is a second output mode for every command that has one, and a
stray `print` on stdout under it corrupts the result for every consumer while looking
fine to a human running the same command. This needs a per-command test asserting that
stdout parses as exactly one JSON object, not a convention.

**Harder, specifically.** D4's `outcome` and D7's wrapper mean the producer now owns
classifications it previously left to callers. That is the point — it is the only place
the current vocabulary reliably exists — but it means adding a run status is no longer
a local change: it must also be classified, on every status-bearing response.

**Given up.** The freedom to change what a machine call returns without thinking about
it. Additive change stays free under the narrowed rule in D2; anything else costs a
version increment and a coordinated update.

**Accepted in v1, and stated rather than discovered.** A run whose process dies without
recording a terminal, or whose producer dies before spawning it, stays non-terminal for as
long as its record exists. A consumer's bounded wait reports it as stopped without an end
every time, and never as finished. Two earlier revisions tried to close this with a rule
every reader would apply, and both produced worse failures than the one they removed: a
healthy child reported as terminally failed, and two hosts disagreeing about one unchanged
record. Closing it properly needs a fenced reconciler with process-incarnation evidence,
which is a protocol with its own failure modes rather than a clause, so it is written out
in the deferred section instead of half-specified here. Naming the run is not resolving it:
the caller learns at once that nobody will finish this run, and still has to decide what to
do about it, which is what consumer obligation 8 is for.

**New failure modes.** A consumer pinned to v1 against an implementation that dropped
v1 refuses to register rather than misbehaving, which operationally looks like a
surface disappearing and needs to be diagnosable — hence `min_supported_version`.
Per-envelope version checking (D2) means a mid-session upgrade degrades a live
consumer instead of corrupting its answers, which is louder and correct.

**What a contributor must now know.** That `lionagi/mcp/` is no longer where these
answers are decided. Adding a field to the MCP server's return value without adding it
here creates exactly the divergence this ADR exists to prevent, and the MCP server's
own tests will not catch it.

**Cost of reversal.** D3, D4 and D7 are cheap to extend and expensive to retract. D2 is
the escape hatch: a breaking change is expressible as a version increment rather than a
negotiation. D8 could be dropped without touching the envelope, at the cost of P7
returning. D10 cannot be dropped without re-opening the contradiction with ADR-0066, and
its two marked extensions cannot be dropped without leaving the two documents disagreeing
about what a wait entry contains and what a disconnect does. The clause it decides where
D6 is silent is the opposite case: nothing forces it, an implementation could have put
stopped ids in `pending` and stayed conforming, and it is reversible only until a consumer
has been written against it. The producer floor attached to it is cheaper to reverse than
to introduce, since removing a minimum call duration cannot break a caller that was
tolerating it.

## Alternatives considered

- **Make the MCP server's Python functions the contract.** It exists already and needs
  no new CLI surface. It lost because the external consumer is not written in Python:
  its contract would be a hand-written translation of our signatures, which is P1's
  drift with extra steps, and it makes our implementation its own specification, so
  there is no artifact to check either side against.

- **A long-lived lionagi daemon speaking a socket protocol.** Would avoid per-call
  process startup and allow push notification without a delivery command. It lost on
  P3: a daemon holding run state makes an answer depend on the daemon's uptime, and the
  consumer's whole value is being stateless. Subprocess-per-call also means a crashed
  call cannot corrupt another call's state. Retained as the obvious future direction if
  call latency ever dominates.

- **A closed status enumeration with a documented mapping.** Simplest for a consumer:
  branch directly on status. It lost to the recorded evidence in P2, where exactly this
  design silently converted timeouts into successes. The failure is not that someone
  wrote the mapping badly; it is that a copied vocabulary goes stale and its stale
  behaviour is a wrong answer rather than an error.

- **Publishing only `terminal`, leaving success to the consumer.** This was the
  previous draft's position, and it is wrong. It reads as the conservative choice —
  publish less, let the caller decide — but the caller cannot decide without the
  vocabulary we forbade it from holding, so in practice it either invents one or treats
  every terminal run as fine. `completed_empty` is the counter-example that settles it.

- **A boolean `succeeded` rather than the three-valued `outcome`.** Simpler, and it was the
  first revision's answer. It lost to the orphaned-run case in D6: a process that vanished
  with no terminal recorded definitely ended, and its result is unknowable. A boolean has
  only `false` for that, which asserts failure from an inability to observe. Notably the
  external consumer proposed the three-valued shape independently, from its own analytics
  requirements, while concluding it was not needed in v1 — two unrelated routes to the same
  field is a stronger argument than either one alone.

- **Letting readers derive the terminal state of a spawn failure and of an orphan.** This
  was the previous revision's answer, and it is the most instructive thing that has been
  rejected here. Its appeal was real: a corrective write can fail on exactly the disk that
  just refused the spawn, so a guarantee resting on that write is not a guarantee. But a
  reader computing a *lifecycle transition* invents a durable fact from a local observation,
  and both concrete rules proved it. Spawn-failure-from-missing-pid is racy against the
  required write ordering, so a healthy child is reported terminally failed in the window
  before its pid is attached. Orphan-from-pid-absence rests on a probe that cannot identify
  a process incarnation, so a reused pid hides a dead run and two hosts disagree about one
  unchanged record. What survives is the narrow form: readers derive presentation, the
  lifecycle authority derives lifecycle. The ghost record is answered by naming an owner for
  it, not by hoping every reader will independently reach the same conclusion.

- **Semantic versioning for the contract.** Would allow finer-grained compatibility
  statements. It lost because it invites partial-compatibility logic in code we do not
  control. One integer permits exactly one comparison and no interpretation.

- **A handshake-only version check.** Cheaper: one call at startup. It lost to P5 —
  the binary at the pinned path is replaced during operation, so a startup-only check
  is a claim about a file that no longer exists. Per-envelope checking costs an integer
  comparison against a field already present in every response.

- **Inferring machine mode from a non-TTY stdout.** Zero new flags, and it is what many
  tools do. It lost because it makes the output shape depend on how the process was
  invoked rather than on what the caller asked for: a consumer that works in production
  and differs under a test harness, a pipe, or a CI log is the class of bug hardest to
  reproduce. Machine mode is a request, so it is a flag.

## Notes

**Deliberately not decided here**, and left to the consumer or to a later revision, so
that a consumer author knows these are open rather than assuming a silent default:
per-verb timeout budgets and what happens to the child when a call times out; poll and
wait backoff, jitter and total deadline; record, log and artifact retention, and what a
run id means after collection; submit idempotency when a consumer crashes between spawn
and receiving the envelope; `run_id` validation and path-traversal protection; size and
truncation limits for lists, console text and artifacts; timestamp precision and
ordering guarantees.

The external consumer is expected to ship in-tree in its own repository first, because
that repository cannot load a component from outside its build today. That constraint
belongs to the consumer and does not affect anything here: the contract is the same
whether the consumer is linked in at build time or loaded later. Recorded because the
sequencing question was raised while deciding, and the answer — that it does not bear
on the contract — is worth having written down rather than re-derived.
