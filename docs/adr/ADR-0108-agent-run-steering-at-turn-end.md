# ADR-0108: Agent-run steering at the turn-end boundary

- **Status**: Proposed
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

## Decision

Extend the ADR-0069 transport — not a new mechanism — to agent-kind sessions, with the
steer landing as a warm continuation turn at the run's next turn boundary.

1. **Transport unchanged.** `li o ctl msg <id> "text"` enqueues a `session_controls`
   row with verb `message`. Id resolution (session, invocation, run, prefix) is the
   generic resolver ADR-0069 already uses. No schema change.

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
   warm branch — prefixed as an operator redirect that supersedes conflicting parts of
   the original instruction — then stamped applied. Steers enqueued during a
   continuation are caught by the next drain iteration. Continuation turns persist
   through the same stream/snapshot directories, so the run record remains one run
   with more turns.

5. **Receipt visibility without mid-turn delivery.** The runner's 60-second heartbeat
   reports a queued steer ("lands at end of current turn") so the operator knows it
   was received while the turn is still executing. No attempt is made to deliver
   mid-turn; true mid-turn injection requires engine-level stdin support and is a
   separate future decision.

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
  flows; the redirect lands with context intact instead of after a kill.
- A steer's fate is always observable as exactly one of: refused at enqueue, applied,
  rejected by tombstone, or pending-on-terminal rendered as never-landed.
- Agent-kind `message` semantics ("continuation at next boundary") differ from flow
  semantics ("context render before next op"). The gate refusals and status output
  carry the distinction; documentation of `li o ctl` must state both.
- The drain adds up to one `list_pending_session_controls` read per turn boundary and
  one per heartbeat tick — negligible against a provider call.
