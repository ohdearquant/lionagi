# ADR-0089: Sandbox Backend Seam and Recursive Measurement Loop

**Status**: Proposed
**Date**: 2026-07-03
**Supersedes**: [ADR-0079](ADR-0079-substrate-executor-provider-interface.md) (in part: its
`ExecutionTarget`/`ExecutionLimits` types are adopted by implementing them; its `ExecutorProvider`
iModel-routing protocol half is descoped and parked, not carried into this ADR) and
[ADR-0080](ADR-0080-remote-sandbox-substrate-execution.md) (its backend contract, the
`SandboxBackend` literal and `sandbox_exec_stream()` shape, is absorbed here; its flow/play
integration, `DependencyAwareExecutor._execute_operation()` wiring, is explicitly **not**
absorbed; see [Lineage](#4-lineage-supersession-scope)).
**Builds on**: [ADR-0088](ADR-0088-flow-steering-mechanisms.md) (Accepted; the measurement
discipline this ADR inherits).

## Context

A sandboxed measurement harness is needed to run controlled A/B trials against two targets: the
lionagi prompting system (the ADR-0088 steer-adherence replication is the first proof) and the
notification-prompting surface (`li monitor` / how-do-I-get-notified). Both targets are gated by
sandboxed numbers carrying ADR-0088's measurement discipline: provider-family split, N valid per
cell, a pre-registered lift gate. On top of the harness sits a recursive improvement loop: a model
proposes variants, the harness measures them, a gate decides promotion.

The crux is the backend-abstraction seam: one contract for `provision`, `run_cell`, `collect`,
and `teardown` that hides local-worktree, Docker, Apple Container, Daytona, and Codespaces
differences from every caller. Getting this contract right before backends multiply is the point
of this ADR; building a fifth or sixth backend against a leaky abstraction is expensive to unwind
later.

### What already exists (source-verified)

- `lionagi/tools/daytona.py` (`DaytonaSandbox`) is shipped and SWE-bench-proven. It has all four
  contract legs already: `create()` (provisioning, snapshot or image), `exec_stream()` (streaming
  run-cell via `on_stdout`/`on_stderr` callbacks over a Daytona session), `download()`/`read_text()`
  and `git_diff()` (collect), and `delete()` (teardown). `benchmarks/orchestration/_sandbox_smoke.py`
  and `_daytona_smoke.py` already exercise this path end to end, including injecting a provider API
  key (`DEEPSEEK_API_KEY`) as sandbox env and parsing an `@@SIG@@`-prefixed stdout protocol for
  live signals, concrete prior art for the exec-cell shape this ADR formalizes.
- `lionagi/tools/sandbox.py` (`SandboxSession`) is shipped and is the git-worktree isolation
  `CodingToolkit` already uses: `create_sandbox()`, `sandbox_diff()`, `sandbox_commit()`,
  `sandbox_merge()`, `sandbox_discard()`. It has provision/collect/teardown but no `run_cell` leg.
- `benchmarks/orchestration/harness/runner.py:run_once()` is the existing measurement-loop
  consumer: it drives one `(config, task)` trial today entirely in-process, with no substrate
  selector.
- `benchmarks/orchestration/harness/stats.py` (`wilson()`, `disjoint()`, `Proportion`) and
  `benchmarks/orchestration/suites/steering/report.py` (`evaluate_gate()`, `GATE_TEXT`,
  `MIN_VALID_N = 20`) are the merged ADR-0088 measurement stack: Wilson-CI proportions, a
  disjoint-interval overlap test, and a three-arm (`no_steer`/`steer_buried`/`steer_rendered`)
  adherence gate at `arm2 - arm1 >= 0.4` and `arm2 >= 0.8` on `>= 2` of 4 provider families, with
  `arm0 <= 0.1` validating the fixture. `benchmarks/orchestration/suites/steering/fixture.py`'s
  `is_steer_adherent()` is the machine-checkable, structural (not single-token) adherence check
  this gate scores against.
- `benchmarks/orchestration/score.py` imports `harness.judge.score`, a second, LLM-judge-based
  scorer used for review-quality benchmarking, distinct in kind from the steering gate. Both
  scorer classes exist in the tree today; they are not interchangeable (see
  [Measurement discipline](#6-measurement-discipline-inherited-from-adr-0088)).
- [ADR-0079](ADR-0079-substrate-executor-provider-interface.md) and
  [ADR-0080](ADR-0080-remote-sandbox-substrate-execution.md) are both Proposed with zero
  implementing code (`lionagi/substrate/` does not exist). 0079 defines `ExecutionTarget` /
  `SubstrateStreamEvent`-shaped types for executor routing (`#1196`); 0080 sketches a backend
  contract for flow/play remote execution (`#1195`), targeting
  `DependencyAwareExecutor._execute_operation()` in `operations/flow.py`. Three overlapping
  Proposed sandbox ADRs would be worse than one that resolves the overlap.

## Decision

One `SandboxBackend` Protocol plus a `Handle` carrying session state. Backend divergence is
absorbed in `provision()` and `capabilities()`, never in `run_cell()`'s signature. The contract
distinguishes two cell kinds, **prompt-cell** and **exec-cell**, because they need different
isolation, secrets handling, and bias controls. The measurement loop is a driver on top of the
seam, gated behind the seam shipping and the stats-reuse discipline being proven.

### 1. Contract shape

```python
class SandboxBackend(Protocol):
    async def provision(self, spec: ProvisionSpec) -> Handle: ...
    async def run_cell(self, handle: Handle, cell: Cell, on_event: Callable[[SubstrateStreamEvent], None] | None = None) -> CellResult: ...
    async def collect(self, handle: Handle, paths: Sequence[str]) -> dict[str, bytes]: ...
    async def teardown(self, handle: Handle) -> None: ...
    def capabilities(self) -> Capabilities: ...
```

`Handle` extends ADR-0080's `SandboxSession` shape: `backend`, `remote_id`, `remote_repo_path`,
`metadata`. It is state, not behavior, and it does not itself dispatch by backend.

`capabilities()` is not optional sugar; it is where backend divergence becomes visible to callers
instead of leaking into `if backend == ...` branches. It declares at minimum: cold-start class
(sub-100ms / seconds / minutes), streaming support, mount-vs-upload semantics, image-build
support, and whether the backend can host a prompt-cell's provider call host-side (see
[Cell kinds](#3-run-cell-semantics-and-the-two-cell-kinds) below). Callers degrade explicitly by
reading `capabilities()`, never by branching on a backend name.

Two existing-library alternatives were evaluated and rejected as the contract base (kept as prior
art): `llm-sandbox` (MIT, Docker/Podman/K8s only, no Daytona, no Apple Container; adopting it
would couple this measurement-critical seam to an external project's release cadence for no
adapter savings, since both a Daytona and an Apple Container adapter would still need to be
hand-written) and LangChain Deep Agents' `BaseSandbox` (an `execute(command)`-only contract that
drops `capabilities()` entirely; cold-start class is the single largest axis of divergence across
this ADR's backend roster, and an execute-only shape forces backend branching back into every
caller, the exact leak this seam exists to prevent). Both independently converge on the same
four-verb least-common-denominator shape, which is corroborating evidence that the shape is right,
not a reason to adopt either as the base.

### 2. Seam location

The seam ships in `lionagi/tools/`, next to `daytona.py` and `sandbox.py`, now. Its first and only
consumer is `benchmarks/orchestration/harness/runner.py:run_once()`. A move to a shared
`lionagi/substrate/` package (the ADR-0079 framing) is a **non-goal until a second, non-benchmark
consumer exists**: that consumer is flow/play production remote execution, which this ADR
explicitly defers (see [Lineage](#4-lineage-supersession-scope)). The "promotion path" this ADR
commits to is a consumer path (benchmark runner today, flow/play later if a second ADR takes it
up), not a package-location path decided in advance of that need.

### 3. Run-cell semantics and the two cell kinds

`run_cell()` streams via callback, matching the one shipped backend today
(`daytona.py exec_stream(on_stdout, on_stderr)`) and the ADR-0080 design
(`sandbox_exec_stream(on_event=...)`). An async-iterator alternative is explicitly deferred: no
backend in the day-one or slice-2 roster needs it, and `capabilities()` can gate it in later
without a contract break. Building both shapes now would be speculative generality against a
backend set that does not ask for it.

A cell is **one scored trial** (seed inputs, an entrypoint, an artifact manifest), not one shell
command. The measurement unit is a trial; pushing trial assembly into every caller by defining a
cell as "one shell invocation" would just relocate that assembly into every call site.

Every cell declares a **kind**, and this is the single most consequential fork this ADR resolves:

- **prompt-cell**: the provider call (the LLM under test) runs **host-side**, already
  authenticated. The sandbox holds only the workspace and the collected artifacts. No secrets
  cross into the box. Egress and timeouts are the host's. This is the shape of every cell the
  ADR-0088 steer fixture and the notification-prompting fixture (below) need: an op drafts or
  redirects, a downstream op executes, and a machine-checkable text artifact is scored. No
  untrusted code executes, so sandboxing here buys reproducibility, parallelism, and an
  observability mirror, not isolation from anything hostile.
- **exec-cell**: the agent-under-test runs code **inside** the box (the SWE-bench shape
  `daytona.py` already proves out). The provider call originates inside the sandbox; secrets must
  be injected (as `_sandbox_smoke.py` already does for `DEEPSEEK_API_KEY`); egress is the
  backend's.

`capabilities()` declares whether a backend can host a prompt-cell's provider call host-side.
This fork governs four things at once, which is why it is load-bearing rather than cosmetic:

- **Secrets**: prompt-cells never receive provider keys in the box; exec-cells always do, via a
  reference or broker rather than raw env where the backend allows it (echoing 0080's own open
  question; this ADR does not resolve broker-vs-raw-env, see
  [Open questions](#open-questions-for-ocean)).
- **Egress**: a prompt-cell's provider call never crosses the backend's network, so backend choice
  cannot bias it.
- **Teardown cost**: a prompt-cell's box holds no long-lived state, so teardown is cheap regardless
  of backend; an exec-cell's teardown cost is backend-dependent (see the roster below).
- **Measurement bias**: this is the sharpest one. For an exec-cell, egress IP, per-IP rate-limit
  buckets, TLS/DNS path, and wall-clock timeout budget all shift with the backend. ADR-0088's core
  control is the provider-family split (claude_code / codex / gemini / one-API-model). If backend
  choice correlates with which provider ran a trial, backend becomes confounded with provider and
  the split stops isolating the provider effect. **Fence (normative): a cell's identity is
  `(experiment, variant, provider, backend, fixture)`. The gate MUST NOT pool across backends
  within a provider-family comparison; cross-backend numbers are reported side by side, never
  merged.** Preferring host-side provider calls for prompt-cells is precisely how this ADR removes
  backend from the provider comparison entirely for the cell kind that dominates its first two
  targets.

### 4. Lineage (supersession scope)

This ADR adopts ADR-0079's `ExecutionTarget` and `ExecutionLimits` dataclasses by implementing
them: they are the shared vocabulary this seam needs, and they are frozen, codeless data types
with no reason to redesign. ADR-0079's other half, the `ExecutorProvider` protocol that types
`claude_code`/`codex` endpoints for iModel routing (`#1196`), is orthogonal to sandbox backends
and is **descoped and parked**, not carried into this ADR.

ADR-0080's backend-contract half (the `SandboxBackend` literal, `sandbox_exec_stream()` shape) is
absorbed into the contract defined above. ADR-0080's other half, wiring flow/play production
operations through `DependencyAwareExecutor._execute_operation()` in `operations/flow.py`, is
**explicitly out of scope for this ADR**. It is not silently absorbed (it has its own blast radius
and its own Leo gate) and it is not left as codeless vaporware under this ADR's number: it
**reopens as a fresh, named future ADR when a second, non-benchmark consumer of this seam
exists.** Until then, `lionagi/substrate/` is not built and `DependencyAwareExecutor` is untouched.

### 5. Backend roster

Backends are not isolation-equivalent substitutes for one another; each buys something different,
and the roster below is driven by workload need, not by treating every named backend as a
day-one requirement:

| Backend | Slice | Buys | Cost / risk |
|---|---|---|---|
| Worktree / host | Day 1 | Clean git state, zero isolation overhead: the right home for prompt-cells, which need no isolation | None new; already shipped in `sandbox.py` (gains a `run_cell` leg) |
| Daytona | Day 1 | Horizontal scale (sub-100ms cold start, hundreds of trials), the proven SWE-bench exec-cell path | Went closed-source 2026-06-11 and is managed-cloud-only going forward, with no self-host option; see the hard constraint below for why this ADR never treats Daytona as sufficient alone |
| Docker | Slice 2, **required** | The zero-managed-vendor guarantee for exec-cells (see the hard constraint below); sovereign, local exec-cell repro | None new; universal, mature API |
| Apple Container | Deferred, documented | microVM-per-container isolation this workload does not need | No official socket/REST API, CLI-and-XPC only; the `mocker` compatibility shim is CLI-only (no Docker Engine API), leaks on teardown (no `--rm` equivalent, ~10s graceful-stop tax per cell), and measures roughly 3.6x slower cold start than Docker Desktop in the shim author's own numbers. At a hundreds-of-trials workload, that teardown and cold-start tax compounds; buying unused microVM isolation at that cost is not justified for v1 |
| Codespaces | Supported at the seam level; adapter deferred past v1; never the loop's default backend | Ocean named it explicitly. A batch or non-interactive execution profile the seam can express through `capabilities()`, and a concrete demonstration that the seam itself is pluggable to a backend this ADR did not have to build first | Minutes-scale cold boot without prebuilds and GiB-month-plus-core-hour billing are wrong-shaped for a default driver of hundreds of short trials; `capabilities()` reports its cold-start class as minutes-scale so callers batch around it or skip it per trial, rather than the seam hardcoding an exclusion. This is why it is supported, not why it is excluded |
| wasmtime (khive-cloud plugin layer) | Out of scope | A different isolation tier (wasm-module, not OS/container) | Not evaluated further here |

**Hard constraint (normative): the recursive measurement loop MUST be runnable with zero
managed-vendor dependency.** Daytona announced going closed-source on 2026-06-11 and is
managed-cloud-only going forward, with no self-host option. A roster whose only exec-cell backend
is Daytona would make the loop hostage to one vendor's pricing, availability, and policy changes.
The Docker backend is what satisfies this constraint, not an optional local-repro convenience: the
roster MUST always contain at least one exec-cell backend requiring no managed third-party
service, and slice 2 does not ship without it. Any future roster change that would remove Docker
MUST replace it with an equivalent zero-managed-vendor backend before it lands.

Apple Container in-roster is a strategic call, not a technical one settled by this ADR; see
[Open questions](#open-questions-for-ocean). Codespaces' in-roster status is settled above: it is
supported, its adapter is deferred past v1, and it is never selected as the loop's default backend.

### 6. Measurement discipline (inherited from ADR-0088)

Two distinct things are inherited, and conflating them is a category error this ADR corrects:

- **Reused verbatim, never forked**: `harness/stats.py` (`wilson()`, `disjoint()`) and
  `MIN_VALID_N = 20`, plus the ADR-0088 pre-registration protocol: provider-family split, N
  valid per cell, a threshold frozen before data is collected.
- **A template, not a shared callable**: `suites/steering/report.py`'s `evaluate_gate()` computes
  a specific three-arm render-vs-buried lift. It is bespoke to that hypothesis, not a general A/B
  scorer. The recursive loop's per-experiment gate (steer-adherence, notification-prompting
  recall/precision, or any future target) authors its **own** gate predicate, in the same shape
  (Wilson-CI-backed, provider-split, `N >= 20`, pre-registered before data) but with its own
  threshold. `evaluate_gate()` is the reference implementation to copy the shape from; it is never
  imported for a differently-shaped metric, and it is never edited to serve one.
- Backend is a logged dimension of every cell (see [Cell kinds](#3-run-cell-semantics-and-the-two-cell-kinds)
  above); the gate never pools across backends within a provider-family comparison.

### 7. The recursive loop

The loop is a driver on top of the seam: a variant proposer (the model) generates candidate
prompt or notification-surfacing variants; the harness runs a sandboxed A/B over
`{variants} x {cells} x {providers} x {backends}`; an objective scorer decides adherence; a
pre-registered gate on held-out data decides promotion; a human approves. It is gated: this
section does not start building until (a) the seam ships and (b) the stats-reuse and
pre-registration discipline is proven by replicating the ADR-0088 steer-adherence table through
the new seam.

Two targets are defined for the first two loop slices:

1. **Lionagi prompting system**: replicate the ADR-0088 steer-adherence fixture (op1 drafts a
   plan, a steer redirects it, op2 executes, `is_steer_adherent()` scores the artifact) as the
   first end-to-end proof that variants can be proposed, measured, and gated through the seam.
2. **Notification-prompting surface**: defined here, not left open, per the advisor's pinning.
   The fixture is a run trace carrying a labeled ground-truth critical event (a stuck job, a
   terminal-status flip, a failed op). The notification surface produces a summary or alert. The
   machine-checkable cell is **critical-event surfacing recall/precision**: does the surface
   include the ground-truth-critical fact as a token-checkable assertion, above the fold and out
   of noise? This is structurally identical to the steering fixture's token-presence check, so it
   reuses the same stats layer and gate-authoring template. The subjective dimension, whether the
   phrasing is *good*, is explicitly deferred to a human and is never automated into the loop.

**Anti-reward-hacking fences (normative):**

1. **Objective scorer only.** The promotion gate MUST be machine-checkable (token, AST, exit-code,
   or metric-based) and MUST NOT be an LLM judge. `benchmarks/orchestration/score.py` already
   imports an LLM judge (`harness/judge.py`) for a different purpose (review-quality scoring); if
   the loop reused that pattern, a variant could win by learning to flatter the judge rather than
   by genuinely improving, and the judge shares a model family with the proposer, making
   proposer-judge collusion a live risk. `suites/steering/report.py`'s token/structural check
   (`is_steer_adherent()`) is unfoolable by construction and is the model to extend, not
   `harness/judge.py`. Any dimension that genuinely needs subjective judgment (some
   prompt-quality axes do) is out of the automated loop and goes to a human explicitly.
2. **Train/holdout fixture split.** The proposer tunes on train fixtures; the promotion gate
   evaluates lift only on held-out fixtures the proposer never sees. This is the fixture-overfitting
   fence: the loop is effectively gradient descent on fixtures by an LLM, so this hygiene is
   mandatory, not optional.
3. **Pre-registered gate predicate, frozen before data.** No post-hoc threshold tuning, inheriting
   ADR-0088's pre-registration protocol.
4. **Human-approved promotion.** The model proposes variants; a human approves the fixture set,
   the gate predicate, and the promotion itself. Auto-promotion is off until a variant class has
   cleared repeated human review.
5. **A machine-readable decision record per variant**, keyed
   `(variant-hash, fixture-set, provider, backend)`.

### 8. Cost and resource governance

A recursive loop on metered Daytona, multiplied by iterations and trials, is a real financial and
disk-floor risk. This ADR requires: a per-loop hard budget cap (`trials x iterations x est. $/trial`);
a per-backend circuit breaker that halts on error-rate or spend threshold; auto-halt on either
cap; and heavy artifacts written to external storage rather than the internal disk floor. **The
spend ceiling and any unattended-cadence spend rate are Ocean's call**: this ADR frames the
decision, it does not pick the number (see [Open questions](#open-questions-for-ocean)).

### 9. Observability harvest

An unmerged branch (host-side observability mirror: sandbox protocol, bridge, entry, and run
modules, plus tests green as of early June) makes a sandboxed run appear identically in `li
monitor`. It is named here and harvested in its own slice with a re-green gate (its tests must be
re-verified after rebase before being trusted), not inlined into the seam slice. It is not
required for slice 1's credibility, which rests on the committed provider-by-arm adherence table,
not on a monitor view. It becomes load-bearing once the loop runs unattended (slice 3).

### 10. Determinism: no replay cache in the measurement path

Provider responses are never cached for cheap re-scoring inside the measurement path: caching
would measure the cache, not the model. Nondeterminism is handled by raising N per cell (per
ADR-0088) and fixing temperature/seed where the provider allows it. Caching is permitted only for
re-running the scorer over already-captured artifacts (free, deterministic, no provider call
involved), never for the provider call under test.

## Consequences

**Positive**

- One contract absorbs backend divergence in `provision()`/`capabilities()`; every existing
  measurement-loop caller (`run_once()`) grows a backend selector instead of a set of backend-name
  branches.
- `daytona.py` and `sandbox.py` keep their current callers and behavior; the seam wraps them
  rather than replacing them.
- The prompt-cell/exec-cell split removes an entire class of unnecessary secret injection and
  measurement bias for the two targets this ADR names first.
- The three-ADR overlap (0079, 0080, and a hypothetical new one) collapses into one document with
  one lineage story.
- The anti-reward-hacking fences make the recursive loop's evidence trustworthy on the same terms
  as ADR-0088's steering measurement, rather than introducing a second, weaker measurement
  standard.

**Negative**

- The seam is new surface area (`lionagi/tools/sandbox_backend.py` or equivalent) that must be
  exercised without launching Daytona in CI, or its tests will be slow and flaky.
- Deferring flow/play integration means the seam's second consumer, and therefore the pressure
  test of whether `capabilities()` is sufficiently complete, does not exist yet. A second
  consumer may reveal gaps in the contract that this ADR could not anticipate.
- Docker is not optional insurance: it is required to satisfy the zero-managed-vendor constraint,
  so it is a second backend to maintain and test against from day one of slice 2, not a deferred
  nice-to-have.
- Supporting Codespaces at the seam level ahead of building its adapter means `capabilities()`
  must express a cold-start class wide enough to cover it (minutes-scale) without that width
  degrading the sub-100ms/seconds-scale reporting the day-one backends need.
- The loop's human-approval requirement bounds its throughput; it is deliberately not a
  fully-autonomous promotion pipeline in this ADR.

## Alternatives Considered

| Alternative | Trade-off |
|---|---|
| Adopt `llm-sandbox` as the contract base | Covers only Docker/Podman/K8s; no Daytona or Apple Container adapter exists, so both would be hand-written anyway, for zero savings, while coupling a measurement-critical seam to an external project's release cadence. |
| Adopt LangChain Deep Agents' `BaseSandbox` shape | Its `execute(command)`-only contract drops `capabilities()`, forcing backend branching back into callers: the leak this seam exists to prevent. Kept as convergent validation of the four-verb shape, not as the contract. |
| Promote the seam into `lionagi/substrate/` now | Cosmetic package move ahead of a second consumer; risks becoming the exact vaporware package ADR-0079 never filled. Deferred until flow/play integration is taken up. |
| Revise ADR-0079 and ADR-0080 in place instead of superseding | Leaves three overlapping Proposed sandbox designs live at once, which the packet and this ADR both judge worse than one document with an explicit lineage note. |
| Ship the recursive loop as a sibling ADR gated on the seam | Considered, but Ocean framed the loop as the product; exiling it to a second document while it depends entirely on this seam's contract would separate a decision from its dependency without a real benefit. |
| All backends (worktree, Daytona, Docker, Apple Container, Codespaces) day one | Apple Container's teardown tax and ~3.6x cold-start penalty are not justified by a workload that does not need microVM isolation, and a Codespaces adapter is real implementation work with no day-one consumer; staged rollout matches backend cost/benefit to when each is actually needed. |
| Exclude Codespaces from the roster entirely | Rejected: Ocean named it explicitly, and its cold-start class is a batch/non-interactive profile `capabilities()` can express honestly rather than a shape the seam should refuse to acknowledge. Supported-but-not-loop-default is the position that keeps the seam honest about what Codespaces is good for without making it the default driver. |
| Use `harness/judge.py`'s LLM judge as the loop's promotion scorer | Reward-hackable by construction, and shares a model family with the proposer, creating a collusion risk. Rejected in favor of machine-checkable scorers only. |
| Cache provider responses for cheap re-scoring | Would measure the cache, not the model, defeating the point of the measurement. Rejected inside the measurement path; permitted only for deterministic re-scoring of already-captured artifacts. |

## Implementation fences

- **MAY**: add a `SandboxBackend` Protocol, `Handle`, and `capabilities()` (new module under
  `lionagi/tools/`); wrap `daytona.py` as the `daytona` backend; add a `run_cell` leg to
  `sandbox.py`'s worktree session as the `local_worktree` backend (host-side subprocess in
  `worktree_path`); grow `runner.py:run_once()` a backend selector; author per-experiment gate
  predicates that import `harness/stats.py`; add a Docker backend in slice 2; declare Codespaces
  in `capabilities()`-facing documentation as a supported, minutes-scale-cold-start backend even
  before its adapter ships; add the recursive loop driver in slice 2 behind all five
  anti-reward-hacking fences.
- **MAY NOT**: edit `harness/stats.py` or `suites/steering/report.py` (`evaluate_gate()` is a
  template to copy the shape from, never an import target for a differently-shaped metric); inject
  provider secrets into prompt-cells; pool cells across backends within a provider-family
  comparison; use an LLM judge as the loop's promotion scorer; auto-promote a variant without
  human approval; build `lionagi/substrate/` or the flow/play `DependencyAwareExecutor` wiring
  under this ADR; wire Apple Container in v1; ship slice 2 without a zero-managed-vendor backend
  (the hard constraint in [Backend roster](#5-backend-roster)); select Codespaces as the loop's
  default backend, in v1 or later, without a separate decision to do so; run the loop without a
  budget cap and a per-backend circuit breaker; cache provider responses inside the measurement
  path.
- **Verify by**: (1) a fake-backend test exercising provision to run_cell to collect to teardown
  for both cell kinds without launching Daytona; (2) a `capabilities()`-driven degradation test
  proving callers never branch on a backend name; (3) an end-to-end replication of the ADR-0088
  steer-adherence table through the new seam, producing the same provider-by-arm Wilson-CI
  artifact, slice 1's fidelity gate; (4) a loop dry run showing holdout-gated promotion, a
  machine-readable decision record, and a budget-cap halt actually firing.

## Measurement gates (pre-registered, per ADR-0088)

- **Seam gate (slice 1)**: the new seam reproduces the ADR-0088 steer-adherence numbers within CI
  overlap of the direct (non-seam) harness path. This is a fidelity check, not a new statistical
  claim; it proves the seam does not distort measurement.
- **Loop gate (per experiment, authored fresh)**: variant lift over baseline at or above a
  pre-registered threshold, Wilson-CI-disjoint from baseline, on at least 2 of 4 provider
  families, `N >= 20` valid trials per cell, evaluated on held-out fixtures, holding backend
  constant per comparison. The threshold is frozen before data collection for each experiment.

## Slice plan

- **Slice 1, seam + fidelity.** `SandboxBackend` Protocol, `Handle`, `capabilities()`; worktree
  `run_cell`; Daytona wrap; a `runner.py:run_once()` backend selector; reproduce the ADR-0088
  adherence table through the seam. Backends: worktree/host and Daytona. **Goes to the Leo
  spec-gate before this slice merges.**
- **Slice 2, zero-managed-vendor constraint + first loop target.** Docker backend, which satisfies
  the hard constraint that the loop never depend solely on a managed vendor; the recursive loop
  driver with an objective scorer, train/holdout split, human-approved promotion, and a budget
  cap; first target is the lionagi prompting system (steer-adherence variants).
- **Slice 3, notification target + unattended observability.** Notification-prompting
  critical-event-surfacing fixtures and cell; harvest the observability bridge (its own re-green
  gate) so `li monitor` sees loop runs; unattended cadence behind the circuit breaker.
- **Deferred past v1 (adapter work, not open questions).** A Codespaces adapter: the seam already
  declares it a supported, minutes-scale-cold-start backend (see
  [Backend roster](#5-backend-roster)), but no adapter code ships in slices 1 through 3, and it is
  never wired as the loop's default.
- **Deferred (own future ADRs, not this one).** Flow/play production remote execution (the
  ex-ADR-0080 `DependencyAwareExecutor` integration); an Apple Container backend, if vendor risk
  ever forces reconsidering it.

## Open Questions for Ocean

- **Spend ceiling.** A per-loop hard budget cap and an unattended-cadence spend rate are
  resource decisions: frame is "cap at $X per loop, halt at Y% error rate; unattended runs only
  under the circuit breaker." This ADR does not set X or Y.
- **Apple Container in the roster, ever?** v1 says no, on need (it buys microVM isolation this
  workload does not use, at a real teardown and cold-start cost). If sovereignty or vendor-risk
  strategy should override that, that is a strategic call this ADR does not make.
- **Secrets broker vs. reference for exec-cells.** Exec-cells need provider secrets in the box;
  whether that is a broker indirection or a reference the backend resolves (rather than a raw env
  var) is inherited as an open question from ADR-0080 and is not resolved here.

## References

- `lionagi/tools/daytona.py`
- `lionagi/tools/sandbox.py`
- `benchmarks/orchestration/harness/runner.py`
- `benchmarks/orchestration/harness/stats.py`
- `benchmarks/orchestration/harness/judge.py`
- `benchmarks/orchestration/suites/steering/report.py`
- `benchmarks/orchestration/suites/steering/fixture.py`
- `benchmarks/orchestration/score.py`
- `benchmarks/orchestration/_daytona_smoke.py`
- `benchmarks/orchestration/_sandbox_smoke.py`
- [ADR-0079](ADR-0079-substrate-executor-provider-interface.md)
- [ADR-0080](ADR-0080-remote-sandbox-substrate-execution.md)
- [ADR-0088](ADR-0088-flow-steering-mechanisms.md)
