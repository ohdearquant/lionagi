# MCP server internals reference

Terse, per-module reference for invariants, protocol contracts, and non-obvious design
rationale that used to live as long-form comments/docstrings in `lionagi/mcp/`,
`lionagi/hooks/`, and `lionagi/plugins/`. The source now carries a 1-2 line pointer;
this file carries the substance. Organized by module path.

## lionagi/mcp/

### `mcp/jobs.py`

#### jobs-engine

Background job engine for the lionagi MCP server.

`submit()` spawns a `li` command as a detached process and returns immediately
with the run_id. The id is pre-assigned via `LIONAGI_RUN_ID` so it is known
before the child starts (no polling to discover it). `status()` / `output()` /
`kill()` / `list_jobs()` / `wait()` then operate on that id by reading the
run state the CLI persists plus the MCP server's own small per-job record.

The detached child gets its own session/pgid (`start_new_session`), so it
survives an MCP-server restart and can still be signalled as a group. That is why
job state lives on disk rather than in server memory.

Every response that carries a run's `status` carries `terminal` and
`outcome` with it, derived here from the durable record. `status` itself is an
open vocabulary passed through verbatim, so a caller never needs — and must never
keep — a copy of lionagi's status names to tell a finished run from a running one
or a success from a failure. All of these resolve through one path, `status()`,
so no two calls can disagree about the same run at the same moment.

A run's end reaches that path from three writers. The terminal hook the CLI runs
on `--notify` writes it into this package's own job record. A run stopped by
`li kill` never reaches that hook — the kill transitions the lifecycle row and
signals the process, and writes nothing here — so when the process is gone and
the job record shows no end, the state is read from the CLI itself, via
`li lifecycle <run_id> --machine`, and cached back onto the job record. A read
that cannot be made concludes nothing: the run is classified exactly as it would
have been without it.

The third writer is this module's own orphan observer. A run whose process died
before the terminal hook ran has no surviving producer at all: nothing will ever
write its end, and a caller waiting for one waits forever. So when — and only
when — an observation positively establishes that this run's process is gone,
`status()` publishes that end itself, as `outcome="indeterminate"`, before
returning it. Every mutation of a job record goes through one per-run lock, and
the first recorded end wins: a later writer may add what is missing beside it but
never replaces it, so no two readers of one record can disagree about whether the
run ended. A mutation that cannot take that lock records nothing and says so —
the record stays non-terminal and the next observation retries it, rather than a
terminal fact being announced that no reader can find.

#### reap-reason-code-split

Three ways a run can end with nobody around to say what happened, each with
its own `reason_code` — `_derive()` and `reap_orphan()` (`lionagi/mcp/jobs.py`):

- **Spawn never started doing work** — `spawn_state == "failed"`, caught and
  recorded synchronously by `_record_spawn_failure` before this run ever did
  anything. `reason_code=spawn_failed` (`_SPAWN_FAILED_REASON`), `terminal`
  true immediately; never reaches the orphan path at all. Already distinct
  before the notice-survives-lost-persistence change — no new code needed
  here, only documented as the first branch of the split.
- **Notice recorded as undelivered** — the run's own directory carries a
  `notify_outcome.json` with `ok: false` (the refusal `--notify` asked for
  and never got, from `record_notify_rejection_to_run`, or a `--notify`
  adapter that resolved but failed to deliver, from `record_notify_outcome_
  to_run` — see `docs/internals/cli.md#_notifypy`). `reap_orphan()` checks
  this (`_notice_recorded_undelivered`) only once every other admission gate
  already holds (`spawn_state == "started"`, no `finished_at` yet, a
  conclusive `process_gone` finding) — never as a substitute for those
  checks, only as a refinement of which reason code the reap writes.
  `reason_code=process_gone_notice_recorded_undelivered`
  (`LOST_REASON_NOTICE_RECORDED_UNDELIVERED`).
