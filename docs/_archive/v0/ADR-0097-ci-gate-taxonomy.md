# ADR-0097: CI Gate Taxonomy And Fail-Closed Aggregation

**Status**: Proposed
**Date**: 2026-07-07

## Context

`.github/workflows/ci.yml` grew its gates ad hoc: each new job was added to
cover a new concern, but the aggregate `ci-gate` job — the single required
status check branch protection points at — was never revisited to keep pace.
Two problems followed from that drift:

1. **The aggregate can go green while a real gating job never ran.** `ci-gate`
   reads `join(needs.*.result, ' ')` and only fails on the literal substrings
   `failure` or `cancelled`; a `skipped` result is treated as a pass. GitHub
   Actions marks a job `skipped` (not `failure`) when its own `needs`
   dependency fails and the job has no `if: always()`-style override. A
   path-filter job that decides whether a downstream Docker build job should
   run was not listed in `ci-gate.needs` at all, so its own failure was
   completely invisible to the aggregate, and the downstream job it gates
   would report `skipped` either way — a pass, whether or not the skip was
   intentional.
2. **The Docker image path filter under-covers the Dockerfile's real inputs.**
   The filter that decides whether to build the Studio Docker image watched a
   narrower set of paths than the Dockerfile actually copies into the image
   (parts of the Python package tree, the license/readme files consumed by
   the packaging step, and the marketplace/plugin content baked into the
   image were all outside the watched set).

Neither gap changes what *passes* on a correctly-behaving run; both mean a
broken dependency chain can silently present as green. Fixing them is
additive: no existing gate is weakened, and no new gate becomes
merge-blocking that was not already effectively intended to be.

Separately, a CLI flag consumed by the Studio launcher was removed as part of
a mode-flag redesign with no compatibility shim, which is a related but
independent hygiene gap in the same area of the codebase (CLI ownership,
not CI workflow ownership) — see the CLI change in this same commit series
for the deprecation fix; it does not affect the taxonomy below.

## Decision

### Taxonomy: every `ci.yml` job, its protected property, and its gating status

| Gate | Protected property | Trigger / path filter | Gated or informational | Why |
|---|---|---|---|---|
| `lint` | Hygiene, basic correctness | push/PR to `main`, `develop` | Gated via `ci-gate` | Required source hygiene and static checks; must be `success`. |
| `docs` | Documentation build hygiene | Same as above | Gated via `ci-gate` | Docs must build for merge confidence. |
| `test` | Correctness | Same as above; PR matrix runs a subset of Python versions | Gated via `ci-gate` | Primary regression gate. |
| `frontend` | Frontend correctness and packaging | Same as above | Gated via `ci-gate` | Type/build/test coverage for the Studio frontend. |
| `studio-e2e` | End-to-end correctness | Same as above | Gated via `ci-gate` | Browser-level Studio behavior. |
| `changes` | Gate-control correctness | Path filter for Studio Docker inputs | Gated via `ci-gate` (this change) | Its own result controls whether a real gate runs; must be `success` or the aggregate cannot trust the Docker gate's skip. |
| `studio-docker` | Packaging, container install, release-image early warning | Runs only when `changes` reports Docker-relevant inputs changed | Conditionally gated via `ci-gate` | Required when image inputs changed; a `skipped` result is acceptable only when `changes` succeeded and reported no image-relevant change. |
| `vscode` | Extension packaging and test hygiene | Same as `ci.yml` | Gated via `ci-gate` | Extension build/test coverage. |
| `marketplace` | Marketplace/package-content hygiene | Same as `ci.yml` | Gated via `ci-gate` | Marketplace lint always covered by the aggregate. |
| `ci-gate` | Branch-protection aggregate integrity | Same as `ci.yml` | Gated aggregate | Single stable required context; must fail closed. |
| `publish` | Package publishing | Push to `main` only | Release/deploy gate, not a PR merge gate | Should not be part of the PR branch-protection aggregate. |
| Typecheck workflow's type-check job | Typing hygiene | push/PR to `main`, `develop` | Informational | Explicitly advisory (soft-fails, baseline check continues on error); making it merge-blocking is a policy call, not a mechanical one. |
| Marketplace-lint workflow's lint job | Marketplace hygiene | PR path filter over marketplace/plugin paths | Informational relative to `ci-gate` | Separate path-gated workflow that supplements the gated `marketplace` job in `ci.yml`. |
| CodeQL workflow's analyze job | Supply-chain/security scanning | push/PR + weekly schedule | Informational relative to `ci-gate` | Separate security workflow; no evidence it is wired into the aggregate. |
| Benchmarks workflow | Performance | push/PR | Informational | Perf data collected separately; blocking on regression is a new gate, not a fix to an existing one. |
| Baseline-refresh workflow | Performance baseline maintenance | Manual dispatch only | Informational/manual | Should never block a normal PR. |
| Release workflow's test/deploy/docker jobs | Release correctness / packaging / supply-chain | `release: published` | Release gate | Blocks the release workflow, not PR merge. |
| Vercel deploy workflow | Frontend deployment | Release published / manual | Deploy gate, not PR merge gate | Deployment is a separate decision from merge confidence. |
| Docs-deploy workflow | Documentation deployment | Push to `main` / manual | Deploy gate, not PR merge gate | Post-merge publish path. |

