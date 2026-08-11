# `lionagi/cli/agent.py` internals

Design rationale for `li agent` extracted from inline comments/docstrings during the
comment/docstring density pass. Each heading is pointed to by a
`# See docs/internals/agent.md#<anchor>` comment or docstring line at its call site.

## tombstone-unclaimed-steers

`_tombstone_pending_steers`: a steer enqueued while the run was live but never drained
must not sit pending forever. Best-effort — a failure here logs and leaves the row
visibly pending; the status surface independently renders a pending control on a
terminal run as never-landed, so the operator still sees the truth.

Only rows no consumer ever claimed are tombstoned, and that is enforced at the write
rather than read off the snapshot. A claimed row belongs to the leg that took it,
which may be another leg still inside its provider call, or one that died between the
claim and the apply. Rejecting either would assert that the message was not
delivered, which nothing here knows; the row stays visible as claimed, carrying its
owner and its age, for an operator to resolve. Called after the run's teardown, so a
control admitted against a still-running session is normally already committed and
visible to the read below, and one arriving later is refused at the writer; the
terminal check at the top of the function is what makes that hold when teardown
failed to persist the transition it was asked for.

## direct-path-terminal-notice

`_deliver_direct_notice` (in `_run_agent`): sends this run's one terminal notice on
the no-persistence route. No session entity ever existed for this run, so the
registered notify path was never reached and nothing else will ever deliver for it.
Which makes *when* this is called the whole question: it reads `_terminal_status` at
call time, and a status is not this run's answer until every later line that can
still change it has run.

Shielded, because the guarantee it exists to provide is that a terminal notice
arrives, and the caller is a teardown path where a cancellation is exactly what is
expected to arrive.

Idempotent by flag rather than by the caller being careful: it is reached from a
`finally` and from the ordinary tail, and those two overlap on nothing today, which is
the sort of thing that stays true only until someone adds a branch.

## terminal-race-tombstone-ordering

In `_run_agent`'s teardown `finally` block: the tombstone sweep runs after
`teardown_agent_persist`, not before (skipped entirely when auto-resume keeps the run
alive — the resumed leg's drain will consume the steer instead). When that teardown
does persist the terminal transition, this ordering is what leaves no gap for a
control to slip through: the writer admits a new control only while the session reads
`running`, so a control that got in is committed before the transition and is
therefore visible to the sweep, and one arriving after it is refused at the writer
instead of landing on a terminal run with nobody left to consume it. Teardown can also
fail and return the requested status without having written it, which is why the
sweep re-reads the stored session and declines a non-terminal one rather than trusting
call order.

That ordering also means the handle in `live` is gone by the time the sweep runs —
teardown closes it in its own `finally`. The sweep opens a fresh `StateDB` connection
rather than reusing the corpse, because a sweep that fails on a closed engine would
fail into its own must-not-raise catch, turning the entire tombstone path into one log
line per run while the rows it exists to close stay pending forever. The connection is
opened here (not inside the sweep) so callers that hand it a handle of their own,
including tests, keep doing so — but still inside the same best-effort boundary as the
sweep: `StateDB` re-raises out of `__aenter__`, so an unguarded `async with` outside
that boundary would turn a completed run into a reported infrastructure exception.
