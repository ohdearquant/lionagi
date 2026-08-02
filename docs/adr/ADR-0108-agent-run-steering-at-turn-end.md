# ADR-0108: Agent-run steering at the turn-end boundary

- **Status**: Accepted
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
   spaces are not searched together, and that has a consequence worth stating
   plainly rather than leaving for someone to discover. A prefix that matches
   both an entity primary key and a `sessions.run_id` resolves to the primary
   key, silently: the generic sweep finds it, returns, and the run-id fallback
   is never reached, so no ambiguity is reported even though the prefix was
   genuinely ambiguous across the two spaces. A full-length run id cannot hit
   this, and the reason is the alphabet rather than the length: run ids are
   `YYYYMMDDTHHMMSS-hex6` (`_new_run_id`, `cli/_runs.py`), so they carry a `T`
   and a `-` at fixed positions that a hexadecimal primary key can never hold
   there. Length alone would not be enough — a pure-hex string of the same
   width could still prefix-match a longer hex key. Those non-hex characters
   are therefore part of the contract this ordering depends on, and changing
   the run-id format to pure hex would silently reintroduce the collision this
   paragraph rules out. The reason to accept the asymmetry rather than merge
   the two searches is that merging would change what a prefix collision means
   for every existing caller of the generic resolver, including `monitor.py`,
   `kill.py`, and `status.py`'s per-kind resolvers, which pass explicit
   `tables=` and expect the current meaning. Ambiguity *within* the run-id
   space is still refused rather than picked; it is only ambiguity *across* the
   two spaces that resolves by precedence.

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
   each batch is stamped applying (same crash-recovery stamp order as the flow
   poller), joined in arrival order into one continuation `operate()` turn on the same
   warm branch — framed as a live correction from the operator who started the run,
   not a claim of override authority over the original instruction — then stamped
   applied. Steers enqueued during a
   continuation are caught by the next drain iteration. Continuation turns persist
   through the same stream/snapshot directories, so the run record remains one run
   with more turns.

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

- Enqueue keeps the existing running-status gate: a clearly-terminal run refuses the
  steer outright, pointing at `li agent -r`.
- The remaining window (enqueued while running, run reaches terminal before the drain
  sees it) is closed consumer-side: run teardown finalizes any still-pending controls
  for its session as `rejected: run reached terminal status before the steer could
  land — use \`li agent -r\``. Teardown already runs on every terminal path. The
  tombstone is skipped when auto-resume keeps the run alive, because the resumed leg's
  own drain will consume the steer.
- **Forced-consumer honesty.** The tombstone's failure path logs, but that log has no
  forced consumer and is not the guarantee. The guarantee is state-shaped and computed
  at read time: the status surface renders a pending control on a terminal run as
  "never landed — use `li agent -r`" (text and `--json` views), derived from row
  status plus session status at query time. It therefore holds even when the tombstone
  write itself failed, and its consumer is the operator's own status query — exactly
  the process that runs when someone cares whether a steer landed.

**Timeout budget — same wall clock, no extension.** Continuation turns spend whatever
remains of the run's original `--timeout`, measured from leg start. The budget preamble
the agent saw at spawn stays true, and steering cannot keep a leg alive indefinitely: a
drain that reaches the deadline stops without consuming, leaving the remaining rows to
the terminal tombstone. A steer arriving near the deadline runs its continuation with
the remaining budget under normal timeout semantics; its row still stamps applied, so
the attempt is visible.

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
  rejected by tombstone, or pending-on-terminal rendered as never-landed.
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