- **True silence** — no spawn failure, no recorded notice either way, process
  conclusively gone. `reason_code=process_gone_without_outcome` (`LOST_
  REASON`), unchanged from before.

The file read for the second case is best-effort and one-directional: found
and parseable with `ok: false` upgrades the reason code; anything else (file
absent, unreadable, `ok: true`, wrong shape) falls through to `LOST_REASON`.
An *absent* file is never evidence of true silence — run directories are
pruned by retention, so "the file used to say undelivered and is gone now" is
indistinguishable on disk from "nothing was ever recorded" — but the
resulting reason code (`LOST_REASON`, the pre-existing default) is the
correct one for both, so this never mislabels anything as more or less
informative than it actually is. `ok: true` similarly falls through, but is
moot in practice for an MCP-spawned run: a delivered `--notify` means
`lionagi.mcp._notify_hook` ran and called `mark_terminal`, which sets
`finished_at` and disqualifies the run from reaping before this check is
ever reached.

#### unresolved-spawn-window

`UNRESOLVED_SPAWN_AFTER_SECONDS` (= `WAIT_MAX_SECONDS`) is a defensible default,
not a derivation, for how long a spawn may sit unresolved before `wait()` stops
holding its window open. Nothing here terminalises a run.

What it can be argued from: backward-looking only. Past this line, a caller who
had waited since submission would already have spent a full maximum window, so
the bucket never speaks about a spawn nobody could have waited out yet — a floor
on when this may report, not a claim about whether the spawn will resolve.

What it cannot be argued from: forward-looking. A record aged exactly this long
may still resolve a second later, true of any threshold — no value distinguishes
itself as uniquely correct here. Choosing the longest window this function will
honour is a bet that a spawn which has outlived one is likelier stuck than slow.

#### kill-reason-codes

`kill()` reason-code taxonomy (`lionagi/mcp/jobs.py`), grouped by what a caller
should do next rather than by surface similarity:

- `KILL_RECORD_UNREADABLE` vs `KILL_RECORD_WRONG_SHAPE` — the first is bytes
  that couldn't be read/parsed and may read differently next call; the second
  parsed cleanly into something other than an object and only a person can fix it.
- `KILL_RECORD_FOREIGN_RUN` — parsed fine and simply names another run; a call
  the caller can resolve on its own, unlike the two shape codes above.
- `KILL_NOT_RECORDED` — the signal went out but the record of it couldn't be
  serialized: something *was* signalled, unlike the other refusal codes where
  nothing was, and the durable trace is missing so a caller may want to retry.
- `KILL_NO_RECORDED_IDENTITY` vs `KILL_IDENTITY_UNUSABLE` — absent identity
  fields vs. present-but-damaged ones; different things for an operator to fix.
- `KILL_PID_RECYCLED` / `KILL_LEADER_UNVERIFIABLE` / `KILL_LEADER_IDENTITY_CHANGED`
  — identity-bearing records split by settled-forever (mismatch, foreign group)
  vs. a failed measurement that may succeed on retry (unreadable probe, and a
  leader start-time read twice that disagrees with itself).
- `KILL_GROUP_SCAN_INCOMPLETE` vs `KILL_GROUP_OWNERSHIP_UNPROVEN` — the first is
  a member whose environment wouldn't open (may answer next call); the second is
  the scan completing and finding no ownership marker anywhere (won't change on
  retry, only an operator can settle it).

#### locked-job-contract

`_locked_job()` (`lionagi/mcp/jobs.py`) is a read-modify-write critical section
over one run's job record, shared across processes.

`os.replace` publishes a record without ever tearing it, but two writers that
read, merge, and publish in turn still lose one of the two updates — the
second's merge starts from bytes the first has already replaced. The terminal
hook, pid attachment, lifecycle cache, delivery result, and orphan observer all
do exactly that from different processes, so the whole reread-merge-publish
cycle must be exclusive, not just the final publish.

