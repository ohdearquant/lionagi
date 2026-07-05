# Reliable Artifact Production

Every recipe in this chain ends at the same question: does the artifact exist, and can a
consumer trust it? This recipe is about the producing side — writing runs whose evidence
survives timeouts, cancellations, and budget exhaustion.

## Write incrementally, not at the end

A gated leg (a reviewer, a verifier, a synthesis step) that accumulates its whole verdict in
memory and writes one file at the end produces nothing when it hits a deadline at 95%. The
run record then truthfully reports `run.failed.missing_artifact` — but the work is still lost.

Structure prompts and harnesses so evidence lands as it is produced:

- Instruct agents to write findings to the artifact file as they confirm them, section by
  section, rather than composing the full report in one final write.
- Prefer append-friendly formats (markdown sections, JSONL rows) over formats that only make
  sense complete.
- Put the verdict line last. A partial artifact with findings but no verdict is honest — a
  consumer can see how far the run got. A verdict with no findings behind it is the thing the
  contract exists to catch.

## Scope heavy checks so evidence survives the deadline

When a leg runs an expensive validation (full test suite, large diff review), order the work
so the cheap, high-signal portion completes and is written first. A run that times out after
writing "sections 1–4 verified, section 5 not reached" is a usable partial result; a run that
times out inside one monolithic check leaves nothing. Give the timeout budget to the harness
knob (`--timeout`), and give the leg a smaller internal budget so it finishes writing before
the harness kills it.

## Let the verifier see what you produced

Artifact verification runs against the run record's artifact paths. Two habits keep it honest:

- Write artifacts under the run's save directory (`--save`) or the run directory, so the
  manifest points at what was actually produced. Files written to arbitrary paths are
  invisible to verification.
- Never touch an artifact after the terminal status is written. Consumers compare artifact
  mtimes against run rounds to detect stale evidence; post-terminal edits defeat that check.

## The payoff

With incremental writes and scoped checks, the completion contract's reason codes become
fully trustworthy in both directions: `run.completed.ok` means verified evidence exists, and
a failure reason still leaves behind the partial evidence needed to resume instead of restart.

Next: [Reliable review runs](reliable-review-runs.md) — the consuming side of the same
contract.
