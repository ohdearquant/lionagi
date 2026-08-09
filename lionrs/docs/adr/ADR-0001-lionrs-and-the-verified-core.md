# ADR-0001: `lionrs`, and building the orchestrator on a core that is already proven

- **Status**: Proposed
- **Date**: 2026-08-08
- **Supersedes**: none
- **Related**: lionagi ADR-0114 (executable flow definition and role capabilities)

## Approval

| Role | Decision | Date |
|---|---|---|
| Director | Approve | 2026-08-08 |

## Context

lionagi is a Python package. This ADR opens a Rust tree inside the same repository and
says what belongs in it, what does not, and why the first thing to land there was not
written for the occasion.

### What was measured before deciding

**The stateful core already exists and is published.** `lion-core` is on crates.io at
0.4.0. Its source is not a sketch: `src/state/` carries actor, kernel, memory, plugin,
thread and workflow state; `src/step/` carries authorization, host calls, kernel
operations and plugin-internal steps; `src/types/` carries capability, identifiers,
policy, rights, runtime and security. Alongside it are 75 Lean files whose subject is
that state machine, including an end-to-end correctness proof with no remaining
`sorry`.

It was maintained in a separate repository (`LNkernel`) while every consumer of it lives
here. Measured: 187 tracked files, 2.92 MiB packed, 11 commits. The 586 MB and roughly
7,900 Lean files that a directory listing reports are the vendored toolchain under
`proofs/.lake`, which is build output restored by `lake` and is not tracked.

**Rust does not pay for itself here on speed.** A healthy agent leg in this system was
measured at a **1.8% duty cycle** over its lifetime, because it is blocked on a remote
API almost all of the time. Rewriting an orchestrator that is idle 98% of the time buys
nothing in throughput, and speed is the usual reason to reach for Rust. That argument
should be abandoned explicitly so nobody re-makes it later.

**What it pays for is a class of defect this system keeps producing.** Recent examples,
each found in running code rather than hypothesised: a checkout writing one schema
version against a database at another; a whole-row write reverting a concurrently
committed one; nodes entering an execution graph with no edges, so nothing downstream
could read their output; a complete and tested veto mechanism reached by zero callers;
and a status derivation with two reachable values where one of the three needed states
could not be expressed at all. These are state and invariant defects. They are the kind
a typed core makes *unwritable* rather than merely testable, and they are why the core
is the right place to start.

## Decision

### D1 — `lionrs/` is the Rust tree, and it is a separate Cargo workspace

`lionrs/crates/` carries its own workspace root. It is not merged with the existing
`apps/studio/desktop/src-tauri` package, which is a standalone Tauri application and
stays that way.

`lionrs/docs/adr/` holds decisions about the Rust core, numbered independently of
lionagi's Python-side ADRs, with `lionrs/docs/_templates/` holding their template. Two
numbering series is the honest representation of two decision surfaces that move at
different rates.

### D2 — The Rust of `lion-core` moves here as a fresh copy; the Lean proofs stay where they are, for now

What moves is the `crates/` workspace only — `lion-core`'s source, manifests and lock
file — copied at LNkernel head `64eeaba` (the 0.4.0 relicense commit), without history.
The Lean proofs are deliberately **not** moved today; they remain in the origin
repository and this ADR does not change how they are maintained.

A subtree merge carrying the 11-commit history was built and verified first (ancestry
confirmed, all commits reachable), then discarded in favour of the fresh copy: the
history's main legal value is the relicense provenance, which the origin repository
retains, and importing history was judged not worth coupling this tree's log to
another repository's. The source commit is recorded here instead: `64eeaba`,
"chore: relicense to Apache-2.0 and release lion-core 0.4.0".

**The consequence to hold onto: the proofs and the code they describe are now in
different repositories.** Until the Lean side moves or a cross-repo check exists, a
change to `lionrs/crates/lion-core` can silently diverge from the state machine the
proofs verify. The interim rule is that semantic changes to `lion-core` here require a
matching proof update in the origin repository before merge, and the delta table makes
closing this split a tracked item rather than an intention.

### D3 — lionagi builds on khive primitives rather than re-deriving them

khive publishes 44 crates and the relevant ones are on crates.io at 0.7.0
(`khive-types`, `khive-storage`, `khive-runtime` verified against the registry).
Dependencies are taken by **version from the registry**, not by path across
repositories. A cross-repository path dependency is a fork that no build failure
reports.