The lock is an advisory file lock on a file of its own beside the record — not
on the record itself, which is replaced rather than written in place, so a lock
held on it would be a lock on bytes already unlinked. It's held for the whole
`with` body plus the write that follows, and the record is reread under it, so
a caller always merges into what's on disk *now*. The record publishes on exit
only if the body actually changed it — a mutation that keeps what it found (the
inside view of first-writer-wins) touches nothing.

A run with no directory gets no lock (making one would leave an empty job
directory that reads back as a damaged record for a run nobody submitted); a
lock that can't be taken for any other reason also yields no record. These two
report as distinct states rather than collapsing into one: an absent record is
a settled fact about the run, while an unavailable lock is no answer at all —
a caller treating it as "no record" would publish a fact it never wrote.

#### group-identity-rules

`_group_identity()` (`lionagi/mcp/jobs.py`) decides whether a live process
group can be the one this run spawned, trying two rules in order:

1. **Marker** (decides positively either way). Every process a run spawns
   carries the run id in its environment; one confirmed member with a matching
   id makes the group this run's (members share a pgid). A member with a
   *different* run's id means the group number was reused. All readable
   markers are collected before applying the rule — deciding on the first one
   read would make the verdict depend on process-table enumeration order.
   Disagreeing markers are `"conflict"`: two runs can't own one group, so an
   unexplained group is never signalled as ours.
2. **Start time** (can only ever exclude). A member older than this run cannot
   be work this run spawned → `"not_ours"`. The converse doesn't follow: every
   member being younger is consistent with both an owned group and an
   unrelated one that started later, so it's never treated as an
   identification. A dead leader whose group yields no marker, fully
   inspected, is `"unproven"`.

`"gone"` means nothing live is left; `"unknown"` means the scan itself
couldn't complete (an unreadable member, or one whose environment couldn't be
read) — neither is a finding about the group, and both may resolve on retry.

#### derive-contract

`_derive()` (`lionagi/mcp/jobs.py`) classifies a job record into the fields a
caller may branch on:

- `status` is an open vocabulary — whatever the CLI recorded passes through
  verbatim, never matched against a local set to decide anything.
- `terminal` ("stop waiting") comes only from a recorded end: a `finished_at`
  written by the terminal hook or by `kill`, a caught+recorded spawn failure,
  an end recorded in the lifecycle store (where a run stopped by `li kill`
  leaves its only trace), or the orphan transition this module publishes for a
  conclusively-gone process. Every source is a durable record read back from
  disk — a live observation is never turned into a latch, which is what keeps
  two readers of one unchanged record from disagreeing. Never inferred from the
  status string, and never from a missing pid (a healthy child has no pid yet
  between the pre-spawn write and the pid-attach write).
- `lifecycle` is the `li lifecycle` summary, or None when nothing could be
  established; None never terminalises anything — a failed read leaves
  classification exactly as it was before the read was attempted.
- `outcome` ("did the work come out right") is null whenever `terminal` is
  false, including for a run whose process is gone but whose loss couldn't be
  established conclusively — it has stopped looking alive and is still not
  terminal.

#### liveness-findings

`_run_process_liveness()` (`lionagi/mcp/jobs.py`) settles two questions in
evidence order: (1) does the pid hold a live process at all — needs only the
pid, asked first on every path; (2) is that live process *this run's* — needs
the recorded start time, asked second only where one was recorded. Question 1
must precede question 2: the liveness probe reaps only its own children, so a
process exited under a different parent (e.g. after an MCP-server restart)
stays a zombie that a record-first check would read as running.

Findings, each mapped to a public `pid_identity` via `_PID_IDENTITY_BY_FINDING`:

Conclusive (positive observation this run's process is gone):

- `pid_absent` — pid askable, held no live process
- `disappeared_during_probe` — held one at the liveness probe, none at the
  creation-time probe
- `pid_recycled` — a live process holds the number but started at a different
  time than recorded, so it's a different process

Inconclusive (settle nothing about death):

- `identity_confirmed` — start times match
- `identity_not_recorded` — record captured no start time
- `identity_unusable` — recorded start time can't be compared against (bool,
  NaN, or an unbounded JSON integer — the same three values `kill()` refuses)
- `identity_unreadable` — identity probe errored
- `unusable_pid` — record's pid isn't a number the OS can be asked about, so
  no probe was made at all
- `no_record` — live pid with no record to identify it against

#### status-response-contract

`status()` (`lionagi/mcp/jobs.py`) response fields:

- `status` is the recorded status, verbatim, open vocabulary — display it,
  never match it against a list. Branch on `terminal` ("stop waiting") and
  `outcome` ("did the work come out right", null while `terminal` is false)
  instead.
- `run` is the raw CLI manifest. Its `status` is one-directional evidence: for
  a run that reaches its own teardown, the manifest is rewritten with a
  terminal status truthfully (after the CLI finalizes the run in the
  StateDB) — but a killed or crashed run leaves the manifest reading `running`
  forever, since nothing survived to rewrite it. Read the top-level `status`,
  not `run["status"]`.
- `alive` is about the process this run spawned, not whatever now holds its
  pid — a recycled pid reports as not alive. `pid_identity` says how that was
  settled (`confirmed`/`recycled`/`gone`/`unreadable`/`not_recorded`/
  `unusable`/`unusable_pid`/null).
- `liveness_conclusion` is what the observation established (`process_gone` /
  `alive` / `unknown`). Only `process_gone` can end a run — done by writing
  the end before this call returns, so `terminal` here is always a durable
  fact, never just this observation.
- `terminal_source` says what wrote the end (`cli_terminal_hook` /
  `lifecycle_cache` / `spawn_failure` / `mcp_orphan_reaper` / null for
  pre-field records); `terminal_evidence` carries bounded evidence for an end
  nobody reported.
- `notify_delivery` is null only while `terminal` is false. A terminal run with
  no configured notifier reports `{"attempted": false}`. Delivery writes an
  attempted/unknown object before launching the notifier and replaces it with
  success or failure afterward, so cancellation after an external side effect
  cannot erase the fact that an attempt occurred.
- `possibly_orphaned` flags a gone process with no end recorded whose loss
  wasn't conclusively established (unaskable pid, or an unpublishable
  transition) — advisory, never makes the run terminal.
- `mcp_config*` mirror what `submit()`'s handle returned. `mcp_config_servers`:
  `[]` means the question was settled with "none"; `null` means either the
  caller named their own config (never read by this run), no config was found,
  or the record predates the field — `mcp_config_reason` disambiguates the
  first two. This reports what was RESOLVED, not that the child's provider
  then actually started each server.
- `known` / `record_state`: only `"absent"` means the run is unknown;
  `"unreadable"`/`"wrong_shape"` mean a file is on disk and damaged — reporting
  either as unknown would send an operator away from a file that exists.

#### listing-negative-sweep

A caller who suspects nothing still needs to find the run whose terminal
notice never arrived — by construction that run announced itself to nobody.
`job.list` is that surface: every row carries `notify_delivery_state`
(`none` / `delivered` / `delivered_unverified` / `failed` / `unknown`, collapsed from the
full `notify_delivery` object `status()` reports for one-run detail), so the
sweep is "list, act on `failed` or `unknown`" with no per-run reads. `none` covers both
not-terminal-yet and no-notifier-configured — silence is the documented
default there, never a failure — and `delivered_unverified` is deliberately
not collapsed into either neighbor (a zero exit from a command shape whose
zero exit doesn't prove a send supports neither claim). `unknown` means the
attempt began but the process ended before its final result could replace the
write-ahead record; inspect or reconcile it rather than treating it as success.

#### signal-leader-group-safety

`_signal_leader_group()` (`lionagi/mcp/jobs.py`) signals a process group only
after the confirmed run leader is shown to belong to it. The caller has
already established *pid* is this run's process (via *observed_at*, the start
time that identified it); what's still open is whether the record's *pgid* is
really that process's group — the two numbers are stored separately.

