# ADR-0108: Agent-run steering at the turn-end boundary

- **Status**: Accepted (2026-08-02, PR #2773); amended 2026-08-02 — the claim
  condition binds every consumer of a claimed row, not only the agent drain
- **Kind**: Aspirational
- **Area**: scheduling-control-plane
- **Date**: 2026-08-01
- **Relations**: extends ADR-0069

## Context

ADR-0069 gave flow and playbook runs a live control plane: `li o ctl pause|resume|msg`
writes a `session_controls` row from a separate process, and the run's own control
poller consumes it, rendering operator messages into an operation's context immediately
before its next provider call. Agent runs (`li agent`, and every surface that spawns
one) had no equivalent. An operator who learned mid-run that the instruction was wrong
had exactly one lever: kill the run and resubmit with a new prompt, discarding the
leg's accumulated context.

The structural difference that forced a different seam: a flow executes many operations
with provider-call boundaries between them, so a steer can land "before the next op."
An agent run is a single `branch.operate()` call. For CLI-backed engines the whole leg
is one long provider conversation. There is no interior boundary at this layer, and
pretending otherwise (injecting into a running subprocess conversation) would be
engine-specific and fragile.

The cost of that missing lever was measured during this ADR's own authoring. A leg was
already running when a new constraint arrived for it. Nothing could deliver the
constraint to the running turn, so the only available response was to let the turn
finish, check its output against a requirement it had never been told about, and spend a
second full run correcting the difference. A one-sentence redirect cost an entire
additional leg.

That is the shape of the problem generally. The information that would change a leg's
course usually arrives while the leg is running, because that is when someone is
watching it. An operator holding new information and a running agent has three options
today: interrupt and lose the turn's accumulated work, wait and pay for a second run, or
say nothing. Steering at the turn-end boundary adds the fourth, and it is the one an
operator reaches for first.

It also explains why the receipt matters more than it looks. An operator who cannot tell
"queued, will apply when this turn ends" from "lost" will not use the feature, because
waiting in silence is indistinguishable from the option they already had.

## Decision

Extend the ADR-0069 transport — not a new mechanism — to agent-kind sessions, with the
steer landing as a warm continuation turn at the run's next turn boundary.

1. **Transport unchanged; run-id addressing is new.** `li o ctl msg <id> "text"`
   enqueues a `session_controls` row with verb `message`. No schema change. Id
   resolution for session, invocation, play, and their prefixes is the generic
   resolver ADR-0069 already uses — that resolver has no `runs` table and never
   did. A run id (`LIONAGI_HOME/runs/{run_id}/`, allocated in `cli/_runs.py`) is
   a separate id space, mirrored onto a session only through the nullable
   `sessions.run_id` column. It is also the handle an operator is most likely to
   hold for an agent leg — `li agent` prints it back, and the job surface this
   gate serves takes `run_ids` — so this work adds a run-id fallback consulted
   after the generic sweep comes up empty, the same shape as the pre-existing
   branch-id fallback (`status._resolve_session_by_branch_id`): exact id first,
   then an unambiguous prefix, with a colliding prefix refused rather than
   picked. `sessions.run_id` carries no uniqueness constraint and
   `StateDB.get_sessions_for_run` already documents why: "one run can persist
   more than one session." The fallback follows that existing contract and
   returns the most recently updated one rather than an arbitrary row.

   Because the fallback runs only after the generic sweep misses, the two id
   spaces are consulted in order rather than together. Ordering them is not the
   same as keeping them apart. A prefix that matches both an entity primary key
   and a `sessions.run_id` is genuinely ambiguous, and resolving it by
   precedence answers a question the input does not settle. Callers of this
   unscoped resolver act on that answer — `li o ctl` queues a control, `li wait`
   blocks on a run, checkpointing writes against it — which is the
   same reason sessions, invocations and plays are searched together rather than
   one after another. So a cross-space collision is refused, naming both
   candidates, instead of resolving to the primary key.

   A full-length run id cannot collide, and the reason is the alphabet rather
   than the length: run ids are `YYYYMMDDTHHMMSS-hex6` (`_new_run_id`,
   `cli/_runs.py`), so they carry a `T` and a `-` at fixed positions that a
   hexadecimal primary key can never hold there. Length alone would not be
   enough — a pure-hex string of the same width could still prefix-match a
   longer hex key. Those non-hex characters remain part of the contract, and
   changing the run-id format to pure hex would widen the set of colliding
   prefixes. What stays exposed is therefore the short prefix: a run id opens
   with a date, and every digit of a date is valid hex, so a date-length prefix
   can fit a UUID as well.

   The refusal lives in `status._resolve_any_target`, the unscoped resolver, and
   not inside the generic `resolve_entity`. That is deliberate: putting it in
   the generic resolver would change what a prefix collision means for callers
   that pass explicit `tables=` and never consult the run-id space at all,
   including `monitor.py`, `kill.py`, and `status.py`'s per-kind resolvers.
   Ambiguity *within* the run-id space was already refused rather than picked;
   this makes ambiguity *across* the two spaces behave the same way, without
   touching the callers that only ever search one.

2. **Per-verb consumer gate.** The enqueue gate becomes verb-aware
   (`_CONSUMER_KINDS_BY_VERB`): `message` is consumable by `flow`, `play`, and `agent`
   kinds; `pause` and `resume` remain `flow`/`play` only and are refused for agent
   kind with an explanatory message. A single `operate()` has no pause seam, and a
   refusal that says so beats a queued row nobody will read. Kinds with no consumer
   for the requested verb are refused for the same reason as before: a queued control
   nobody reads would sit pending forever.

3. **Agent-kind is not sufficient — the session must be owned by a run.**
   `invocation_kind="agent"` does not imply a lionagi runner exists. Both
   transcript mirrors write agent-kind sessions: `claude_mirror` stamps
   `agent_name="claude-code"` and `codex_mirror` stamps `agent_name="codex"`,
   and a mirrored session sits at `status="running"` for as long as the
   external tool is live. Nothing drains those rows, so widening the gate on
   kind alone would accept steers that can never be delivered — reintroducing,
   at enqueue, exactly the never-lands outcome this design exists to prevent.
   The discriminator is `run_id`: the agent runner stamps one on every session
   it creates, and neither mirror does. Agent-kind `message` controls
   therefore additionally require a non-null `run_id`, and fail closed without
   one. This was found by an acceptance run whose steer was enqueued against a
   mirrored session rather than the leg under test; the run is what turned an
   invisible exposure into a refusal plus a regression test.

4. **Turn-end drain in the agent runner.** When the in-flight `operate()` returns and
   the run would otherwise finalize, the runner drains pending `message` controls:
   each batch is stamped `applying:<run id>` (same crash-recovery stamp order as the
   flow poller), joined in arrival order into one continuation `operate()` turn on the
   same warm branch — framed as a live correction from the operator who started the run,
   not a claim of override authority over the original instruction — then stamped
   applied. Steers enqueued during a
   continuation are caught by the next drain iteration. Continuation turns persist
   through the same stream/snapshot directories, so the run record remains one run
   with more turns.

   The claim names the leg because more than one leg can be alive on a branch, and a
   bare `applying` cannot distinguish "someone else is mid-apply" from "I am". The
   stamp is a compare-and-set on an unclaimed row, so two drains reading the same
   pending row cannot both send it; the loser leaves it alone. `claimed_at` is stamped
   with it. The terminal write back to `applied` carries the same claim string as a
   condition, so it lands only while this leg still holds the row.

   That condition is a compare-and-set between cooperating consumers and **not an
   authorization boundary**, and the difference is worth stating because the two read
   alike. What it rules out is a consumer writing an outcome onto a row whose state it
   has not re-read: a claim that has moved on since the write was decided causes the
   write to land nowhere instead of overwriting. What it cannot rule out is a consumer
   that means to, since the claim lives in a column every reader can see and anything
   able to call the method is equally able to write the row directly. Nothing reachable
   from a shared database can do better, and the honest scope of the guarantee is the
   accidental case rather than the adversarial one.

   **The condition binds every consumer of a claimed row, and originally it did not.**
   The paragraph above describes the protocol the agent drain follows; the flow poller
   did not follow it. Its finalize calls on the already-claimed path carried no
   condition, which is `WHERE id = :id` with no predicate on the outcome, so a poller
   returning late overwrote whatever the row had come to hold. The ordering that
   exposes this is the one `ctl resolve` is built for rather than an exotic race: the
   verb exists for a claimant that has not reported back, so the case it serves is a
   slow claimant, which is exactly the state a person resolves by hand. Claim, resolve,
   claimant wakes and stamps over the resolution. The reject paths were the worse half,
   turning a delivery a person recorded as applied into one the record says never
   arrived.

   Two things follow, and both are now the design. **The claim is returned by the code
   that writes it** rather than rebuilt by callers: a guard only guards while the
   expression that produces it and the expression that stored it agree, and two copies
   of a string are what stop agreeing. **A guarded write that lands nowhere says so**:
   the drain reports the refusal instead of discarding it, because a receipt for a
   write that did not happen is the same defect as the overwrite, one level up.

   **What this guarantees, and what it does not.** The guarantee is about the record
   only: a row's outcome is written by whoever still holds the claim, and no writer
   silently replaces an outcome it does not own. It is not a guarantee about the
   effect. The effect and the record are not written together, and on the agent drain
   the effect comes first: the operator message is handed to the branch, the
   continuation turn runs, and the finalize happens after it returns. A hand
   resolution landing inside that window is refused nothing — it owns the row, so it
   wins — and the result is a row reading `abandoned` for a message that was already
   delivered to the model. The drain reports that disagreement rather than hiding it,
   which is all it can do from where it stands; the delivery cannot be recalled. The
   flow poller has the same shape, with the delivery written into executor context
   before its own finalize.

   Closing that gap means fencing the effect against the claim, so a delivery whose
   claim has been revoked cannot land. That is a larger change than this decision
   makes, it belongs to the transport rather than to either consumer, and it is
   deliberately out of scope here rather than absent by oversight.

5. **Receipt visibility without mid-turn delivery.** The runner's 60-second heartbeat
   reports a queued steer ("lands at end of current turn") so the operator knows it
   was received while the turn is still executing. The heartbeat is armed
   unconditionally, not only when `--timeout` is set: a leg spawned without a
   timeout is exactly the case where a turn can run long enough for the receipt
   to matter, and a receipt that silently depended on an unrelated flag would
   read as "received" everywhere it fires and as nothing at all — not "lost",
   just absent — everywhere it doesn't. The added cost is one sleeping
   coroutine per leg, negligible next to the provider call it watches. No
   attempt is made to deliver mid-turn; true mid-turn injection requires
   engine-level stdin support and is a separate future decision.

## Pinned edge semantics

**Terminal race — a steer is never silently swallowed.** Two ends own it:

- Enqueue carries the running-status condition inside the insert statement rather than
  in a caller-side check before it. A read followed by an insert is two statements and
  a run can terminalize between them; evaluated by the insert itself, the condition
  admits a control only against a session that is still running at the moment the row
  is written, and a control aimed at a run that has already stopped inserts nothing and
  is refused, pointing at `li agent -r`.
- **On PostgreSQL the condition additionally locks the session row, because evaluating
  it is not on its own enough there.** SQLite serialises writers, so a condition in the
  statement is decisive. PostgreSQL runs two clients at once and evaluates the condition
  against a READ COMMITTED snapshot, so an admission can pass while another transaction
  is terminalizing the same session and commit after that run's sweep has already looked
  — leaving a committed, pending, consumerless row, which is the outcome the condition
  exists to prevent. Measured on PostgreSQL 16 through this method: the unlocked form
  admits, the terminalizing transaction's sweep sees zero rows, and one row is pending
  once the admission commits. Taking `FOR UPDATE` on the session row in the insert's
  source makes a concurrent terminal transition wait for the admission instead of
  passing it, which restores the property the SQLite path has by construction. The wait
  is bounded by a single-statement insert.
- The remaining window (enqueued while running, run reaches terminal before the drain
  sees it) is closed consumer-side: run teardown finalizes the pending controls for its
  session that no consumer ever claimed as `rejected: run reached terminal status
  before the steer could land — use \`li agent -r\`` (claimed rows are the bullet
  below). Teardown already runs on every terminal path. The
  tombstone runs after that teardown, not before it, which is what normally leaves the
  two ends with no gap between them: a control that got in was admitted while the
  session still read running and is therefore committed before the terminal transition
  and visible to the sweep, and a control arriving after it is refused at the writer.
  Teardown can also fail and return the status it was asked for without having written
  it, so the sweep re-reads the stored session and declines a non-terminal one rather
  than trusting the call order. The tombstone is skipped when auto-resume keeps the run
  alive, because the resumed leg's own drain will consume the steer.
- **A claimed row is never resolved by anything but its claimant.** The tombstone
  finalizes only rows no consumer ever claimed, and that condition rides the write
  rather than being read off the pending-row snapshot. The distinction is the whole
  guarantee: another leg sitting at its own turn boundary can claim a row and hand the
  steer to the model between the sweep's read and its write, and an unconditional write
  would then record a delivered message as never delivered while the claimant's own
  guarded finalize correctly refused, leaving the false outcome as the one that
  survived. A claimed row belongs to the leg named
  in its claim, which may be another leg still inside its provider call or one that died
  between the claim and the apply, and `rejected` would assert that the message was not
  delivered, which nothing at teardown knows. The row stays visible as claimed. Nothing
  auto-resolves it on a timer either, because a timer would record the same guess with a
  delay. The status surface renders the owner and the claim's age so the operator who
  finds the wedge can decide dead-versus-slow there, and `li o ctl resolve <control-id>
  --as applied|abandoned [--by <who>]` is how they then close it. That verb exists because
  the design requires a human to end this state, and a state nothing in the product can
  end is not a degraded state but an abandoned row. It refuses anything that is not a
  claimed row, so it can neither overwrite an outcome a consumer recorded itself nor
  stand in for the teardown sweep. It records who resolved it, defaulting to the account
  running the command rather than to a placeholder, because an operator action that
  leaves no operator identity recreates one level up the dead end it is closing. It keeps
  the claim it replaces verbatim, since the record of who held a message and who then
  decided about it is the reason the row was kept standing at all. And it reports nothing
  when its own conditional write matches no rows, which is what happens on PostgreSQL if
  the claimant reports back between the resolver's read and its write: the compare-and-set
  refuses correctly, and returning the composed result anyway would hand the operator a
  receipt for a write that never landed. Locking the row on that read instead was tried
  and removed, because the UPDATE takes the same row lock a moment later and the lock
  therefore changed no outcome.

  **Why the verb is not on the MCP surface**, where it is catalogued as unavailable with
  a reason rather than simply omitted. Every other control verb reports something the
  system can observe: a queue read, a status, an enqueue. This one records a finding
  about a delivery the system explicitly cannot determine — that is the whole reason the
  row was left standing. A machine caller reaching for it would not be reporting that
  finding, it would be manufacturing it, because it holds exactly the knowledge the row
  is waiting for, which is none. The verb would still write, the row would still close,
  and the resulting record would read the same as one a person stood behind. The cost of
  the omission is that an agent noticing a wedged control must surface it to a person
  instead of clearing it, which is the intended cost: an operator's judgement is the
  input, and a surface that accepts a substitute for it produces confident rows nobody
  checked. It is catalogued rather than hidden so a caller that goes looking finds the
  reason instead of an absence it has to interpret.
- **Forced-consumer honesty.** The tombstone's failure path logs, but that log has no
  forced consumer and is not the guarantee. The guarantee is state-shaped and computed
  at read time: the status surface renders an unclaimed pending control on a terminal
  run as "never landed — use `li agent -r`" (text and `--json` views), derived from row
  status plus session status at query time. It therefore holds even when the tombstone
  write itself failed, and its consumer is the operator's own status query — exactly
  the process that runs when someone cares whether a steer landed. A claimed row on a
  terminal run is not folded into that rendering: its consumer took it and did not
  report back, so whether the message reached the model is the one thing nobody knows,
  and printing "never landed" there would be the same fabricated negative the tombstone
  declines to write.

**Timeout budget — same wall clock, no extension.** Continuation turns spend whatever
remains of the run's original `--timeout`, measured from leg start. The budget preamble
the agent saw at spawn stays true, and steering cannot keep a leg alive indefinitely: a
drain that reaches the deadline stops without consuming, leaving the remaining rows to
the terminal tombstone. A steer arriving near the deadline runs its continuation with
the remaining budget under normal timeout semantics; its row still stamps applied, so
the attempt is visible.

The deadline gates when new provider work may **start**, and it is checked again after
the queue read and before anything is claimed, because that read is I/O and can cross
the deadline on its own. Recording the outcome of work already performed is exempt from
it, and the reason is the same one that governs the claim protocol above: a finalize
skipped to honour the clock would leave a message that was delivered on record as
undelivered, which is a worse artifact than a write that lands a moment late. The write
is not unguarded for being unclocked — it carries this leg's claim as its condition, so
a late finalize closes the row this leg still holds and lands nowhere if the claim has
moved on. That is the cooperative bound described above and not a stronger one; what it
buys here is that a write decided before the deadline cannot be applied to a row whose
state changed after it. What the deadline therefore bounds is the leg's provider work,
not its bookkeeping; a run that ends a fraction past its budget having written one
terminal row is the intended behaviour, not a violation of it.

## Considered and rejected

- **A new steer channel for agent runs** (dedicated table, file drop, or message-bus
  pattern): rejected — one transport with two consumers is strictly less machinery
  than two transports, and `ctl status` observability comes free.
- **Extending the run's timeout to cover continuations**: rejected — it makes the
  spawn-time budget statement false and gives the steer surface an unbounded
  keep-alive property.
- **pause/resume for agent kind**: rejected in this ADR — no honest seam exists inside
  a single `operate()`. Revisit only with engine-level support.
- **Mid-turn injection via engine stdin**: deferred, not rejected — it is
  engine-specific (per-engine input protocol), and turn-end delivery already converts
  the motivating failure (kill + cold resubmit) into a warm redirect.

## Consequences

- Operators steer running agent legs with the same verb and id resolution they use for
  flows, addressed by session, invocation, play, branch, or run id; the redirect lands
  with context intact instead of after a kill.
- A steer's fate is always observable as exactly one of: refused at enqueue, applied,
  rejected by tombstone, pending-on-terminal rendered as never-landed, or claimed by a
  named leg that never reported back. The last one is the honest name for the case the
  other four cannot cover, and it is deliberately not resolved for the operator: it
  carries the owner and the claim's age so they can find out which it was, and
  `li o ctl resolve --as applied|abandoned` puts their answer on the record. That is the
  whole of what this design can truthfully offer when the consumer of a non-idempotent
  message disappeared mid-apply, and the operator verb is the part that keeps it a
  degraded state rather than an abandoned one.
- Agent-kind `message` semantics ("continuation at next boundary") differ from flow
  semantics ("context render before next op"). The gate refusals and the enqueue
  acknowledgment text ("lands as a continuation turn" vs. "applies within ~2s while
  the flow is live") both carry the distinction; `li o ctl status`'s pending/applied/
  rejected/never-landed rendering does not restate it and does not need to, since it
  reports outcome, not delivery mechanism. Documentation of `li o ctl` must state the
  distinction too.
- The drain adds up to one `list_pending_session_controls` read per turn boundary and
  one per heartbeat tick — negligible against a provider call. The heartbeat itself
  now runs for the life of every leg, timeout or not, for the same reason.
