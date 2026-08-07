# ADR-0111: A run records the code it was reasoning about, and can refuse to start

- **Status**: Proposed
- **Kind**: Aspirational (records the target state)
- **Area**: orchestration
- **Date**: 2026-08-07
- **Relations**: extends ADR-0110 (deterministic manifest fan-out — D1 here adds
  fields to the run manifest that a manifest-driven round inherits, and D2's
  refusal happens before the fan-out ADR-0110 describes); depends on the
  existing `lionagi/cli/_code_identity.py`, whose `git_identity()` already
  computes every value D1 records

## Context

A long analysis run can be wrong about which code it was reading, and today
nothing in this package would notice.

The concrete case. A design run spent roughly five hours and nineteen agents
producing a specification for consolidating `lionagi/cli/main.py`. Its central
deliverable proposed deleting the file's block of dispatch forwarders, which at
the branch it read is 18 `_load_*` and 22 `run_*` definitions. That block does
not exist on the trunk: `#2894` had already migrated dispatch to the
auto-registration seam, leaving three lazy-import helpers and one special-cased
`run_skill`. Every path and line number the run cites resolves against code
that is not the trunk, so none of its output is usable. The run was launched
from a worktree on a branch that the trunk had already moved past, and it
recorded nothing about that.

Two distinct failures are tangled together here, and they need different
controls:

- **F1, wrong base at launch.** The run started on a branch the trunk had
  already moved past: 29 merges had landed on the trunk that the branch did not
  have. This was true before the run spent anything, and it is detectable at
  launch.
- **F2, base voided mid-run.** `#2894` merged 37 minutes *after* the run
  launched. A check evaluated only at launch cannot see it. Distinguishing F2
  from F1 matters: a launch-time check alone looks sufficient against this
  incident, because F1 happened to be fatal first, and it is not.

The pattern generalises past this incident. Any run whose premise is a
statement about a codebase — audits, reviews, migrations, consolidation
planning — holds that premise for its whole duration while the codebase moves
underneath it.

