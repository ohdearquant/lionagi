# CLI subprocess lifecycle internals

Design rationale for `lionagi/providers/_cli_subprocess.py` pulled out of
inline docstrings to keep the source to one-line invariant statements.
Source pointers back to here read `# See docs/internals/_cli_subprocess.md`.

## Redacting runtime fields before validation (`redact_runtime_fields_in_place`)

Called at the top of every model-level `mode="before"` validator, because
that is the one place a validator holds the WHOLE raw input, and pydantic
keeps a failing validator's raw input on the error. `exclude` and
`repr=False` do not reach that channel: they govern the model, and this
runs before a model exists.

Every declared runtime field is wrapped, not just `env`. `on_spawn` is a
callback, and a bound one carries its receiver into its own `repr`, so a
supervisor holding credentials would print them from the same error. The
wrapper is unwrapped by the field validators, which are the only code that
needs the value.

Anything the raw mapping holds under any other key is untouched and is not
covered by this. The claim here is about the two declared runtime fields.

**An immutable mapping is refused, not skipped.** Substituting in place is
not a convenience here, it is the whole mechanism: pydantic keeps the object
that was passed INTO the failing validator, so handing back a sanitized copy
changes nothing about what the error holds. When the raw input cannot be
written to, there is no way to make it safe, and the only two options are to
leak it or to refuse it. `BaseModel.model_validate()` accepts any mapping,
so this route is public and reachable, and skipping quietly meant a
credential in `str(exc)`, `exc.errors()` and `exc.json()` alike.

The refusal is a `TypeError` because pydantic converts `ValueError` and
`AssertionError` into a `ValidationError` that quotes the rejected input,
which would reintroduce exactly what is being prevented. It names the
fields and the mapping type and never the values. A mapping carrying
neither runtime field has nothing to protect and passes through, so
read-only inputs are not broken in general.

## Cleaning up a spawn cancelled before its handle returns (`_kill_abandoned_spawn`)

Runs as a done-callback because by then there is nothing left to await from:
the coroutine that asked for the child has already unwound.

A cancelled task is a known hole here, not a case of nothing having
happened, and it is logged rather than passed over in silence. Interpreter
shutdown cancels pending tasks, and a cancellation landing inside the
creation call leaves a child the OS has made and whose pid was never
returned to anyone in this process. asyncio closes the transport on that
path, which ends the direct child; the group it leads is not reached,
because reaching it needs the pid.

A leg spawned under a loop that then shuts down can leave a
SIGTERM-ignoring descendant running. Recording the handle as soon as the
creation call returns was tried and removed — it covers only a window
between the call returning and the caller resuming, which is not where the
cancellation lands.

Closing it needs the pid before the creation call returns. There is a route
to that: driving `loop.subprocess_exec` with a protocol that records
`transport.get_pid()` in `connection_made`, which the loop schedules before
the cancellable wait. It is declined here because it means reimplementing
`create_subprocess_exec` on top of stdlib classes outside that module's
`__all__`, pinning this file to their shape across every Python version
supported.

Nor does anything recover the pid later: the orphan is not in any record
the caller can later find. `on_spawn` fires only once the creation call has
returned, which is precisely what did not happen here, so the window leaves
no record of any kind — which is what the log line is for, and why it is a
warning. Left as a stated hole, not a handled one.

The exception is retrieved where there is one, or asyncio reports it as
never-retrieved at exit and a cancelled spawn starts looking like a defect
in the spawn.

## Draining a child's process group through cancellation (`end_child_group`)

Two things this does that awaiting the graceful helper alone does not.

It drains the GROUP rather than the process. The graceful helper returns as
soon as the process it holds a handle to is gone, and a descendant that
ignores SIGTERM outlives a parent that does not — so the group is read
afterwards and killed if anyone is still in it. A group that answers with
members is still the group whose id was recorded, because a group id is not
reissued while it has members.

It cannot be interrupted into leaving something running. The graceful pass
waits out a grace period, and that wait is a cancellation point that a
runner being torn down is exactly where it meets. So a synchronous kill runs
in a `finally` when that pass did not finish: no await, so nothing can
interpose.

Every signal it sends is conditioned on the recorded group id still being
this child's, and there are exactly two things that establish that. Either
the child has not been waited, in which case its pid cannot have been
reissued and the polite signal is safe; or the group answers with a live
member, and an occupied group is never reissued. Nothing else counts, and
the graceful helper is therefore reached ONLY on the not-yet-waited path: it
signals the group id it is given without checking anything, so calling it
after a normal drain would send SIGTERM to whatever now holds a recycled id.

The escalation keys on that membership evidence rather than on whether the
direct child is dead. Those are different facts: a leader that died to
SIGTERM sets `returncode` while a descendant ignoring SIGTERM is still in
its group, and a backstop gated on the leader's liveness reads that as
nothing left to do.

What it therefore cannot close, rather than papering over it: a scan that
could not read the whole process table and saw no members leaves emptiness
unproved, and this refuses to signal on that, because an unprovable group
and a reissued one look the same from here. That refusal is logged rather
than silent — it is the one outcome where something may still be running
and nothing was done about it.
