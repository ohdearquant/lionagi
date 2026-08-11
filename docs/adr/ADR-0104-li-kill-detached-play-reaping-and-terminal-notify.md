# ADR-0104: `li kill` reaping of detached-play workers and terminal-notify on kill

- **Status**: Accepted (2026-07-15), amended 2026-07-27 — see Amendment 1
- **Kind**: Aspirational (records the target state)
- **Implementation-status**: partial — D3/D5 (terminal-notify on kill) are on main;
  D1/D2 (play-worker reaping) shipped code that cannot reach a worker; that code is
  removed and the guidance corrected (see Amendment 1)
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
  design.
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