**We already built the instrument and the orchestration path is the only
surface that does not consult it.** `lionagi/cli/_code_identity.py` exposes
`git_identity(tree)`, which returns the resolved 40-hex `commit`, `branch`,
`detached`, `dirty`, a `worktree_fingerprint`, a `comparison_ref` with its
provenance (configured upstream, else the remote's default branch), and
`ahead`/`behind` counts against that ref. It is fail-closed by construction: a
git call that did not run yields `status: unknown` with a stated reason rather
than a zero that reads as agreement.

`li doctor`, `li --machine`, the MCP dispatch surface, the MCP server and the
Studio admin surface all call it. `lionagi/cli/_runs.py` and
`lionagi/cli/orchestrate/` do not. So `li doctor` will tell an operator exactly
where their checkout sits, while a nineteen-agent run beside it records
nothing.

## Decision

### D1 — Every run records its code position, unconditionally

At run start, `_runs.py` calls `git_identity()` on the run's working tree and
writes the result into `run.json` under a `code_position` key. This is
unconditional and has no opt-out, because recording is cheap, is already
implemented, and is what makes every later question answerable.

The recorded `commit`, `branch`, and `behind` are stamped into each leg's
prompt preamble. A leg that can cite a line number should be able to state the
ref that line number is relative to.

`git_identity` returning `status: unknown` is recorded as such. An
unestablished code position is a fact worth having, and it must never be
written as a clean one.

### D2 — A design-class run declares a trunk, or declares that it has none

Playbooks gain a `trunk:` key. For runs in the design and analysis classes it
is **required**, and a run whose playbook omits it is refused before it spends
anything.

`trunk: none` is an accepted value and is recorded as a deliberate choice.

The refusal is on **omission**, not on the absence of a trunk. The defect this
addresses is not that a run lacked a comparison point; it is that nobody chose
one, so nothing could check it and no one was accountable for it. An explicit
`none` converts a silent omission into a stated decision, which discharges the
failure class for the cost of one line. Every other run class warns rather than
refuses.

| Run class | `trunk:` absent | `trunk: none` | `trunk: <ref>` |
|---|---|---|---|
| design, analysis, audit, migration-planning | **refuse** | proceed, choice recorded | proceed, compared |
| every other class | warn | proceed, choice recorded | proceed, compared |

Warning everywhere was considered and rejected. A warning-only gate on a class
of work that routinely spends nineteen agents fails permissive by
construction, and warnings habituate precisely where the real failure hides.
Fail-closed is only fair to impose because D1 makes the declaration nearly free
to satisfy.

### D3 — Playbooks declare preconditions, evaluated before the DAG and at phase boundaries

Playbooks gain a `preconditions:` block: assertions evaluated before the DAG
runs, whose failure aborts with a named reason rather than producing confident
output. `artifacts:` already declares what a play owes on the way out;
`preconditions:` declares what must hold on the way in.

The first supported assertion is trunk movement: *the paths this run is about
have not moved on the declared trunk since the recorded base*.

**Preconditions are re-evaluated at phase boundaries, not only pre-DAG.** This
is required in v1 rather than deferred, because F2 above is the case a
pre-DAG-only check cannot see: the merge that voided the premise landed 37
minutes into a five-hour run. A re-evaluation aborts the run at the next phase
boundary and states which paths moved and what moved them.

Re-evaluation is bounded to the declared paths and runs at phase boundaries
only, so its cost is a fetch and a diff per phase, not per leg.

### D4 — Gate artifacts carry a machine-written code position, and acceptance keys on it

Every gate and verdict artifact gets a footer written by the harness, not
authored by the agent: resolved ref, timestamp, working tree, model.

The footer alone is decoration. **Acceptance of a gate artifact keys on
`footer_ref == expected_ref`**, so an artifact about the wrong base fails at
the point of consumption rather than at the point of authoring. Without a
forced consumer this decision would produce a provenance header that reads as
rigour and blocks nothing, which is the state the motivating incident was
remediated into by hand.

## Consequences

Runs become answerable about their own premises. "Which code was this
reasoning about" becomes a field rather than an inference from a worktree that
may since have moved.

Design-class playbooks need one new line. Existing playbooks in other classes
keep working and gain a warning.

A run can now abort mid-flight for a reason unrelated to its own execution.
That is intended, and the abort must name the paths and the commits that moved,
or it will read as a flake and be retried into the same wall.

The failure this does not address is a run whose premise is wrong in a way git
cannot see: correct base, correct trunk, wrong understanding. Preconditions
check that the ground has not moved, not that the run is looking at the right
ground.

Measurement should land before any of this. `CLISession.populate_summary()` is
called by all four CLI providers — `anthropic/claude_code.py`,
`openai/codex.py`, `google/gemini_code.py` and `pi/cli.py` — and the field it
fills is read by nothing, so a successful fifty-leg run records zero everywhere.
The cost figures motivating this ADR were obtained by counting output
directories by hand. Policy set on anecdote is policy that cannot be evaluated
afterwards.

## Alternatives considered

**Record nothing, rely on the caller.** This is today's behaviour, and today's
behaviour produced a five-hour run against a base nobody had chosen. Prompt
instructions to state a ref are requests, not controls.

**Resolve the base but never assert on it.** Cheaper and strictly better than
nothing, and it is D1 on its own. Rejected as the whole answer because the
motivating run would have recorded a correct and accurate `behind` count and
proceeded to spend everything anyway. A number nobody is required to read is
not a control.

**Put the check in the knowledge substrate instead of the harness.** Attractive
because a "what shipped recently" record has other uses, but the window that
mattered was 37 minutes and the authority for what landed on a branch is git.
Ingesting merged work into the knowledge base remains worthwhile for the
question of *why* something changed; it is the wrong instrument for *whether*
the ground moved under a running job.

**Refuse on the absence of a trunk rather than on omission.** Rejected: it
blocks legitimate work on detached or experimental checkouts while addressing
the same failure that an explicit `trunk: none` addresses for one line.
