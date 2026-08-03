# ADR-0110: Deterministic manifest fan-out — legs from briefs, no planner

- **Status**: Proposed
- **Kind**: Aspirational (records the target state)
- **Area**: orchestration
- **Date**: 2026-08-03
- **Relations**: extends ADR-0106 (machine result contract; D6 here names one
  additive change to it); composes with the MCP job surface (`job.output` is
  the artifact and round-summary read)

## Context

Every orchestrating surface this package ships puts a planner model between the
caller's task statement and the legs that execute it. `li o fanout` runs a
decomposition phase first (`lionagi/cli/orchestrate/fanout.py`, phase 1:
"orchestrator decomposing task into ≤N assignments") and the assignments the
workers receive are the planner's text, not the caller's. `li o flow` has the
orchestrator compose the DAG. Playbooks template the planner's prompt; they do
not remove the planner.

That is the right shape for a prose task and the wrong shape for a class of
work that is common and currently unserved. The problems, concretely:

- **P1 — planner interposition corrupts fixed inputs.** When the caller
  already holds N pre-written briefs — review instructions, audit scopes,
  per-module checklists — each brief IS the contract for its leg. A planner
  that can rephrase, merge, or re-scope them is not adding intelligence; it is
  corrupting the input. Multi-round document review is the sharpest instance.
  A prompt telling the planner "do not rewrite" is not a fix: prompt
  prohibitions are requests, not controls.
- **P2 — the deterministic fallback costs N of everything.** Callers in this
  position submit N independent agent runs: N handles to track, N terminal
  notices when one answer to "is the round done" was wanted, and artifact
  harvest by hand.
- **P3 — sandboxed legs contort output paths.** CLI legs under a
  workspace-write sandbox cannot write outside their own cwd tree, so briefs
  must smuggle output paths that land inside it. Meanwhile every run already
  has an artifacts directory listed by `job.output`
  (`lionagi/mcp/jobs.py`, `output()` returns `artifacts` + `artifacts_state`)
  — but a leg is never told where it is and could not write there if it were.
  `job.status` deliberately carries no artifact fields; the artifact read is
  and stays `job.output`.
- **P4 — per-leg working directories are the norm.** One round may span two
  repositories, and PR-review rounds are per-worktree by construction. A
  single run-level cwd excludes the most common round shapes.

| Concern | Decision |
|---------|----------|
| Input format and validation | D1: closed manifest schema v1, file-path-only, snapshotted at submit |
| Execution and aggregation | D2: independent parallel legs; per-leg timeout from spawn; no parent deadline in v1; total outcome rules |
| Durable per-leg record and ordering | D3: per-leg records + round summary durable before cooperative terminalization; two-stage kill; the hard-kill window is observable, never silent |
| Leg artifact channel | D4: scratch dir inside leg cwd, env-announced, harvested by descriptor-anchored bounded copy |
| Planner absence | D5: no-planner is a tested invariant with the profile-default drift vector as a named failure case |
| Observation contract | D6: closed job outcome preserved; round facts served by a versioned additive `round` field on `job.output`; the notice is the signal, not the carrier |

