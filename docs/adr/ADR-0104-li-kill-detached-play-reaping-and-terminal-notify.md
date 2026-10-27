# ADR-0104: `li kill` reaping of detached-play workers and terminal-notify on kill

- **Status**: Accepted (2026-07-15), amended 2026-07-27, 2026-10-11, 2026-10-20 and 2026-10-27 — see Amendments 1 to 4
- **Kind**: Aspirational (records the target state)
- **Implementation-status**: partial — D3/D5 (terminal-notify on kill) are on main;
  D1/D2 (play-worker reaping) shipped code that cannot reach a worker; that code is
  removed and the guidance corrected (see Amendment 1); Amendment 4 (terminal-session worker
  cleanup) is decided and not yet implemented
- **Area**: cli-surface
- **Date**: 2026-07-13
- **Relations**: extends ADR-0058 (unified lifecycle transition service, whose terminal-callback emit this ADR relies on); none superseded

## Amendment 1 (2026-07-27) — D1 and D2 shipped a reap that reaches nothing

**The delivered-behaviour claim in this ADR was wrong.** D1/D2 below state that
`li kill <play_id>` reaps the play's worker chain, and the status line above used
to say the behaviour was on main. Code implementing that shape did merge, so the
claim was not fabricated — but the code cannot do what the ADR says it does, and
so the operator expectation this ADR set out to fix was left in place under a new
description.

The mechanism is the `plays.session_id` linkage D1 resolves through. The play
creation path never binds it: a play row is written without a session id, and the
worker sessions a play starts carry no back-reference to the play. So
`_list_running_children` on a play resolves to zero children on every play this
code path produces, and the transitive walk added for it terminates immediately.
The result was a kill that reported success while every worker process kept
running — a worse failure than the original no-op, because the original at least
did not claim to have stopped anything.

**A writer for the column does exist.** The Studio show importer binds
`plays.session_id` when it materialises a play from an imported show, so the
column is not dead by construction and the join D1 added is not unreachable in
principle. Measured against a development store carrying 400 play rows: none had
`session_id` bound, no session was named for an imported show, and none carried
a show topic; a control query on the same table returned 400 rows with a non-NULL
status, so the store and the query were both live. **The correction is therefore
"measured-unreachable", not "impossible by construction".** Anyone reinstating
the join should establish that the binding writer is reachable on the path they
care about, rather than inferring reachability from the column's existence.

**What replaces it.** `li kill <play_id>` no longer pretends. It marks the play
row `blocked` — the terminal status a play has always taken on kill, as distinct
from the `cancelled` a session or invocation takes — states plainly that the
worker processes were not stopped and why (a play records no link to the sessions
it started), points the operator at `li monitor` to find the running session ids,
and exits non-zero. `--recursive` on a play is documented as a no-op for the same
reason.

**Everything below that depends on D1 or D2 is historical.** The decision bodies,
their exact semantics, the D3 sentence about one notification per reaped child,
the D5 reaping test, the transitive-walk entries under Consequences, the
current-vs-ideal deltas, and the alternatives rejected in favour of reaping all
describe a contract that was never delivered. They are kept because an ADR is a
record of what was decided and why, not a description of the current system, and
because the reasoning is what a future attempt should argue with. None of them
should be read as a statement about how the command behaves.

**The kill path is not the only consumer.** `--all-stale` decides whether to
sweep a play by asking whether its linked session has terminated, and that check
returns false whenever the link is unset — so the sweep skips every play for the
same reason the reap reached none, and the CLI reference describes a swept play
being cancelled alongside its worker session, which cannot happen. That is a
distinct command with its own decision to make (skip silently, or say so the way
the kill now does), so it is reported separately rather than folded in here.
Recorded because the premise is shared: anyone fixing the binding fixes both, and
anyone reading only this amendment would fix one.

**What is still open.** Making a play kill actually reach its workers needs the
missing link written at play-creation time. That is a capability change with its
own design question (which writer owns the binding, and what a play with several
worker sessions means), tracked separately. This amendment does not decide it.

## Amendment 2 (2026-10-11) — a run whose persistence setup failed is stopped by its run id

`li agent` keeps running when its persistence setup fails (a locked state database is the
common cause): it records `persistence_degraded_reason` on its run.json and goes on with no
session, since setup returned none. Every id `li kill` resolves is a row, so such a run had
no handle printed for it: its run id named no row, and the only row it could have is one
setup committed before it failed, which the run never prints and `li monitor` lists,
`running`, under that row's own id. Without that row a leg started that way could only be
stopped by finding its pid by hand.

