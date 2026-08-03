# ADR-0110: Deterministic manifest fan-out — legs from briefs, no planner

- **Status**: Proposed
- **Kind**: Aspirational (records the target state)
- **Area**: orchestration
- **Date**: 2026-08-03
- **Relations**: extends ADR-0106 (machine result contract); composes with the
  MCP job surface (`job.status` artifact listing)

## Context

Every orchestrating surface this package ships puts a planner model between the
caller's task statement and the legs that execute it. `li o fanout` runs a
decomposition phase first — "orchestrator decomposing task into ≤N assignments"
(`lionagi/cli/orchestrate/fanout.py:254`) — and the assignments the workers
receive are the planner's text, not the caller's. `li o flow` has the
orchestrator compose the DAG. Playbooks template the planner's prompt; they do
not remove the planner.

That is the right shape for a prose task. It is the wrong shape for a class of
work that is common and currently unserved: the caller already holds N
pre-written briefs — review instructions, audit scopes, per-module checklists —
and each brief IS the contract for its leg. A planner that can rephrase,
merge, or re-scope those briefs is not adding intelligence; it is corrupting
the input. Multi-round document review is the sharpest instance: the brief
states what the reviewer must check, so any surface that lets another model
rewrite it disqualifies itself.

Callers in this position today fall back to submitting N independent agent
runs. That works — it is the only deterministic form — but the costs compound
with round count:

- N run handles to track and N terminal notices to receive, when the caller
  wants one answer to "is the round done".
- Artifact harvest by hand: each brief must name output paths, and sandboxed
  CLI legs (workspace-write) cannot write outside their own cwd tree, so the
  paths must be contorted to land inside it. Meanwhile every run already has
  an artifacts directory that `job.status` lists (`lionagi/mcp/jobs.py:736-750`,
  `:1740-1741`) — but a leg is never told where it is and could not write
  there if it were.
- Per-leg working directories are the norm, not the exception: one round may
  span two repositories, and PR-review rounds are per-worktree by
  construction. Any surface with a single run-level cwd excludes the most
  common round shapes.

## Decision

Add a deterministic fan-out mode: legs are built one-per-brief from a caller
manifest, verbatim, with no planner phase.

**1. Manifest in, one run out.** A YAML/JSON manifest declares the legs:

```yaml
defaults:
  model: gpt/large-effort-spec   # any model spec or agent profile name
  timeout: 1200
legs:
  - brief: /abs/path/briefs/module-a.md
    cwd: /abs/path/worktrees/module-a
    label: review-module-a
  - brief: /abs/path/briefs/module-b.md
    cwd: /abs/path/worktrees/module-b
    label: review-module-b
    model: other/spec            # per-leg override wins over defaults
```

Each leg's prompt is the brief file's text, unmodified. `brief`, `cwd`, and
`label` are per-leg; `model`/`agent`, `timeout`, and other run knobs may be
set in `defaults` and overridden per leg. Brief files are read and snapshotted
at submit time (same rule as `prompt_file` on the agent surface: editing the
file afterwards cannot change what an already-submitted run executes).

**2. One run id, one terminal notice.** The manifest run is a single job. Legs
execute in parallel under the existing worker concurrency machinery; the
terminal notice fires once, when the last leg ends, and carries per-leg
outcomes. Per-leg status remains observable mid-run through the job surface.

**3. A sanctioned leg artifact channel, without widening any sandbox.** For
each leg the runner creates a scratch directory inside that leg's own cwd tree
and exports its absolute path to the leg process as `LIONAGI_LEG_ARTIFACTS`.
A sandboxed leg can always write there — it is inside the tree the sandbox
already permits. When the leg reaches a terminal state, the runner harvests
the scratch directory's contents into the run's artifacts directory under the
leg's label and removes the scratch. `job.status` then lists every leg's
artifacts with zero new read surface, and briefs stop carrying output-path
contortions.

**4. Same surface everywhere.** The mode ships as a CLI command and as an MCP
verb beside `fanout.submit`, taking the manifest as a file path. Submission
validation follows the existing refuse-early pattern (`lionagi/mcp/dispatch.py:600-617`):
a manifest that would be refused on start — unreadable brief, relative path,
empty legs — is refused at submit, before a job record exists.

## What this is not

- Not a replacement for `fanout`/`flow`: prose tasks that need decomposition
  keep the planner surfaces. This mode refuses to plan by design.
- Not a DAG: legs are independent by construction. Dependencies between legs
  are the flow surface's job.
- Not a scheduler: one manifest, one round, one run.

## Consequences

- The N-briefs round becomes one submission, one wait, one notice, and one
  `job.status` read for all verdict artifacts.
- The brief-as-contract property becomes structural: nothing between the
  manifest and the leg can rewrite a brief, so a review round's inputs are
  exactly what the caller wrote.
- The manifest is a durable record of what was dispatched — the run snapshots
  it, so "what did leg 3 actually receive" has a first-class answer.
- The scratch-and-harvest channel adds a small teardown obligation (harvest
  must run on every terminal path, including kill), and its failure mode must
  be loud: a leg whose scratch cannot be harvested reports that fact in its
  outcome rather than presenting an empty artifact list as if nothing was
  written.

## Open questions

1. Command and verb naming.
2. Whether an inline manifest (object in the MCP call, no file) is accepted in
   v1 or file-path-only.
3. Whether per-leg environment passthrough (beyond `LIONAGI_LEG_ARTIFACTS`) is
   in scope for v1.