### Implemented (this commit series, additive only)

- `ci-gate.needs` now includes the path-filter job alongside the jobs it
  already listed. The aggregate's check script was rewritten from a
  substring match over `join(needs.*.result, ' ')` to an explicit,
  per-job `result != 'success'` check for every hard gate, with a single
  named conditional exception: the Docker build job may be `skipped` only
  when the path-filter job succeeded and reported no Docker-relevant
  change. Any other outcome for any hard gate — `skipped`, `failure`, or
  `cancelled` — now fails `ci-gate`.
- The Docker build job's own `if:` condition was made explicit
  (`always() && <path-filter job>.result == 'success' && ...`) so its
  dependency intent reads directly off the job definition, not only off
  the aggregate's downstream logic.
- The Docker image path filter was broadened to match every repo-controlled
  path the Dockerfile actually copies into the build (the full Python
  package tree instead of a subdirectory, the license/readme files consumed
  by the packaging step, and the marketplace/plugin content copied into the
  image), plus the workflow file itself.

None of the above makes a previously-informational job merge-blocking. The
aggregate is stricter about jobs that were always intended to be
merge-blocking; nothing new enters `ci-gate.needs` except the pre-existing
path-filter job whose own result was the gap.

### Proposed, founder-gated (not implemented in this commit series)

| Gap | Evidence | Proposed change |
|---|---|---|
| No PR-time test leg covers the Python version the Docker image runtime uses | The Docker base image pins a newer Python minor version than the PR test matrix covers; the image is only exercised indirectly through the conditional Docker build | Add a PR-time test leg (or a separate packaging check) for that Python version, if the founders accept the added merge-blocking cost |
| Type-check job is advisory | Soft-fail command plus a continue-on-error baseline step | Convert to merge-blocking only after a baseline burn-down and explicit owner sign-off |
| Security scanning workflow is not in the aggregate | Runs as a fully separate workflow with no reference from `ci-gate.needs` | Require it as a separate branch-protection check if desired, rather than folding it into `ci-gate`, so its independent schedule (including the weekly run) is preserved |
| Performance gates are informational only | Benchmarks and baseline-refresh workflows never block a PR | Define regression thresholds before any merge-blocking performance gate is proposed |

Nothing in this table is enforced by this change. Each row requires a
founder decision on the added merge-blocking cost before implementation.

## Consequences

**Positive**

- `ci-gate` can no longer go green while a job it depends on for a real
  protected property has failed, been skipped unexpectedly, or been
  cancelled.
- The Docker build path is now evaluated against the real set of files the
  image depends on, so an unrelated PR outside that set still skips the
  build (no new merge-blocking cost), while a PR that touches any real input
  is caught.
- The taxonomy gives future gate additions a template to slot into: protected
  property, trigger, gated-vs-informational, and justification.

**Negative**

- The aggregate's check step is now a small inline Python script instead of
  a one-line shell case statement; it is more verbose but the fail-closed
  semantics are inspectable per hard gate rather than folded into a single
  substring match.
- The founder-gated table above is a to-do list, not a resolved backlog; it
  will need re-validation against the workflow files at the time any of it
  is picked up.

## Alternatives Considered

| Alternative | Why rejected |
|---|---|
| Keep the skip-tolerant `join(needs.*.result)` check but add the path-filter job to `needs` | Still passes if any hard gate is skipped for an unrelated reason (e.g. a runner outage); the reporter's core complaint — skip is not verified as *intentional* — would remain unresolved for every job, not only the Docker path. |
| Make the Docker build job unconditionally required (drop the path filter) | Turns every PR into a Docker-build PR regardless of whether Docker-relevant files changed; increases CI cost and merge-blocking surface for unrelated changes without a corresponding safety gain. |
| Fold CodeQL / perf / type-check into `ci-gate` now | Silently converts three informational workflows into merge-blocking ones without an explicit sign-off; deferred to the founder-gated table instead. |

## References

- `.github/workflows/ci.yml` — `changes`, `studio-docker`, `ci-gate` jobs.
- `apps/studio/Dockerfile` — the Docker build's actual `COPY` inputs.
- `lionagi/studio/cli.py` — unrelated CLI deprecation fix landed in the same
  commit series; tracked separately from this taxonomy.