**Decision.** A run whose persistence setup failed records its own process on its run.json
(`pid`, `pid_create_time`, `pid_host`, `pid_boot_time`, `process_identity_mode`, the same
markers a session row carries) and prints `stop it with: li kill <run_id>` on stderr.
`li kill <run_id>` resolves such a run when no row matches: only a run.json that carries
`persistence_degraded_reason`, read from the run directory the id names, and only an id that
is a single path component. Its process is stopped by the checks a session's pid gets (host,
boot, identity mode, then the recorded start time and a lionagi command line, refusing when
the start time is absent), and the kill prints `killed run <run_id>`. A run that
took the SIGTERM writes its own terminal status; one that was force-killed or had already
died still says `running`, and the kill writes `cancelled` and `ended_at` on it. Invocations
that share a run id (`LIONAGI_RUN_ID`) share its run.json, so the kill reads it again under
the run's manifest lock and writes only if it still records the process the kill stopped, by
pid and start time: a record another invocation wrote is that invocation's, and is left as
it is. Every writer of a run.json takes that lock for its write (`RunDir.write_manifest`,
and the record of a degraded setup across its read and its write), so, where the lock can be
taken, no record lands between the kill's read and its write (Limits). A run.json the kill
cannot read then, or whose lock it cannot take, is left as it is too, with a warning, since
what it records cannot be checked.

A degraded run that `li state import` picked up while it was still running has a session row
named by its run id, with no pid on it, and that row resolves first. When a session row that
resolves has no pid and the run directory of the same id holds a degraded run.json, the kill
stops the process that run.json records, by the same checks, then cancels the row and settles
the run.json as above.

Whichever way the id resolves, when that run.json records no pid, or its pid fails the checks,
nothing is signalled and nothing is written: the kill reports the process as not stopped and
`li kill` exits non-zero. It writes `cancelled` only over a record of a process it stopped or of one that no longer runs.
The second is sound because the run writes that record about itself, its pid and that pid's
start time read from its own process in one call, and a pid is not reissued while its holder
lives: a recorded pid that nothing holds, or that only an unreaped exited process holds, means
the run has ended. A record whose pid and start time name two different processes is not one
the run writes, and the kill does not defend against it.

**Why this and not refusing to start.** Running without persistence is deliberate here: a
run that loses its store still delivers its terminal notice itself and records why it
degraded, and the lion MCP job record reads that reason. Refusing to start would trade a
transient lock for a failed leg. What was missing was only a way to stop it.

**Limits.** No other run id stops anything. It does not resolve, unless `li state import`
gave it a row; that row records no process, so the kill refuses it and writes nothing, and
`li kill` exits non-zero. A run with a
session row of its own is stopped by that session id, which also writes the cancellation to
the row. The orchestration path (`li o flow`, plays) degrades the same way and is not
covered: its runs record no process on their manifest, so at the moment one degrades it
prints that its run id is no `li kill` handle, with its pid. The manifest lock is POSIX
advisory locking on a file beside run.json (`.run.json.lock`); where it does not exist
(Windows) nothing is held, and an invocation sharing the run id that records itself between
the kill's read and its write has its record replaced. A program outside lionagi that
rewrites a run.json without taking the lock is not covered either. A writer that cannot take
the lock (a filesystem without `flock`, a lock file it cannot open) writes without it, with
a warning, rather than fail the run; the kill in that case leaves run.json as it is. Setup
can commit the run's session row and then fail: that row carries the process's pid and start
time but no branch, is never completed, and stays `running` after the run ends, as it did
before this amendment, which does not close it; that is tracked separately.

## Amendment 3 (2026-10-20) — a run records the terminal write its store refused, and the reaper names it

A leg whose state store refuses writes keeps going: signal persistence skips and counts what it
could not write (`signal_persist_dropped` on run.json at teardown), and a setup that fails
records `persistence_degraded_reason` (Amendment 2). Its end can still be lost. Teardown's
terminal transition is a store write. When that write fails, as it does when the store refuses
the writer lock, teardown hands back what is durable (the row's status, usually `running`, or
`unknown` when it cannot read the row back) instead of the status the run decided. With no
committed transition the lifecycle callback never runs, so the notify hook never records the end
on the job record. run.json is then rewritten
with the durable status and the decided one is gone. When the process exits, the reaper finds a
started job with no end and reports `process_gone_without_outcome` with no detail, the answer
for a run that said nothing about its end, although this one decided its end and lost only the
write. A leg killed outright while its store refuses writes leaves even less: its count of
dropped signals lives in memory and dies with it.

**Decision.** Two records in the run directory, and the reaper reads them.

1. When a store read or write fails teardown (teardown keeps the store error it caught: a
   database error, or the store having no row for its write) and teardown hands back a status
   that is not terminal for a run whose teardown decided a terminal one, the `li agent` run
   writes `terminal_write_lost` on its run.json in the same manifest write that records
   `status`: the decided status, its reason code, and the time. The decided status
   and code are the outcome teardown records for its own run before its write
   (`terminal_reason`). When teardown failed before deciding, they are the terminal status it was
   asked to write and the code it resolved for that status, which it records before any later
   write so that the SIGTERM latch is read once; when it failed before resolving one,
   `resolve_run_reason` gives the code. `status` keeps saying what is durable; the new field says
   what the run decided and that the store never got it. A run that defers its end to an
   auto-resume, or whose teardown decided `running` because a linked engine session still runs,
   decided no terminal status and writes nothing. A teardown that fails on anything but a
   store error, such as an artifact removed while it was verified, reached no write the store
   refused, and the run writes nothing either. A terminal status other than the decided one
   is another writer's record (a concurrent teardown won the row, or the state doctor ended it),
   not a lost write.
