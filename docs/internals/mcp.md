# MCP server internals reference

Terse, per-module reference for invariants, protocol contracts, and non-obvious design rationale that used to live as long-form comments/docstrings in `lionagi/mcp/`, `lionagi/hooks/`, and `lionagi/plugins/`. The source now carries a 1-2 line pointer; this file carries the substance. Organized by module path.

## lionagi/mcp/

### `mcp/jobs.py`

<a id="jobs-engine"></a>

#### jobs-engine

Background job engine for the lionagi MCP server. `submit()` spawns a `li` command as a detached process and returns immediately with the run_id, pre-assigned via `LIONAGI_RUN_ID` so it's known before the child starts (no polling to discover it). `status()`/`output()`/`kill()`/`list_jobs()`/`wait()` operate on that id by reading the run state the CLI persists plus the MCP server's own small per-job record. The detached child gets its own session/pgid (`start_new_session`) so it survives an MCP-server restart and can still be signalled as a group — which is why job state lives on disk rather than in server memory.

Every response carrying a run's `status` carries `terminal` and `outcome` with it, derived from the durable record; `status` itself is an open vocabulary passed through verbatim — a caller never needs, and must never keep, a copy of lionagi's status names. All resolve through one path, `status()`, so no two calls can disagree about the same run at the same moment.

A run's end reaches that path from three writers: (1) the terminal hook the CLI runs on `--notify`, writing into this package's own job record; (2) for a run stopped by `li kill` (which never reaches that hook), the state is read from the CLI via `li lifecycle <run_id> --machine` and cached back onto the job record — a read that cannot be made concludes nothing, the run is classified exactly as it would have been without it; (3) this module's own orphan observer — when an observation positively establishes that a run's process is gone with no surviving producer, `status()` publishes the end itself as `outcome="indeterminate"` before returning it. Every mutation of a job record goes through one per-run lock; the first recorded end wins (a later writer may add what's missing beside it but never replaces it). A mutation that cannot take the lock records nothing and says so — the record stays non-terminal and the next observation retries.

<a id="unresolved-spawn-window"></a>

#### unresolved-spawn-window

`UNRESOLVED_SPAWN_AFTER_SECONDS` (= `WAIT_MAX_SECONDS`) is a defensible default, not a derivation, for how long a spawn may sit unresolved before `wait()` stops holding its window open — nothing here terminalises a run. Backward-looking justification only: past this line, a caller who had waited since submission would already have spent a full maximum window, so the bucket never speaks about a spawn nobody could have waited out yet (a floor on when this may report, not a claim about whether the spawn resolves). It cannot be argued forward-looking — a record aged exactly this long may still resolve a second later, true of any threshold. Choosing the longest window this function will honour is a bet that a spawn which has outlived one is likelier stuck than slow.

<a id="kill-reason-codes"></a>

#### kill-reason-codes

`kill()` reason-code taxonomy (`lionagi/mcp/jobs.py`), grouped by what a caller should do next rather than by surface similarity:

- `KILL_RECORD_UNREADABLE` vs `KILL_RECORD_WRONG_SHAPE` — bytes that couldn't be read/parsed (may read differently next call) vs. parsed cleanly into something other than an object (only a person can fix it).
- `KILL_RECORD_FOREIGN_RUN` — parsed fine and simply names another run; caller-resolvable, unlike the two shape codes above.
- `KILL_NOT_RECORDED` — the signal went out but the record of it couldn't be serialized: something *was* signalled, unlike other refusal codes, so the durable trace is missing and a caller may want to retry.
- `KILL_NO_RECORDED_IDENTITY` vs `KILL_IDENTITY_UNUSABLE` — absent identity fields vs. present-but-damaged ones; different things for an operator to fix.
- `KILL_PID_RECYCLED` / `KILL_LEADER_UNVERIFIABLE` / `KILL_LEADER_IDENTITY_CHANGED` — identity-bearing records split by settled-forever (mismatch, foreign group) vs. a failed measurement that may succeed on retry (unreadable probe, a leader start-time read twice that disagrees with itself).
- `KILL_GROUP_SCAN_INCOMPLETE` vs `KILL_GROUP_OWNERSHIP_UNPROVEN` — a member whose environment wouldn't open (may answer next call) vs. the scan completing and finding no ownership marker anywhere (won't change on retry, only an operator can settle it).

<a id="locked-job-contract"></a>

#### locked-job-contract

