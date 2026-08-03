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
  Deny-by-default: nothing travels from the submitting process's environment;
  beyond its baseline, a leg receives exactly the variables named here, and
  the manifest snapshot is their durable source. A declared key that also
  exists in the leg's baseline is overridden by the manifest value — that is
  the feature (the declared value is the reproducible one); the D4 refusal
  rule protects only the runner's own reserved name, and
  `LIONAGI_LEG_ARTIFACTS` is accordingly refused here at submit. Declared
  keys are listed in the leg's durable record as `env_keys`; the record never
  re-prints values. Manifests are durable evidence, so credentials do not
  belong in them: a secret a leg needs stays in the runner's own environment
  and reaches the leg through the baseline, named nowhere. The consuming
  workflow demonstrated the concrete cases (actor identity resolving wrong on
  workspace cwds, per-worktree build target directories).
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
  termination, and its harvest is still attempted during cooperative teardown.

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
  "harvest_state": "harvested-3",
  "harvest_detail": {"files": 3, "bytes": 18211, "skipped": []},
  "artifacts": ["module-a/verdict.md", "module-a/notes.md", "module-a/log.txt"]
}
```

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

`round_state` is the honesty field: `complete` means every leg record and
harvest is durably written; `pending_harvest` means a terminal status became
visible before cleanup finished (possible only on the non-cooperative paths
below). A reader who finds a terminal job with `round_state: pending_harvest`
is told, in the record itself, that leg facts are still landing — the window
exists and is OBSERVABLE, never silent.

- **Cooperative ordering guarantee**: on normal completion and per-leg
  timeout, every leg's harvest runs and its record persists, then
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
  not terminalize within a bounded grace (default 30 s — long enough for N
  bounded harvests, short enough that a kill still means something), the kill
  surface performs a manifest-aware reap: harvest each leg's scratch from
  disk as D4 specifies, write each leg record with what could be established
  (`harvest_failed` with a reason where a scratch is unreadable — never an
  empty artifact list), write `round.json`, then make its single terminal
  write. Records written by a reaper say so (`"recorded_by": "reaper"`).
- **An already-dead parent** (crash, OOM, machine restart) is found by the
  existing orphan-reaping path on the job surface; for manifest runs that
  reaper performs the same disk-side harvest-then-record sequence before its
  terminal write. Where the existing reaper (or any non-manifest-aware
  writer) has already published a terminal status, the manifest-aware pass
  still runs, writes the records late, and flips `round_state` from
  `pending_harvest` to `complete` — late facts beat lost facts.
- **The bound, stated plainly**: harvest-before-notice holds on cooperative
  paths. On kill and reap paths the guarantee is weaker and explicit —
  `round_state` names whether the facts are all in, and every leg record
  distinguishes what was established from what could not be.

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
- **The scratch dir is removed after harvest** on cooperative paths. On
  hard-kill paths it may survive; the reap-time harvest (D3) consumes it.
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

The job surface's `outcome` is a closed vocabulary —
`{succeeded, failed, cancelled, indeterminate}` (`lionagi/mcp/jobs.py`,
`_OUTCOMES`) — and consumers legitimately bind to it. `partial` does NOT
join that set; widening a closed vocabulary breaks every consumer that
enumerated it, for the benefit of one producer.

- **Mapping**: round `completed` → job outcome `succeeded`; round `partial`
  or `failed` → job outcome `failed`; a round killed before any leg spawned
  → `cancelled`. The coarse job outcome answers "did the round come out
  clean"; anything finer is the round summary's job.
- **The read**: for manifest runs, `job.output`'s response carries one
  additive field, `round` — the full `round.json` content (round_version,
  round_state, result, counts, and the per-leg records inlined). Additive
  field on an existing read: consumers that do not know it ignore it;
  nothing existing changes shape. This is a named, versioned amendment to
  the ADR-0106-adjacent result surface, carried by this ADR rather than
  smuggled in by implementation.
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
  branch), and the harvester must be written as a hostile-input consumer
  (descriptor-anchored, no-follow, link-count checks) rather than a tree
  copy.
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
  deny-by-default is what the consuming workflow itself asked for.

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