2. The first time a run's signal persistence opens a skip window (the store refused the writer
   lock), the `li agent` run writes `signal_persistence_degraded_at` on its run.json, once per
   run, on a thread of its own under the run directory's manifest lock. The window exists so
   that an emit never waits on the store, and this record must not make it wait on a file instead.
   `bind_db_persistence` takes the callback that does this (`on_window_opened`) and calls it
   once per binding, on the event loop. A run killed after that thread has written the time
   still leaves this marker, though not its count. A whole-file write of run.json
   (`RunDir.write_manifest`) keeps the earliest time on file, so neither the run's own setup
   write nor one by another invocation sharing its run id (`LIONAGI_RUN_ID`) erases or moves it.
3. An auto-resumed leg runs in a run directory of its own while the job reads the first run's
   run.json, so the first run hands it a marker for that run.json too, and a place to leave a
   lost terminal write, which the first run copies onto its run.json when the leg ends,
   setting that one field under the manifest lock and leaving the rest of the file as it is,
   except a degradation time no reader takes as one, which it drops as a whole-file write does.
4. `reap_orphan` reads run.json through the bounded, fail-closed reader it already uses for
   `persistence_degraded_reason`, and assigns `reason_detail` beside
   `process_gone_without_outcome` in this order: `terminal_write_lost`, then
   `persistence_degraded`, then `signal_persistence_degraded`. A lost terminal write's decided
   status is copied into `terminal_evidence` as `decided_status`, also beside
   `process_gone_notice_recorded_undelivered`, which keeps its own code and no detail. The
   outcome stays `indeterminate` in every case: none of these is a recorded status, and a job
   outcome taken from a run's own decision would contradict the session row the store holds.
   The notice the reaper sends carries the detail.
5. `job.status` lifts `signal_persist_dropped` and the two new fields to the top level beside
   `persistence_degraded_reason`, as read-time projections, each null when absent or malformed;
   they store nothing on the job record, whose only new stored values are decision 4's: the
   `reason_detail` values `terminal_write_lost` and `signal_persistence_degraded`, and
   `decided_status`.

**Why the run directory.** It is the one place a run can still write when its store refuses it,
and the reaper already reads run-directory files for the same purpose (`notify_outcome.json`,
`persistence_degraded_reason`). A field on the job record would need the leg to take the job
lock from teardown, which is the hook's path and is the one that did not run.

**Why a detail and not new reason codes.** `reason_code` is the value readers branch on, and
`process_gone_without_outcome` is still true of each case: the process is gone and no recorded
status says how it ended. A `reason_detail` beside it, as `persistence_degraded` already is,
adds the cause without changing an answer any reader keys on.

**Limits.** No detail is assigned from the absence of a field: retention prunes run directories,
and an absent field is what an older run, a pruned one and a silent one all look like. A
malformed record reads as absent. A leg killed before its store refused anything leaves no
marker and is reaped as before. One killed after its store refused the lock but before the
marker's thread wrote the time leaves no marker either, and one killed after its terminal write
failed but before the manifest write that records the loss leaves no `terminal_write_lost`. The
count of dropped signals is still written only at teardown. A store that refuses a write in
compatibility mode is not a lost write here: the run fails with that refusal as its reason
(ADR-0128 D6), run.json records `failed`, and the status the run decided is not kept; a
timed-out leg that would have auto-resumed fails the same way and is not resumed. A
compare-and-set miss is not a lost write either: when another writer has moved the session
row off the status this teardown read, the teardown reads the row back and returns its
status without raising, a non-terminal status included, and nothing is recorded. Invocations
sharing a run id share one run.json, and `terminal_write_lost` is lifecycle data like `status`:
a later `RunDir.write_manifest` by another invocation replaces it.
Flow, fanout and play runs write neither field, and neither do runs the Studio operator
persists: the record is written by `li agent`'s teardown, and the operator's
`setup_agent_persist` call passes a run id and an artifacts path but no `run` or
`run_manifest`. They degrade without the record, as Amendment 2's Limits say for their stop handle. A job whose hook could not take the job lock, so
that run.json holds a real terminal status the job record never got, is a neighbouring case this
amendment does not cover; so is a teardown that is itself interrupted part way by Ctrl-C or a
cancellation, which records the interrupt's status on run.json without knowing whether its
transition landed. A run cancelled before its teardown, whose teardown then runs and fails its
terminal write, is covered: the record names the cancellation. A
marker write that cannot start its thread, or cannot read or write run.json, is logged and
dropped; the run's later manifest writes still carry the time, and a run that handed its end
to an auto-resumed leg, which writes no manifest after it, writes the time again when the leg
ends. The new run.json fields are
additive and use names no reader keys on today; `li kill`'s run-id resolution (Amendment 2)
still keys on `persistence_degraded_reason` alone.

