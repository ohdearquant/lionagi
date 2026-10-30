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

Every start time that is recorded or compared for identity is read through
`raw_create_time()`, never the plain `psutil.Process.create_time()`. On macOS
psutil adjusts that value by a boot-time reading it freezes when psutil is
imported, and the boot clock moves under NTP slew, so the process that
recorded a start time and the process checking it later (or the same process
an hour later) read different values for one live process, and a live run
reads as a recycled pid. `raw_create_time()` takes psutil's unadjusted reading
on the platform that applies the adjustment and the plain epoch value
everywhere else; on Linux the unadjusted reading is seconds since boot, which
would change what every recorded value means. The reader is chosen on the
first call. A start time that is reported as a wall-clock instant rather than
compared, such as the Studio daemon's start, uses the plain call.

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
as safe to signal would be wrong. A non-empty result establishes only that a
group currently occupies the number. It does not tie that group to an earlier
child after the original group may have drained; signal authorization requires
matching one of the start-time-pinned witnesses captured before that boundary.

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

`terminate_process_group()` / `aterminate_process_group()` always signal the
direct child. They signal its process group only when the caller also supplies
frozen, spawn-bound `group_witnesses`; omitting that evidence deliberately
limits cleanup to the direct child. Passing `grace=None` skips the polite signal
and sends SIGKILL immediately. With a grace period, the async version waits up
to `grace` seconds (via an `anyio`
cancel scope, not `asyncio.wait_for` — `wait_for` raises "no running event
loop" on a non-asyncio `anyio` backend before its timeout can apply). The
child's exit does not end the group's grace period while members remain. An
unexpected first group-signal error is retained and the async path retries
SIGKILL after grace with the original witnesses checked again. A missing group
or permission refusal does not authorize a retry; a permission refusal is
logged. The child's own cleanup remains independent of the group result.

Group-wide signals have a stricter identity rule than a liveness probe can
provide. Before spawn, `prepare_process_group_env()` gives the caller an
environment carrying a fresh private marker. Immediately after spawn,
`observe_process_group_leader()` accepts a create time only when that marker,
the leader group, and both sides of a start-time bracket agree. The caller then
uses `capture_group_witnesses()` while that exact leader still anchors the group
to freeze start-time-pinned member witnesses. An unreadable or mismatched marker
is fail-closed, as are absent witnesses: the terminators signal only the direct
child. Once the direct child has been reaped, its process-group id may be reused
as soon as the original group empties. Therefore
`killpg(pgid, 0)` can establish only that *some* group currently has that
number; it cannot establish that this is still the group being terminated.
Before escalating, the async path requires a live group member whose pid,
exact start time, and group membership match an entry witness. An unreadable
or incomplete observation, a replaced member, or a populated group with no
matching witness is unproven: the group-wide SIGKILL is skipped, while
direct-child `kill()` cleanup is still attempted. This intentionally prefers
leaving a straggler for higher-level cleanup over killing an unrelated process
group.

One case has no member witness to match: a spawn marker was supplied but the
group scan finds no member carrying it, as happens where the platform will not
list processes. While the leader is still running it holds the group id, so
`capture_group_witnesses()` anchors the group to the leader instead, and a
positive `killpg(pgid, 0)` probe stands in for the member match. The anchor is checked again when it is used, not only when it is
captured: once the leader has been reaped the group id is free to be reissued,
so the anchor authorizes nothing and no probe is sent. A leader already reaped
at capture gets no anchor at all.

That verification narrows but cannot close the final race. The identity read
and `killpg()` remain separate system calls; the verified group can drain and
its number can be reassigned in between them. POSIX process groups provide no
verify-and-signal primitive. The honest guarantee is consequently **no delayed
group signal without a positive identity witness**, not **the signal can never
reach a recycled group**. Closing that last TOCTOU requires a non-reusable
kernel containment handle, such as a Linux cgroup (or a Windows job object),
rather than another process-group probe.