**Out of scope**: dependencies between legs (the flow surface's job);
scheduling recurring rounds (`li schedule` composes on top); any change to the
planner surfaces themselves; artifact content conventions (a verdict file's
format is the caller's contract with its own legs).

## Decision

### D1 — Manifest contract v1

A round is declared by a manifest file (YAML or JSON), passed by absolute
path. The manifest is read and snapshotted at submit, same rule as
`prompt_file` on the agent surface: editing the file afterwards cannot change
what an already-submitted round executes. Every brief file is likewise read
and snapshotted at submit, and each snapshot's content hash is recorded in the
run directory as durable evidence of what was dispatched.

```yaml
manifest_version: 1            # required, exactly 1
defaults:                      # optional; every key below optional
  model: <model spec>          # XOR agent, at each level
  agent: <profile name>
  timeout: 1200                # per-leg default, seconds, positive, <= 86400
legs:                          # required, 1..64 entries
  - brief: /abs/path/briefs/module-a.md    # required
    cwd: /abs/path/worktrees/module-a      # required
    label: review-module-a                 # required
    model: <model spec>        # optional per-leg override
    timeout: 900               # optional per-leg override
    env:                       # optional; closed map of named variables
      CARGO_TARGET_DIR: /abs/path/targets/module-a
```

Exact semantics, refuse-early at submit (nothing spawns, no job record is
created — the pattern `lionagi/mcp/dispatch.py` already applies to
would-be-refused submissions):

- **Unknown keys anywhere are refused by name.** The schema is closed; v1
  accepts exactly the fields above. A misspelled knob must fail the submit,
  not silently configure nothing.
- **`brief`**: absolute path to an existing regular file, resolved through
  symlinks at read time and then treated as bytes; empty (after strip) is
  refused. Snapshot + BLAKE-family content hash recorded per leg.
- **`cwd`**: absolute path to an existing directory.
- **`label`**: required, matching `[a-z0-9][a-z0-9._-]{0,63}` after
  lowercasing, unique across the manifest after normalization. The label is
  an artifact-directory component (D4), so path separators, `..`, and
  empty/dot-only names are unrepresentable by the pattern rather than
  filtered by a check.
- **`model` XOR `agent`** at each level. A leg naming either uses its own and
  ignores both defaults (no cross-level merging of the pair — merging `model`
  from one level with `agent` from another would construct a configuration
  nobody wrote).
- **`timeout`**: positive integer seconds, at most 86400, and it is a PER-LEG
  value at both levels: `defaults.timeout` is nothing more than the default
  each leg inherits. The ceiling is a sanity bound, not a derivation: a leg
  that needs more than a day is not a round leg.
- **`env`** (per-leg, optional): a closed map of named environment variables
  set for that leg — keys matching `[A-Z][A-Z0-9_]{0,63}`, string values,
  passed via the process environment array at spawn (never through a shell).
  Deny-by-default at the manifest surface: no manifest mechanism forwards
  any environment — not the submitting client's, not the serving process's;
  the map is literal values only, and the manifest snapshot is their durable
  source. The baseline a leg otherwise inherits is the runner process's own
  environment — today the serving daemon's environment as constructed at
  spawn (`lionagi/mcp/jobs.py`, the submit path's `env` dict handed to
  `Popen`) — and this ADR neither defines, freezes, nor filters that
  baseline: scoping it is engine-wide hardening for every job kind at once,
  a separate decision this ADR names and does not carry. A declared key
  that also exists in the baseline is overridden by the manifest value —
  that is the feature (the declared value is the recorded one); the D4
  refusal rule protects only the runner's own reserved name, and
  `LIONAGI_LEG_ARTIFACTS` is accordingly refused here at submit. Declared
  keys are listed in the leg's durable record as `env_keys`; the record
  never re-prints values. Manifest values are recorded verbatim in the
  snapshot, which is exactly why a credential in one is a rule violation
  rather than a supported path — the mechanism cannot stop an author from
  writing one, and what it CAN guarantee is that nothing hides: whatever a
  manifest carries, its snapshot shows. A leg that legitimately needs a
  secret gets it from the serving environment, the channel the existing
  agent surface already provides; this ADR adds no new one.
  The reproducibility claim is scoped accordingly: declared keys are the
  recorded, reproducible deltas over that baseline, and a round is
  reproducible given the same serving environment, no stronger. The
  consuming workflow demonstrated the concrete cases (actor identity
  resolving wrong on workspace cwds, per-worktree build target directories).
- **Leg count 1..64.** The floor is definitional. The ceiling is one order of
  magnitude above the largest observed round (13) — a bound that exists so a
  generated manifest with a bug cannot fan out unbounded, chosen loose enough
  that no legitimate round has to think about it.
- **File-path-only in v1.** An inline manifest object in the MCP call is
  DEFERRED (see Alternatives): the file path gives snapshot semantics,
  a natural durable-evidence story, and parity with `prompt_file`, and the
  consuming workflow already produces brief files on disk.

### D2 — Round execution: independence, clocks, total aggregation

Legs execute in parallel under the existing worker concurrency machinery;
concurrency caps compose the same way they do for the planner fanout.

- **Legs are independent by construction.** A leg failing, timing out, or
  being killed never cancels a sibling. For the motivating workload every
  completed verdict has value regardless of a sibling's fate. Fail-fast is a
  rejected alternative, not an option flag, in v1.
- **Per-leg timeout clock starts at leg process spawn**, not at submit and not
  at queue admission — queue wait under a concurrency cap is not the leg's
  time. What a timeout interrupts is the leg's own execution.
- **There is no parent deadline in v1.** `defaults.timeout` is only the
  per-leg default (D1). A round ends when its last leg ends. External
  cancellation (`job.kill`) is an EVENT, not a clock, and is specified in D3;
  a leg stopped by it records `cancelled`. A `round_timeout` field is
  deliberately absent until a consumer demonstrates the need — one knob, one
  meaning.
- **Leg terminal vocabulary**: `succeeded`, `failed`, `timed_out`,
  `cancelled`, `killed` — plus the orthogonal harvest state (D3). Every leg
  ends in exactly one.
- **Parent aggregation is total by construction** — three rules cover every
  combination of leg terminal states and harvest states, so no mixed round is
  undecided:

| Rule (evaluated in order) | Round `result` |
|---|---|
| every leg `succeeded` AND no leg `harvest_failed` | `completed` |
| at least one leg `succeeded` (anything else true of the others) | `partial` |
| no leg `succeeded` | `failed` |

  `dir-empty` and `dir-absent` never degrade the result by themselves: a leg
  whose whole answer is its final message legitimately writes no artifact.
  `harvest_failed` always degrades below `completed` — artifacts were (or may
  have been) written and cannot be served, a loss the result must not paper
  over.

- **A timed-out leg** is recorded `timed_out`, receives cooperative
  termination escalating to hard kill, and its harvest runs only after its
  process's death is confirmed. The quiescence invariant (D3) is
  path-independent in what it protects, and its predicate names the one
  process that must survive to publish: `round_state: complete` is never
  published before a quiescence sweep has run against every recorded
  control group — the runner's own and each leg's, all captured at spawn.
  On reap paths the reaper belongs to no recorded group, so the predicate
  is absolute: every recorded group observed empty. On the cooperative
  path the publishing finalizer is a member of the runner's own group and
  must outlive the sweep to write the summary, so the predicate there is
  every leg group observed empty and the runner's group holding no member
  but the identified finalizer itself — demanding the finalizer's own
  absence would make cooperative publication impossible, not safer. D3
  states the domain exactly and names the residuals the sweep cannot
  close.

### D3 — Durable records, ordering, and the two-stage end

Each leg gets one durable record in the run directory,
`{run_dir}/legs/{label}.json`:

```json
{
  "label": "review-module-a",
  "status": "succeeded",
  "started_at": "...", "finished_at": "...",
  "cwd": "/abs/path/worktrees/module-a",
  "model": "<resolved spec>",
  "env_keys": ["CARGO_TARGET_DIR"],
  "brief_hash": "<content hash recorded at submit>",
  "pgid": 41230,
  "harvest_state": "harvested-3",
  "harvest_detail": {"files": 3, "bytes": 18211, "skipped": []},
  "artifacts": ["module-a/verdict.md", "module-a/notes.md", "module-a/log.txt"]
}
```

The dispatch facts — label, cwd, model, `env_keys`, `brief_hash`,
`started_at`, and the leg's own process group (`pgid`) —
are durably recorded in the leg record's first write, at spawn; status and
harvest fields complete the record at finalization. The `pgid` capture is
the manifest runner's own duty, named here as required work: it reads the
group immediately after the spawn returns, the same spawn-time capture the
job surface performs for its own child. The provider subprocess layer
starts each leg's new session but today neither captures nor persists the
resulting group, so the runner performs the read itself (or a provider API
is added that returns it) — this ADR does not describe that capture as
existing. The spawn-time write is
what makes a reaper's quiescence sweep possible at all: the control domain
is read from the run directory, never from a live runner's memory, so a
reaper that shared nothing with the dead runner sweeps the same groups the
runner would have.

The round gets one summary record, `{run_dir}/round.json`:

```json
{
  "round_version": 1,
  "round_state": "complete",
  "result": "partial",
  "legs_total": 4, "legs_succeeded": 3,
  "legs": ["review-module-a", "review-module-b", "..."]
}
```

`round.json` is first written at spawn with `round_state: "pending_harvest"`
and flipped to `complete` as the last act of finalization — the summary
exists before any leg runs, so a terminal status published by ANY writer at
ANY point (including a legacy or non-manifest-aware one) is observably
pending rather than silently incomplete. `round_state` is the honesty field:
`complete` means every leg record and harvest is durably written; a reader
who finds a terminal job with `round_state: pending_harvest` is told, in the
record itself, that leg facts are still landing — the window exists and is
OBSERVABLE, never silent.

- **Finalization has exactly one owner at a time, and the claim cannot go
  stale.** The claim is an OS advisory lock (`flock`-style, exclusive,
  acquired non-blocking) held on `{run_dir}/finalize.lock` for the duration
  of finalization; the cooperative runner acquires it when teardown starts.
  The descriptor is opened close-on-exec — a lock a spawned leg could
  inherit would keep a dead runner's claim alive from inside a living leg.
  The kernel couples the lock's lifetime to its holder's: a dead owner's
  claim vanishes with its process, so there is no stale-lock repair path
  for two reapers to race on — takeover IS acquisition, the same primitive
  every claimant uses. Acquisition carries an obligation: every decision
  to claim is made on a PRE-acquisition observation, and the gap between
  observing and acquiring is exactly where a live holder finishes,
  publishes, and exits — releasing the very lock whose availability the
  claimant then reads as confirmation of its premise. So a claim holder's
  FIRST act, on every path, is to re-read the run's terminal status and
  `round.json`'s `round_state` under the claim, and to proceed on what
  that re-read shows — never on the observation that motivated the claim.
  A failed non-blocking acquire means a live owner
  exists; the failed claimant re-checks later and touches no scratch
  directory or record meanwhile. The file's content (owner role `runner` /
  `kill-reaper` / `orphan-reaper`, pid, the run's job marker, claimed_at)
  is observability, written by the holder after acquiring — never the
  mechanism itself. Every leg record and `round.json` write lands by
  temp-file-plus-atomic-rename, and a writer that finds a complete leg
  record already present leaves it — first write wins, `recorded_by` names
  the winner — so even a mis-sequenced writer cannot produce two competing
  records for one leg.
- **A hung live holder has a named recovery, per holder — one signal path
  does not cover all three.** The runner holder is the runner process
  itself, and the claim descriptor is close-on-exec, so no surviving child
  holds the claim. `job.kill` delivers the stop request (the MCP surface
  sends a fixed SIGTERM and exposes no signal choice). Where that is
  ignored, the recovery is an operator kill of the recorded leader pid
  with the identity checks the job record already supports — the recorded
  pid, and the group-marker environment variable every group member
  inherits, exist for exactly this verification — escalating to SIGKILL,
  which releases the claim with the process. No existing CLI or MCP
  surface performs that escalation for this id class today; a first-class
  escalation parameter is possible future work, not claimed here. Claim
  recovery by the operator is deliberately narrower than group cleanup —
  the operator only needs to free the claim. The reaper that then acquires
  it owns the rest: its pre-harvest quiescence sweep (above) is what makes
  a leader-only kill safe, because surviving domain members
  hold no claim and cannot outlive the reaper's quiescence sweep.
  The reaper holders are server-side actors a job-group signal cannot
  reach; their work is bounded by construction — the same per-leg file and
  byte caps that bound every harvest — and one that nonetheless hangs
  holds the claim until the serving process restarts. Restart is the named
  recovery for a server-side holder, and it is sufficient because the
  claim is kernel-held and leaves no persistent state behind.
- **Cooperative ordering guarantee**: on normal completion and per-leg
  timeout, the finalizer first proves every recorded control group quiet.
  For each leg's recorded group it hard-kills identity-verified survivors
  and verifies absence — a leg that ended normally leaves its group empty
  already, and the sweep confirms exactly that; a straggling descendant
  still inside it is ended at round close rather than tolerated into the
  harvest window. For the runner's own group — which it cannot group-kill,
  being a member — it scans and signals survivors other than itself
  individually, identity-checked the same way; the cooperative predicate
  is "no member but the finalizer" (D2), because the finalizer must
  survive its own sweep to publish.
  Only then every leg's harvest runs and its record persists, then
  `round.json` is written with `round_state: complete`, and only then does
  the parent terminalize and its single notice fire. A notification consumer
  and a polling consumer read the same facts; there is no cooperative window
  where the notice says "done" and a record is missing.
- **`job.kill` is two-stage for manifest runs.** The current kill path writes
  `status: "killed"` and `finished_at` immediately when no end is recorded
  (`lionagi/mcp/jobs.py`, `_mark_killed`), which would make the parent
  observable as terminal while cleanup has not run. For a manifest run the
  kill surface instead records `kill_requested_at` and signals the group
  WITHOUT writing the terminal fields; the runner's cooperative teardown then
  harvests, records, and terminalizes exactly as above. If the runner does
  not terminalize within a bounded grace (default 30 s), the kill surface
  checks the finalization claim: while a live owner holds it, the kill
  surface waits and re-checks — the stop signal has been delivered, and the
  terminal write belongs to the claim holder; grace expiry is when the
  reaper first CHECKS the claim, not an unconditional handoff. Only on
  acquiring the lock — which a dead owner cannot still hold, the kernel
  released it with the process — does it proceed, and per the claim
  obligation above its first act is the re-read of terminal status and
  `round_state`. A round found `complete` means a finalizer proved every
  recorded group quiet before publishing; the reaper releases the claim
  and the kill path is never entered — the grace-expiry observation is
  stale, and firing the one destructive primitive in this sequence on it
  would act on a premise the claim's own availability had already
  falsified. Only when the re-read shows the round unfinalized does the
  destructive work begin, and it begins with
  quiescence, not harvest: a hard kill of every recorded control
  group — the runner's and each leg's, read from the run directory (the
  raw `os.killpg` primitive, identity-verified against each recorded pgid
  and the group marker every descendant inherits — environment survives a
  new session, so the marker identifies leg-group members too — not the existing
  plain-kill helper, whose killed-marking record write belongs to ordinary
  kills; the reaper's single terminal write comes later, after harvest),
  then verification that no member of any recorded group survives — a
  reaper belongs to no recorded group, so the reap-path predicate is
  absolute emptiness, differing from the cooperative predicate (D2) by
  exactly the publishing finalizer. `round_state: complete` is never
  published before that sweep has observed every recorded group empty. The recorded groups are the
  quiescence domain, stated exactly, and it is plural by design: the
  provider subprocess layer starts every leg in its own session, so one
  shared group never existed to sweep — an ordinary leg sits inside the
  domain because its group was recorded at spawn, not because it stayed
  inside anyone else's. That correction earns a standing rule for any
  future revision of this domain: a membership sweep names and cites the
  mechanism that populates the set it sweeps, because a sweep over a
  domain nobody joins is indistinguishable from a clean sweep and fails
  toward reassuring. Here the populating mechanism is the runner's
  spawn-time `pgid` capture written into each leg record's first write,
  plus the job surface's recording of the runner's own group. Its
  companion rule, earned by the successor defect: a membership or
  quiescence predicate states where the observer sits relative to the set
  it observes — the observer is inside the measured population unless
  something puts it outside, and a predicate that forgets its own
  observer fails toward unsatisfiable, while one that forgets who joins
  fails toward reassuring. That is why the predicates above are stated
  per path rather than once: the reap-path observer is outside every
  recorded group and demands absolute emptiness; the cooperative observer
  is a member of the runner's group and exempts exactly itself. What the sweep cannot close, named rather than
  papered over: a descendant that deliberately leaves its own leg's
  recorded session while keeping the scratch path can still write after
  `complete`; a member that forks during the sweep can leave a child the
  verification pass never observed; and identification and signal are two
  syscalls, so the sweep inherits the job surface's stated guarantee shape
  — never a signal without positive identification, not never a missed or
  misdirected one (the internals documentation states this window and why
  process groups alone cannot close it). All three residuals fall under
  the same consumer rule below. Such an escapee is the caller's own
  agent executing the caller's own brief, so it crosses no privilege
  boundary and sabotages only its author's round: D4's harvest copies
  defensively, so the published record describes the harvested copies and
  stays internally consistent — writes made after recorded-domain
  quiescence simply miss the round. What a consumer does about the
  residual, stated as a rule: consume through the round record only —
  `job.output` serves the harvested copies under the run directory and
  never reads a scratch tree, so the read surface enforces this by
  construction; a leg-artifacts directory that reappears after
  `round_state: complete` — or survives it without that leg's record
  naming a failed removal (D4 removes every harvested scratch before
  `complete` on all paths) — is an escapee's signature, sits outside the
  round's guarantees, and is disposable — deleting it changes nothing
  recorded; and a leg that detaches workers past its own round is a defect
  in that leg's brief, fixed in the brief, not a runner defect. A mechanically non-escapable process
  domain would close the residual; that is platform-specific hardening
  this ADR names and does not adopt. Only then the manifest-aware reap:
  harvest each leg's scratch from disk as D4
  specifies, write each leg record with what could be established
  (`harvest_failed` with a reason where a scratch is unreadable — never an
  empty artifact list), write `round.json`, then make its single terminal
  write. Records written by a reaper say so (`"recorded_by":
  "kill-reaper"`).
- **An already-dead parent** (crash, OOM, machine restart) is found by the
  existing orphan-reaping path on the job surface; for manifest runs that
  reaper acquires the same finalization lock (its previous owner is dead by
  definition of the path, so the lock is free; acquisition still serializes
  it against a concurrent kill-reaper) and performs the same
  quiescence-then-harvest-then-record sequence before its terminal write —
  a dead parent does not mean dead legs, so the pre-harvest quiescence
  sweep applies identically — recording
  `"recorded_by": "orphan-reaper"`. Where the existing reaper (or any non-manifest-aware
  writer) has already published a terminal status, the manifest-aware pass
  still runs, writes the records late, and flips `round_state` from
  `pending_harvest` to `complete` — late facts beat lost facts.
- **The bound, stated plainly**: harvest-before-notice holds on cooperative
  paths. On kill and reap paths the guarantee is weaker and explicit —
  `round_state` names whether the facts are all in, and every leg record
  distinguishes what was established from what could not be. At every point
  there is at most one finalization owner and records are first-write-wins,
  so the weaker guarantee is about WHEN facts land, never about competing
  versions of them.

### D4 — The leg artifact channel

For each leg the runner creates a scratch directory inside that leg's own cwd
tree — `{cwd}/.lionagi/leg-artifacts-{run_id}-{label}/` — and exports its
absolute path to the leg process as `LIONAGI_LEG_ARTIFACTS`. A sandboxed leg
can always write there: it is inside the tree the sandbox already permits.
No sandbox configuration changes anywhere.

- **Harvest is a descriptor-anchored bounded copy.** The leg author controls
  the scratch tree's contents, so the harvester (which is NOT sandboxed)
  treats it as hostile input. The scratch root is opened once as a directory
  with no-follow semantics and all traversal proceeds from that descriptor
  (`openat`-style), never by re-resolving paths — a path re-resolution
  between check and open is exactly the race a hostile leg would use to swap
  a checked regular file for a symlink. Each candidate is opened no-follow
  and its opened identity is verified against the pre-open `lstat`
  (device+inode); a mismatch records `skipped_swapped`. Symlinks are never
  followed (`skipped_symlink`). **Hard links are refused**: a hard link is a
  regular file and would pass a naive regular-files-only rule while making
  the harvester copy an inode the leg never produced under the scratch root
  — the copy-proxy escape by another door. Files with link count other than
  one record `skipped_hardlink`. Special files are skipped and recorded.
  Relative paths are preserved under `{run_dir}/artifacts/{label}/`.
- **Caps are enforced during the copy, not before it**: 1024 files, 256 MiB
  per leg — an order of magnitude above observed verdict artifacts
  (single-digit markdown files); a pre-copy size check would race a growing
  file, so bytes are counted as written and the cap aborts the copy at the
  boundary, recording `harvest_failed` with the counts at the cap. Never a
  silent truncation.
- **Collisions are unrepresentable**, not handled: labels are unique and
  path-safe by D1's pattern, and each label owns its directory.
- **The scratch dir is removed after harvest on every finalization path** —
  cooperative, kill-reaper, and orphan-reaper alike — and the removal
  precedes `round_state: complete`. That ordering is what entitles D3's
  residual rule to read a surviving directory as an escapee's signature
  rather than permitted reap residue. A removal that fails is recorded in
  that leg's record, and a directory whose survival is recorded there is
  not escapee evidence.
- **The env var name is a decision with a check**: before implementation
  merges, the name is swept against the variables a leg already inherits
  (provider CLIs document theirs; the leg baseline environment is
  enumerable), and a test asserts the runner refuses to overwrite a variable
  that already exists in the leg's inherited environment — a collision is a
  configuration error surfaced loudly, not a silent override.
- **The read surface is `job.output`** (D6). `job.status` stays
  artifact-free.
- **This directory is a new named surface and inherits no protections.** It
  gets its own adversarial pass before the implementation merges, and the
  required arms now include: hostile file names, symlink escapes, HARD-LINK
  escapes, check-to-open swap races, cap overflow mid-copy, and kill during
  harvest — with the victim-alive-and-feature-works outcome asserted, not
  just absence of the attack's effect.

### D5 — No-planner is a tested invariant, not a documentation claim

The mode's run record carries `planner_invocations: 0` as an asserted field —
the mode has no code path that constructs a planning turn, and the record
says so per round rather than the docs saying so once.

The named drift vector is not this mode's own code: it is a
configuration-side default. Agent profiles carry model/effort/system-prompt
defaults, and a profile (or a future orchestrator default) that would
silently interpose a planning model on submissions that name it must FAIL a
test. Concretely: the test suite includes a submission whose profile is
configured the way the planner surfaces expect (an orchestrator-shaped
profile), and the mode either refuses the configuration by name or executes
the round with zero planning turns — a planned round is a test failure, not
a fallback. No diff of this feature's own code would show that drift, which
is exactly why it is pinned by a test rather than a review.

### D6 — Observation contract: closed outcomes preserved, round facts on `job.output`

The job surface's `outcome` is a closed vocabulary, and consumers
legitimately bind to it. The contract text (ADR-0106) froze at three
values (`succeeded | failed | indeterminate`) on 2026-07-25; the wire
began emitting `cancelled` hours later that same day, when the li-kill
terminal fix landed, and has shipped it ever since (`lionagi/mcp/jobs.py`,
`_OUTCOMES`; `indeterminate`, reserved at freeze, gained its producer when
ADR-0107's reaper landed) — an unversioned expansion that
ADR-0107's Notes later recorded as a pending ADR-0106 amendment item.
This ADR carries that amendment as an ERRATUM with a stated migration
policy, and `contract_version` does not move. Precision the record owes
its readers: envelope stamping itself began at 20:11 that same day, so
stamped `contract_version: 1` envelopes spoke a three-value wire for under
three hours on 2026-07-25 and a four-value wire ever since. A bump today
was considered and declined: D2's mismatch rule tells a conforming
consumer to stop trusting the surface, which is the right medicine when
decoding would otherwise go wrong — here the payload shape is unchanged,
the only delta is one more value in a closed set, and a bump would cost
every current consumer a coordinated update for a change none of them
would observe in payload shape. The proportionate remedy is the policy
now stated normatively in ADR-0106's correction: consumers built against
the three-value text add the `cancelled` branch, and until they do they
treat an out-of-vocabulary `outcome` the way `indeterminate` is treated —
result not establishable, never success and never failure. The erratum is
also a consumer notice, stated plainly there: any consumer exhaustively
matching three values has been exposed to an unlisted `cancelled` since
2026-07-25 22:54. Recording the violation as an erratum with a migration
policy keeps D2's rule intact instead of manufacturing an exception to
it. `partial` does NOT join the set either way: widening a closed
vocabulary with a genuinely new value breaks every consumer that
enumerated it, for the benefit of one producer.

- **Mapping**: round `completed` → job outcome `succeeded`; round `partial`
  or `failed` → job outcome `failed`; a round killed before any leg spawned
  → `cancelled`. The coarse job outcome answers "did the round come out
  clean"; anything finer is the round summary's job.
- **The read**: for manifest runs, `job.output`'s response carries one
  additive field, `round`, and its shape is exact. `round` is an ADR-0106
  D7 availability wrapper — `{available, value, reason_code, detail}` —
  because it is read-derived and D7 applies to every read-derived field.
  `available: false`, with `reason_code` distinguishing missing from
  unreadable from malformed, is a read failure of `round.json`; it is NOT
  how in-flight harvest is expressed — `round_state: "pending_harvest"` is
  data inside a readable summary. `value` has two parts, matching the two
  kinds of read behind it: `summary` is the `round.json` content verbatim
  (its `legs` stay labels, as D3 shows), and `leg_records` is an array read
  from `{run_dir}/legs/*.json`, each entry its own wrapper
  `{label, available, value, reason_code, detail}` — one unreadable leg
  record must not poison the others or masquerade as an absent leg.
  Consumers that do not know `round` ignore it; nothing existing changes
  shape. This is an additive field of the kind ADR-0106 D2 already permits,
  introduced and specified here rather than smuggled in by implementation —
  ADR-0106 itself is unchanged by it and contains no `round` text to look
  for.
- **The notice is the signal, not the carrier.** The terminal-notice payload
  is unchanged. A notification consumer that needs leg facts performs the
  `job.output` read on receipt; the cooperative ordering guarantee (D3) makes
  that read complete by the time the notice fires, and `round_state` covers
  the non-cooperative window honestly.

## Consequences

- The N-briefs round becomes one submission, one wait, one notice, and one
  `job.output` read for round result, per-leg outcomes, and all verdict
  artifacts.
- The brief-as-contract property becomes structural: nothing between the
  manifest and the leg can rewrite a brief, and the recorded content hashes
  make "what did leg 3 actually receive" a first-class, verifiable answer.
- Briefs stop carrying output-path contortion; sandboxed legs write to an
  announced in-tree path and the runner does the serving.
- What becomes harder: the runner takes on a harvest obligation on every
  terminal path, kill becomes two-stage for manifest runs (a contributor
  touching `job.kill` or the reaper must now know the manifest-aware
  branch), finalization becomes a claimed single-owner step
  (`finalize.lock`) every terminal writer must respect, and the harvester
  must be written as a hostile-input consumer (descriptor-anchored,
  no-follow, link-count checks) rather than a tree copy.
- The `pending_harvest` window is a deliberate admission: on non-cooperative
  ends, facts can arrive after the terminal status. The alternative — holding
  the terminal status until harvest completes on a path where the harvesting
  process may itself be dead — would trade an observable window for an
  unbounded wait.
- Reversal costs: D1 (manifest schema) is versioned and extendable; D4's env
  var name is effectively frozen the day a consumer's briefs reference it —
  which is why its collision check happens before first merge, not after.
  D6's additive field is cheap to add and expensive to remove, which is the
  usual asymmetry of read surfaces.

## Alternatives considered

- **N independent agent submissions (status quo)** — fully deterministic and
  available today; loses on N handles, N notices, hand harvest, and
  per-brief output-path contortion. Remains the correct fallback until this
  mode lands and is the interim recommendation.
- **Planner fanout with a "do not rewrite the briefs" instruction** — would
  reuse the whole existing surface; loses because a prompt prohibition is a
  request, not a control, and the failure mode (silently rephrased contract)
  is exactly the one the round cannot tolerate or even reliably detect.
- **Caller-managed artifact paths in briefs** — no runner changes at all;
  loses because it is the P3 status quo: sandbox-constrained path contortion
  in every brief, no uniform read surface, artifacts invisible to
  `job.output`.
- **A separate collector process that sweeps leg cwds after the round** —
  decouples harvest from the runner; loses because it is a second lifecycle
  to operate (its own liveness, its own failure states) and it cannot give
  the harvest-before-notice ordering guarantee without re-coupling to the
  runner's terminalization anyway.
- **Extending `job.status` with artifact fields** — one read instead of two
  for pollers; loses because the artifact listing already exists on
  `job.output`, `status()` is deliberately the cheap frequently-polled read,
  and widening it duplicates a contract consumers already bind to.
- **Adding `partial` to the closed `_OUTCOMES` vocabulary** — would let the
  job outcome carry the round result directly; loses because the set is
  closed precisely so consumers can enumerate it, and every existing
  consumer's match over four values silently mishandles a fifth. The round
  summary field is additive instead; ignorance of it is safe.
- **Holding the terminal write until reap-time harvest completes** — would
  make harvest-before-notice unconditional; loses because on the
  already-dead-parent path there may be nobody to finish the harvest
  promptly, and an unbounded non-terminal state is worse than an observable
  `pending_harvest` window (a caller waiting on terminal would wait on a
  corpse).
- **Inline manifest object in the MCP call** — DEFERRED, not rejected: it
  would save a temp file for machine-generated rounds, but v1's consumers
  produce brief files on disk anyway, the file path gives snapshot-and-hash
  evidence for free, and adding a second input shape later is
  backward-compatible while removing one is not. The consuming workflow has
  since confirmed it will use the file path exclusively.
- **Wildcard environment inheritance for legs** — one flag instead of named
  keys; loses because it forwards whatever the submitting process happened to
  carry (secrets included), makes a round unreproducible from its manifest,
  and turns the collision check into an unenumerable surface. Named keys with
  deny-by-default is what the consuming workflow itself asked for. This
  rejection is about a manifest-level mechanism; the baseline the runner
  already provides to every job kind is a distinct, pre-existing channel,
  named in D1 and out of this ADR's scope.

## Notes

- Command and MCP verb naming is an open question for sign-off; the mode
  ships beside `fanout.submit` whatever the name.
- Per-leg environment is IN v1 as D1's `env` map: the consuming workflow
  demonstrated the need with named cases and asked for deny-by-default with a
  closed per-leg allowlist. What contains it: keys are recorded per leg
  (`env_keys`), values reach the process only through the environment array
  (D1), and the reserved-name refusal is a submit-time validation with its
  own test. The D4 collision check still owns the runner's reserved name.
- The 30 s kill grace in D3 is a default, not a derivation: it must cover N
  bounded harvests (256 MiB cap each, local disk) while keeping `job.kill`
  meaningful as an interruption; implementations may make it configurable
  but the default ships as stated.