## Amendment 4 (2026-10-27) — `li kill` stops the recorded CLI worker of a terminal session

The question this amendment answers: "A session can end `failed` while the CLI worker it launched
(a codex or claude child, own process group, PPID 1) keeps running with a live network socket.
Today the operator cannot stop that worker through lionagi." It is an operational fix for a gap
found while running the CLI; no product requirement or user request originated it.

`li kill <session>` refuses a session whose status is terminal before it reads any pid (`_do_kill`
prints `already in terminal state ... nothing to kill`), and `li kill --all-stale` selects only
`running` rows. Both are right about the session: the row records the `li` runner, and that runner
is gone. What survives is a provider CLI the runner started in a process group of its own, and
nothing durable joins that worker's identity to the session. Subagent admission slots record a
child only when caps are configured, and carry no session, host, boot or group. An engine run
holds its worker handles in memory. The shared spawn's observation of a child can also lack a
start time: `observe_spawned` returns `create_time=None` when a read fails, and the second look
that refuses such a spawn runs only when admission slots are held, so a spawn observed without
identity can reach its callbacks. Among the ids an engine run carries, the CLI engine-run id is
deliberately the id of the signal-session row it creates, when it creates one, while the
in-memory `Session` and the `EngineRun`'s own short id are separate values; cleanup binds to the
persisted row, never to one of the others by equality.

**Decision.**

1. **A record per spawn, in the state database.** Native schema version 5 adds a
   `cli_worker_spawns` table and a nullable `sessions.cli_worker_generation`. A row is written for
   each OS spawn attempt a persisted run makes: a `spawn_id` allocated once per attempt, the
   session id, the generation, optional run, engine-run and branch ids kept as diagnostics, the pid
   and pgid (`pid == pgid`, greater than 1), the start time observed at spawn, the host, the boot
   identity and the process identity mode, the time it was recorded, and a nullable
   `confirmed_empty_at`. `session_id` is a logical reference with no foreign key to `sessions`, so
   the database neither cascades a session delete into these rows nor refuses one because of them,
   and the association stays as written; whether lionagi deletes such a session is decided by
   Decision 7. Identity and association columns never change. A row is accepted only while
   its session is `running` and its generation is the session's current one; a retry of the same
   `spawn_id` with identical contents is accepted once, with different contents refused. Rows that
   predate version 5 get no generation and no records, and nothing is backfilled from pids,
   admission slots or environments.
2. **Generations.** A fresh or resumed execution installs a new generation in the same status
   write that creates or reopens its session. A launch that would adopt a still-running row does
   not take that row's generation unless it joins the same execution.
3. **Producers.** `li agent` (including a resume, which records against the session it adopted),
   flow and fanout (the orchestration session, for planning, workers and synthesis alike), the CLI
   engine (its signal-session row) and background flow (the detached process, never its launcher)
   bind a recorder for their persisted session and generation before any worker starts. The
   provider layer receives the recorder and does not import the state database. The record is
   written inside the shielded spawn path, after the identity retry, which now applies to every
   recorded launch whether or not admission slots are held, and before any output is read. When
   identity cannot be read or the record cannot be written, the spawn is ended through its
   witnessed group teardown and fails; the admission slot stays held until that teardown is known
   to have ended the group. Teardown orders its terminal status write after the registrations it
   admitted. A run with no persisted session (`--no-persist`, a degraded setup, an unbound embedded
   caller) records nothing and claims no worker recovery.
4. **The terminal arm.** For a session in a terminal status, `li kill <session>` reads, in one
   snapshot, every unresolved record of exactly that session across its generations, with the
   session's status and current generation, and never adds to that set. For each record it checks
   that host, boot and identity mode are this runtime's, then requires the original leader live
   under its recorded start time and leading its recorded group. It captures the group's members
   between two identical observations of that leader, freezes those identities, sends SIGTERM to the
   group, and after the grace period sends SIGKILL only while a frozen member still matches. A
   leader that exits on SIGTERM leaves the frozen members to carry the authority to escalate.
   Before each signal, under a short guard shared with registration and resume, it confirms the
   session's status, generation and the record itself are as snapshotted; a resume, a status change
   or a deletion stops further signals and the command exits 1 saying the session changed during
   cleanup. No lock is held through the grace wait. The arm writes no session field, records no
   transition and sends no notification; `--reason` and `--recursive` do not widen it.
5. **Settlement.** A record is marked `confirmed_empty_at` only when the kernel reports its group
   empty (`killpg(pgid, 0)` raising "no such process group"), observed after the recorded leader is
   no longer live under its recorded start time. A start time that does not match means identity
   is not established: nothing is signalled and nothing is settled. A group holding only zombies
   stays unsettled. A process-table scan, complete or not, never settles a record and never
   authorizes a signal. Settled records are kept as evidence.