Two checks, both required:

1. **Group equality** — read the leader's live group, require it equals the
   record's *pgid*. Mismatch (settled fact) and unreadable (may resolve later)
   get different reason codes.
2. **Run-id marker** on the leader itself — a *different* run's id means the
   record doesn't describe this process, whatever the numbers matched. The
   marker only ever withholds a signal; absent/unreadable reads identically to
   "no marker", so requiring one to *permit* a signal would make every
   unreadable process permanently unreapable.

Why re-read the start time exactly (not within the tolerance used for the
disk-recorded value): a run's leader's pid equals its pgid by construction, so
when the group drains and the OS reassigns that number to a new session
leader, the new leader's group number matches too — group equality alone can
hold for a process this run never spawned, and the marker can't rule that out
(it can only withhold, never permit). Bracketing with an *exact* start-time
re-read closes that gap; a tolerance here would weaken the one check that
tells a recycled number from the process that held it.

#### kill-safety-contract

`kill()` (`lionagi/mcp/jobs.py`) signals the process group `run_id` was spawned
into. The record carries what a bare pid can't: when the leader started, and
the group it was given at spawn — turning "group still running after its
leader exited" (worth reaping) and "pid handed to an unrelated process" (must
never be signalled) into decidable cases.

**The guarantee, exactly**: every signal is preceded by a positive
identification — either the live leader's start time matches the record and
its current group is the recorded one, or a live member of the recorded group
carries this run's id in its environment. A group is never signalled just for
looking young enough. A probe that errors is `"unknown"`, and unknown refuses
— the outcome being optimized for is an accurate refusal reason, not the
largest possible number of processes stopped. This holds even for a record
with no process identity at all (refused; group left for an operator to reap
by hand).

**What is NOT established**: who wrote the record. Fields are compared against
the running process — they identify a process that still matches, not that
this run described it originally. The store (the invoking user's own
directory) is a trusted input; this is a premise, not an oversight, since
anything able to rewrite a record there can call `killpg` directly without
going through here anyway. The claim is scoped to "given a record this run
wrote, no signal without positive identification" — record provenance is out
of scope.

**The TOCTOU window**: identification and the signal are two separate syscalls
— `killpg` takes a group number, not a reference to the inspected group, so
there's no "signal only if it still holds the verified process" primitive to
use. In the gap, the identified group can empty and its number be reassigned
to an unrelated group, which then receives the signal. Unclosable with process
groups alone; stated here rather than papered over, because the guarantee is
"never signalled without an identification," not "never signals the wrong
group."

#### wait-result-buckets

`wait()` (`lionagi/mcp/jobs.py`) returns one entry per requested id plus
`all_terminal`, `timed_out`, `pending`, `stopped_without_end`, and
`unresolved_spawn` — never a bare boolean, since mixed outcomes are the norm.

