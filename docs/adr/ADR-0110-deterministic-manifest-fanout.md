# ADR-0110: Deterministic manifest fan-out — legs from briefs, no planner

- **Status**: Proposed
- **Kind**: Aspirational (records the target state)
- **Area**: orchestration
- **Date**: 2026-08-03
- **Relations**: extends ADR-0106 (machine result contract); composes with the
  MCP job surface (`job.output` is the artifact listing read)

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
| Execution and aggregation | D2: independent parallel legs; per-leg timeout from spawn; parent outcome table |
| Durable per-leg record and ordering | D3: per-leg record persisted before parent terminalization and its one notice |
| Leg artifact channel | D4: scratch dir inside leg cwd, env-announced, harvested as bounded regular-file copy |
| Planner absence | D5: no-planner is a tested invariant with the profile-default drift vector as a named failure case |

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
  timeout: 1200                # seconds, positive, <= 86400
legs:                          # required, 1..64 entries
  - brief: /abs/path/briefs/module-a.md    # required
    cwd: /abs/path/worktrees/module-a      # required
    label: review-module-a                 # required
    model: <model spec>        # optional per-leg override
    timeout: 900               # optional per-leg override
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
- **`timeout`**: positive integer seconds, at most 86400. The ceiling is a
  sanity bound, not a derivation: a leg that needs more than a day is not a
  round leg.
- **Leg count 1..64.** The floor is definitional. The ceiling is one order of
  magnitude above the largest observed round (13) — a bound that exists so a
  generated manifest with a bug cannot fan out unbounded, chosen loose enough
  that no legitimate round has to think about it.
- **File-path-only in v1.** An inline manifest object in the MCP call is
  DEFERRED (see Alternatives): the file path gives snapshot semantics,
  a natural durable-evidence story, and parity with `prompt_file`, and the
  consuming workflow already produces brief files on disk.

### D2 — Round execution: independence, clocks, aggregation

Legs execute in parallel under the existing worker concurrency machinery;
concurrency caps compose the same way they do for the planner fanout.

- **Legs are independent by construction.** A leg failing, timing out, or
  being killed never cancels a sibling. For the motivating workload every
  completed verdict has value regardless of a sibling's fate. Fail-fast is a
  rejected alternative, not an option flag, in v1.
- **Per-leg timeout clock starts at leg process spawn**, not at submit and not
  at queue admission — queue wait under a concurrency cap is not the leg's
  time. What a timeout interrupts is the leg's own execution.
- **An optional round timeout** (defaults key `timeout` does double duty: it
  is the per-leg default; a future `round_timeout` is deliberately NOT in v1
  — one knob until a consumer demonstrates the second is needed).
- **Parent terminalization**: the parent run becomes terminal only when every
  leg has a durable per-leg record including its harvest state (D3). Parent
  `outcome` derives from the leg records:

| Leg records | Parent `outcome` |
|---|---|
| every leg `succeeded` and `harvested-N`/`dir-empty`/`dir-absent` | `completed` |
| at least one leg succeeded; at least one `failed`/`timed_out`/`killed`/`harvest_failed` | `partial` |
| no leg succeeded | `failed` |

  `dir-empty` and `dir-absent` do not degrade the parent outcome by
  themselves: a leg whose whole answer is its final message legitimately
  writes no artifact. `harvest_failed` always degrades to at least `partial`
  — artifacts were (or may have been) written and cannot be served, which is
  a loss the outcome must not paper over.

- **A timed-out leg** is recorded `timed_out`, receives cooperative
  termination, and its harvest is still attempted during cooperative teardown.

### D3 — The per-leg record, and what must be durable before the notice

Each leg gets one durable record in the run directory,
`{run_dir}/legs/{label}.json`:

```json
{
  "label": "review-module-a",
  "status": "succeeded",
  "outcome": "success",
  "started_at": "...", "finished_at": "...",
  "cwd": "/abs/path/worktrees/module-a",
  "model": "<resolved spec>",
  "brief_hash": "<content hash recorded at submit>",
  "harvest_state": "harvested-3",
  "harvest_detail": {"files": 3, "bytes": 18211, "skipped": []},
  "artifacts": ["module-a/verdict.md", "module-a/notes.md", "module-a/log.txt"]
}
```

- **Ordering guarantee**: on every cooperative path (normal completion,
  per-leg timeout, round-level cancellation, `job.kill` reaching the group
  cooperatively), the leg's harvest runs and its record is persisted BEFORE
  the parent terminalizes, and the parent's single terminal notice fires only
  after the last record is durable. A notification consumer and a polling
  consumer therefore read the same facts; there is no window where the notice
  says "done" and a leg record is still being written.
