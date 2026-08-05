# ADR-0109: A mirrored session's idleness is not its completion

- **Status**: Accepted (2026-08-02, PR #2776)
- **Kind**: Aspirational (records the target state)
- **Area**: persistence-state
- **Date**: 2026-08-02
- **Relations**: extends ADR-0105, ADR-0095

## Context

The transcript mirrors write a session's status from a silence window.
`reconcile_status` (`lionagi/state/_mirror_common.py:35-68`) computes
`live = (now - last_message_at) <= live_window`, wants `running` when live and
`completed` when not, and takes an override write on the way back out of a
terminal status. `live_window` defaults to 300 seconds
(`lionagi/cli/mirror.py:27`).

So `completed` on a mirrored session means "no transcript line for five
minutes". It does not mean the transcript ended. An agent that thinks for six
minutes is written `completed` and then un-completed on its next line. The
actors `claude-mirror-reconcile` and `codex-mirror-reconcile` do this on every
pass.

The mechanism is working as written. What is wrong is that one value carries two
meanings, which is the same failure ADR-0105 fixed one column over.

### Three consequences

**1. Consumers read a live agent as finished.** Verified at
`lionagi/studio/services/sessions.py:918-926`: `is_session_stream_done` closes a
session's live stream once the status is terminal and has been stable for
`SESSION_DONE_STABLE_SECS`. A mirrored session that goes quiet for five minutes
is marked `completed`, and sixty seconds later the Studio stops following it.
The reconciler will flip it back to `running` on the next transcript line, but
the stream has already been closed. A reader watching an agent work sees the
view stop updating while the agent is still going.

**2. The override audit channel fills with idling.** ADR-0105 chose an override
rather than a declared `terminal → running` edge specifically so that leaving a
terminal status stays rare and attributable, in its words keeping the exception
scoped to the one caller that has earned it, and it emits a
`status_transition_override` admin event to make each reopening visible. The
mirror makes reopening routine, so the channel built to make it legible is now
mostly noise. This erodes the property the override was chosen to protect.

**3. The terminal-delivery ledger is contaminated, latently.** `session` is in
`EXECUTION_ENTITY_KINDS` (`lionagi/state/lifecycle/callbacks.py:44`), so each
idle-to-completed write is a terminal transition on an execution entity and
joins the population `reconcile_unacknowledged` returns
(`lionagi/state/lifecycle/deliveries.py:43-80`), which is the durable
cross-process ledger rather than the in-process fire-and-forget registry.

This one is **latent under the default configuration, and live under a
configured one**. An earlier revision said flatly that nothing registers
terminal callbacks in the mirror process, and attached "checked rather than
assumed" to it, which made a claim that is only conditionally true read as
settled. What is actually true: both processes that host a mirror install a
process-wide terminal callback if, and only if, `notify.on_terminal` resolves
to a handler. The CLI entry point calls `register_settings_terminal_callback`
before dispatching any subcommand, `li mirror` included
(`lionagi/cli/main.py:674-678`, inside `_run` and ahead of argparse dispatch),
and Studio service startup calls it as well (`lionagi/studio/app.py:190-192`,
the same module that starts the in-process mirror); the
function registers when the setting resolves and unregisters when it does not
(`lionagi/state/lifecycle/notify_settings.py:648-662`). So with no
`notify.on_terminal` configured, `emit` is a no-op and this consequence is
latent; with one configured that permits sessions, an `idle → completed` write
reaches a live handler.
And no production code calls `reconcile_unacknowledged`: the only callers are
`tests/state/lifecycle/test_terminal_callbacks.py`, checked against the
definition site in the same search. It is a trap for the next consumer of that
ledger, which would find a queue of terminal events for sessions that never
terminated. If this reading is wrong the error runs toward understatement, since
absence of callers was verified rather than absence of effect.

### What is not the problem

The reconciler is **not** the write amplifier. A flip is roughly three rows (the
update, a transition row, an admin event) at roughly ten flips an hour, against
message insertion measured at 88 rows in 45 seconds, about 7,000 an hour. Two to
three orders of magnitude apart. The mirror's own message insertion is the write
volume and is inherent to mirroring live transcripts. That hypothesis is closed.

## The decision to make

The obvious remedy is to stop writing `completed` for idleness and let consumers
derive idleness from `last_message_at`. **That remedy has a hazard its own
proposer missed**, found while enumerating consumers for this note, and it is
recorded here rather than discovered later: if the mirror never writes a
terminal status, mirrored sessions stay `running` forever. Perpetually-running
is also a false claim, and it is the more dangerous one, because sweeps act on
`running` and ignore terminal. ADR-0105 enumerated exactly these: the phantom
reaper (`studio/services/lifecycle.py`), the health sweep
(`studio/services/admin.py`), `li kill --all-stale` and `li state doctor`, all
of which select `status='running'` and threshold on age. A mirrored session for
a transcript that ended a month ago would sit `running` and become sweepable.
Trading a false `completed` for a false `running` moves the defect toward the
consumers that mutate rather than the ones that display.

So the fork is real:

**(A) Never terminal from the mirror; derive idleness at read time.** Simplest
write path. Requires every `running`-selecting sweep to exclude mirrored
sessions explicitly, which is a new obligation on code that does not know the
mirror exists.

**(B) A distinct non-terminal `idle` status.** Says what is true, and sweeps that
select `running` stop seeing idle sessions for free. Costs a lifecycle policy
change (`lionagi/state/lifecycle/policy.py`), new declared edges, and a decision
at every consumer that currently branches on two states.

**(C) Terminal only on evidence the transcript ended, not on silence.** Keeps
today's shape and fixes the meaning. The difficulty is that the mirror reads
JSONL files and has no process handle, so "ended" has to be inferred from
something stronger than a longer window. Simply raising `live_window` is not
this: on its own it is the same conflation with a slower fuse, because the one
status still carries both meanings.

Note for anyone reading the amendment below against this list: **the amended (B)
does take a long window from (C)**, and that is not the contradiction it looks
like. What (C) alone fails to fix is the conflation, since it leaves a single
status meaning both "quiet" and "finished" no matter how the window is tuned.
(B) separates the two meanings into two statuses first, and only then uses a
window to decide the second one. The window stops being a disguise for a missing
distinction and becomes an ordinary threshold on a distinction that now exists.

**Recommendation: (B), amended.** Stated precisely, because an earlier draft
claimed more than this design delivers and the overclaim is the sentence an
implementer would quote: `idle` is *true*, in that it asserts only short-window
silence, which is exactly what was observed. `completed` after `ended_window`
remains an *inference* that the transcript ended, and a reversible one; the
amendment below is explicit that the error decays rather than disappears. What
(B) buys is that the two claims stop sharing one value, so the false-terminal
window shrinks from five minutes to a day and each consumer can tell which of
the two it is looking at. Against the three consequences: no terminal status at
all for a five-minute lull, so stream and view consumers stop being misled on the
common path; override events drop from one per lull to one per genuine
post-`ended_window` resume; and nothing enters the terminal-delivery ledger for a
session that merely went quiet. Its
cost is a one-time policy and consumer change, which is bounded and enumerable,
against (A)'s open-ended obligation on every future sweep author.

Note on consequence 2, since an earlier draft overclaimed here: this design does
not drive override events to zero, and it should not. See "Why `completed →
running` is retained" below. Zero would have meant no way back from a terminal
status at all, which is a worse defect than the noise it removes.

### The amendment, and the hole it closes

(B) as first drafted said only "stop writing terminal on silence". Read
literally that means the mirror never writes a terminal status at all, so **a
transcript that genuinely ends never reaches `completed` and sits `idle`
forever**. Invisibility to sweeps is exactly the property this note sells as
(B)'s benefit, and applied to a session that really is finished it becomes the
same defect in a new place: a permanently non-terminal row for work that ended.
The first draft also contradicted itself, because its migration section happily
applied an "idle beyond a day means ended" rule to existing rows while the
decision section had just refuted that rule as (C) with a slower fuse.

**So the conflation is not removed, it is moved to a granularity where it is
defensible, and that is stated as an accepted trade rather than hidden.** The
reconciler writes two things:

- `idle` when the transcript has been silent past `live_window` (300s today).
- `completed` when it has been silent past a much longer `ended_window`, on the
  scale of a day.

Why this is acceptable at a day and not at five minutes, which is the whole
argument and the reason a longer `live_window` alone is still refused: the cost
of a wrong terminal is the probability the session resumes multiplied by the
damage of having called it finished. At five minutes that probability is high,
which is why a live stream closes under a working agent. At a day it is low, and
the damage is low too, because nothing is streaming a transcript that has been
silent for a day and no reader is waiting on it. The error does not disappear,
it decays. Naming the window in the ADR rather than leaving it implicit is what
makes it reviewable.

`ended_window` is a value this ADR must fix rather than leave to implementation,
because it is the entire safety margin. Proposed: 24 hours, with the reasoning
that the longest plausible gap in a live agent transcript (an overnight pause on
a leg someone resumes the next morning) is under a day, and anything longer is a
session nobody is coming back to.

The alternative to the day-scale window is to name end-evidence the mirror can
actually observe, and this ADR does not have one. The mirror reads JSONL files
and holds no process handle; a Claude Code transcript carries no closing marker,
and file mtime says the same thing silence already says. If someone can produce
real end-evidence, that supersedes the window and is strictly better. Recording
that I looked and did not find one, rather than implying no one should look.

## Declared edges and who may write them

`idle` is a new non-terminal status, so the session policy
(`lionagi/state/lifecycle/policy.py`) gains edges rather than inheriting any.
Stated explicitly because the current policy declares exactly one edge,
`running → {terminal}`, and every addition widens what the whole system permits.

| Edge | Written by | Trigger |
| --- | --- | --- |
| `running → idle` | mirror reconcilers only | silence past `live_window` |
| `idle → running` | mirror reconcilers only | a new transcript line |
| `idle → completed` | mirror reconcilers only | silence past `ended_window` |
| `completed → running` | mirror reconcilers only, by override | a new transcript line on a completed session |
| `running → completed` | unchanged: existing writers | unchanged |

### Why `completed → running` is retained rather than removed

An earlier draft of this ADR had no path out of `completed`, on the reasoning
that a day-silent transcript is finished. **The weekend resume falsifies that.**
A leg goes quiet on Friday, becomes `idle` after five minutes and `completed`
on Saturday, and on Monday morning someone resumes it and new transcript lines
arrive. With no exit from `completed` those lines mirror into a terminal
session forever: stream closed, view dead, status false, for an agent that is
working. That is the original defect rebuilt at exactly the granularity the
probability-times-damage argument above claims is safe, and it shows the weak
premise in that argument. "No reader is waiting on a day-silent transcript" is
false at precisely the moment it matters, because the person resuming it is the
reader.

So the terminal reactivation ADR-0105 introduced is kept, restricted to the
mirror reconcile actors, and triggered only by new-line evidence on a
`completed` session.

This does not reintroduce consequence 2, and it is worth being exact about why,
because the obvious reading is that any retained override weakens the audit
channel. Today every five-minute lull emits an override event, which is what
makes the channel useless. Under this design the only event is a genuine resume
after `ended_window`, which is rare and is exactly the kind of thing the channel
was built to record. The count goes from routine to rare, and rare-and-real is a
better outcome than zero, because zero events would have been achieved by making
the reactivation impossible rather than by making it meaningful. Consequence 2
is closed more strongly with the override retained than without it.

`idle → running` is deliberately **not** an override and emits no
`status_transition_override` event, because `idle` is not terminal and leaving it
is ordinary. That is what removes consequence 2: the audit channel stops carrying
routine idling because there is no longer a terminal status being exited.

A mirrored session reaches `completed` through `idle`, never directly from
`running`, so the day-scale window is evaluated in one place.

**Writers of `idle` are restricted to the mirror reconcilers.** The reason is
sharp: `idle` is invisible to every sweep that selects `status='running'`, so a
non-mirror writer minting `idle` would hide a real, live session from the
phantom reaper, the health sweep, `li kill --all-stale` and `li state doctor`
all at once. A status whose purpose is to exempt rows from sweeps is a status
that must not be reachable by accident.

**What that restriction can and cannot be, stated before the mechanism, because
the difference decides what the mechanism is worth.** It is a check against
*accident*: code that writes a session's status without meaning to claim it is a
mirrored transcript. It is not, and at this layer cannot be, a check against
*impersonation*. `StateDB.update_status` takes `source` and `actor` as ordinary
caller-supplied strings (`lionagi/state/db.py:2649-2660`) and they arrive at the
lifecycle service inside an `ActorRecord` whose `id` is a plain `str`
(`lionagi/state/lifecycle/models.py:18-21`). Any caller in the process can pass
`actor="claude-mirror-reconcile"` and satisfy any identity rule built on that
field. There is no privilege boundary between callers of a Python module, so no
rule expressed in this layer can create one. Read the restriction accordingly:
it makes the intended writer explicit and turns a silent mistake into a
rejection, and a caller determined to write `idle` can still do so by naming
itself the reconciler. That is the honest ceiling, and pretending otherwise
would be the more dangerous document.

Mechanism. An earlier draft of this section said the restriction was enforceable
today by registering a `session.idle.*` reason prefix and having the transition
service reject a non-mirror actor. **That is wrong, and the way it is wrong is
worth recording, because it would have handed an implementer a false premise.**
Three things block it, each read at the source:

- `EdgePolicy` (`lionagi/state/lifecycle/models.py:69-73`) carries
  `actor_types`, not an allow-list of actor identities. Both reconcilers write
  as `system`, so an actor-type rule admits every system writer and restricts
  nothing that matters here.
- The mirror does not write through the edge-enforcing path at all. It calls
  `StateDB.update_status` (`lionagi/state/_mirror_common.py:46-68`), which the
  compatibility adapter routes to `service._transition`
  (`lionagi/state/lifecycle/adapters.py:98-99`) without passing
  `enforce_edges`. That parameter defaults to `False`
  (`lionagi/state/lifecycle/service.py:225`), and at
  `service.py:293` the declared edges are read as an empty tuple when it is
  false. So the declared-edge policy, `actor_types` included, is never
  inspected on the only path that writes a mirrored session's status.
- A reason prefix is not an authorization: `lionagi/state/reasons.py:282-304`
  validates exact registered codes, and the session policy admits the whole
  `session` prefix (`lionagi/state/lifecycle/policy.py:205-216`). Registering
  `session.idle.*` binds no code to either reconciler.

So the restriction is a requirement this ADR states and a capability the
lifecycle layer does not yet have. Implementing `idle` means implementing the
restriction with it, in this order:

1. Give the edge contract an identity-level rule, an explicit actor allow-list,
   alongside the existing `actor_types`. Type is the wrong granularity for a
   status whose whole purpose is to exempt rows from sweeps. Per the paragraph
   above, this catches the writer that did not mean to claim to be the mirror,
   which is the whole of what it is for.
2. Move the mirror's status write onto an edge-enforcing call, or give the
   compatibility adapter an enforcing variant, so that policy is consulted at
   all on that path. Without this step, step 1 is dead code.
3. Register the exact reason codes and bind them to the reconcile actors, rather
   than relying on a prefix.

Verification arms for the restriction are listed with the others below. An
earlier revision of this section got their target wrong in a way worth recording,
because it contradicted the step list directly above it: it required both arms to
run through `StateDB.update_status` on the grounds that this is the path the
mirror uses. That is true only *before* step 2. Step 2 moves the mirror off that
path, and before step 2 that path cannot reject the transition under test:
`StateDB.update_status` routes through `run_update_status`, which calls
`LifecycleService._transition` without `enforce_edges`
(`lionagi/state/lifecycle/adapters.py:98-100`), so the parameter takes its
default of `False` (`lionagi/state/lifecycle/service.py:225`) and the declared
edge graph is never consulted (`service.py:293`). That path can still reject two
things — a same-status move where the policy says `reject`, and leaving a
terminal status without an override (`service.py:341-342`, `service.py:365`) — but
`running → idle` is neither, so it falls through to the unrestricted write. The
requirement therefore asked for a test that is impossible before the change and
irrelevant after it. The requirement is therefore not "the current
path" but **the entry point the mirror calls once step 2 lands**: whichever
enforcing call or enforcing compatibility variant step 2 introduces, both the
positive and the negative arm run through that exact function, and the
implementation names it. Testing the legacy permissive `update_status` remains
worthwhile as a separate arm, but it asserts something different, namely that the
old surface stays deliberately permissive for its existing callers.

A declared edge alone would not be enough even after those three steps: edges say
what transitions exist, not who may perform them, which is the same distinction
ADR-0105 relied on when it chose an override over a declared `terminal → running`
edge.

## Consumers keying on session terminal status

This section enumerates rather than assumes, and states what was driven and what
was not. Population: 548 Python files under `lionagi/` (instrument armed by
confirming `_mirror_common.py` is inside it). Files naming
`SESSION_TERMINAL_STATUSES`: 13. Two of those are excluded from the table
below, and the reason is not that they fail to read the constant. `state/db.py`
defines it. `state/_mirror_common.py:41` reads it, to decide whether a terminal
status may be left, but it is the reconciler doing the writing this note is
about, so it is the subject of the change rather than a downstream reader of
it. That leaves the **11 downstream consumer files** enumerated below. Read
"11" as a description of the table and not as a count of everything in the tree
that reads the constant: the reconciler reads it, and `state/db.py` derives a
second name from it at `lionagi/state/db.py:371`.

| Site | Reachable from a mirrored row? | Basis |
| --- | --- | --- |
| `studio/services/sessions.py:918` `is_session_stream_done` | **Yes, and it is consequence 1** | read at the source; closes the live stream on terminal + 60s stable |
| `studio/services/run_resume.py:274` | **Yes if resume is offered on a mirrored row** | read at the source: `queued = source_status not in SESSION_TERMINAL_STATUSES`, so a falsely-terminal session launches a resume immediately instead of queueing it behind live work. **Whether the Studio offers resume for a mirrored session is UNVERIFIED and is the first thing to check.** |
| `studio/services/run_view.py:109,160` | Likely, display only | not driven |
| `studio/services/artifact_verification.py:62` | Unverified | not driven |
| `studio/services/run_resume_worker.py:66` | Unverified | not driven |
| `cli/monitor.py:1089,1101,1155` | Unverified | not driven |
| `cli/machine.py:529` | Unverified, display flag | not driven |
| `cli/_runs.py:557,707,884,1064` | **Yes, by construction, and this row was wrong in an earlier draft** | read at the source: `_linked_engine_session` (`:436-461`) resolves `session_db_id(engine_session_uid)`, which is the **Claude**-mirror row and only that one, and `:557` branches on that row's terminal status while `:576` handles it being `running`. An earlier draft wrote "the claude/codex-mirror row" and that is wrong: the resolver imports `session_db_id` from `lionagi.state.claude_mirror` (`lionagi/cli/_runs.py:451`), and the Codex mirror derives its ids from a deliberately different namespace so that the two can never collide (`lionagi/state/codex_mirror.py:69-71`, which says so in a comment). A Codex-mirrored session is therefore not reachable through this consumer at all, which is a **separate gap and not a covered case**: whatever `idle` does to a linked Claude row, the equivalent Codex row is not linked here in the first place. Adding `idle` gives a linked row a third value that matches neither branch, so a session that is merely quiet falls through to the phantom `failed` the `running` branch exists to suppress. See the obligation below. Separately, and unchanged from the earlier draft: the ADR-0105 reopen path at `:1064` is keyed by a branch id, and a mirror branch id is deterministic, so a hand-typed resume against one remains an open edge case. |
| `cli/agent.py:933` | Probably not: own run's session | not driven |
| `cli/orchestrate/_orchestration.py:1654` | Probably not: own run's session | not driven |
| `hooks/builtins.py:55,114` | Probably not: own run's session | not driven |

The unverified rows are not a claim that they are safe. Each must be driven
before this lands, because the entire point of the change is that these
consumers have been reading a value that did not mean what they took it to mean.

**Obligation from the `cli/_runs.py` row, since a wrong disposition there is what
this section exists to prevent.** Introducing `idle` is not additive for a
consumer written as a terminal branch plus a `running` branch: the new value is
in neither set, so it takes the fall-through. `_teardown_common` must decide
explicitly what a linked mirror row in `idle` means, and the answer is almost
certainly the same as `running`, since an idle transcript is a live session that
is quiet and suppressing the phantom `failed` is exactly as correct there. That
decision needs a regression driving a linked mirror row in `idle` through the
provider-error path, and it is the first arm to write, because it is the one
place where an ADR about statuses changes behavior in code that never mentions
the mirror.

The general form is worth stating once, because it applies to every row above
that this note marked unverified: **a status vocabulary change is a change to
every consumer that partitions statuses into two sets.** Adding a third value
does not preserve either branch; it silently creates a third path that nobody
wrote. Driving a consumer therefore means driving it with the new value, not
confirming that it still compiles.

## Migration of existing rows

Rows already written `completed` by a mirror reconciler fall into two sets, and
they cannot be told apart from the status alone, which is the defect restated.

- **Transcripts that genuinely ended.** `completed` is the right value. Under (B)
  they should stay terminal and need no migration.
- **Transcripts merely idle at the moment they were last reconciled.** These
  self-heal on the next transcript line, under today's code and under (B) alike,
  since the reactivation path is retained. Migration still matters for the ones
  that do not receive another line soon: until something arrives they read
  `completed` to every consumer, and the whole point of `idle` is that a quiet
  session should not be telling readers it finished. The migration is therefore
  a correctness fix for the resting state rather than the only escape from a
  permanently wrong row.

Proposed migration, to be run once as part of the change: for every session
whose last transition was written by `claude-mirror-reconcile` or
`codex-mirror-reconcile` to `completed`, re-derive from `last_message_at` using
**the same `ended_window` the reconciler will use going forward**, and rewrite
to `idle` those that fall inside it. The reconciler actors are recorded on the
transition rows, so the mirror-written set is selectable and no hand-maintained
list is needed.

The first draft of this section applied a day-scale rule here while the decision
section refused that same rule for new writes, which was an outright
contradiction and is the reason the decision now owns the window explicitly.
Migration and steady state must apply one rule, or the migration is quietly
deciding policy the ADR declined to state. With the amendment they do: a row
silent past `ended_window` is `completed` whether it got there by migration or
by a reconciler pass.

Two properties the migration must have. It must be **idempotent**, since a
partial run followed by a rerun is the expected failure mode. And it must
**not touch sessions whose terminal status was written by anything other than a
mirror reconciler**, because those are real completions and rewriting one to
`idle` would resurrect a finished run in every sweep that selects non-terminal.

## Verification

- A regression that a mirrored session going quiet past the window does **not**
  become terminal, and that its live stream stays open. This must fail against
  today's code, or it is testing nothing.
- A regression that a genuinely ended transcript does reach a terminal status,
  as the control. Without it, an implementation that simply never marks anything
  terminal passes the first test.
- A regression that the migration leaves non-mirror terminal sessions untouched,
  driven with one mirror-written and one run-written terminal session in the
  same fixture, since a migration that selects too widely passes any test that
  only checks the rows it was supposed to change.
- An assertion that no `status_transition_override` event is emitted by a
  reconciler pass over an idle session, which is consequence 2 stated as a check.
- A regression that a session silent past `ended_window` **does** reach
  `completed` through `idle`. This is the amendment's own control: without it,
  an implementation that only ever writes `idle` passes every test above, which
  is precisely the hole the first draft of this ADR shipped with.
- A regression that a transition targeting `idle` from an actor that is not a
  mirror reconciler is **rejected**, paired in the same run with a mirror actor
  performing the same transition successfully. Asserting only the rejection
  would pass against a policy that refuses `idle` from everyone, which would
  disable the feature while looking enforced. Both arms go through the entry
  point the mirror calls *after* step 2 of the mechanism, which the
  implementation names; running them against today's `StateDB.update_status`
  would assert nothing, since that path never consults the declared edge graph
  and so rejects no actor for a nonterminal move like `running → idle`, whoever
  they claim to be. A third arm on the legacy path is worth keeping, asserting the
  opposite: that the permissive surface stays permissive for its existing
  callers.
- A regression that a linked **Claude**-mirror session in `idle` is treated as
  live by `cli/_runs.py`'s provider-error reconciliation, not left to the
  fall-through that yields a phantom `failed`. Drive it with three linked rows
  in one fixture, terminal, `running` and `idle`, since an implementation that
  folds `idle` into the terminal branch also passes a test that only checks
  `idle` alone did not crash. The arm says Claude because the resolver reaches
  only Claude rows; do not write it as a mirror-agnostic arm, because a passing
  mirror-agnostic name over a Claude-only fixture is how the Codex gap above
  would come to look covered.
- A regression that `idle → running` produces no override event and no row in
  the terminal-delivery ledger, driven by reading `reconcile_unacknowledged`
  before and after. This is the only check that covers consequence 3, which has
  no live consumer today and so will not surface any other way.
- The weekend resume, as one run with both arms: a session taken past
  `ended_window` to `completed` and then given a new transcript line must reach
  `running` and emit **exactly one** override event, paired in the same run with
  the `idle → running` case above emitting none. Two arms because each alone
  admits a wrong implementation: asserting only the reactivation passes against
  a design that emits an override on every idle lull, which is the noise this
  ADR removes, and asserting only the silence passes against a design with no
  exit from `completed`, which is the defect that made this arm necessary.
  Count the events rather than checking for presence, since "at least one" is
  satisfied by the routine-noise behaviour.