- **`stopped_without_end`**: a run that stopped — or can't be shown to be
  progressing — and could not be resolved. Three ways in: a loss that
  couldn't be conclusively established (e.g. an unaskable pid — stopped
  looking alive but may still be running for all this can tell); a
  conclusive loss whose fenced reap could not be published (the transition
  is retried by the next observation); and a record predating the
  spawn-phase field that is not shown alive — phase absence alone is never
  stopped evidence, and a live pre-field record is classified running and
  stays in `pending`. Only
  an explicitly-`preparing` record is kept out (fresh → `pending`, aged →
  `unresolved_spawn`). Not
  `pending` (waiting longer can't resolve it), not a per-id `error`
  (observing it succeeded). Because such an id resolves nothing by waiting, a
  caller looping until `all_terminal` would otherwise re-poll as fast as
  possible — so a call that would return having waited zero time, while any id
  is here or in `unresolved_spawn`, first sleeps one poll interval (bounded by
  the remaining window)
  before observing again. This floor is spent once at the boundary rather than
  relying on every client to back off on its own; `max_wait=0` is exempt by
  construction (no window to spend).
- **`unresolved_spawn`**: a record whose spawn phase is still `"preparing"`
  past `unresolved_spawn_after` (echoed in the result). The classifier
  deliberately makes no claim about such a spawn's fate (no bound tells a
  loaded machine from a dead spawn) — but leaving it in `pending` would set
  `timed_out` for a run that may never have started. Moving it here instead
  writes no outcome and leaves `terminal` false; a spawn that resolves
  afterwards is classified by the next observation as always. One population
  can never enter this bucket by construction: `submitted_at` and the opening
  `spawn_state` are set in the same record literal and published atomically,
  so no run this code submits can hold `"preparing"` without a stamp (only a
  pre-field record could, and a missing/unreadable stamp is read as *no
  evidence of age*, never as evidence of being old).
- **Reading the triple**: `unresolved_spawn` non-empty + `timed_out` false +
  `all_terminal` false means "not worth waiting on and not finished either —
  go look at it", distinct from `timed_out=true` (keep waiting) or
  `all_terminal=true` (stop). `all_terminal` means every run has a recorded
  end, not that every run succeeded — read each entry's `outcome` for that.

#### reservation-giveback

`_discard_reservation()` (`lionagi/mcp/jobs.py`) gives a reserved run
directory back after a submission fails before its job record is published.

A submission that fails partway through writing has already left files
behind, so removing only an empty directory would give the reservation back
for some failures and not others. The files a submission writes into its own
reservation are named by a fixed list and only those are ever removed — they
are addressed as fixed names under the reservation directory, never through a
path a caller handed in (a caller-named MCP config file lives wherever it
lives and is never touched, whatever it points at).

`rmdir` refuses a directory with anything in it, and that refusal stays the
safety net rather than becoming a check taken beforehand: whatever this is
asked to remove, a directory holding a run's state survives it. A removal
that fails for any other reason leaves a directory nobody claimed, which is
worth less than the error that sent us here, so it is swallowed rather than
raised.

The function returns whether the directory is actually gone afterward. When
it is not, a marker (`RESERVATION_ROLLBACK_INCOMPLETE`) is left in what
remains of it, so a directory found later under the jobs root with no job
record reads as a giveback that could not run rather than one that
succeeded — both otherwise look like the same empty absence of a job. The
marker write is itself best-effort: `_discard_reservation_and_warn()` checks
the marker's actual presence before logging, rather than assuming it landed
just because the directory survived.

#### write-job-publish

