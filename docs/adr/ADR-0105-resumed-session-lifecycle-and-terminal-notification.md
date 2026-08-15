# ADR-0105: Resumed session lifecycle and terminal notification

- **Status**: Accepted (2026-07-25; implementation merged)
- **Kind**: Aspirational (records the target state)
- **Implementation-status**: implemented (the lifecycle and notification behavior
  specified here is on main)
- **Area**: persistence-state
- **Date**: 2026-07-24
- **Relations**: extends ADR-0095

## Context

`li agent -r <branch_id>` reopens an existing branch and runs another turn on it.
Resume is how re-review rounds work: a second, third or fourth pass over the same
PR reuses the branch so the reviewer keeps its context.

A background leg is expected to announce its own completion. `li agent --notify
"<cmd> --status {status}"` registers a terminal callback bound to the run's session
entity; when that session reaches a terminal status the callback fires and executes
the command. For lionmcp-submitted jobs that command is `lionmcp/notify.py`, which
does two things: stamps the job record terminal (`jobs.mark_terminal`) and delivers
an inbox notice to the submitting seat.

Resume legs do neither. The leg finishes, and nothing announces it.

**P1 — A resumed leg's terminal callback never fires.** `setup_agent_persist`
(`lionagi/cli/_runs.py`) creates a session row with `"status": "running"` on the
new-branch path. On the resume path it looks up `existing_branch`, adopts its
`session_id`, and never touches the session's status. That session was already
terminalized to `completed` by the leg that created it. At teardown the resumed leg
calls `update_status(session, to_status="completed")`, and the emit in
`lionagi/state/lifecycle/service.py` is gated on the status actually changing:

```python
if (
    transition_id is not None
    and previous_status != command.to_status
    and command.entity_type in EXECUTION_ENTITY_KINDS
    and command.to_status in policy.terminal_statuses
):
    envelope = _build_terminal_envelope(...)
    await self._terminal_callbacks.emit(envelope)
```

