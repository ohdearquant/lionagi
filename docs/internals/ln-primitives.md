# Process-group identity: telling a live child from a reused pid

`lionagi/ln/_proc.py` answers one question for callers that manage child
processes (agent runtimes, sandboxed tool execution): is the process I
started still the one I think it is? The kernel reuses pids, and on a
long-running host a pid recorded at spawn time can point at a completely
different process minutes later. Every function in this module exists to
answer identity questions without being fooled by that reuse.

## The bracketing technique

A single read of "is pid P in group G" is not enough to trust, because the OS
can recycle P between the moment you read its group and the moment you act on
that answer. `pinned_member()` fixes this by bracketing the group-membership
read with two reads of the process's start time (`process_create_time()`):
read the start time, read group membership and an ownership marker, then read
the start time again. If the two start-time reads disagree, the pid was
reissued to a different process somewhere in between, and the whole
observation is discarded as `"unknown"` — never treated as evidence about the
original process or its group.

`process_create_time()` itself returns one of three states, and the
distinction matters to every caller:

- `("found", t)` — the process exists and started at `t`.
- `("gone", None)` — the process has exited (including a zombie: it still
  holds its pid until reaped, so a zombie cannot be a candidate for pid
  reuse; treating it as gone is what keeps that guarantee).
- `("unknown", None)` — the probe itself failed. This must never be read as
  "the process is dead" or as license to signal it; it means the read did
  not come off.

`start_time_matches()` compares a recorded start time against a freshly read
one within `CREATE_TIME_TOLERANCE` (0.1s), because two reads of a kernel
clock for the *same* process can differ in the last decimal place even
though two live reads of the same process must otherwise agree exactly.

## The marker, and why a `None` reading is ambiguous

`process_marker()` reads an environment variable from a target process
(`psutil.Process(pid).environ()`), used to mark which run "owns" a spawned
process group. A `None` result is ambiguous on its own: on macOS, reading the
environment of a protected system binary silently returns an empty
environment instead of raising, so "the marker was read and is absent" and
"the environment could not be read at all" produce the identical `None`.
`pinned_member()` carries a separate `marker_read` flag alongside the marker
value specifically to break that ambiguity — a member that was inspected and
found markerless is distinguishable from one that refused inspection.
Absence of a marker can withhold ownership; it can never assert that a group
is *not* the caller's, precisely because the negative reading is not
trustworthy.

## Scanning a whole group

`live_group_members()` and `group_member_pids()` both scan every pid on the
system and keep the ones in a target process group, but they are not
interchangeable and one is not a cheaper version of the other:

- `live_group_members()` returns full `(pid, create_time, marker,
  marker_read)` tuples via `pinned_member()`, because a caller weighing a
  member's marker against its age needs both facts to come from one
  bracketed observation of the same process, not two independent reads that
  could straddle a pid reuse.
- `group_member_pids()` is the marker-free membership read, for a caller that
  only wants to know whether a group is empty. It re-derives membership with
  its own start-time bracket rather than dropping a field from
  `live_group_members()`, because the marker has to be read *inside* the
  identity bracket to belong to the same observation — reading it outside
  that bracket, and then discarding it, is a different (and looser)
  observation, not a cheaper version of the same one.

Both functions return a `complete` flag alongside the results. A process that
vanished mid-scan is simply not a live member, but a process whose identity
*couldn't* be determined — an `OSError` on `os.getpgid`, an unresolved
start-time bracket — sets `complete = False` rather than being silently
dropped, because the group may still hold a member the scan never resolved.
An incomplete scan is never read as an empty group: it is a member that may
not have been seen, which is exactly the situation where treating the group
as safe to signal would be wrong. `group_member_pids()` leans on one more
fact to justify acting on a non-empty answer without further identity work:
a process group id is not reissued while the group still has members, so a
group that answers with members is still the group whose id was recorded,
even though individual member pids inside it can still be reused elsewhere.

## Signalling a group

`safe_pgid_value()` and the module-private `_safe_pgid()` both refuse to
return a group id that is unsafe to signal: `pgid <= 1` (init, or the value a
misbehaving process double reports) and the caller's own process group (so a
bad child object can never cause the caller to signal itself). `_safe_pgid()`
additionally treats `pid == 1` as unsafe for the same reason — on CI, pid 1
is often the harness's own session leader, and `MagicMock().pid` also
defaults to 1, so this check doubles as a test-double guard.

`kill_group_now()` is a synchronous, no-`await` SIGKILL: it exists as the
backstop for cleanup paths whose own graceful shutdown can itself be
cancelled. A backstop that can be interrupted mid-await is not a backstop, so
this one never awaits anything.

`terminate_process_group()` / `aterminate_process_group()` send `sig_first`
(default SIGTERM) to both the process group and the direct child — signalling
the child directly is normally redundant with the group signal, but prevents
orphaning it on platforms where `killpg` is unavailable. Passing `grace=None`
skips the polite signal entirely and sends SIGKILL immediately. With a grace
period, the async version waits up to `grace` seconds (via an `anyio`
cancel scope, not `asyncio.wait_for` — `wait_for` raises "no running event
loop" on a non-asyncio `anyio` backend before its timeout can apply) and
escalates to SIGKILL if the process hasn't exited by then.
