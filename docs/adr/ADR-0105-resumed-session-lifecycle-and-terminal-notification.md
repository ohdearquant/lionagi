# ADR-0105: Resumed session lifecycle and terminal notification

- **Status**: Proposed
- **Kind**: Aspirational (records the target state)
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
    # A reopened session is running again: its closing transition must be a real
    # status change, or the terminal callback that announces this leg never fires.
    if existing_session["status"] in SESSION_TERMINAL_STATUSES:
        await db.update_status(
            "session",
            session_id,
            new_status="running",
            reason_code="session_reopened_by_resume",
            reason_summary="branch resumed by a new leg",
            expected_statuses=SESSION_TERMINAL_STATUSES,
            extra_fields={"ended_at": None},
        )
```

`update_status` is the ADR-0035 machinery in `lionagi/state/db.py`; `new_status` and
`reason_code` are required, and `expected_statuses` supplies the CAS guard.
`SESSION_TERMINAL_STATUSES` is derived from the lifecycle policy registry rather than
hand-maintained, and currently holds `aborted`, `cancelled`, `completed`,
`completed_empty`, `failed`, `timed_out`.

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
status may go `completed` → `running` → `completed`. Anything that treats a terminal
session as permanently final is wrong under resume and was already wrong, since
resume legs were mutating `ended_at` on terminal sessions before this ADR.

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
means finished forever" must tolerate reopening. In practice this was already true
and merely unobserved, since resume legs already rewrote `ended_at` on terminal
sessions, but it becomes an explicit contract here.

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