6. **A running row whose runner is dead.** `li kill` keeps its existing cancellation for it and
   then runs the terminal arm in the same command for that same execution: the evidence that the
   runner is dead is bound to the generation and runner identity it read, both are confirmed under
   the guard before the terminal write, and the worker selection is frozen for that execution, so a
   resume landing meanwhile is not cleaned up in its place. The worker results are reported before
   the command's own result. With no unresolved record, the cancellation's own result decides the
   exit status, as it does today; otherwise the command exits 0 only when the cancellation succeeded
   and every selected group ended confirmed empty, and 1 otherwise.
7. **Retention.** Pruning and direct deletion skip a session that still has an unresolved record,
   checked inside the same locked recheck that guards the session's other dependents. Settled
   records do not hold a session back.

Outcomes of the terminal arm, per record and then for the command:

| Condition | Exit | Output |
|---|---:|---|
| Every selected group stopped and confirmed empty, at least one signalled | 0 | `stopped worker for session {id12} (pid={pid}, pgid={pgid}, signal={signal}); terminal status remains {status!r}` per group |
| No records, or every record already settled or confirmed empty | 1 | the existing `already in terminal state` refusal; with no records, no process is inspected |
| Identity mismatch, malformed or foreign evidence, leader not live | 1 | the refusal plus an indented `session {id12}: recorded worker identity did not match — kill skipped`, or `... could not be verified — kill skipped` |
| Some groups stopped, others refused, or a signal sent without a confirmed empty group | 1 | each group's line, and `worker group for session {id12} could not be confirmed stopped; terminal status remains {status!r}` where it applies |
| The session was resumed, changed status or was deleted during cleanup | 1 | the groups already stopped, then that the session changed during cleanup |
| A running row whose runner is dead, cancelled in the same command (Decision 6) | 0 or 1, as Decision 6 states | each group's line, then the existing cancellation output |

**Why a record in the state database.** A session id stamped into each worker's environment
gives a label without the independently recorded birth time and generation a candidate must be
compared against, and psutil does not promise that another process's environment is readable at
all (`Process.environ()`), so absence of the label proves nothing. Requiring both the label and a
record adds refusals without adding proof. Per-run files survive a store that refuses writes, but
a resumed `li agent` and the CLI engine have no file locator a later kill can resolve, and the
kill already opens the state database to resolve the session. Stopping workers only at launch
cannot help once the runner has been killed outright.

**Limits.** The capability is prospective: a worker started before version 5, or by a run with
no persisted session, has no record and is not stopped by this arm. A runner that dies between
the OS spawn and the record's commit leaves a worker with no record. A worker whose leader has
already exited when `li kill` runs is refused even if a descendant still runs, since no recorded
fact proves that descendant belongs to the group's original owner; a descendant that ignores
SIGTERM is left running in the same way when a resume, status change or deletion stops the
cleanup during the grace period. Each signal follows an identity read, and the gap between the
two is the one `ln/_proc.py` already accepts for group signals. On Linux each interpreter adds its
own once-read boot time to a process's boot-relative start, so `li kill` can read a live worker's
start time outside the 0.1 s tolerance and refuse it; the check is not loosened to avoid that. A
version-4 build refuses a native writable open of a version-5 store (`SchemaTooNewError`); read-only
inspection retains its no-migration behavior. Workers of unpersisted SDK or engine use stay outside it.

**Rollout.** Three changes, in order: the schema and its readers contract; producer registration
with generation admission; the terminal arm with the running-row handoff. The arm does not ship
before its producers and its refusal tests.

## Depth contract

This ADR fixes a materially wrong operator expectation: that `li kill <play_id>`
stops a detached play. It does not. The reasoning below is grounded in a source
read of `cli/kill.py`, `state/lifecycle/service.py`, `state/lifecycle/callbacks.py`,
`state/lifecycle/notify_settings.py`, and `cli/main.py` at commit `18d51911a`
(the 0.29.0 release). Where a claim depends on an existing contract, the contract
is shown.

## Context

The documented operator guidance for background runs is: stop any `li agent` /
`li play` leg via `li kill <id>`, never a naked `kill <pid>` (which orphans
children and leaves lionagi run-state dangling). That guidance is correct for
`li agent` sessions/invocations but **materially wrong for detached `li play`**: today
`li kill <play_id>` marks the play row `blocked` and terminates no process. The
play's spawned session/invocation workers — the OS processes actually doing the
work — survive. This ADR specifies what `li kill` must do to make the convention
true, and clarifies what already works on the terminal-notify side so the fix is
scoped to the real gap and nothing more.

**P1 — `--recursive` does not reach a play's workers.** `_do_kill(recursive=True)`
calls `_list_running_children` once (single level, no transitive walk):

```python
# cli/kill.py — _list_running_children(db, entity_type, entity_id)
#   entity_type == "show"        -> plays        WHERE show_id = ? AND status='running'
#   entity_type == "session"     -> invocations  (via sessions.invocation_id)
#   entity_type == "invocation"  -> sessions     WHERE invocation_id = ? AND status='running'
#   (there is NO entity_type == "play" branch)
```