`_locked_job()` (`lionagi/mcp/jobs.py`) is a read-modify-write critical section over one run's job record, shared across processes. `os.replace` publishes a record without ever tearing it, but two writers that read, merge, and publish in turn still lose one update — the second's merge starts from bytes the first has already replaced. The terminal hook, pid attachment, lifecycle cache, delivery result, and orphan observer all do exactly that from different processes, so the whole reread-merge-publish cycle must be exclusive, not just the final publish.

The lock is an advisory file lock on a file of its own beside the record — not on the record itself, which is replaced rather than written in place. Held for the whole `with` body plus the write that follows, with the record reread under it, so a caller always merges into what's on disk *now*; it publishes on exit only if the body actually changed it. A run with no directory gets no lock (making one would leave an empty job directory that reads back as a damaged record); a lock that can't be taken for any other reason also yields no record — these report as distinct states (absent record is a settled fact; unavailable lock is no answer at all).

<a id="group-identity-rules"></a>

#### group-identity-rules

`_group_identity()` (`lionagi/mcp/jobs.py`) decides whether a live process group can be the one this run spawned, trying two rules in order:

1. **Marker** (decides positively either way) — every process a run spawns carries the run id in its environment; one confirmed member with a matching id makes the group this run's (members share a pgid). A member with a *different* run's id means the group number was reused. All readable markers are collected before applying the rule (deciding on the first read would make the verdict depend on process-table enumeration order). Disagreeing markers are `"conflict"`.
2. **Start time** (can only ever exclude) — a member older than this run cannot be work this run spawned → `"not_ours"`. Every member being younger is consistent with both an owned group and an unrelated later one, so it's never treated as identification. A dead leader whose group yields no marker, fully inspected, is `"unproven"`.

`"gone"` means nothing live is left; `"unknown"` means the scan itself couldn't complete (unreadable member/environment) — neither is a finding about the group, both may resolve on retry.

<a id="derive-contract"></a>

#### derive-contract

`_derive()` (`lionagi/mcp/jobs.py`) classifies a job record into fields a caller may branch on:

- `status` — open vocabulary, passed through verbatim, never matched against a local set.
- `terminal` ("stop waiting") — comes only from a recorded end: a `finished_at` written by the terminal hook or `kill`, a caught+recorded spawn failure, an end recorded in the lifecycle store (a `li kill`-stopped run's only trace), or the orphan transition this module publishes for a conclusively-gone process. Every source is a durable record read back from disk; never inferred from the status string or a missing pid (a healthy child has no pid yet between pre-spawn write and pid-attach write).
- `lifecycle` — the `li lifecycle` summary, or `None` when nothing could be established; `None` never terminalises anything.
- `outcome` ("did the work come out right") — null whenever `terminal` is false, including for a run whose process is gone but whose loss couldn't be conclusively established.

<a id="liveness-findings"></a>

#### liveness-findings

`_run_process_liveness()` (`lionagi/mcp/jobs.py`) settles two questions in evidence order: (1) does the pid hold a live process at all (needs only the pid, asked first always); (2) is that live process *this run's* (needs the recorded start time, asked second only where one was recorded). Question 1 must precede question 2 — the liveness probe reaps only its own children, so a process exited under a different parent (e.g. after an MCP-server restart) stays a zombie a record-first check would read as running.

Findings, mapped to a public `pid_identity` via `_PID_IDENTITY_BY_FINDING`:

| Class | Finding | Meaning |
|---|---|---|
| Conclusive (positive: process gone) | `pid_absent` | pid askable, held no live process |
| Conclusive | `disappeared_during_probe` | held one at liveness probe, none at creation-time probe |
| Conclusive | `pid_recycled` | a live process holds the number but started at a different time than recorded |
| Inconclusive | `identity_confirmed` | start times match |
| Inconclusive | `identity_not_recorded` | record captured no start time |
| Inconclusive | `identity_unusable` | recorded start time not comparable (bool, NaN, unbounded JSON int — same three values `kill()` refuses) |
| Inconclusive | `identity_unreadable` | identity probe errored |
| Inconclusive | `unusable_pid` | record's pid isn't a number the OS can be asked about — no probe made |
| Inconclusive | `no_record` | live pid with no record to identify it against |

<a id="status-response-contract"></a>

#### status-response-contract

`status()` (`lionagi/mcp/jobs.py`) response fields:

- `status` — recorded status, verbatim, open vocabulary; display it, don't match against a list. Branch on `terminal`/`outcome` instead.
- `run` — the raw CLI manifest. Its `status` is one-directional: a run that reaches its own teardown gets the manifest truthfully rewritten with a terminal status, but a killed/crashed run leaves it reading `running` forever. Read the top-level `status`, not `run["status"]`.
- `alive` — about the process this run spawned, not whatever now holds its pid (a recycled pid reports not alive). `pid_identity` says how that was settled (`confirmed`/`recycled`/`gone`/`unreadable`/`not_recorded`/`unusable`/`unusable_pid`/null).
- `liveness_conclusion` — what the observation established (`process_gone`/`alive`/`unknown`). Only `process_gone` can end a run — done by writing the end before this call returns, so `terminal` here is always a durable fact, never just this observation.
- `terminal_source` — what wrote the end (`cli_terminal_hook`/`lifecycle_cache`/`spawn_failure`/`mcp_orphan_reaper`/null for pre-field records); `terminal_evidence` carries bounded evidence for an end nobody reported.
- `possibly_orphaned` — flags a gone process with no end recorded whose loss wasn't conclusively established; advisory, never makes the run terminal.
- `mcp_config*` — mirror what `submit()`'s handle returned. `mcp_config_servers`: `[]` means settled with "none"; `null` means the caller named their own config (never read by this run), no config was found, or the record predates the field — `mcp_config_reason` disambiguates. Reports what was RESOLVED, not that the provider actually started each server.
- `known`/`record_state` — only `"absent"` means the run is unknown; `"unreadable"`/`"wrong_shape"` mean a file is on disk and damaged.

<a id="signal-leader-group-safety"></a>

#### signal-leader-group-safety

`_signal_leader_group()` (`lionagi/mcp/jobs.py`) signals a process group only after the confirmed run leader is shown to belong to it. The caller has already established *pid* is this run's process (via *observed_at*); what's open is whether the record's *pgid* is really that process's group. Two checks, both required:

1. **Group equality** — read the leader's live group, require it equals the record's *pgid*. Mismatch (settled) and unreadable (may resolve later) get different reason codes.
2. **Run-id marker** on the leader itself — a *different* run's id means the record doesn't describe this process, whatever the numbers matched. The marker only ever withholds a signal; absent/unreadable reads as "no marker", so requiring one to *permit* a signal would make every unreadable process permanently unreapable.

Start time is re-read exactly (not within tolerance): a leader's pid equals its pgid by construction, so when the group drains and the OS reassigns that number to a new session leader, the new leader's group number matches too — group equality alone can hold for a process this run never spawned, and the marker can only withhold, never permit. An exact start-time re-read closes that gap; a tolerance would weaken the one check that tells a recycled number from the process that held it.

<a id="kill-safety-contract"></a>

#### kill-safety-contract

`kill()` (`lionagi/mcp/jobs.py`) signals the process group `run_id` was spawned into. The record carries what a bare pid can't — leader start time and spawn group — turning "group still running after its leader exited" (worth reaping) and "pid handed to an unrelated process" (must never be signalled) into decidable cases.

**Guarantee**: every signal is preceded by a positive identification — either the live leader's start time matches the record and its current group is the recorded one, or a live member of the recorded group carries this run's id in its environment. A group is never signalled just for looking young enough; a probe that errors is `"unknown"` and refuses. This holds even for a record with no process identity at all (refused; left for an operator to reap by hand).

**Not established**: who wrote the record. Fields are compared against the running process — they identify a process that still matches, not that this run described it originally. The store (invoking user's own directory) is a trusted input by design, since anything able to rewrite a record there can call `killpg` directly anyway.

**TOCTOU window**: identification and the signal are two separate syscalls (`killpg` takes a group number, no "signal only if still verified" primitive exists). In the gap, the identified group can empty and its number be reassigned to an unrelated group, which then receives the signal — unclosable with process groups alone. The guarantee is "never signalled without an identification," not "never signals the wrong group."

<a id="wait-result-buckets"></a>

#### wait-result-buckets

`wait()` (`lionagi/mcp/jobs.py`) returns one entry per requested id plus `all_terminal`, `timed_out`, `pending`, `stopped_without_end`, and `unresolved_spawn` — never a bare boolean, since mixed outcomes are the norm.

- **`stopped_without_end`** — a run whose process is gone but whose loss couldn't be conclusively established (e.g. unaskable pid); not `pending` (waiting longer can't resolve it), not a per-id `error` (observing it succeeded). Because such an id resolves nothing by waiting, a call that would return having waited zero time, while any id is here, first sleeps one poll interval (bounded by the remaining window) before observing again — spent once at the boundary rather than relying on every client to back off. `max_wait=0` is exempt by construction.
- **`unresolved_spawn`** — a record whose spawn phase is still `"preparing"` past `unresolved_spawn_after` (echoed in the result). Makes no claim about the spawn's fate, but leaving it in `pending` would set `timed_out` for a run that may never have started; moving it here writes no outcome and leaves `terminal` false. `submitted_at` and the opening `spawn_state` are set in the same record literal and published atomically, so no run this code submits can hold `"preparing"` without a stamp.
- **Reading the triple** — `unresolved_spawn` non-empty + `timed_out` false + `all_terminal` false means "not worth waiting on and not finished either — go look at it", distinct from `timed_out=true` (keep waiting) or `all_terminal=true` (stop). `all_terminal` means every run has a recorded end, not that every run succeeded — read each entry's `outcome` for that.

### `mcp/_notify_hook.py`

<a id="deliver-terminal-notice-two-callers"></a>

#### deliver-terminal-notice-two-callers

`deliver_terminal_notice` decides the whole delivery: which command is configured, what the run's fields substitute into it, whether a missing sender makes it unusable, and how each is recorded. One function because it has two callers — the hook, running in the dying run's own process, and the job observer publishing an end for a run whose process never got this far — and a notice sent by the second must be the one the first would have sent.

The working directory is taken from the run's record rather than this process's own cwd: the two callers never share a directory (hook runs in the run's, observer in the server's), so resolving identity from the process's own location would sign the same notice with a different seat depending on which caller got there first, silently. Reading it from the record makes the two callers agree by construction.

Nothing in this path raises: the caller is either a terminal path that has already finished, or a read that has already published a durable end. Every way a delivery does not happen comes back as an outcome describing it.

### `mcp/projection.py`

<a id="accepts-no-values-required-unenforced"></a>

#### accepts-no-values-required-unenforced

`_accepts_no_values` flags a positional with `nargs="*"` (consumes zero or more values, so the command runs without it) even though argparse marks the action required and never enforces that. Carrying `required` into the schema as-is would tell a caller a parameter is mandatory when the parser doesn't enforce it, with no way to check — such an action is still reported, under `x-required-unenforced`, since `required: []` alone would read as "a call with no arguments is valid," a different and wronger claim.

The check is stated about the action's shape rather than read off `action.required`, because `required` is what's unreliable here: Python 3.14 stopped setting it for exactly these actions, so trusting it would make the schema say different things on different interpreters about one unchanged command.

## lionagi/hooks/

### `hooks/external.py`

<a id="hooks-private-copy-trust-pinning"></a>

#### hooks-private-copy-trust-pinning

`_BoundExecutable`/`_materialize_private_copy`/`_hash_private_copy`/`_prepare_trusted_execution` implement content-pinned trust for an external hook command. An open fd pins the *inode*, not the content: hashing the fd and separately re-reading it to build the executed copy would leave a window where an in-place overwrite between the two reads gets copied and executed as trusted, even though the earlier digest still matches.

Closing that window means never hashing the mutable source at all — the private copy is made first, from whatever bytes are at the fd right now, into a directory nothing but this process holds a handle on; the trust digest is then computed by re-hashing that immutable, single-process-owned copy, never the source fd/path again. A source overwrite at any point can therefore only ever affect the source, never the copy that gets compared or exec'd. `path` inside `private_dir` is what actually gets exec'd — the configured/approved path is never spawned directly, so a same-path substitution after approval (in-place overwrite, or symlink retarget) cannot change what runs.

<a id="hooks-stdout-decision-parsing"></a>

#### hooks-stdout-decision-parsing

`_parse_stdout_decision` parses exit-0 hook stdout. Empty stdout is the *only* case that legitimately means "no structured output" (documented no-opinion convention — allow). Every other case that fails to yield a recognized decision form sets `malformed=True` instead of reusing the empty-stdout convention: non-empty stdout that isn't valid JSON, a JSON value that isn't an object, an object with neither `hookSpecificOutput.permissionDecision` nor a top-level `decision`, and an explicit `hookSpecificOutput.permissionDecision: null` (present but null, unlike the key being absent) — all deny on a blocking seam.

A top-level `decision` of `"block"` normalizes to `"deny"`; `"allow"`/`"approve"` (or an explicit top-level `null`) normalize to `None` (allow) — the one place an explicit null is a documented convention rather than malformed, since the top-level shape's null means "no decision" the same way an absent field would.