The cost to state plainly: the registry version can lag the upstream working tree, so a
capability visible in that checkout is not automatically available here. That is a
deliberate trade of freshness for a reproducible build, and the remedy when it binds is
an upstream release rather than a path override.

### D4 — What is claimed by proof, by test, and by neither is stated per claim

`lion-core` is described by machine-checked Lean proofs. That is a strong property and
it is also the easiest thing in this tree to overstate, because "formally verified"
reads as covering more than it does — and after D2 it covers even less than before,
since the proofs live in another repository and nothing in this one runs them. Every
ADR here carries a verification note naming which of its claims are proof-backed,
which are test-backed, and which are neither.

The immediate instance: **nothing in this repository's CI checks the proofs, and
nothing checks that this copy of the code still matches them.** What CI here can
honestly assert about `lionrs/crates` is that the Rust compiles and its 246 tests pass.
The "proven" in this ADR's title is inherited from the source commit and decays with
every semantic change made here; D2's interim rule and delta #1 are what keep that
decay visible.

## Consequences

**The repository gains a second toolchain and a second release channel.** Rust builds,
and a crate published from here rather than from the origin repository. That lands on
top of a Python publishing path that has already produced one release which was tagged,
GitHub-released, and absent from the package index with nothing reporting the gap. A
second channel should not be armed before the first one is trustworthy.

**Crate metadata is corrected as part of the move.** `repository` and `homepage` in
`lionrs/crates/Cargo.toml` now name this repository. The published 0.4.0 on crates.io
still points at the origin repository and will until the next publish, which is delta
#2.

**Contributors must know which tree a change belongs in.** A change to how a run's state
is represented may now belong in Rust, in Python, or in both, and "both" is the answer
that silently produces two divergent representations.

**A three-way split exists until the Lean question is settled.** The code is here, the
proofs are in LNkernel, and the published crate was built from LNkernel. Each pair can
now drift independently, and only one of the three pairings (code vs tests, here) is
machine-checked in this repository.

## Alternatives considered

**Start a fresh `lion-state` crate in `lionrs/crates/`.** This was the original plan and
it was wrong. A registry check found `lion-core` already published, under the same
ownership, containing the state machine and step relation that crate would have
re-derived, with proofs. Building it would have produced a second, unproven
representation of the same thing.

**Depend on `lion-core` from the registry and leave it where it is.** Cheapest, and it
preserves the split this ADR exists to close. It also leaves the proofs governing a
version that consumers here may not be on.

**Subtree-merge the whole repository with history.** This was built first and verified
(ancestry confirmed before it was discarded). Rejected for two costs the fresh copy
avoids: it couples this tree's log to another repository's, and it drags in 70 files of
`archive/v1` manuscript that are not code and do not belong in a public code tree's
import. The provenance the history mainly carried — the Apache-2.0 relicense — stays
attributable in the origin repository, which remains live because the Lean proofs still
live there; D2 records the source commit instead.

**Rewrite the orchestrator in Rust.** Rejected on the duty-cycle measurement above. The
orchestrator's cost is latency waiting on remote models, and the language does not touch
it.

## Current-vs-ideal delta

| # | Delta | Size | Issue |
|---|-------|------|-------|
| 1 | Close the code/proof split: either move the Lean proofs here with their own CI job, or build a cross-repo check that pins which proof revision describes which `lion-core` revision. Acceptance for the CI form: deliberately break one theorem and confirm the gate goes red — a passing job proves nothing about whether it runs the proofs. | M | |
| 2 | Publish the next `lion-core` from this tree, so the registry's `repository` metadata stops pointing at the origin. Acceptance: crates.io metadata for the new version resolves to this repository. | XS | |
| 3 | Add `lionrs/crates` to this repository's CI (fmt, clippy, test). Acceptance: a PR that breaks a `lion-core` test goes red here, demonstrated once by a deliberate break. | S | |
| 4 | Record in the origin repository that the Rust source of record moved here at `64eeaba`, while the proofs remain there. Acceptance: a reader arriving at either repository is told where each artifact lives. | XS | |
| 5 | State the first orchestration surface to sit on the kernel, with the invariant it is meant to make unwritable. Until this exists, the tree is a relocation rather than a foundation. | M | |

## Notes on verification

Nothing in this ADR is proof-backed. The file counts, the registry versions, the crate
contents, the source commit identity, and the 246-test pass in the new location are all
test-backed in the weak sense that each was produced by a command that can be re-run. The
duty-cycle figure is a single measurement of one healthy run and should be treated as an
order of magnitude rather than a constant.