- **Hard-kill honesty**: a process cannot run cleanup after an uncatchable
  kill. For a leg that died hard, the surviving parent (or, if the parent
  itself died, the existing orphan-reaping path on the job surface) records
  harvest state from what is on disk at reap time. A scratch directory that
  cannot be read then records `harvest_failed` with a reason — never an empty
  artifact list presented as if nothing was written. The guarantee is
  explicitly bounded: harvest-before-notice holds on cooperative paths;
  hard-kill paths guarantee only that the record says what could and could
  not be established.
- **Failure states are distinct and never collapse** (each is its own fact a
  reader may act on differently): `dir-absent` (leg never created the scratch
  dir — NOT evidence the leg had no artifacts to write), `dir-empty` (created,
  nothing written — also not "clean", it is its own fact), `harvested-N`
  (N regular files moved), `harvest_failed` (scratch existed or should have
  and could not be fully served; partial harvests record what landed plus the
  failure).

### D4 — The leg artifact channel

For each leg the runner creates a scratch directory inside that leg's own cwd
tree — `{cwd}/.lionagi/leg-artifacts-{run_id}-{label}/` — and exports its
absolute path to the leg process as `LIONAGI_LEG_ARTIFACTS`. A sandboxed leg
can always write there: it is inside the tree the sandbox already permits.
No sandbox configuration changes anywhere.

- **Harvest is a bounded copy of regular files only.** Relative paths under
  the scratch dir are preserved under `{run_dir}/artifacts/{label}/`.
  Symlinks are never followed — each is recorded by name in
  `harvest_detail.skipped`, because a leg-created symlink pointing outside
  the leg's tree would otherwise turn the harvester (which is not sandboxed)
  into a copy proxy and falsify the no-sandbox-widening claim. Special files
  are skipped and recorded the same way. Per-leg caps: 1024 files, 256 MiB —
  an order of magnitude above observed verdict artifacts (single-digit
  markdown files); the cap exists so one runaway leg cannot fill the run
  store, and hitting it records `harvest_failed` with the count at the cap,
  never a silent truncation.
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
- **The read surface is `job.output`**: its existing `artifacts` +
  `artifacts_state` fields list the harvested files with zero new read
  surface. `job.status` stays artifact-free. Nothing in this ADR changes the
  ADR-0106 result-contract shapes.
- **This directory is a new named surface and inherits no protections.** It
  gets its own adversarial pass before the implementation merges (leg writes
  hostile names, symlink escapes, cap-overflow, mid-harvest kill), with the
  victim-alive-and-feature-works outcome asserted, not just absence of the
  attack's effect.

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

## Consequences

- The N-briefs round becomes one submission, one wait, one notice, and one
  `job.output` read for all verdict artifacts.
- The brief-as-contract property becomes structural: nothing between the
  manifest and the leg can rewrite a brief, and the recorded content hashes
  make "what did leg 3 actually receive" a first-class, verifiable answer.
- Briefs stop carrying output-path contortion; sandboxed legs write to an
  announced in-tree path and the runner does the serving.
- What becomes harder: the runner takes on a harvest obligation on every
  terminal path, and its failure modes must stay loud (D3's distinct states
  exist precisely so a harvest problem cannot masquerade as an artifact-less
  leg). A contributor touching leg teardown must now know the
  harvest-before-notice ordering.
- Reversal costs: D1 (manifest schema) is versioned and extendable; D4's env
  var name is effectively frozen the day a consumer's briefs reference it —
  which is why its collision check happens before first merge, not after.

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
  and widening it duplicates a contract that ADR-0106-adjacent consumers
  already bind to. Rejected in favor of naming `job.output` correctly.
- **Inline manifest object in the MCP call** — DEFERRED, not rejected: it
  would save a temp file for machine-generated rounds, but v1's consumers
  produce brief files on disk anyway, the file path gives snapshot-and-hash
  evidence for free, and adding a second input shape later is
  backward-compatible while removing one is not.

## Notes

- Command and MCP verb naming is an open question for sign-off; the mode
  ships beside `fanout.submit` whatever the name.
- Per-leg environment passthrough beyond `LIONAGI_LEG_ARTIFACTS` is out of v1
  unless the consuming workflow demonstrates a need; every added variable is
  surface area the collision check and the adversarial pass must then cover.