There is no `play → session` branch. So `li kill <play_id> --recursive` finds zero
children. `test_do_kill_recursive_kills_child_invocations` covers only
`session → invocation`; the play path is untested because it does not exist.

**P2 — plays carry no PID, so the play row kill is status-only.** For a play,
`_read_pid_from_entity` returns `None` (plays are orchestrators; only
sessions/invocations record a PID — this is stated in code: "Only
sessions/invocations carry PIDs; plays/shows are orchestrators"). So `_kill_one`
takes the `signal="no_pid"` path and only calls `_persist_cancel` → the play row
transitions to `blocked` with no OS signal. Nothing in the process tree is touched.

**P3 — the operator has no single command that stops a detached play.** The only
current workaround is to enumerate the play's child session/invocation ids (or the
worker PID) and kill those directly — exactly the manual, error-prone step the
convention exists to remove.

**P4 (clarified, NOT a gap) — terminal-notify on kill already fires for
settings-configured notify.** This ADR records this explicitly because an earlier
analysis wrongly flagged it as a gap. `cli/main.py` registers the settings-based
`notify.on_terminal` handler for **every** `li` subcommand, including `kill`,
before dispatch:

```python
# cli/main.py:main()
register_settings_terminal_callback(project_dir=_cwd_override)   # runs for `li kill` too
```

and the lifecycle service emits a terminal envelope on any terminal transition of
an execution entity:

```python
# state/lifecycle/service.py — after the committed write
if (
    transition_id is not None
    and previous_status != command.to_status
    and command.entity_type in EXECUTION_ENTITY_KINDS       # {session, invocation, schedule_run, play}
    and command.to_status in policy.terminal_statuses        # session/invocation: 'cancelled'; play: 'blocked'
):
    await self._terminal_callbacks.emit(_build_terminal_envelope(...))
```

Because the kill path runs `_persist_cancel → StateDB.update_status → service`, a
kill **emits** the `RunTerminalEnvelope`, and the handler fires **in the kill
process** — immune to the SIGKILL that kills the leg. So a settings-configured
`notify.on_terminal` already notifies the watcher on kill. The only notify case
that is missed is a **per-run `--notify` override**: the separate `li kill` process
resolves notify from settings only, and never sees a run's per-run override.

| Concern | Decision |
|---------|----------|
| `li kill <play_id> --recursive` reaps nothing | D1: add transitive `play → session[→ invocation]` reaping to `_list_running_children` / the recursive walk |
| bare `li kill <play_id>` silently no-ops the workers | D2: killing a play implies recursing into its workers; a play kill without reachable workers warns |
| terminal-notify on kill is misunderstood | D3: DOCUMENT that settings `notify.on_terminal` already fires on kill; recommend it as the default over per-run wrappers |
| per-run `--notify` override lost to the kill process | D4 (DEFERRED): optionally persist a run's resolved notify override so the kill process can honor it |
| the kill→terminal-emit behavior is untested | D5: add a regression test locking that a kill emits a terminal envelope and reaps play workers |

**Out of scope:**

- The identity-guard `_check_pid_identity` behavior (create_time,
  `LIONAGI_SESSION_ID`, cmdline) — unchanged; owned by the existing kill safety
  design. It still governs a session's own runner; the recorded CLI workers of a
  terminal session are checked by the separate rules of Amendment 4.
- `--all-stale` sweep semantics — unchanged (it already excludes plays/shows by
  design as orchestrators without direct PIDs).
- Show-level reaping — shows are not in `EXECUTION_ENTITY_KINDS` and are out of the
  detached-run workflow's scope; a show kill remains status-only.

## Decision

### D1 — Transitive `play → session[→ invocation]` reaping

> **Amended 2026-07-27 — this decision is withdrawn.** The linkage it resolves
> through is never bound on the play-creation path, so the branch described below
> reaches zero children on every play it was written for. See Amendment 1.

`_list_running_children` gains a `play` branch that resolves the play's running
worker chain, and the recursive kill walks transitively (BFS) rather than one
level. A play links to its worker session via `plays.session_id` (the column read
today by `_play_child_stale` and by the `li monitor` project filter). The play
branch resolves that session and its running invocation, so `li kill <play_id>
--recursive` reaches the PID-bearing workers and issues the real SIGTERM→SIGKILL
to each.

**The contract** (new/changed shape in `cli/kill.py`):

```python
# _list_running_children gains:
#   entity_type == "play" -> the running session at plays.session_id (kind "session"),
#                            AND that session's running invocation (kind "invocation")
# The recursive driver walks the returned children transitively:
#   play -> session -> invocation   (each reaped child re-queried for its own children)
```

**Exact semantics:**