`completed` → `completed` is not a change, so no envelope is built and no callback
runs. The gate is deliberate and documented in ADR-0095 ("a same-status reason
append is not a new event"); it is not the defect.

**P2 — The failure is silent and looks like a hang.** A detached leg that ends
without notifying is indistinguishable from one still running. The caller waits on
an inbox that will never receive anything. This is precisely the state the notify
hook exists to prevent, and it is worst in re-review rounds, which is where resume
legs concentrate.

**P3 — The job record is left non-terminal, from the same cause.** lionmcp's
`notify.py` is what calls `jobs.mark_terminal`, and it is executed *by* the terminal
callback. No callback means the job record keeps `status: running` and a null
`finished_at`. lionmcp later observes the pid is gone with no terminal record and
reports `exited` (`jobs.py`, "pid gone, no terminal record captured"). That is
honest reporting of someone else's silence, not a second bug.

### Measured evidence

Across 18 consecutive lionmcp job records (2026-07-24), separation was total with no
exceptions in either direction:

| Leg shape | Marked terminal + notified |
|-----------|---------------------------|
| resume (`-r` in argv) | 0 of 2 |
| fresh | 16 of 16 |

`--notify` was present in argv for the failing legs, so this is not a submit-side
omission.

The row write itself succeeds. Session rows for the two resume legs carry
`started_at` from the **original** leg and `ended_at` stamped to the **resumed**
leg's own end time:

| session | started_at | ended_at | resumed leg's own run window |
|---------|-----------|----------|------------------------------|
| ab16e2d1 | 1784928295.6 | 1784929746.2 | 1784929531.9 → 1784929746.2 |
| 831109db | 1784928576.6 | 1784929677.0 | 1784929547.2 → 1784929677.3 |

So the UPDATE lands and only the callback dispatch is skipped. An earlier hypothesis
that the write was rejected by a CAS miss or the ADR-0035 terminal guard is **wrong**,
and it matters: a fix aimed at the write path would have targeted working code.

| Concern | Decision |
|---------|----------|
| How a reopened session re-enters its lifecycle | D1: resume returns the session to `running` before the leg runs |
| Whether the change-gate should relax | D2: the gate is unchanged; same-status writes never emit |
| What happens to the session's time span | D3: `started_at` is preserved, `ended_at` is cleared on reopen |
| Whether the job record needs its own repair | D4: no; it is downstream of D1 |

**Out of scope.** The terminal-callback registry, envelope shape and delivery
semantics (ADR-0095 owns them). The ADR-0035 terminal guard and CAS machinery. The
`defer_terminal` auto-resume path, which already has a correct answer and is only
described here for its interaction. Whether lionmcp should stamp `from_actor` at the
source (separate, tracked outside this ADR).

## Decision

### D1 — A resumed session returns to `running` on reopen

When `setup_agent_persist` adopts an existing branch's session, it transitions that
session back to `running` before the leg executes, through the same
`update_status` path every other lifecycle write uses rather than a direct UPDATE.

Contract (`lionagi/cli/_runs.py`, resume path of `setup_agent_persist`):

```python
existing_branch = await db.get_branch(branch_id)
if existing_branch:
    session_id = existing_branch["session_id"]
    existing_session = await db.get_session(session_id)
    # A session that is no longer there is not an error: maintenance removes old
    # terminal sessions, and there is then nothing to resume into. The leg records
    # itself under a new session rather than reading a row that no longer exists.
    # A reopened session is running again: its closing transition must be a real
    # status change, or the terminal callback that announces this leg never fires.
    if existing_session is None:
        existing_branch = None  # take the new-session path
    elif existing_session["status"] in SESSION_TERMINAL_STATUSES:
        await db.update_status(
            "session",
            session_id,
            new_status="running",
            reason_code=SessionReasons.REOPENED_BY_RESUME,
            reason_summary="branch resumed by a new leg",
            expected_statuses=SESSION_TERMINAL_STATUSES,
            extra_fields={
                "ended_at": None,
                "node_metadata": json.dumps({**node_metadata, **current_pid_markers()}),
            },
            override=True,
            override_actor="cli.resume",
            override_justification="branch resumed by a new leg; the session is executing again",
        )
```

`update_status` is the ADR-0035 machinery in `lionagi/state/db.py`; `new_status` and
`reason_code` are required, and `expected_statuses` supplies the CAS guard.
`SESSION_TERMINAL_STATUSES` is derived from the lifecycle policy registry rather than
hand-maintained, and currently holds `aborted`, `cancelled`, `completed`,
`completed_empty`, `failed`, `timed_out`.

The override is load-bearing and was missing from this ADR's first draft. The session
policy declares exactly one edge, `running → {terminal}`, and the transition service
rejects any move out of a terminal status unless an override is supplied
(`lionagi/state/lifecycle/policy.py`, whose own comment states "No exit from a terminal
status without override"; enforced at `lionagi/state/lifecycle/service.py` in the
`previous_status in policy.terminal_statuses` branch). Without it this write does not
land. Measured rather than reasoned about: `StateDB.update_status` raises
`TransitionRejectedError("refusing to write 'running' without override=True")`, and
`setup_agent_persist` catches `Exception` broadly, logs a warning, and returns `None`,
which disables persistence for the entire run. So the version of D1 without an override
does not merely fail to fix the missing notice; it costs every resumed leg all of its
state persistence, behind one warning line. That consequence is worse than the defect
and is the reason this correction is stated at the contract rather than left to
implementation.

`reason_code` must also be a code registered in `lionagi/state/reasons.py`; an
unregistered string raises before the transition is attempted. This ADR adds
`SessionReasons.REOPENED_BY_RESUME` (`"session.reopened.by_resume"`), which satisfies the
session policy's `reason_prefixes`.

Override rather than a new declared edge, deliberately. Declaring `terminal → running`
in `session_edges` would legalize terminal-exit for every session writer in the system,
and the finality of a terminal session is the property the reaper, the teardown guard,
and `li wait` all rest on. The override keeps the exception scoped to the one caller
that has earned it and, because `override` requires a non-empty actor and justification
and emits a `status_transition_override` admin event, it makes each reopening
attributable. A reopened session is a real event and should leave a record saying who
reopened it and why; the declared-edge version would leave none.

`ended_at` is already in the session policy's `patch_fields`. `node_metadata` is added to
it, and to `EXTRA_STATUS_WRITE_FIELDS_BY_ENTITY_TYPE` in `lionagi/state/db.py`, so that
the process markers move in the same transaction as the status.

That is not a convenience. The sweeps select rows by status and then ask the recorded
process markers whether the row is still alive, so status and markers are read together
and have to agree at every instant a sweep could look. Splitting the two writes leaves a
window in which the row reads `running` while still carrying the markers of the leg that
already exited, which is precisely the shape `li kill --all-stale` cancels
(`lionagi/cli/kill.py`: select `status='running'`, age past the threshold, recorded PID
not alive). The markers must also not be written on a lost race, because the row then
belongs to a different leg and our markers would make that leg's liveness answer for our
process. Both requirements are satisfied by the one guarded write: it either wins and
installs status and markers together, or it loses and touches nothing. This is what the
same-row allowlist exists for — its own comment says it keeps a caller from splitting a
status change and a dependent column into two transactions.

Markers, not liveness in general: a terminal session is never checked for liveness, so
stale markers on one were harmless. A running one is checked, which is what makes them
load-bearing from the reopen onward.

Exact semantics:

- Session already terminal (any of the six above) → transitioned to `running`; the
  leg's closing transition is then a genuine change and emits normally.
- Session already `running` → left alone. This is a resume racing a live leg on the
  same branch; reopening is a no-op and the guard makes it observable rather than
  clobbering.
- Transition returns `conflict` (another process moved the row first) → the leg
  proceeds. A resumed leg must not fail because its bookkeeping lost a race; the
  cost is one missed notice, which is the status quo, not a regression.
- `to_status="running"` is not in `policy.terminal_statuses`, so reopening never
  itself emits a terminal envelope.

Why this way: the defect is that a session's status stops describing the session.
Returning it to `running` restores the invariant that a session marked terminal is
not currently executing, and every downstream consequence (the callback firing, the
job record closing) follows from that invariant holding rather than from special
handling.

### D2 — The change-gate is not relaxed

`previous_status != command.to_status` stays exactly as it is.

Why: it is load-bearing idempotency for every caller, not a notify implementation
detail. ADR-0095 introduced it so that a same-status reason append is not a new
event. Relaxing it so terminal writes always emit would re-fire completion notices
fleet-wide on any repeated terminal write — reaper sweeps, status refreshes, retried
teardowns — turning a missing-notice defect into a duplicate-notice one affecting
every consumer instead of one path. The narrow fix at the resume path costs one
guarded write; the broad fix at the gate costs correctness everywhere.

### D3 — `started_at` is preserved, `ended_at` is cleared on reopen

Reopening sets `ended_at = NULL` and leaves `started_at` untouched.

Semantics: `started_at` is when this session began, and resume continues a session
rather than starting one, so it keeps the original value. `ended_at` must be null
while running, because "has an end time" and "is still executing" cannot both be
true — that is the same one-value-two-meanings failure this ADR is fixing, and
leaving a stale `ended_at` on a running session would reintroduce it one column over.

Today's behaviour is the incoherent middle: `started_at` original, `ended_at` from
whichever leg last closed, status terminal throughout. A reader cannot tell from the
row whether the session ran once or four times.

Consequence a consumer must know: a session's `ended_at` may move later, and its
status may go `completed` → `running` → `completed`. The first of those was already
true in production, since resume legs were mutating `ended_at` on terminal sessions
before this ADR. The second is genuinely new, so it is not asserted here on the
strength of the first. The enumeration is in "Consumers of session finality" below.

### D4 — The job record needs no separate repair

lionmcp's stuck `status: running` record is downstream of D1, not a second defect.
`notify.py` performs both the record stamp and the notice delivery, and it is
executed by the callback D1 restores. When the callback fires, both happen.

This is recorded as a decision rather than left implicit because the two symptoms
travelling together is suggestive but not proof of one cause, and the alternative —
having lionmcp reconcile pid-gone jobs itself — is a real option that is being
declined. Declined because it would paper over a missing notification with an
inferred one: lionmcp would report `completed` for a leg it never heard from,
which is a worse failure than reporting `exited` honestly.

## Consumers of session finality

D1 makes a session's status go terminal → `running` → terminal, which no consumer has
seen before. Every live reader of session terminal-status and `ended_at` was enumerated
by grep across `lionagi/` and read at its call site, rather than reasoned about from the
decision. Results:

| Consumer | Under terminal → running → terminal | Verdict |
| --- | --- | --- |
| `studio/services/sessions.py` `is_session_stream_done` (callers in the same file) | Re-evaluated each poll; a closed stream stays closed, a stream opened during the window stays open | unaffected |
| `studio/services/run_view.py` `build_outcome` | Falls through to invocation/occurrence while running, self-corrects when the leg closes | transient only |
| `studio/services/run_view.py` `exit_code_for_view` | Returns the running exit code during the window, which is what is true | unaffected |
| `cli/_runs.py` linked-engine phantom-failure suppression | Does not fire mid-window; falls back to today's behaviour | unaffected |
| `cli/_runs.py` teardown terminal-skip guard | **Changes**: see below | intended |
| `cli/_runs.py` `BRANCH_END` emission | Reads a local `final_status`, not the row | unaffected |
| `cli/monitor.py` `_effective_session_status` | Early-returns on terminal, reconciles while running | unaffected |
| `cli/monitor.py` `_poll_pending_sessions_once` | Completed sessions leave the pending set and are never re-added | unaffected |
| `cli/wait.py` terminal-status waits | A waiter started mid-window waits for the resume leg, which is the leg it cares about | unaffected |
| Duration/`ended_at` arithmetic in `studio/cli.py`, `services/sessions.py`, `services/run_view.py` | All guard `is not None`; a null `ended_at` is already handled | unaffected |
| `studio/services/lifecycle.py` `reap_null_status_sessions` | Selects `status IS NULL` only | unaffected |
| `studio/services/admin.py` `list_phantom_sessions` → `lifecycle.py` `reap_phantom_sessions` | **Newly reachable**: see below | disclosed |
| `studio/services/admin.py` health sweep (`UPDATE ... WHERE status='running'`) | Same class, additionally guarded on `last_message_at`/`updated_at` equality | disclosed |
| `cli/kill.py` `--all-stale` sweep | **Newly reachable**: selects `status='running'` past an age threshold, then vetoes on a live recorded PID. Safe once the markers move with the status; unsafe if they lag it | fixed in D1 |
| `cli/state.py` `_doctor` (`li state doctor`) | **Newly reachable and not veto-guarded**: see below | fixed |
| `studio/services/db_maintenance.py` `prune_old_data` | **Newly reachable in both directions**: a session can leave terminal while its history is being removed, and can be removed while a resume is reopening it | fixed |

No consumer breaks. Four findings need stating; two of them needed a fix.

**The teardown terminal-skip guard starts working.** `cli/_runs.py` skips its status
write when the session was already terminal at teardown start, logging that the earlier
terminal record is protected. Before D1 that meant a resumed leg's outcome was silently
dropped. After D1 the session is `running` at teardown, so the write lands and the
resume leg's outcome is recorded. This is correct and is a second defect D1 fixes, but
it is a real semantic change: the resume leg's terminal status now replaces the original
leg's on the row. The earlier one is not lost — every applied transition appends to
`status_transitions` — but the row itself shows the latest leg.

**A resumed session becomes eligible for phantom reaping.** Both sweeps above select
`status = 'running'`, so a terminal session is invisible to them today. A reopened one
is not. If a resume leg dies without writing a terminal status and the session then sits
stale for `PHANTOM_STALE_HOURS`, the reaper transitions it to `failed`, and a session
that had previously completed now reads `failed`. This is judged acceptable rather than
handled: the session genuinely was re-run and the re-run genuinely died, `running →
failed` is a declared edge that applies normally, and the earlier `completed` survives
in `status_transitions`. It is recorded here because the derived row no longer shows it,
and a reader of the row alone would draw the wrong conclusion about the first leg.

**`li state doctor` was measuring the wrong thing, and D1 exposed it.** The command
sweeps sessions "stuck at `status='running'`" whose `started_at` is older than a
threshold, and its own help text promised that "an actively-running CLI process is left
alone". Age answers *how long since this session first started*, which equals *how long
this process has been running* only for a session that ran once — the case that used to
be the only one. A resumed session keeps its original `started_at` (D3) while its process
is new, so a live resumed leg became sweepable from the moment D1 made it `running`, and
at the default threshold a session first started more than a day ago is sweepable
immediately and permanently, for as long as the leg runs.

This one is fixed rather than disclosed, because the promise was already written down and
D1 would have falsified it. `_doctor` now requires both conditions: the age past the
threshold **and** the recorded process gone, the same veto `li kill --all-stale` already
applied. Sessions with no recorded PID are unchanged, so a crash that never wrote markers
is still reapable, and the help text now states the predicate it enforces.

The general shape, worth naming because two consumers hit it in one change: a sweep that
selects on `status` and thresholds on `started_at` is asking "is this stuck" and answering
"is this old". Those were the same question while a session had exactly one leg.

**The prune and a resume can now collide, and it had to be settled in both directions.**
`prune_old_data` removes terminal sessions past a retention window. It clears their
associations and transition history first, then deletes the row, all inside one
transaction. Before D1 a session selected as terminal stayed terminal for the length of
that pass; after D1 it can be reopened partway through.

*A resume landing mid-prune* leaves the session with its row and without the history
already cleared for it. Putting the terminal predicate on each destructive statement is
necessary and not sufficient: every statement is then individually correct and the
sequence can still land half-applied, because another transaction commits between two of
them. The prune now writes to each candidate row before reading the status that decides
the batch — the write is what takes the lock, on postgresql by holding the rows and on
sqlite by escalating the transaction to a write. A resume that arrives before the lock is
dropped at the re-read, which is a quiet zero-count pass. A post-condition after the
delete re-reads the candidates and abandons the transaction if any survived, so the pass
applies whole or not at all.

*A prune landing mid-resume* is the same race from the other side, and the lock decides it
in the prune's favour: a resume already waiting is released to find its session gone.
Two paths turned that into a leg with no persistence at all — the status write reports a
missing row by raising, and persistence read the branch and its session as two separate
reads and then dereferenced a row that no longer existed. Both now treat a missing session
as "nothing to resume into": the leg records itself under a new session, exactly as a
branch nobody has seen before does. The old session's history is gone either way, since
removing it is what the prune was for; what is preserved is the current leg.

## Alternatives considered

**Create a new session per resume leg, linked to the branch.** Each leg would own
exactly one session, statuses would never resurrect, and `started_at`/`ended_at`
would need no special rules — the cleanest model on paper. Rejected because session
identity is the unit resume exists to preserve: the branch's message progression and
artifact contract hang off the session, and `find_branch` resolution assumes one
session per branch lineage. Changing that is a much larger migration for a defect
whose fix is one guarded write. Worth revisiting if session-per-invocation is ever
wanted for other reasons; this ADR does not foreclose it.

**Emit a synthetic terminal envelope from the resume teardown.** Bypass the gate by
constructing an envelope directly when the leg knows it resumed. Rejected: it
duplicates the emit path outside the service that owns it, so the two can drift, and
it makes "was this a real transition" unanswerable from the transition log — the row
would show no change while a consumer saw an event.

**Register the notify callback on the invocation entity instead of the session.**
Invocations are per-leg, so a resumed leg would have its own fresh entity. Rejected:
`cli/agent.py` documents why this is session-scoped — invocation records are
finalized externally and would never fire. This trades a missing notice for a
different missing notice.

**Give resume and prune a shared claim or lease over a session.** The prune would refuse
to delete a row an in-flight resume had claimed, so neither side could lose. Rejected: a
lease is only as good as its expiry, and the process that holds one is exactly the
process that can die without releasing it — the failure it introduces (a session nothing
will ever prune because a dead leg holds its claim) is quieter than the one it prevents.
Letting the prune win and having the resume start a new session reaches the same end
state without a second liveness problem to get right. The lock is held for one
transaction by the database itself, which needs no expiry.

**Have `--notify` fall back to running the command unconditionally at teardown.**
Simple and always fires. Rejected: it abandons the entity model entirely, would fire
for deferred auto-resume legs that deliberately suppress their terminal
(`defer_terminal=will_auto_resume`), and reintroduces double-delivery whenever the
callback does fire.

## Consequences

Easier: a detached resume leg announces itself like any other leg, so re-review
rounds stop requiring polling, and the job record closes on its own. Session status
becomes trustworthy as a description of whether the session is executing.

Harder: session status is no longer monotonic. A consumer that latched "terminal
means finished forever" must tolerate reopening. The enumeration above found no such
consumer, and the three that change behaviour do so in ways stated there. Reopening is
also the system's only sanctioned exit from a terminal status, so it carries an
override audit row rather than passing as an ordinary write.

Also harder, and the part most likely to bite a future change: a session's row now
describes the *current* leg, while its start time still describes the session. Anything
that derives "how long has this been running" from `started_at` is correct only for a
session that ran once. Two sweeps made that assumption, both found here; a third written
later would make it again, and the row gives no hint that the two ever differ.

New failure mode: a crashed resume leg leaves the session `running` with a null
`ended_at` where previously it would have kept a stale terminal status. That is
better for a reader and worse for anything counting running sessions, which is what
the orphan-recovery path in ADR-0095 exists to reconcile.

Reversal cost: D1 is one guarded write and its regression test — cheap to revert.
D3 is coupled to D1 and reverts with it. D2 changes nothing, so it has no reversal
cost. D4 is a decision not to build something.

## Verification

The regression must fail without D1 and must assert delivery rather than
registration: run a resume leg with a `--notify` command that writes a file, and
assert the file exists and the session's transition log records a real
`running` → `completed` change. Asserting only that a callback was registered would
pass against today's broken behaviour, since registration was never the problem.

A second regression covers the reopen write itself: assert that reopening a terminal
session applies rather than returning `rejected`. Without the override this write is
refused, and the notice regression above would fail for a reason unrelated to what it
is testing — a silent rejection reads from the outside exactly like the defect. Pin
them separately so a future change to the override path fails at the write, not at the
notice three steps downstream.

A third regression covers the markers, and it takes two cases because there are two ways
to get them wrong. Reopening must leave the row carrying this process's markers, merged
with whatever unrelated metadata was already there; and it must install them through the
status write rather than a second one, which is pinned by making any separate write fail
the test. A version that writes them separately passes the first case and fails the
second, which is the point: the window is the defect, not the value.

A fourth covers the sweep: a session past the age threshold whose recorded process is
alive is left alone, and one whose process is gone is swept, in the same run. Asserting
only the first would pass against a sweep that had stopped working. A session whose
recorded number is alive but belongs to a different process is swept too, since a live
PID alone would otherwise protect a stuck row for as long as that number stayed in use.

A fifth pins the shape of what reopening reads. `node_metadata` has held values that are
not JSON objects, and reading one as an object raises: for a string that is not JSON at
all, and for a JSON scalar or list when the object is merged. The resume must survive all
of them, because the caller catches broadly and turns any raise here into a run with no
state persistence at all — a far larger failure than the field that caused it.

A sixth covers the prune, from both sides, and each side needs more than one case. On the
prune side: the reopen is driven past each destructive statement in turn, and every case
asserts the session keeps its row, its transition history and its associations, so a guard
that covers one statement and not the next fails. A separate case pins the order — the
first statement the prune issues against `sessions` must be a write, since reading first
leaves the decision resting on a value another transaction may change before the writes
land. On the resume side: a session deleted between the read and the write must not raise,
and a session deleted between the branch read and the session read must leave the leg with
persistence under a new session rather than none at all. Both of those fail on the
untreated code at the exact line that breaks, one raising and one dereferencing `None`.