`_write_job()` (`lionagi/mcp/jobs.py`) publishes a job record by writing a
per-write-unique temp file in the same directory and calling `os.replace()`.
`os.replace` is atomic on the same filesystem, so a concurrent reader
(`status()` / `list_jobs()`) never observes a torn file, and a failed write
leaves the previous record intact instead of a partial one. The temp name
being unique per write means two writers to the same run (the pid-attach
write in `submit()` and the terminal hook) never collide on the temp file
itself. This makes each publish all-or-nothing; it does not serialize two
writers, so a read-modify-write pair can still lose an update (last replace
wins — see `_locked_job()` in [locked-job-contract](#locked-job-contract) for
what does serialize writers).

Non-finite floats are refused before the temp file is opened, so a refused
record leaves neither a staging file nor a published one. `json.dumps` would
otherwise write a non-finite float as the bare token `NaN` or `Infinity`,
which only Python reads back — every non-Python reader of this record, and
every strict JSON parser, would fail on it long after the run that wrote it.
The start time already has a representation for "unreadable" (`null`), so
nothing here encodes a sentinel this check would need to special-case.

### `mcp/_notify_hook.py`

#### deliver-terminal-notice-two-callers

`deliver_terminal_notice` decides the whole delivery: which command is
configured, what the run's fields substitute into it, whether a missing
sender makes it unusable, and how each of those is recorded. It is one
function because it has two callers — this hook, running in the dying run's
own process, and the job observer publishing an end for a run whose process
never got this far — and a notice sent by the second must be the one the
first would have sent. Two resolution paths would mean two answers to "what
is configured here", and the run that needs the notice most is the one whose
own process isn't around to be asked.

The working directory is part of "what is configured here", so it is taken
from the run's record rather than from this process's own cwd. The two
callers never share a directory (the hook runs in the run's, the observer in
the server's), so resolving identity from the process's own location would
sign the same notice with a different seat depending on which caller got
there first, silently — downstream routing acts on that signature. Reading
it from the record is what makes the two callers agree by construction
rather than by coincidence.

Nothing in this path raises: the caller is either a terminal path that has
already finished, or a read that has already published a durable end, and
neither can be failed by a notifier. Every way a delivery does not happen
comes back as an outcome describing it.

### `mcp/projection.py`

#### accepts-no-values-required-unenforced

`_accepts_no_values` flags a positional with `nargs="*"` (consumes zero or
more values, so the command runs without it) even though argparse marks the
action required and never enforces that. Carrying `required` into the schema
as-is would tell a caller a parameter is mandatory when the parser itself
doesn't enforce it, with no way for the caller to check. Such an action is
still reported, under `x-required-unenforced`: a caller told only
`required: []` would read that as "a call with no arguments is valid," a
different and wronger claim than the one `required` was dropped to avoid.

The check is stated about the action's shape rather than read off
`action.required`, because `required` is what's unreliable here: Python 3.14
stopped setting it for exactly these actions, so trusting it would make the
schema — and anything pinned to it — say different things on different
interpreters about one unchanged command.

## lionagi/hooks/

### `hooks/external.py`

#### hooks-private-copy-trust-pinning

`_BoundExecutable`/`_materialize_private_copy`/`_hash_private_copy`/
`_prepare_trusted_execution` implement content-pinned trust for an external
hook command. An open fd pins the *inode*, not the content: hashing the fd
and then separately re-reading it to build the executed copy would leave a
window where an in-place overwrite between those two reads gets copied and
executed as trusted, even though the earlier digest still matches.

Closing that window means never hashing the mutable source at all — the
private copy is made first, from whatever bytes are at the fd right now,
into a directory nothing but this process holds a handle on; the trust
digest is then computed by re-hashing that immutable, single-process-owned
copy, never the source fd or path again. A source overwrite at any point
relative to this call can therefore only ever affect the source, never the
copy that gets compared or, on a match, exec'd. `path` inside `private_dir`
is what actually gets exec'd — the configured/approved path is never
spawned directly, so a same-path substitution after approval (an in-place
overwrite, or a symlink retarget) cannot change what runs.

#### hooks-stdout-decision-parsing

`_parse_stdout_decision` parses exit-0 hook stdout. Empty stdout is the
*only* case that legitimately means "no structured output" (documented
no-opinion convention — allow). Every other case that fails to yield a
recognized decision form sets `malformed=True` rather than reusing the
empty-stdout convention: non-empty stdout that isn't valid JSON, a JSON
value that isn't an object, an object with neither a
`hookSpecificOutput.permissionDecision` nor a top-level `decision` field,
and an explicit `hookSpecificOutput.permissionDecision: null` (present but
null, unlike the key being absent) — all deny on a blocking seam rather
than being treated as a genuinely empty response.

A top-level `decision` of `"block"` normalizes to `"deny"`;
`"allow"`/`"approve"` (or an explicit top-level `null`) normalize to `None`
(allow) — this is the one place an explicit null is a documented
convention rather than malformed, since the top-level shape's null means
"no decision" the same way an absent field would.