- `li kill <play_id> --recursive`: reap the play's running session and that
  session's running invocation (each a real PID kill via `_kill_one`), then mark
  the play row `blocked`. Order: children before parent (a worker is stopped before
  its orchestrator row goes terminal), matching the existing session/invocation order.
- `plays.session_id` is `NULL` or dangling: no worker to reap; the play kill is
  status-only and emits a warning ("play <id> has no running worker session to
  reap") so the operator is never silently misled.
- A child already terminal: skipped (the existing `_persist_cancel` pre-check and
  the `status='running'` filters already handle this; no double-cancel).
- Identity mismatch on a child PID: that child is left running and reported as
  `blocked` in the result (unchanged `_kill_one` identity-guard behavior), and the
  parent play kill still records which children were and were not reaped.
- BFS depth: bounded by the real entity chain (play → session → invocation →
  possibly a chain-child session). The walk terminates because the running-status
  filter strictly shrinks the frontier; a guard cap (e.g. 100 nodes) backstops a
  pathological cycle.

**Why this way:** the existing recursion is already child-before-parent and
already re-queries children per node for the session/invocation case; extending it
transitively and adding the one missing edge (`play → session`) is the minimal
change that makes the convention true. Resolving via `plays.session_id` reuses the
exact linkage `li monitor` and the stale-sweep already trust, so there is no new
join semantics to validate.

### D2 — Killing a play implies recursing into its workers

> **Amended 2026-07-27 — this decision is withdrawn.** It rests on D1's reaping,
> which reaches nothing. A play kill now reports the workers it cannot stop and
> exits non-zero instead of implying it stopped them. See Amendment 1.

Because a play row has no PID, `li kill <play_id>` **without** `--recursive` is
close to useless — it blocks the row while the workers run on. This ADR decides
that a play kill treats worker reaping as implied: `li kill <play_id>` reaps the
worker chain by default, and `--recursive` remains the explicit form for the
session/invocation case (and a no-op-if-already-implied for plays).

**Exact semantics:**

- `li kill <play_id>`: reaps the play's worker chain (same as D1) and blocks the
  play row. No separate `--recursive` needed for the play case.
- The output names each reaped child and the parent, so the operator sees the full
  set that was stopped (not just "blocked play <id>").

**Why this way:** the alternative (require `--recursive` for plays) preserves a
footgun — the common `li kill <play_id>` keeps silently stranding workers, which is
exactly the reported failure. A play's reason to exist is to orchestrate its
workers; stopping the play means stopping them. This is a behavior change to
`li kill <play_id>`, called out in Consequences.

### D3 — Document that settings `notify.on_terminal` already fires on kill

No code change. The ADR records the P4 finding as the supported contract:
`li kill` notifies a watcher on kill **iff** `notify.on_terminal` is configured in
`.lionagi/settings.yaml` (project or global). Operators running detached
background runs should configure `notify.on_terminal` in settings rather than rely
on a per-leg `cmd; notify` wrapper (which SIGKILL bypasses) or a per-run `--notify` override
(which the separate kill process cannot see). Each reaped child's terminal
transition emits its own envelope, so with settings-notify a `li kill <play_id>`
that reaps three workers fires the notify for each.

> **Amended 2026-07-27.** The notify contract in this decision shipped and holds.
> The last sentence does not: a play kill reaps no workers, so it fires one
> envelope for the play row itself and none for the workers, which keep running.
> A kill of a session or invocation still fires per entity as described.

### D4 — Persist a run's resolved `--notify` override (DEFERRED)

**DEFERRED.** To close the last notify case — a run launched with a per-run
`--notify` override rather than settings — the run would persist its resolved
`ResolvedNotifyHandler` spec (argv or python-ref, plus filter) on its own row, and
the kill process would load and register that spec before transitioning the entity,
so the override's handler fires from the kill process too.

Deferred because: (a) the settings path (D3) already covers the detached-run workflow,
(b) persisting a resolved handler spec means persisting an argv/command, which
needs the same no-secrets and no-shell hygiene the live resolver enforces
(`notify_settings._looks_like_shell`, argv-only), and that hardening is its own
scope. Target design retained here so it is not a lost design.

### D5 — Regression test for kill→terminal-emit and play-worker reaping

Add tests to `tests/cli/test_kill.py` that lock:

- `li kill <play_id>` reaps the play's seeded running session + invocation (their
  rows go `cancelled`) and blocks the play row.
- A kill of a session/invocation/play emits exactly one `RunTerminalEnvelope` to a
  test-registered `TerminalCallbackRegistry` handler, with the right
  `terminal_status` (`cancelled` / `blocked`) and `reason_code` — locking the P4
  behavior so a future refactor of the emit condition cannot silently strand the
  kill-path notify.
- A play with `NULL`/dangling `session_id` blocks the play row and warns, reaping
  nothing (no crash).

> **Amended 2026-07-27.** The first and third bullets test D1/D2 and are
> withdrawn with them; no test can lock a reap that cannot happen. What shipped
> instead is a test that a play kill leaves both worker rows running, marks the
> play row `blocked`, reports the workers it did not stop naming that same
> status, and exits non-zero. The second bullet, the terminal-envelope test, is
> unaffected and did ship.

