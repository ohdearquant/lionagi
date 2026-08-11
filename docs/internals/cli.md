# `lionagi/cli/` internals

Non-obvious invariants, protocol contracts, and design rationale for the CLI package that
don't belong inline as long-form comments/docstrings. Organized by module. Source-of-truth
inline comments stay 1-2 lines; anything needing more context lives here.

## `main.py` — `li` entry point

`_handle_play_shortcut`: expands `li play NAME [...]` sugar into `li o flow
-p NAME [...]` before argparse runs. When a flag precedes NAME (`li play
--bypass NAME "prompt"`), a throwaway probe parser (the flow subparser's
base flags only, before playbook-specific schema injection) locates NAME by
separating recognized flag(+value) pairs from bare positional tokens — the
real parse happens later once the playbook's own `args:` schema can be
injected, so only base flags are understood at this point and custom
playbook flags placed before NAME aren't supported (they must follow it).
NAME is removed from the exact partition it was selected from, never by
string value across the whole argv, so an earlier flag VALUE equal to NAME
(e.g. `--team-mode foo -- foo`) isn't deleted in its place.

`main()`: `-v`/`--verbose` and `--cwd` are scanned before argparse runs
(before any subcommand parser exists), strictly before the `--` sentinel so
a scheduled `action_prompt` containing `--verbose` can't flip verbose mode.
`li agent`, `li o flow`/`li o fanout`, and `li schedule` each parse
standalone rather than through normal subparser dispatch, because
`parse_intermixed_args` drops the `--` sentinel between its two passes
(letting a hostile prompt like `--bypass` placed after `--` toggle real
flags on the re-parse) and nested subparser dispatch can't intermix flags
with `[MODEL] PROMPT` positionals. Both `flow` and `fanout` had a
pre-fix disease from the same root cause: flow silently misassigned a
flags-preceded prompt to the model slot (both positionals were `nargs='?'`,
so argparse's greedy left-to-right fill grabbed the first one), while
fanout hard-rejected with "unrecognized arguments" (a flag between its two
positionals split them into two groups argparse can't reconcile). All three
special-cases split manually at the `--` sentinel, parse flags, and fold
leftover positionals back in order. `li agent status`, `li monitor run
<id>`, and `li wait <id>` are each intercepted before their command's
argparse dispatch, because a positional `id`/free-form-ids slot would
otherwise swallow a literal token like `"status"` or `"run"`.

## `_code_identity.py` — worktree fingerprint semantics

`_worktree_fingerprint` digests uncommitted state via the status listing, the diff against
HEAD, and each named path's size/mtime/inode — never file content, so cost doesn't scale with
file size. A clean tree skips the diff call entirely. Absence (a deletion) is a measured value,
not a gap; a path that exists but can't be read yields no digest at all ("cannot tell", not
"unchanged"). The allowance shared with the git calls means a timeout mid-measurement also
yields no digest, never a partial one — a partial digest would compare unequal to a full one
taken later and report a phantom edit.

Known blind spots, kept as an explicit list because "only one case" silently goes stale:
metadata-preserving rewrites (same size/mtime/mode/inode); anything under `.gitignore`;
ownership and xattrs. The inode is included so a rename-over-file replacement stays visible,
at the cost of false positives on filesystems (some network/userspace ones) that hand out
fresh synthetic inodes for unchanged paths.

This asymmetry is deliberate: the surface exists to answer "is the code I'm running the code
I think I'm running", and a false "nothing changed" defeats that purpose far worse than an
occasional false "something changed".

## `machine.py` — machine-result envelope contract

Every `li <command> --machine` call answers with exactly one JSON object on stdout and
nothing else; every human-facing byte goes to stderr — the consumer is a subprocess caller in
another language that cannot read this file, so the shape is fixed here and `CONTRACT_VERSION`
is the only thing it has to negotiate. Pieces: `ok`/`failure` are the two envelope constructors
(`ERROR_KINDS` is closed); `available`/`unavailable` are the availability wrapper, so "there are
none" and "could not establish whether there are any" never share an encoding; `reserve_stdout`
takes stdout away from everything except the envelope at the file-descriptor level, so a stray
`print` (or a child process that inherited the descriptor) lands on stderr instead of corrupting
the result; `dispatch_machine` runs one machine command and turns any failure into an envelope
rather than a traceback.

## `_mcp_resolve.py` — MCP server resolution at submit time

A CLI-backed agent otherwise discovers MCP servers by walking up from its own working
directory, making its tool surface a property of *where it was told to work* rather than of the
submission — nothing fails or is reported when this yields no servers, the agent just quietly
has fewer tools than its instructions assume. The submitting side resolves the config from its
own directory and hands the result to the child explicitly, snapshotted rather than passed as a
path (a path would be re-read by the child at an unknown later moment, and would have to live
inside the child's working directory to survive the provider's repo-containment check — exactly
the directory that doesn't have it). "Nothing was configured" and "something was configured and
could not be used" are returned as distinct states, so a failed resolution always carries its
reason.

## `machine_schedule.py` — `li schedule ... --machine` payload rules

Three rules shape every schedule subcommand's machine payload (the envelope contract itself
lives in `machine.py`): each subcommand's argv is parsed by the CLI's own human-invocation
parser, never a mirror of it, so a flag unmoved to the machine path is refused by name rather
than silently parsed and dropped. A mutation reports only what landed, not what may follow:
`trigger` reports the fire was accepted and its allocated run id, not that anything ran yet;
`create` reports the written row plus, separately, when the trigger next resolves — computed
through the scheduler's own resolver, since an echoed cron expression only proves the string
survived the trip. Reaching the schedule store is itself a fact that can fail — most of these
commands are HTTP calls to a running Studio, so an unreachable Studio answers with an explicit
`unavailable` naming the URL, never an empty list that reads as "there are no schedules".

## `orchestrate/` (`li o fanout` / `li o flow`)

`_common.py`'s `TEAM_COORD_SECTION` / `TEAM_COORD_SECTION_MESSENGER`: two
variants of the team-mode coordination prompt section, appended onto the
base worker system prompt. CLI-provider workers (codex/gemini subprocesses,
no tool-calling surface) get only the bash `li team` channel
(`TEAM_COORD_SECTION`); API-model workers additionally get the in-process
`messenger` tool bound to their branch and get the messenger-only variant
instead. Which variant applies is decided by `messenger_bound` in
`build_worker_branch` before the system prompt is assembled — see
`team_worker_system()` in `_orchestration.py`.

`_notify.py`: `--notify` is scoped compatibility sugar over the terminal-
callback registry. `li agent` and `li o fanout` always scope to their
**session** entity because only their sessions transition to terminal. `li o
flow` / `li play` additionally finalize and scope to an **invocation** when
`--invocation` is set, because flow owns that finalization. This is
deliberately different from the settings-level `notify.on_terminal` handler
(bootstrapped once per process, unscoped, delivering the new minimal
envelope) — `--notify` is a per-run override carrying the old payload shape
for existing consumers, not a second copy of the same delivery. It
registers as an *override* (`TerminalCallbackRegistry.register`), so it
replaces the settings-resolved handler for this one run's entity only;
other runs still get the settings-level handler unaffected. For backward
compatibility with the documented `{payload}`/`{status}`/`{invocation_id}`
command-template placeholders and the legacy `LIONAGI_NOTIFY_PAYLOAD`/
`LIONAGI_NOTIFY_STATUS`/`LIONAGI_NOTIFY_INVOCATION_ID` environment
variables, both are populated: placeholders are substituted into each
parsed argv token directly (no shell is ever constructed, so a literal
`{payload}` inside a quoted argument stays exactly one argv element), and
the same three values are set as environment variables on the child
process. There is no longer a direct teardown call into a notify hook: the
terminal event now comes from the guarded lifecycle transition itself
(`db.update_status()` on the run's session/invocation), so registering here
and letting the registry's own post-commit push fire it is what prevents
double delivery.

**`deliver_flow_notify_now`** — the direct-path sibling of
`register_flow_notify_scope`, for the one case that registered path cannot
cover: `setup_agent_persist` failing before a session entity exists (`li
agent`, `agent.py`). With no session there is no terminal transition to
register against, so the run delivers its own `--notify` adapter itself, at
process end in the `finally` block, once its own terminal status is already
known — same `resolve_notify_config` resolution, same legacy payload/argv/env
shape (`_legacy_argv_env_builders`, shared with the registered path), just
invoked against a synthesized `RunTerminalEnvelope` instead of one raised by
a DB transition. This is why the MCP server's job-spawn `--notify` template
(`lionagi/mcp/jobs.py`, `_notify_template`) still fires for a run whose
persistence broke: the argv it invokes (`lionagi.mcp._notify_hook`) carries
its own `run_id` and needs nothing from the session/StateDB path to record
the job's end and attempt delivery — it is agnostic to which of the two
notify paths called it.

Idempotence is a single file check, not the per-run lock the MCP job record
uses: `run.notify_outcome_path.exists()` before attempting delivery, skipping
if something is already recorded there. This is sufficient (not merely
convenient) because the two notify paths are mutually exclusive by
construction within one process — whether a session entity exists is decided
once, right after `setup_agent_persist` returns, and never changes for the
rest of the run, so the registered path and the direct path can never both
fire for the same run. The check exists for re-entrancy safety (a second
pass through the same `finally` body), not to arbitrate a real race.

The refusal record (`record_notify_rejection_to_run`, `notify_outcome.json`
with `reason` set) that used to fire unconditionally the moment persistence
failed now fires only when delivery is actually attempted and genuinely
cannot be completed — nothing configured to run, or the configured adapter
itself fails to resolve/build. A `--notify` adapter that resolves and builds
but fails to *launch or exit cleanly* is recorded by `record_notify_outcome_
to_run` instead (no `reason` key — see `lionagi/state/lifecycle/
notify_settings.py`), which is what lets `lionagi/mcp/jobs.py`'s orphan
reaper (see `docs/internals/mcp.md#reap-reason-code-split`) tell "this run
said its notice never arrived" from "this run said nothing at all."

### `_orchestration.py` — team-mode worker/coordinator wiring

`team_worker_system()`/`team_history_context()`: a worker never has both
channels — `messenger_bound` selects which of `TEAM_COORD_SECTION` /
`TEAM_COORD_SECTION_MESSENGER` (see above) describes its channel.
`messenger_names` (computed once per team, before any branch exists, via
`worker_is_cli`) flags teammates that are CLI-provider and therefore
unreachable via `messenger(action="send", to=...)`, so the prompt never
names an unreachable target. The orchestrator itself is never bound into
the live messenger roster — a messenger-bound worker escalates via
`action="help"` instead, and its roster line is flagged not-a-`to=`-target
the same way an unreachable CLI teammate is. `team_history_context` handles
`--team-attach` onto an existing team: a messenger-bound worker's Exchange
is fresh in-memory state that never replays messages predating the
messenger tool, so any prior message addressed to it is surfaced instead as
`operate(context=...)` data — explicitly labeled transcript, never promoted
into the system prompt, since message content is untrusted prior text, not
a vetted instruction. A bash-channel worker doesn't need this: `li team
receive` already gives it live access to the same history.

`build_worker_branch`'s `messenger_bound` return value: True only when
team messaging is active AND the worker is a non-CLI provider; callers use
it to decide whether to enable action serialization on that branch.

Rung-2 coordinators (`make_help_coordinator`, `TeamLifecycleCoordinator`):
plain Python routing, no LLM call — the counterpart to
`ReactiveExecutor._schedule_escalation` for flow-mode. The help coordinator
folds "blocked"-urgency signals into `env._escalated_evidence` for the run
summary; rung 3 (model-bump) and synchronous human paging are out of scope.
`TeamLifecycleCoordinator` keeps no separate liveness bookkeeping — every
running/idle/retired fact comes straight from `team.compute_quiescence`
over the team inbox, so what it decides always matches `li team show` and
the pure predicate's own unit tests, with no in-memory state to drift out
of sync with the file. `on_done`/`on_finished` write structured team-inbox
entries via `team.post_done_signal`/`post_finished_signal` (code, not the
model); `on_finished` retires a worker permanently — `compute_quiescence`
never revives it. `build_round_operations` re-invokes one `Operation` per
pending worker on that worker's OWN branch (session continuity is the
point of a "round" instead of a fresh spawn), folding unread mail into
`context` as `prior_team_messages` — never the system prompt, so a
teammate's message can't smuggle new standing instructions into the
model's persistent framing. Consuming the unread mail (file inbox via
`team.pop_unread_messages`, Exchange via `_exchange_prior_messages`) and
posting a coordinator-authored `wakeup` signal are side effects: the
`wakeup` post is what flips those workers back to "active" on the next
quiescence read, which is what prevents the same round from being
double-injected.

### `flow.py` — dividing a time budget across a DAG

When a flow run carries a total time budget, each op needs to be told its own
share of it — and that share has to be a bound computed before any op has
run, not a measurement of how long ops actually take.

`critical_path_depth(dep_indices)` is the longest dependency chain in the
plan, in ops — how many ops the *dependencies alone* force to run one after
another. It's a lower bound on wall-clock, not the bound: a concurrency cap
can serialize ops this function considers parallel — under
`--max-concurrent 1` it still reports the dependency-only depth even though
the run is forced fully sequential, which is why `max_sequential_depth`
below, not this function, is what actually sizes a budget. Dependencies
point only backwards (op *i* may depend
only on ops before it), so a single forward pass suffices; an out-of-range
dependency index is ignored rather than raising, since a bad index in a
budget hint shouldn't be able to take down a run.

`max_sequential_depth(dep_indices, num_ops, max_concurrent)` is what actually
sizes the budget. It has to be an upper bound rather than an estimate,
because a simulated schedule (a ready-queue admitting `max_concurrent` ops at
a time) only models the equal-duration case — give four ops a cap of two and
let one of them run six times longer than the rest, and the other three
serialize behind it into a chain of three, while a duration-blind simulation
would have counted two. Unequal op durations are the normal case, not the
corner, so exactness on the equal-duration schedule is the wrong thing to
optimize for. Being a bound also fixes the error's direction: undercounting
hands every op *more* time than the flow can actually afford and the flow
overruns its deadline; overcounting only tells an op it has slightly less
time than it might have had.

Two things force ops into sequence, and the bound is the worse of the two:
dependencies force a chain no capacity can shorten, and capacity forces the
rest — a run of ops executing strictly one after another can contain at most
one op from any set that started together, bounding it at
`num_ops - max_concurrent + 1`. Neither can exceed `num_ops` (the
everything-serializes case). `max_concurrent <= 0` reads as unbounded,
matching how the executor itself interprets it; a plan whose dependency data
doesn't describe all its ops falls back to `num_ops` (assume nothing
overlaps, since nothing more can be said).

`op_budget_share` divides the total budget by `max_sequential_depth`, not by
`num_ops` — that's the whole point of computing the depth bound rather than
just counting ops.

`_build_budget_preambles` needs `deadline_epoch` — the instant the run's
clock started, captured by the caller — and deliberately has no default for
it. The obvious fallback, `now + total_budget`, would be wrong by however
long the run has already been going, and wrong in the permissive direction
(every op told it has more time than it actually does). Missing the instant
therefore means no preamble at all: telling an op nothing is an honest
failure, telling it a late deadline is not. A budgeted run that reaches this
function with no deadline instant is a wiring error, not a configuration
choice — the budget is still enforced on these ops, they're just never told
about it, so this case logs a warning and returns no preambles rather than
inventing a value.

**`_hand_mcp_servers`** — a caller-named MCP server set is applied to every
worker whose provider has a transport for it; a worker whose provider has
none is warned about, naming the worker (`label`) since a run's workers can
resolve different providers. `{}` (an explicit empty set) and `None` (no set
at all) are different requests: `{}` means "apply no servers" and is applied
as the whole set; `None` means there's nothing to hand over and the model
keeps whatever it already had.

**`collect_worker_artifacts`** — the worker roster comes from the artifact
directories the run actually handed out, not a scan for non-empty ones, so a
worker that wrote nothing is still a `reported` entry with empty `files`
rather than being silently absent. A worker the run expected but that never
registered a directory is emitted as `unregistered` rather than dropped
(otherwise that failure looks identical to "the run just had fewer
workers"). Per-entry `status`: `unreadable` (traversal raised), `missing`
(the registered path is gone), `unregistered` (expected, no directory ever
recorded), `reported` (looked, `files` is what was there).

### `flow.py` — reactive DAG orchestrator (`li o flow`)

Artifact contract has two write classes to `artifact_contract_json`: `_build_dag`
does the planned-leg write once at DAG-build time (all roles resolved, before
any leg runs), and `_execute_dag` does a second, append-only write after each
reactively spawned node completes. Both are sound because what's expected of a
spawned node (role defaults + spawn_id) is frozen before it's ever queued — the
append only adds entries, never edits the planned-leg set. The planned-leg
write must reach the session row directly (not just `env._live_persist`) so a
crash or orphan exit before teardown still leaves the DB accurate.

`_reconstruct_spawned_nodes` rebuilds a checkpoint's `spawned` entries into a
fresh graph on resume, checked BEFORE any node is added so a refusal never
leaves the graph partially mutated. Three soundness checks per entry: (1) it
must carry `operation` (CHECKPOINT_VERSION 2+; older checkpoints have nothing
to rebuild from); (2) if it has a `parent_id`, that parent must itself be
checkpointed terminal (completed/failed) or be another entry in the same
`checkpoint_spawned` list — a parent merely existing in the DAG isn't enough,
since resuming while that parent reruns live risks duplicating or dropping the
spawn decision; (3) an entry with `assignee` must also carry `spawn_id` (both
are stamped together unconditionally by `role_node_builder`, so one without
the other means the checkpoint predates that field or is corrupt). Any failure
refuses resume for only the affected node(s), never the whole run.

Spawn-id sequencing: on resume, the ordinal sequence must start past the MAX
existing ordinal among restored spawns + 1 (not `count`, since a crashed run
can leave gaps — an allocated-but-never-completed spawn never made it into
`spawned`), or a live spawn this generation could reissue a restored spawn_id
and its artifact directory. `n_spawned` accounting always includes restored
spawns from a prior checkpoint generation, not just what the live executor
spawned this generation — otherwise a resume that reconstructs every spawned
node as already-terminal would report `n_spawned=0` and silently skip the
`with_synthesis`-or-`n_spawned` gate in `_run_flow_inner`.

### `_round_records.py` / `_quiescence.py` — proving a manifest round is idle

A manifest round dispatches independent legs as subprocesses, each in its own
process group. Two questions need durable, disk-based answers, because the
observer that eventually has to answer them — a reaper, a cooperative
finalizer — may share nothing with the process that ran the round: which
process groups belong to this round, and are they still holding a live
member.

**`_round_records.py` answers the first question.** Two files per round:
`{run_dir}/legs/{label}.json` records what one leg was dispatched with and
how it ended; `{run_dir}/round.json` records the round summary. The dispatch
half of a leg record — the leg's `pgid` and `pid_create_time` (the start time
of the process that led it) — is written at spawn, before the leg can do
anything. This ordering matters: a group id read only after the leg exits can
by then belong to a different process the kernel has reissued that number to;
pairing it with the start time is how a later reader tells "this run's group"
from "some stranger who now has that number." A leg record missing
`pid_create_time` carries no identity and must be excluded from a sweep
entirely, not treated as a present-but-unpinned group. Every write lands by
temp-file-plus-atomic-rename, and a writer that finds an already-`COMPLETE`
leg record leaves it alone — first writer to complete a leg wins, and
`recorded_by` names the winner. `round.json` starts as
`round_state: pending_harvest` at spawn and is flipped to `complete` only as
finalization's last act, so a terminal read at any point before that, by any
writer, observes "pending" rather than something that looks silently done.

`control_group_domain()` builds the sweep's domain by reading every leg
record from disk (never a live runner's memory) and keeping only groups whose
record carries both a `pgid` and a `pid_create_time`. A record missing either
is counted as `unpinned` and excluded from the domain — a sweep cannot
certify a group it was never told about, and a bare number pretending to be a
domain is worse than an admitted gap. `ControlGroupDomain.complete` is false
whenever anything was unpinned or unreadable, and a sweep over an incomplete
domain must report `unproven`, never `quiet`.

**`_quiescence.py` answers the second question** — is every recorded group
empty — via `sweep_quiet()`. It classifies each group as `QUIET` (no live
member), `BUSY` (a live member was pinned), or `UNPROVEN` (the process-table
scan of that group didn't complete), then rolls the whole sweep into one
verdict by a fixed precedence: `BUSY` beats `UNPROVEN` beats an incomplete
domain beats `NO_DOMAIN` beats `QUIET`. An unread member is indistinguishable
from an absent one, so scan-incompleteness is checked before the emptiness
test even runs — a partial scan that happens to see nothing must never read
as quiet.

Two invariants matter when interpreting a `Quiescence`:

- **Where the observer sits.** A reaper belongs to no recorded group, so its
  predicate is absolute emptiness. A cooperative finalizer runs inside the
  runner's own group and must exempt exactly its own pid via
  `exempt_pgid`/`observer_pid` — nothing else. Passing an `exempt_pgid` the
  observer isn't actually in would wrongly exempt that pid from a group it
  doesn't lead.
- **Group ids are not stable identities.** `getpgid(pid) != pgid` is the only
  thing a scan reads, so a process that calls `setpgid(0, 0)` to leave its
  recorded group — not limited to processes that call `setsid`, a narrower
  class an earlier version of this doc named — becomes invisible to every
  group-based check here, and no amount of re-scanning fixes it. A member
  forked during the sweep can likewise be missed; identification and signal
  are two syscalls, so a group observed empty was empty at the moment it was
  read, not necessarily afterward.

`enforce_quiet()` is the close-time counterpart. It signals only groups
`sweep_quiet` pinned as `BUSY` — never `UNPROVEN`, since an incomplete scan
says a member *may* exist but never who, and signalling on that risks hitting
whatever process now holds a recycled pgid. For a group the observer sits
outside, it kills the whole group (`kill_group_now`, which also catches a
member that appeared after the scan); for the observer's own group it signals
its pinned members individually and skips itself, since killing that group
would kill the observer before it can publish a result. Sending a signal is
not the verdict: `enforce_quiet` waits up to `settle` seconds
(`_wait_for_delivery`) and then re-sweeps — the second sweep (`after`) is
what a caller must trust, not the fact that something was signalled.
`already_quiet` separately records whether anything needed signalling at
all, so a caller can tell "the round genuinely finished clean" from "the
round leaked and this had to clean up after it," even though both end in the
same `QUIET` verdict.

### `_control.py` — `li o ctl pause|resume|msg`

Pure writers: resolve the target session and insert one row into
`session_controls`, without waiting for it to apply. Two live consumers read
that table: the poller in `flow.py`'s `_execute_dag` (flow/play runs) and the
turn-end drain in `cli/agent.py` (agent runs). Only context-mode `msg` is
supported today — the poller appends the message to shared flow context for
operations not yet rendered; operation-mode messages are not (ADR-0069 D1,
D3). `li o ctl status <id>` is how a caller checks whether an enqueued
control actually landed.

**Admission gating.** `pause`/`resume` are only accepted for `flow`/`play`
sessions; `message` is additionally accepted for `agent` sessions. Within
`agent`-kind sessions, `run_id` alone is not proof that something will drain
the control — several producers stamp a `run_id` on the sessions they create
without running the turn-end drain that would consume it. The authoritative
signal is a `drains_controls` flag the runner declares in `node_metadata`
when the session starts; its absence is read as `False` on purpose, since a
runner that never declared draining is exactly the case admission must
refuse. One exception is grandfathered deliberately: a pre-existing CLI
agent-run session row that predates the `drains_controls` declaration is
still admitted, because it does have a real turn-end drain. The precise set
of session shapes/transitions this covers is intentionally not enumerated in
prose — every attempt drifted out of date — see the resume cases in
`tests/cli/test_agent_steer.py` for the executable version of that contract.

The running-session check and the `session_controls` insert are two separate
statements; a session can go terminal between them. The insert re-checks the
running condition so it — not the earlier read — is what decides admission,
avoiding a control queued against a run whose consumer has already exited.

Landing time is a property of the consumer, not the verb: a flow/play poller
renders queued context before the next op (~2s poll interval), while an
agent leg only drains at its next turn boundary, which can be far later than
2s into a long provider call.

### `_finalize.py` — the exclusive claim over closing a manifest round

Three different processes can arrive at the same round wanting to finish it:
the runner itself at teardown, a reaper acting on a kill request, and a
reaper sweeping up an orphan. Only one may act, and the primitive deciding
that is a non-blocking `flock` on `{run_dir}/finalize.lock`. The kernel ties
the lock's lifetime to the holding process, so a dead holder's claim vanishes
with it — takeover *is* acquisition, and there is no stale-lock repair path
because there is no separate repair path at all.

Two properties matter:

- **The descriptor must never reach a spawned leg.** A lock a leg inherited
  would keep a dead runner's claim alive from inside a living child. Since
  PEP 446, every descriptor Python opens is non-inheritable by default, so
  the explicit `O_CLOEXEC` here is a statement of the requirement, not what
  actually enforces it — the way to lose the property is to mark the
  descriptor inheritable, which is what the module's test pins.
- **The claim decides nothing by itself.** Every claimant arrives because of
  something it observed *before* acquiring the lock, and the gap between
  observing and acquiring is exactly where a live holder can finish,
  publish, exit, and release the lock — turning what the claimant saw into
  stale information. So the first act under the claim is always a re-read;
  disposition comes only from that fresh read, never from what motivated the
  attempt. This is why the module's functions take readers as arguments
  rather than pre-observed state — there's no parameter through which a
  caller could hand in what it saw earlier and have that trusted.

## `team.py` — `li team` persistent messaging (inbox pattern)

`_locked_team`: read-modify-write under an exclusive POSIX flock. Explicit
`fp.flush()` + `os.fsync()` before releasing the lock is required — without
it, a waiting reader can acquire the lock the instant it releases and
observe stale (pre-write) content, since `fp.write()` only fills a buffer
and doesn't guarantee bytes have left the process; under concurrent writers
this showed up as a spurious "No team found" moment after a team plainly
existed on disk.

`compute_quiescence`: pure predicate over message `kind`/`from`/`to`/
`read_by` only, never touching a file/branch/agent. Lifecycle model,
derived purely from message kinds: every named worker starts **active**
(presumed still running, nobody has said otherwise yet); a `kind="done"`
message *from* a worker makes it **idle** (finished this turn, may be
revived by a later round); a `kind="finished"` message *from* a worker
**retires** it permanently (never revived again, mirroring
`LionMessenger`'s `finished` action); a `kind="wakeup"` message addressed
*to* a worker makes it **active** again, whether from a teammate (peer
wakeup) or the coordinator's own round injection. The run is quiescent once
every worker has settled (none **active**) AND there is nothing left for
the coordinator to do about it: no unread `kind="message"` mail sitting in
an **idle** worker's inbox, and either the round budget is exhausted or the
caller isn't asking for one more round anyway.

`history_boundary` scopes the active/idle/retired classification to the
current run generation: `--team-attach` reuses one team file (and often the
same role-derived worker names) across runs, so a prior run's `done`/
`finished`/`wakeup` signals sit in the same message list a fresh run reads.
Without a boundary those signals would classify this run's workers as
already idle or retired before either has posted anything of its own —
`TeamLifecycleCoordinator` snapshots `len(messages)` at attach time
(`message_boundary`, wired through `make_team_lifecycle_coordinator`) and
only messages at or after it count toward active/idle/retired. Content
(`kind="message"`) mail is deliberately exempt from the boundary — the
pending-mail scan always looks at the full history, so `--team-attach`'s
"prior content stays visible" contract holds even though prior *lifecycle*
signals no longer leak into the new run.

## `_context_from.py` — context-ref resolution and distillation

`li agent --context-from <ref>`: resolve prior-run refs into a bounded context block.

Ref resolution order: session id (state.db, prefix match) -> branch id
(`~/.lionagi/runs/*/branches/*.json`, prefix match) -> run id (`run.json` manifest prefix
match) -> file path. Distillation is mechanical (no LLM): a saved artifact/summary verbatim,
else the final assistant message + initial instruction, else a loudly-marked head/tail
truncation to fit budget. Budget is shared across the combined injected block (including the
XML wrapper and inter-block separators), allocated in argv order.

**`_find_branch_candidates`** — branch-candidate dedup across run dirs. Mirrors
`_runs.find_branch`'s glob scan but collects every distinct `branch_id` across all run dirs
(instead of returning the first) so ambiguity can be detected; the same `branch_id`
snapshotted into multiple run dirs (resume) is one candidate, not many.

**`build_context_block`** — context-block budget allocation and drop order. `budget_tokens`
bounds the COMBINED injected block (XML wrapper + separators included, not just distilled
payload text); wrapper/marker overhead is reserved first, in argv order, before payload text.
A single ref always yields at least one loud-marker-only block, even at `budget_tokens == 0` —
a slightly over-budget marker beats silently dropping the one ref the caller asked for. With
multiple refs, the total budget is a hard ceiling: any ref that can't fit even its minimum
marker overhead is dropped (and everything after it, since the reserved budget only shrinks).
If even the first ref can't fit, injection is skipped entirely.

## `_providers.py` — agent profile parsing

**`_parse_profile_timeout`** — agent profile timeout field validation. Only a genuine positive
int is accepted — YAML booleans (`True`/`False` are ints in Python) and floats (which `int()`
would silently truncate) are rejected rather than coerced.

## `_util.py` — exit-code and exception classification

**`EXIT_CODE_BY_STATUS["completed_empty"]`** — completion-trust gate exit code. Loop exited
clean but no commits ahead of base and no artifacts were produced. Exits non-zero so scripts,
CI, and `schedule on_fail` chaining treat it as a failure rather than silently trusting an
empty run. (Same rule is re-applied for status classification in `status.py`, see below, and
surfaced as a UI note in `_detect_degraded`/`_audit_degraded` in that same module.)

**`classify_exception`** — SIGTERM classification. SIGTERM is an external termination request,
not an internal failure — it lands in the same terminal bucket as a runtime-cancelled task
(same reason class, same exit code 143) rather than a new status.

**`EXIT_CODE_ENVIRONMENT_ERROR` (78)** — the installation cannot start. A `ModuleNotFoundError`
reaching the top of `main()` means an import failed and nothing handled it, so no command ran at
all. This is deliberately outside `EXIT_CODE_BY_STATUS`, because it is not a run status: there is
no run. It exits 78 (`EX_CONFIG`) rather than 1 so that a caller can tell a broken environment
from a run that started and failed, which both used to exit 1 with a traceback. A wrapper that
reads only the exit status and the presence of an artifact would otherwise report a missing
dependency as the agent's own empty or crashed result, and attribute an environment outage to
whatever the agent had been asked to do.

The traceback is printed first because it names the import chain that reached the missing module;
the one-line summary is emitted last through the CLI error logger, so a caller keeping only the
tail of stderr still receives the diagnosis. Callers distinguishing the two cases should test for
78 specifically; every other exit code retains its existing meaning.

The wrapper in `main()` catches whatever escapes, but a `ModuleNotFoundError` absorbed by a broad
handler on the way to dispatch never reaches it. Every such handler ahead of the wrapper must
therefore split the missing-module case out and return 78 itself, keeping its existing handling for
everything else; a discriminator absorbed upstream is not a discriminator. Two do so today: the
lazy command-module loader, and the agent-profile load in `li play check`. Both keep their concise
report rather than switching to a traceback, because a command that never started has no stack
worth printing and naming the missing module is the whole diagnosis. Any new broad handler in that
region needs the same split.

The boundary in the wrapper is a run having been allocated. `allocate_run` sets a marker, and once
it is set a `ModuleNotFoundError` is *not* reported as an environment fault: a run id, a run
directory and a manifest exist on disk, so telling the caller nothing was executed would be the
same misattribution pointed the other way. Such an error propagates and is reported the way any
other failure during a run is. This is why a lazily imported provider extra going missing mid-run
does not exit 78.

The marker is process-wide by necessity, not by convenience. `run_async` drives each command's
async body on its own thread with its own event loop, so allocation happens on a different thread,
in a fresh context, from the one the entry point returns on — a thread-local or a `ContextVar`
would be invisible to the reader and would claim no run started while one existed. The cost is
precision when two invocations overlap in one process, which is not a supported entry point. That
is handled by making the unsafe direction impossible rather than by pretending it cannot happen:
seeing another invocation's allocation only re-raises, which is the behaviour that predates this
code, while *losing* one would assert something false, so the reset on entry is skipped whenever
another invocation is in flight.

The accepted limitation, stated so it is not rediscovered as a bug: while two invocations overlap,
one that dies on a missing import before allocating anything can see the other's allocation and
re-raise rather than reporting 78. Making that precise requires attributing an allocation to an
invocation, and allocation happens on a thread the entry point did not create, so attribution means
threading a token through `run_async` into every command's async body — a change to a shared
concurrency primitive, for an entry point that is not supported, to turn an ambiguous report into a
precise one. Revisit if concurrent in-process invocation ever becomes supported.

## `dispatch.py` — dispatch outbox (ADR-0059)

Module docstring: `li dispatch` inspects and acknowledges durable `dispatch_outbox` rows.
Enqueue is not a CLI verb here: dispatches are produced by schedule actions and the delivery
loop, both already running inside the daemon process. The read/ack verbs follow `li monitor`'s
direct-DB-read discipline (not `li schedule`'s daemon-HTTP-only discipline): if `li dispatch
ack` required the daemon to be up, a daemon restart window would strand acks, defeating the
point of a durable outbox. Every write here is a single-row guarded compare-and-swap inside
`BEGIN IMMEDIATE` (via `StateDB._tx()` / `lionagi.state.transitions.transition()`).

## `engine.py` — engine run dispatch

**`_do_engine_run` create_session** — `create_session` is `INSERT OR IGNORE`: a pre-existing
row with this id would be silently reused, appending our signals to an unrelated session and
mirroring terminal status onto it. `run_id` is a fresh `uuid4` so this should never happen —
but never bind to a row this run did not create.

**`_do_engine_run` engine.run() dispatch** — `CodingEngine` has its own `.run()` signature
(positional spec + keyword `test_cmd`/`workspace`/`export_dir`); other engines use
`Engine.run()` which dispatches to `_run(run, <main_arg>, **run_kwargs)`.

**`_do_engine_run` BaseException handling** — `asyncio.CancelledError` and
`KeyboardInterrupt` are `BaseException` paths that bypass the `except Exception` handler.
Mark the row cancelled before re-raising so Studio doesn't show it as permanently "running".
`run_async()` (`lionagi/ln/concurrency/utils.py:86`) cancels the task on SIGINT and then raises
`KeyboardInterrupt` at `:108`; re-raising here preserves that exit-code behavior (interpreter
default for SIGINT).

**`_do_engine_run` export_dir sourcing** — serializes the result to stdout as JSON.
`export_dir`: the CLI knows what directory it passed; neither `CodeResultRecorded`
(`lionagi/engines/coding.py:153` — fields: `passed`, `measurements`, `caveats`,
`experiment_ref`, `verdict_ref`) nor the hypothesis string echo it back. `export_dir` is sourced
from args directly for kinds that accept the flag, falling back to `result_data` for any future
engine model that does include it.

## `invoke.py` — invocation records

**`_start_invocation`** — invocation records carry no pid marker. `li invoke start` is a
short-lived CLI command that creates the row and exits (`INV=$(li invoke start ...)`); it is
NOT the long-lived owner. Recording its pid would be a recycled-PID hazard — a later live
process reusing it could make `li kill` mis-signal or make `--all-stale` skip the invocation.
An invocation is a PID-less umbrella over the child sessions it spawns; those carry their own
pid markers.

## `skill.py` — skill path resolution

**`resolve_skill_path`** — symlink containment (security). Rejects any path whose resolved
target escapes the resolved skills root. Blocks the disclosure vector where a `SKILL.md` is
itself a symlink to an arbitrary file on disk.

## `stats.py` — aggregate run stats

**`_reject_non_positive_since`** — `li stats --since` positivity tightening vs `li monitor`.
Monitor's shared `_since_timestamp()` accepts `0d`/`-1d` without complaint (fine for its own
semantics), but for an aggregate report that would silently produce a false-empty or
nonsensical result instead of failing loudly — this only tightens `li stats runs`. Malformed
values (bad unit, non-numeric) are still left to `_since_timestamp` to reject.

**`_query_run_stats`** — `group_by` validate-before-SQL-interpolation contract. `group_by`
entries are validated against `GROUP_BY_COLUMNS` before this is ever called, so interpolating
the resolved column names is safe.

**`_run_stats_runs`** — `StateDB(readonly=True)` contract. `readonly=True` skips schema
application, the `BEGIN IMMEDIATE` write-lock event, and every mutating PRAGMA (see
`StateDB.open()` / `make_readonly_engine()`). A reporting command must never write to the DB
it's reporting on, even implicitly via a schema-reconcile pass.

## `wait.py` — `li wait` (ADR-0035 completion contract)

Module docstring: `li wait <id>...` blocks until every named run (an agent session, a play, a
flow invocation, or a scheduled run — any kind, mixed freely) reaches a terminal state, then
prints one frozen, tab-delimited line per run on stdout:

```text
<run_id>\tstatus=<terminal_status>\treason=<reason_code>\t
    artifact_dir=<run_dir>\texit_code=<n>
```

Stdout carries contract lines only; every diagnostic goes to stderr via `_logging`.
`wait_for_terminal()` is the reusable, importable core (no argv, signal handling, or printing)
so other surfaces can await completion without shelling out; `run_wait()` is the thin CLI shim
that resolves argv, drives the poll loop with clean SIGINT/SIGTERM handling, and prints.

**`_resolve_wait_target`** — any-kind resolver order: session, invocation, play
(`_resolve_any_target`, which also falls back to a branch_id), then schedule_run.
Terminal-state definitions live in `TERMINAL_STATUSES_BY_ENTITY_TYPE`
(`lionagi/state/db.py`, ADR-0035).

**`_artifact_dir_for`** — `li wait artifact_dir` resolution contract. The run directory backing
a row is always `RUNS_ROOT / <session id>`, resolved via the backing/primary session id (may
not exist on disk yet). Returns `None` only when there is no backing session id to anchor on.

**`wait_for_terminal`** — callback contract (`on_result`/`should_stop`). Blocks until every id
in `ids` reaches a terminal state; returns one outcome dict per id, in the order given
(ADR-0035 completion contract). Importable and awaitable directly — no CLI concerns (argv,
signals, printing). `on_result` is called once per resolved run, the moment it goes terminal
(or is found unresolvable), so a caller can print incrementally instead of waiting for the
whole set to drain. `should_stop` is polled between ticks so a CLI wrapper can wire
SIGINT/SIGTERM into a clean early return.

**`run_wait`** — CLI SIGINT/SIGTERM handling. `run_async` (`lionagi.ln.concurrency`) installs
its own SIGINT/SIGTERM handlers for the duration of this call, mirroring how `monitor.py`'s
`_dispatch_wait` drives its own poll loop, so Ctrl-C/SIGTERM interrupt the wait cleanly instead
of leaving a stray process.

## `status.py` — `li agent status` / `li play status` / `li o ctl status`

Module docstring: pure reads over sessions/invocations/plays/session_signals. Resolves an id
(or the latest matching run) regardless of terminal state, unlike `li monitor` which only
lists running/active rows.

`--json` emits a flat object with this **stable key set** (parsed by other lambdas/tools —
changing it is a breaking change):

```text
id, entity_type, command, status, terminal, exit_class, exit_code,
current_phase, progress_completed, progress_total, model, provider,
project, last_activity_at, session_id, branch_id, invocation_id, label,
degraded, degraded_reason,
status_reason_code, status_reason_summary, status_evidence_refs,
pending_controls
```

Exit codes: `0` terminal-success, `1` terminal-failure, `3` still running/active, `2` lookup
failed (unknown id, or state.db unreachable; also argparse's own usage-error code).

`pending_controls`: unapplied `session_controls` rows (id, verb, created_at) for the resolved
session, oldest first — queued via `li o ctl pause|resume|msg`, then consumed by the control
poller while a flow runs. `[]` when no backing session or nothing queued.

**`_resolve_session_by_branch_id`** — branches/sessions schema note. There is no `branch_id`
column on `sessions` — `branches` is a separate table, and `branches.session_id` is a NOT NULL
FK, so a matched branch row always names exactly one owning session. `entity_id` is treated as
a branch_id fallback (the resume token printed in `li agent -r <branch_id> "..."` hints and
echoed as `branch_id` in the status view).

**Status vocabularies** (mirror the CHECK constraints / `VALID_*` sets in `state/db.py`) —
sessions/invocations share one vocabulary (`VALID_SESSION_STATUSES`); plays have their own
(`_PLAY_STATUSES`). `"cancelled"` is a real terminal non-success status and lands in FAILURE by
elimination (neither success nor still-running). `"completed_empty"` (the completion-trust
gate — see `_util.py` above) reads as a failure here too: a verified-empty run is not something
an operator or a schedule chain should treat as a trustworthy success.

**`_DB_BUSY_TIMEOUT_S`** — why a status read has a bounded timeout. `StateDB.open()` always
runs a schema-apply step under a write transaction (`BEGIN IMMEDIATE`), even for a pure read —
so a status check can, in the pathological case of another writer holding a long transaction,
wait far longer than reasonable for a diagnostic command. `busy_timeout` is already 5s at the
sqlite layer; this wraps the whole call so a stuck read fails fast with a clear message instead
of hanging indefinitely.

**`_detect_degraded`** — degraded-completion heuristic rationale. Flags a terminal-success
record whose backing session shows no sign the normal teardown path (`persist_session_end` /
`SESSION_END` hook) ever ran — a proxy for an orphan/limit-exit that forced a "completed"
status. Deliberately narrower than a literal "`current_phase` non-terminal OR `duration_ms` is
NULL" check: `current_phase` is only ever written as "executing"/"synthesizing"
(`lionagi/cli/orchestrate/flow.py`) and is never reset to a terminal marker on any path, healthy
or not, so alone it would flag every successful flow/play run. `duration_ms` is never populated
by the CLI teardown path at all (`_collect_branch_usage`'s return has no `duration_ms` key) —
checking it alone would flag every completed session, healthy or not. `num_turns` IS populated
by that path (`persist_session_end` receives it from `_collect_branch_usage`, default 0, not
None), so its absence is a real signal — scoped to `source_kind='live'` because mirrored Claude
Code transcripts (`source_kind='imported_fs'`) use a dormancy-based "completed" with no usage
metrics by design (`lionagi/state/claude_mirror.reconcile_session_status`) and must not be
flagged as degraded.

**`_audit_degraded`** — known coverage gap: `setup_orchestration_persist()`
(`cli/orchestrate/_orchestration.py`) never populates a singular `ctx["branch"]` for multi-leg
DAG sessions (it tracks per-leg branches via `ctx["hooks"]` instead — see `_runs.py` below), so
`teardown_persist()`'s `if _branch is not None` guard (`cli/_runs.py`) always skips
`_collect_branch_usage()` for `invocation_kind IN ('play', 'flow')` — `num_turns`/`duration_ms`/
tokens/cost are therefore never recorded for ANY such session, healthy or not. Until that's
fixed, a 100% (or near-100%) `sessions_degraded` rate reflects this detector-coverage gap, not
a real degradation rate — see the printed note in `_dispatch_audit`.

## `agent.py` — `li agent` run dispatch

**`_run_agent` role-key validation** — a declared profile `role:` key is validated up front,
before the resume/new-branch split: a malformed profile must fail loudly on every invocation
shape, not only when a new branch is composed. The value itself is only USED when composing a
new branch (a resumed branch keeps its persisted system message). A declared key must be a
non-empty string: `role: ""`, `role: false`, or `role: 0` parse to falsy Python values, and a
truthiness fallback would silently grant the implementer role (and its coding authority)
instead of surfacing the malformed config. The "implementer" default applies ONLY when the key
is genuinely absent (bare `--preset coding` with no declared role).

**`_run_agent` opt-in `role:` key -> `create_agent`** — an explicit `role:` in the profile's
frontmatter (parsed into `AgentProfile.extra` by `_parse_profile`) switches a plain
`-a <profile>` leg onto the same `create_agent` path `--preset coding` uses, parameterized with
the profile's own role instead of the hardcoded "implementer" default — so a reviewer profile
gets the reviewer policy block, not the implementer's. `role` is read ONLY from this explicit
key — it is NEVER defaulted from the profile name, since many deployed profiles name no
matching built-in `Role` and would hit `Role.load`'s fail-closed `ValueError` the moment they
were defaulted into this path. A profile without the key keeps today's plain `Branch(...)` path
byte-for-byte.

Use `create_agent` so `CodingToolkit` tools and path-guards are fully wired
(`guard_destructive` on bash, `guard_paths` on reader/editor). The factory installs the full
system message via `set_system()` — the profile extension is composed into the spec BEFORE
calling `create_agent` so both preset role/policy AND the profile prompt land in a single
system message. `AgentSpec.coding(system_prompt=...)` maps to `spec.extra_prompt`, which
`build_system_message()` appends AFTER the role header and policy block — no duplication of the
LION system text. The post-factory `add_message` on the preset path is skipped to avoid
`set_system` replacing the composed message.

**`_run_agent` raw_body vs system_prompt** — uses `profile.raw_body` (not
`profile.system_prompt`) to avoid duplicating `LION_SYSTEM_MESSAGE`: `_parse_profile` prepends
it into `system_prompt` when `lion_system=True`, and `factory.py:117-125` also prepends it
because `spec.lion_system` remains `True`. `raw_body` is the profile body before that expansion;
the factory adds the header exactly once. When `lion_system=False`, `raw_body == system_prompt`
so both paths are consistent.

**`_run_agent` add_message skip guard** — the profile system prompt `add_message` only fires
for a brand-new, non-preset branch. On the preset/role path the profile extension was already
composed into the spec before `create_agent` ran (`add_message` would call `set_system` and
replace the preset system message — see `protocols/messages/manager.py:385`). A resumed or
continued branch (`-r` / `--continue-last` / the automatic timeout-resume leg) already carries
its persisted system message — which, for a role/preset branch, is the composed role+policy
block, not the bare profile body — so `add_message` must not run for it either, or it clobbers
that persisted message via `set_system`.

For a brand-new branch, `took_create_agent_path` (set in the same leg that builds the branch)
is the authoritative signal. A resumed/continued branch cannot use the *current* invocation's
profile for this decision — the profile reloaded this leg (`has_role_key`) describes only what
was passed to *this* `-a`, not how the persisted branch was originally built; resuming a
role-composed branch under a different, plain profile (or the same profile with `role:` since
removed) would then make the guard treat it as plain and clobber its composed message. Instead,
`create_agent` (`lionagi/agent/factory.py`) stamps every branch it builds with an immutable
`CREATE_AGENT_BRANCH_ORIGIN_KEY` marker in `branch.metadata`, which round-trips through
`Branch.to_dict()`/`from_dict()` with the rest of `metadata`. On a resumed/continued leg,
`_run_agent` consults that marker on the reloaded branch instead of re-deriving the guard from
the current profile.

**`_run_agent` auto-resume terminal-status guard** — known before teardown runs: an auto-resume
leg is about to fire on this same session, so this leg's teardown must not stamp a terminal
status the resumed leg would then be blocked from overwriting by the ADR-0035 terminal guard
(see `_runs.py` `_teardown_common` defer_terminal, below).

The same ownership governs the direct no-persistence notice, and for the same reason: an
interim leg has no answer to report. It is delivered from the tail rather than from the
teardown, past every line that can still move the status — the empty-resumed-stream conversion
to `failed`, and the auto-resume recursion that makes this leg interim. Delivering from the
teardown reported `timed_out` for a run that went on to complete, and `completed` for one whose
own return value said `failed`. Only the propagating-exception path still delivers from the
teardown, because there the status is settled and nothing after the `try` block runs; the
delivery is idempotent so those two reaches cannot both fire. The condition is the recursion's
own branch and not the `will_auto_resume` computed earlier in teardown: that one is read before
the effective status is applied, so suppressing on it could silence a leg that then does not
recurse.

## `_agent_depth.py` — inherited agent-depth env marker

`LIONAGI_AGENT_DEPTH` (integer string; unset/`0` = top-level, `>=1` = spawned worker) lets an
external policy hook distinguish a seat session from a worker it spawned, using process
ancestry-independent env inheritance — ancestry breaks under `nohup`/`setsid` detachment and
launchd reparenting, but env inheritance survives it. `LIONAGI_SEAT_PROFILES` is an
operator-configured, comma-separated set of `-a` profile names that reset depth to 0 instead of
incrementing it; the set is empty by default and no profile name is hardcoded in source.

`stamp_agent_depth(agent_name)` (called from `_run_agent`) and `stamp_worker_depth()` (called
from `_run_fanout` and `_run_flow`, which also covers `li play`) both set
`os.environ["LIONAGI_AGENT_DEPTH"]` before any engine spawn. `_cli_subprocess.py`'s
`ndjson_from_cli` passes `env=None` to `create_subprocess_exec` for all three CLI-backed engines
(`claude_code`, `codex`, `gemini_code`), so the child inherits this process's `os.environ`
verbatim — the stamp propagates with zero endpoint changes. `inherited_depth()` is captured once
at import as a module constant rather than read live, so `_run_agent`'s in-process auto-resume
recursion re-stamps to the same depth instead of double-incrementing.

## `kill.py`

No standalone entries beyond the general ADR-0035 CAS-guard pattern documented under
`_runs.py` / `status.py` — `li kill`'s `TransitionRejectedError` handling follows the same
race-window discipline (the session may have gone terminal between resolution and the kill
signal reaching it).

## `_runs.py` — agent session setup/teardown (ADR-0035)

**`_linked_engine_session`** — linking a profile-typed session to its engine-typed mirror twin.
`engine_session_uid` is the provider-native session id (e.g. a Claude Code session uuid or a
Codex thread id), captured from `endpoint.session_id` while streaming — the same id the mirror
derives its StateDB session id from, so this is the link between a profile-typed agent session
and its engine-typed twin. A present `engine_session_uid` means the engine session is real (it
was captured off a live stream chunk); the mirror row can simply not have been written yet at
teardown time. Retry a bounded number of times before giving up — never spin unbounded waiting
for a session that may never mirror.

**`_teardown_common` defer_terminal skip** — ADR-0035 resumed-leg ownership. A recursive
auto-resume leg is about to run on this same session (see `_run_agent`'s finally block) and
will own the real terminal write. Stamping `ended_at`/status here would leave the session
terminal while the resumed leg is still working, so the resumed leg's own teardown hits the
ADR-0035 terminal guard. Skip every DB mutation and let the caller's non-status bookkeeping
(hook unroute, usage emit, observer detach) run as usual.

**`_teardown_common` ProviderError suppression** — suppressing a phantom "failed" for an
unclassified `ProviderError` with a linked engine session. A profile-typed session's own async
wrapper can raise a stream/transport error (the CLI provider's own reported "error" chunk that
`classify_provider_error` could not attribute to a known quota/auth/context pattern, so it
stays the base `ProviderError`) while the engine session it wraps — mirrored separately from
the on-disk transcript — is still alive or already completed. Reading that as "failed" is a
phantom: the actual work is running or done. The demotion is narrowly suppressed for this
known, unclassified-stream-error class only: an exact-type check (not `isinstance`) excludes
the `ProviderQuotaError`/`ProviderAuthError`/`ProviderContextError` subclasses, so a genuine
quota/auth/context failure always stays "failed" even with a linked engine session
running/completed, exactly like a generic bug elsewhere in the wrapper (message persistence,
artifact verification, hook handling, branch mutation) must still fail loud.

**`_teardown_common` linked-engine metadata write** — the link is recorded durably regardless
of whether the mirror row exists yet — the id is deterministic from `engine_session_uid`, so
`li monitor run <profile_session_id>` can follow it and resolve the real status once the mirror
row lands, even if this teardown's bounded wait ran out first.

**`_teardown_common` completion-trust gate** — a leg that declared no artifact contract (or
declared one but produced nothing) still must not read as a trustworthy "completed" on faith
alone. Falls back to a cheap local git check — HEAD ahead of its base ref, or a dirty working
tree — before accepting the loop's own "I'm done" as ground truth. A run whose deliverable is
its response text (research/read-only agents) is legitimate work too, so a durable assistant
message counts as evidence in its own right — this gate only demotes runs with neither a
file/git trace nor a real answer. Only fires when nothing else already made the run loud.

**`_teardown_common` escalation backstop** — a leg that never declared an artifact (so the
completion-trust check above has nothing to verify) but gave up mid-run via
`EscalationRequest` still must not read as a clean completion. Only fires when nothing else
already made the run loud — an existing failure reason (including the artifact check above) is
preserved untouched.

**`_teardown_common` node-failure and gate-rejection backstops** — two more checks that run
*before* the completion-trust gate and escalation backstop above, for the same reason: a DAG
run whose nodes all failed, or whose dependent subtree was skipped after a gate rejection,
typically produces no artifacts or commits either, so the trust gate would otherwise demote it
to `completed_empty` before either backstop got a chance to apply. The node-failure backstop
catches a DAG operation whose `invoke()` raised and was recorded `EventStatus.FAILED`, which
folds into `completed_operations` alongside genuine completions and would otherwise never roll
up into the run's own status. The gate-rejection backstop covers a gate node that rejected
mid-DAG, whose dependent subtree the executor short-circuits to skipped rather than running
against the rejected baseline — a correct, deliberate stop, not a failure, so status stays
`completed` but the reason code must say so explicitly. Because it leaves status at
`completed` instead of flipping to `failed` (unlike the other two backstops), it can't rely on
the trust gate's own `final_status == "completed"` guard to skip it, and must run first so its
evidence is already in place before that gate's no-evidence check runs.

**`_teardown_common` pre_write_status snapshot** — the CAS-guard signal. Snapshot of the status
this teardown itself observed when it started (before any status-changing write in this
function) — this tells apart the two rejection causes in the CAS-miss/terminal-skip handling
below. Only the status is used as the CAS guard, not `updated_at`: this same function may
itself have already touched the row's `updated_at` (artifact verification, the linked-engine
metadata write) between that snapshot and the guarded write, and none of those are the
concurrent writer the guard exists to catch.

**`_teardown_common` terminal-status skip** — skip redundant terminal write (ADR-0035 floor).
This teardown's OWN view of the session was already terminal before it attempted anything (e.g.
a later resume/follow-up reattached to a session an earlier, unrelated run already finalized).
Writing again would be a redundant terminal overwrite the ADR-0035 floor would reject — skip it
outright rather than attempt-and-catch, and report THIS invocation's honest outcome rather than
the persisted one, or a genuine failure/timeout would silently read back as success.

**`_teardown_common` CAS-miss handling** — another writer (e.g. a concurrent teardown of the
same in-flight session) touched the row between this teardown's snapshot and this write. Read
back the now-persisted status instead of raising past callers — it reflects the outcome that
actually won the race for this same live invocation.

**`teardown_persist` SESSION_END skip under defer_terminal** — `persist_session_end`'s own
fallback branch (row not yet terminal) writes status straight through `update_session()` — a
plain column SET with no ADR-0035 guard. Emitting `SESSION_END` here would let that fallback
re-introduce the exact premature terminal stamp `defer_terminal` just skipped, through a side
door `_teardown_common` never touches. The resumed leg's own (non-deferred) teardown emits
`SESSION_END` once for the whole session, carrying the cumulative branch usage for both legs, so
nothing is lost by skipping it here.

**`teardown_persist` finally block** — branch-ownership release must run even on failure.
Releases session ownership of every branch this ephemeral persist session wired, so a later
setup (e.g. in-process resume) can wrap the same long-lived branch in a fresh session. This
must run even when the bookkeeping above failed: a stranded owner marker would make the
long-lived branch unresumable.

**`_open_shared_db` register_shared_db call** — lifecycle hooks (`SESSION_START`/`END`,
`BRANCH_CREATE`) reach a db via `get_shared_db()`; the opened db is registered so they reuse
this owned connection rather than opening a second one whose aiosqlite worker thread leaks.

**`setup_agent_persist` branch-claim ordering invariant** — the branch is claimed BEFORE
touching the shared DB registry: a branch still owned by a live persist session must be
rejected here without side effects (registering a shared DB closes the previous handle, which
would break the owning context's teardown).

**`setup_agent_persist` signal-persistence bind** — signal persistence binds through the
already-open DB so every `Signal` emitted on this session's observer lands in
`session_signals` without opening a new connection per signal (matches message-write cost).

**Data-shape note**: multi-branch orchestration sessions (play/flow) are tracked via
`ctx["hooks"]`, not a singular `ctx["branch"]` — see the `_audit_degraded` coverage-gap entry
under `status.py` above for the downstream consequence (usage metrics never recorded for those
session kinds).

**`_reopen_session_for_resume`** — a closing transition only announces itself when the status
column actually changes. A resume adopts a session an earlier leg already left terminal, so
writing that same terminal status again at the end is a no-op: the leg finishes silently and
the job record never closes. This reopens the session to `running` first, restoring the
invariant the rest of the system reads off that column (a session marked terminal is not
currently executing), so the eventual re-close is a real transition. Reopening is the only
sanctioned exit from a terminal status and carries an explicit override rather than a
session-policy rule, so each reopening stays individually attributable instead of opening
terminal-exit to every writer — finality is what the reapers, the teardown guard, and
`li wait` all rest on.

**`_reopen_session_for_resume` unresolved drain-declaration race** — when a resume does *not*
reopen (the adopted row is still `running`), the row keeps whatever the earlier leg declared
for whether its runner drains operator controls. Three states reach this point: explicit
`True`, explicit `False`, and no declaration at all (a row written before the field existed,
which refuses controls like `False` but means "nobody answered" rather than "no"). Only `True`
is risky — if that leg is genuinely alive it's the right answer, but if it died without
terminalizing, a stale `True` would admit a control for a drain that's gone, and a row reading
`running` is the only evidence available either way; the stale-session doctor resolves it after
the fact. This is left alone deliberately: writing the declaration here would be a
read-modify-write against a row a live leg may be concurrently updating — the same race that
once overwrote an exited leg's process markers with a live leg's — so the narrower failure mode
(a `False`/undeclared row keeps refusing controls it could actually drain) is accepted instead.

## `mirror.py` — Claude transcript mirroring into StateDB

**`_Lineage`** — cross-session conversation-lineage detection protocol. A continued
conversation opens a fresh transcript whose first message points — via `parentUuid` — at the
last message of the session it continues. `_Lineage` indexes each file's current leaf uuid and,
when a file's root parent resolves to a different session's leaf, records a provenance link.

**`_load_states`** — `offsets.json` persisted-state contract. Persists `tool_names` +
`leaf_uuid` alongside the byte offset so a restart resumes mid-conversation without dropping a
later tool_result's function name or losing cross-session lineage — both live only in process
memory otherwise. Legacy `{path: int}` caches (offset only) load as bare cursors, then upgrade.

**`_read_new_events`** — cursor-advance-after-durable-write contract. The cursor is NOT
advanced inside this function — the caller sets `state.offset` to the returned offset only
after the batch is durably mirrored, so a write failure re-reads the same lines next pass
instead of skipping them. Non-object JSON (a bare `[]` or a scalar) is dropped as malformed
rather than handed on as an event.

**`_peek_head`** — why it exists: idle-file session-id/cwd recovery after restart. Recovers
`(sessionId, cwd)` from a transcript's head without consuming the tail. Needed for idle files
after a restart: with no new events to read, the session id is otherwise unknown (the liveness
sweep would never flip it completed) and the cwd needed to attribute it to a project is
otherwise unavailable.

**`_attribute_idle`** — backfilling project attribution without moving the liveness clock. The
activity path attributes a project from streamed events, but a session fully mirrored before
project attribution existed (or before its cwd could be placed) has no new events to trigger
that. This derives the project from the head cwd and backfills the existing row — without
moving the liveness clock.

**`_mirror_one` status="running"** — always created/kept running; the session-level idle sweep
(after the whole pass) is what flips it to completed, so a fresh transcript anywhere in a
multi-file session keeps the whole session live.

**`_one_pass` idle-file peek** — idle/already-read files have no streamed events to derive
from: peek the head once to recover the session id and (one-time) attribute the project,
backfilling a row left as "(no project)" by an earlier pass.

**`mirror_forever`** — vs `li mirror`'s own `_run`: two separate tail loops. Tails recent Claude
transcripts into StateDB until `stop` is set; `since` bounds the scan to the recent window, so
it catches up and tails live without ever backfilling full history. This is Studio's
in-process entry point; `li mirror` keeps its own loop in `_run`.

**`mirror_forever` connection lifecycle** — the StateDB connection opens INSIDE the retry loop,
not once outside it, so a failure to open it (e.g. a locked or half-migrated `state.db` during
first-run startup, when the studio is creating the schema and checkpointing on another
connection) is retried, not fatal. Opening it once outside the loop meant a single transient
open error silently ended the in-process mirror for the whole life of the studio process.

**`_peek_codex_head` rollout classification** — classifies a Codex rollout's first line without
consuming the tail, into `"meta"` (parsed session_meta header), `"headerless"` (a complete
first line that isn't one), or `"torn"` (the line is still being written, or unreadable).
Completeness is decided by the trailing newline BEFORE any parse attempt: rollouts are
append-only JSONL, so a line without its newline may still be arriving even if the bytes so far
happen to parse; a newline-terminated line that fails to parse is permanently corrupt and
settles as headerless. `_mirror_one_codex` defers on `"torn"` rather than reading the file under
the path-stem identity, since the header's real UID could arrive next pass and split one
rollout into two sessions.

**`_mirror_one_codex` orchestrated-rollout absorption** — a rollout whose header names an
originator in `SKIPPED_ORIGINATORS` (an orchestrator's own run, e.g. a lionagi agent leg)
already has a session under the agent's name, so importing the rollout too would double it.
`_mirror_one_codex` absorbs whatever an older mirror version may have imported, under either id
the file was ever keyed by (a pre-header path-stem fallback, and the header's real UID), then
marks the file `orchestrated` and never reads it again. State only commits once both absorption
calls return, so a failed attempt leaves exactly what the next pass needs to retry.

**`_absorb_backfill`** — a process-wide, provenance-driven counterpart to the per-file
absorption above: it reads recorded session provenance rather than the rollout tree, so it also
reaches rows whose backing files fall outside the current sweep window. Returns whether the
sweep completed cleanly; `mirror_forever`/`_run` only stand down (stop calling it) on `True` —
one bad pass must not retire the backfill for the process's whole lifetime. Errors are logged,
never raised, since reconciliation must never take the mirror down.

## `state.py` — `li state` inspect/prune/migrate state.db

**`_doctor` per-row sweep** — the per-row repair sweep goes through the single guarded write
path (ADR-0035): `expected_statuses={"running"}` re-asserts the CAS the old bulk `UPDATE` did
inline, and routes the sweep through `update_status()` so it gets a `reason_code` + a
`status_transitions` audit row instead of a raw column write.

**`_doctor` session age vs process age** — a stale-`running` session isn't necessarily a dead
process: a branch picked up again keeps its session's original `started_at` while the process
serving it is new. The sweep checks the process itself (via the same PID-identity check the
stale-kill sweep uses, not a second weaker rule) before calling a row stuck. A PID that's alive
isn't proof either — the OS can hand a dead session's PID to an unrelated live process — so an
"unverifiable" identity check result is skipped (leaves the row for a later pass) while a
"zombie" result (process exited, not yet reaped) is swept.

**`_collect_message_breakdown`** — messages by role and by age, the two axes a retention
decision needs. A bare row count can't say whether pruning reclaims anything; the age histogram
makes that visible before a prune runs. It reports counts only, never a content-size sum:
summing `LENGTH(content)` over the messages table is the one query here with no useful index
(measured ~57s against 1.68M rows, vs under 2s for everything else) — the reclaim command that
actually touches those rows reports their size directly instead.

**`_prune_candidates` / `_null_content_candidates`** — both re-run their operation's own
selection predicate outside the operation's transaction, and print the recount beside the
result as a cross-check: after a real run it must read zero, after a `--dry-run` preview it
must match what the preview reported. Running inside the same transaction would read the
operation's own uncommitted rows and agree by construction, so it deliberately runs outside it.

**`_null_content`** — replaces old message *bodies* with a size-preserving marker; it exists
because `li state prune` can't reach this content. Prune selects and deletes **sessions**, but
message bytes live on **messages**, and a message any surviving progression still references is
kept regardless of its own age — so a store can be almost entirely message content, have every
message inside prune's keep-window, and give prune nothing to delete. `--dry-run` performs the
real `UPDATE` and rolls it back rather than estimating, which makes a preview a **write** that
holds the same lock for the same duration as the real operation, not a cheap read.

**`_prune` / `_null_content` preview mechanics** — both route their dry-run through a
`_PreviewOnlyError` raised after the real statements ran inside the transaction: the exception
carries the real counts out and forces the transaction to unwind instead of commit, so a preview
number can never drift from what the real operation would have measured.

## `monitor.py` — `li monitor` (dashboard + `li monitor run` wait primitive)

### Table rendering

**`_query_active_shows`** / **`_query_running_plays`** — project scoping is Python-side, not
SQL. A show's `repo` is a filesystem path, not a project slug, so it can't be matched against
`--project` in SQL — that scoping happens in Python (`_gather_table_rows`) via
`detect_project()`. Plays have no project column; project-scoping joins through the linked
session (`session_id`) — a play with no linked session is excluded (same orphan semantics as
`_show_project_matches` below).

**`_NON_TTY_MAX_COL_WIDTH`** — ceiling on a non-TTY column's *layout* width
(header/separator/padding), not the value. A single pathological long value (e.g. a malformed
project string) must never blow up every row's padding to that value's length — the
requirement is "grep never false-negatives on identifying columns", not "pad every row to the
longest value seen". Values longer than this still render in full (Python format specs never
clip a value shorter than its field width), just without alignment padding past the ceiling.

**`_show_project_matches`** — derives the show's project slug via `detect_project()` and
compares; a missing/unresolvable repo path excludes the show under `--project` (same orphan
semantics as a play with no linked session). The trailing `"(remote, ...)"` annotation some
`_show.md` authors append after the path is stripped via a regex anchored to end-of-string, so
a legitimate path segment containing `" (something)"` mid-path is left untouched.

**`_gather_table_rows`** — play-vs-session row dedup discipline. A play row is the canonical
rendering of its backing session, so that session is dropped only when the play row itself is
being shown — dedup happens against what this view actually fetched, never in SQL, so a session
view or a play row outside the window still renders the session once.

**`_detail_session`** — completion-trust evidence block. Surfaces why a terminal status landed
where it did, so trusting `completed` (or catching `completed_empty`) doesn't require a manual
git read.

**`_parse_json_field`** — Postgres vs SQLite JSON column decode. A column may come back as a
Python dict (Postgres JSON auto-decode) or a raw string (SQLite text storage via an ad-hoc
`text()` query with no column typing).

**`_as_number`** — telemetry counts are untrusted persisted data. Anything non-numeric coerces
to 0 — persisted telemetry is untrusted (hand-edited state.db rows, a future writer with a
different shape), and a malformed count must never crash the monitor.

**`_format_khive_injection_line`** — renders the non-zero aggregate context-injection counters
stored under `sessions.node_metadata["khive_injection"]`. Orchestration finalize sums the counters
across its branches; bare `li agent -a <profile>` teardown records the same shape from its branch.
A top-level resume seeds its totals from the durable session; an in-process timeout resume carries
the first leg's counters into the final teardown. Only counts are persisted — never the injected
source text — and absent, zero, or malformed counter sets are omitted from the session drill-in.

**`_format_coordination_line`** — coordination telemetry shape contract. Renders an
invocation's coordination telemetry (`node_metadata["coordination"]`, written by the scheduler
engine's finalize path — see `lionagi.studio.services.scheduler_state.flush_run_telemetry`).
Returns `None` when everything is zero: both the monitor drill-in and the `li monitor run`
wait-line only print this when non-zero. Every nested field is type-checked before use:
`telemetry` is read back from persisted state and may not match the shape this module writes
(e.g. `signals` or `files_overlap` landing as a list rather than a dict) — malformed nested
values are treated as zero/absent rather than raising `AttributeError`.

### `li monitor run` — wait-for-terminal primitive (chain-following)

`_dispatch_wait` is a scripting primitive, not a view: append-only stdout lines (no screen
clearing, no table), meant for a harness to poll `li monitor run <id>` as a background task
rather than hand-rolling raw sqlite polling against the live WAL-mode `state.db`. Separate code
path from `_watch_loop` (the human dashboard) — this one blocks until specific `schedule_runs`
go terminal, then exits with a meaningful code. Ticks via per-call `run_async` (not one
long-lived `asyncio.sleep` loop) so both SIGINT and SIGTERM get a clean exit.

**Chain-following** (default on; `--no-chain` to opt out): the scheduler can fire a child run
from a terminal run's schedule `on_success`/`on_fail` (engine records `chain_parent_id`/
`chain_depth` on the child — see `lionagi/studio/scheduler/engine.py`'s `_fire`). A watched run
going terminal is not necessarily the end of its chain, so once it lands the watch frontier is
extended with any already-fired children and watching continues. A schedule that declares a
chain action for the outcome but whose child hasn't fired yet gets a bounded grace window
(`_CHAIN_GRACE_TICKS` poll ticks) before the chain is concluded on the parent's own exit code —
a schedule with no matching chain action needs no grace wait at all. The aggregate exit code is
**final-link-wins**: each chain's *last* link decides, not every link along the way (an
`on_fail` recovery child that succeeds means the chain as a whole succeeded).

**`_MAX_CHAIN_DEPTH`** — mirrors the scheduler engine's own chain-depth cap
(`lionagi/studio/scheduler/engine.py` `_MAX_CHAIN_DEPTH`) — the engine never fires a chain
child at or past this depth, so a grace window here would just burn ticks waiting on a child
that is never coming.

**`_resolve_schedule_run`** — id-format note: exact match then prefix match. `schedule_run` ids
are 12-char hex, not 36-char UUIDs, so `_util.py`'s length-36 prefix heuristic doesn't apply.

**`_effective_session_status`** — session-status reconciliation contract (ADR-0035). Reconciles
a profile session's status against its linked engine session
(`node_metadata.linked_engine_session_id`), which can otherwise stay pinned at "running"
forever after teardown. When the linked row is terminal, that status is persisted onto the
profile row via CAS-guarded `update_status()` so the DB reflects it, not just the return value.
An already-terminal profile row is authoritative and never rewritten (ADR-0035 terminal guard).
On a terminal-write race (`TransitionRejectedError`) the profile row went terminal between the
read and this write — the persisted row is authoritative and is reported instead of the
synthesized status. On a CAS mismatch (write didn't land but didn't hit the terminal guard
either) the persisted row is likewise authoritative over the synthesized value.

**`_resolve_watched_runs`** — two-tier run-id resolution. Every requested id is resolved once,
up front (not retried per poll tick, unlike the `--follow` discovery scan). Ids that aren't
`schedule_runs` are also tried against `sessions`, so `li monitor run <session_id>` can drill
into an agent run, not just scheduler runs.

**`_poll_pending_once`** — cancellation-atomicity contract. Checks every still-pending run
once; prints, records into `done`, and drops from `pending` any that are now terminal. Prints
immediately per row (not batched) so a harness tailing stdout sees each result as it lands.
`done` is mutated in place: the print/append/delete trio has no `await` between them, so it's
atomic wrt task cancellation — a row can't leave `pending` without also landing in `done`, even
if a SIGINT discards this coroutine's return value. A row that existed at resolution time but
is gone now (e.g. its parent schedule was deleted, cascading the run) resolves as a failure so
the wait can't hang on state that never comes back.

**`_advance_chains` / `chain_state`** — folds the newly-terminal tail of `done` into chain
bookkeeping: extends `pending` with any already-fired children, resolves roots with no matching
chain action immediately, and starts/advances a grace countdown for roots awaiting one. Mutates
`pending` in place, tick-by-tick, like `_poll_pending_once` mutates `done`. `chain_state` keys:
`root_of` (run_id -> watched roots it accounts for), `chain_tail_exit` (root id -> most recent
terminal exit_code), `awaiting_grace` (run_id -> `{roots, ticks_left}`), `resolved_roots`
(roots whose chain concluded), `schedule_cache` (memoized schedule lookups), `chain` (bool,
chain-following on/off), `done_ids` (already-folded run ids — lets discovery tell an
already-processed child apart from one still in `pending`), `exit_of` (run id -> its folded
exit_code — unlike `chain_tail_exit`, keyed by root, this answers "what did run X itself exit
with" for any folded run, watched root or not), `handoff` (parent run id -> the chain child its
grace window handed off to — lets a later-discovering ancestor follow an already-processed link
forward to wherever the chain currently lives instead of stopping at that link's own exit).

Only terminal runs that actually reached the engine's fire path can ever get a chain child: the
engine only fires one when `chain_depth` is still under its cap AND the chain block (which sits
after the subprocess returns a real exit code) actually ran — a cancelled run
(`CancelledError` -> `status="cancelled"`), a skipped run (created terminal by
`create_skipped_run`, never fired), and a run that failed before spawning (argv build error or
internal exception; `status="failed"` with `exit_code=None`) all bypass it entirely. Opening a
grace window for one of these would just burn the full window before falling back.

Child-discovery/handoff algorithm: a discovered child can itself already own a root (e.g. it's
a directly-watched id in an overlapping watch set) — the parent's root(s) are unioned into
whatever the child already owns instead of overwriting, so the child's own root still resolves
once the child (now the chain's tail) goes terminal. If the child already went terminal
(possibly in the same tick as its parent) and was already printed once by `_poll_pending_once`,
re-adding it to `pending` would print it again next tick — instead, the handoff trail is
followed to the chain's current carrier first (a child's own grace window may itself have
already handed off to a deeper link; stopping at this child would resolve the parent's root(s)
with an intermediate exit code instead of the final link's). If the trail ends at a still-live
descendant in `pending`, the parent's root(s) ride on it. If it ends at a terminal carrier: join
its already-open grace window if it has a matching chain action of its own, else the parent's
root(s) resolve right along with it.

**`_query_schedule_runs_since`** — `--follow` discovery query uses strict `>` (not `>=`) on
`created_at` vs `baseline` — the same baseline-first, anti-backlog-replay discipline used by
any other "watch for new stuff" loop in this codebase: a row already seen at exactly `baseline`
must never be re-reported on the next tick.

**`_dispatch_wait` interrupted-before-resolution** — if interrupted before resolving even
completes, there is no aggregate to report (which ids exist / their state is unknown) — this
reports as "still in progress" (`EXIT_RUNNING`), not success or failure.

**`_dispatch_wait` max_wait bounding** — a stuck session (an unresolvable linked-engine mirror,
a wedged subprocess) must not hang the wait loop forever — `max_wait` bounds total wall-clock;
on expiry, whatever is still pending is reported the same way an interrupt would
(`EXIT_RUNNING`), not a silent hang. `None` falls back to the bounded
`_DEFAULT_MAX_WAIT_SECONDS` default; `0`/negative is the explicit opt-in to unbounded waiting.

**`_dispatch_wait` `--follow` exit-code semantics** — `--follow` has no natural end, so its
exit code is whatever the *initial* bounded set already resolved to; new runs discovered during
the tail print their own lines but don't feed the final aggregate.

**`_run_tick` KeyboardInterrupt handling** — `run_async` raises a bare `KeyboardInterrupt` when
SIGINT lands during its own call (it installs a temporary handler for that duration — see
`run_async`'s docstring in `ln/concurrency/utils.py`). Treated exactly like `interrupted` being
set between ticks: no traceback, no half-updated state, just the same clean stop.