## Consequences

> **Amended 2026-07-27.** The first two bullets describe D1/D2, which are
> withdrawn. What actually shipped is corrected inline below; see Amendment 1.

- ~~**Easier:** the operator expectation becomes true — one `li kill <play_id>`
  stops a detached play and its workers; no manual child-id enumeration.~~ It did
  not become true. The caveat that `li kill` does not stop detached `li play`
  workers stands, and is now stated by the command itself at the moment it
  applies rather than left to documentation.
- ~~**Behavior change (D2):** `li kill <play_id>` now terminates the worker
  processes, where before it only blocked the row.~~ The worker processes were
  never terminated. The behaviour change that shipped is the report: a play kill
  marks the row `blocked`, says which processes it did not stop and how to find
  them, and exits non-zero.
- ~~**Harder / new failure modes:** the transitive walk touches more processes per
  kill; a partial reap (one child identity-mismatches or is already dead) is now a
  normal, reported outcome rather than an all-or-nothing.~~ There is no transitive
  walk; it was removed with D1. A play kill touches no worker process, so there is
  no partial reap to make legible.
- **Maintenance:** a contributor must know that ~~(a) plays reach workers via
  `plays.session_id`,~~ (a) plays do **not** reach workers — neither end of the
  pair references the other on the path that creates them, which is why the kill
  reports rather than reaps; (b) the terminal-emit condition in `service.py` is
  what makes kill-path notify work and is covered by a test; (c) the per-run
  `--notify` override case is a documented DEFERRED gap, not an accidental one.
- **Cost of reversal:** D1/D2 were localized to `cli/kill.py` and did revert
  cleanly, which is how they came out. D3 is documentation. D5 is additive tests.

## Current-vs-ideal delta

> **Amended 2026-07-27.** Rows 1, 2 and the reaping half of row 4 were delivered
> as code that reaches nothing, and are withdrawn — see the Status column. They
> are listed rather than deleted so the record shows what was attempted.

| # | Delta | Size | Status |
|---|-------|------|--------|
| 1 | Add `play → session[→ invocation]` transitive reaping to `_list_running_children` + recursive driver | M | WITHDRAWN — the link it resolves through is never bound |
| 2 | Make `li kill <play_id>` reap workers by default (D2) | S | WITHDRAWN — depends on row 1 |
| 3 | Document settings `notify.on_terminal` as the kill-notify contract; default-config recommendation | S | Delivered |
| 4 | Regression tests: play-worker reaping + kill→terminal-emit (D5) | S | Terminal-emit delivered; the reaping test withdrawn with rows 1-2 |
| 5 | DEFERRED: persist resolved per-run `--notify` override for the kill process (D4) | M | Still deferred |
| 6 | Report the unreachable workers, exit non-zero, and name the status actually written | S | Delivered in place of rows 1-2 |
| 7 | Bind a play to the sessions it starts, so a play kill can reach them at all | M | Open — the capability rows 1-2 assumed already existed |

## Alternatives considered

> **Amended 2026-07-27.** Both process-model alternatives below were rejected in
> favour of reaping the recorded worker entities, and that comparison assumed the
> recorded link exists. It does not. So the rejections stand on their own
> reasoning but not on the comparison that decided them, and the option the
> amendment actually took — report the workers the command cannot reach, and exit
> non-zero — is not among them because it was not considered.

- **Require `--recursive` for plays (keep bare `li kill <play_id>` row-only).**
  Rejected: preserves the exact footgun reported — the common invocation keeps
  stranding workers. A play with no PID has no useful row-only kill semantics.
- **Give plays a PID and SIGKILL the play process directly.** Rejected: a detached
  play's process model is orchestrator-over-workers; the play process may already
  have exited while workers run, and killing a single "play PID" would not reach
  workers in separate process groups. Reaping the recorded worker entities is both
  more precise and reuses the existing PID-kill path per worker.
- **Emit a distinct `killed` terminal status/event separate from the normal
  terminal envelope.** Rejected: the lifecycle service already emits a
  `RunTerminalEnvelope` carrying `terminal_status` (`cancelled`/`blocked`) and
  `reason_code` (`CANCELLED_MANUAL_KILL` / `CANCELLED_FORCE_KILL`); a watcher can
  distinguish a kill from a natural completion by `reason_code` already. A parallel
  event type would duplicate the contract.
- **Have the kill process reconstruct the killed run's per-run `--notify` from its
  run manifest.** Deferred, not rejected — see D4. It closes a real (narrow) case
  but needs the persisted-argv hygiene work first.

## Notes

Grounded in a source read at commit `18d51911a` (lionagi 0.29.0). The terminal
callback layer this ADR relies on shipped in 0.29.0 as the generic on-terminal
callback layer; ADR-0058 owns the unified lifecycle transition service whose
committed-write emit point is the anchor for D3/D5.
