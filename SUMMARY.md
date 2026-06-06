# orch-substrate — DESIGN play SUMMARY

**Branch**: `show/lionagi-sweep/orch-substrate` (based on `main`)
**Commit**: `a0527ac42` — `docs(adr): substrate ADR cluster 0077-0080 + role→substrate routing slice`
**Status**: committed locally, **NOT pushed, no PR** (per task).
**Critic verdict**: APPROVE-WITH-FIXES → the one blocking fix (MAJ-1 ADR numbering
collision) was applied before this commit. See `critic/verdict.md`.

---

## ADRs written (all status: Proposed)

| ADR | Title | Issue |
|-----|-------|-------|
| **ADR-0077** | Substrate Executor Provider Interface | #1196 |
| **ADR-0078** | Remote Sandbox Substrate Execution | #1195 |
| **ADR-0079** | Configurable Flow Planning | #1197 |
| **ADR-0080** | Role→Substrate Routing Policy | #1210 |

Four ADRs, one per concern, cross-referenced into a coherent set. Numbering scheme
(executor=0077, sandbox=0078, planning=0079, routing=0080) is exactly what the task
("next number: ADR-0077") and the inter-ADR links assume.

---

## Key design decisions per concern

### ADR-0077 — Executor providers (#1196)
- Introduce a shared substrate type `ExecutionTarget` + `SubstrateStreamEvent`
  (proposed `lionagi/substrate/types.py`) that the existing `claude_code`/`codex`
  CLI providers retro-fit into via the **existing** `EndpointRegistry` /
  `build_imodel_from_spec(...)` path — **no parallel executor registry**.
- A non-LLM / arbitrary process plugs in as a `process/<profile>` provider that
  emits the same `SubstrateStreamEvent` stream, so flow/fanout consume one
  contract regardless of whether the op is an LLM call or a shell process.
- Alternatives weighed: new dedicated executor registry (rejected — duplicates
  matching/aliases/registration already in `EndpointRegistry`); overloading the
  provider table only (rejected — no clean non-LLM seam).

### ADR-0078 — Remote sandbox (#1195)
- **Extend, not replace**: add a `backend: SandboxBackend = "local_worktree" |
  "daytona"` field + `execution_target()` to `SandboxSession`
  (`lionagi/tools/sandbox.py`); default path stays local and unchanged. Daytona
  backend reuses the real `DaytonaSandbox` wrapper (`client.create`, `git.clone`,
  `create_session`/`exec_stream`, `@@SIG@@` framing) cited at exact lines.
- New `sandbox_exec_stream(...)` / `DaytonaSandbox.run_lionagi_command(...)` stream
  typed `SubstrateStreamEvent`s back to the host so monitor/artifact consumers keep
  working remotely.
- "Replace `SandboxSession`" and "PlayRunner-only (ADR-0057)" alternatives both
  explicitly rejected (too large for this tranche).

### ADR-0079 — Configurable planning (#1197)
- Exact injection point named: `lionagi/cli/orchestrate/flow.py:513-516` (the first
  `await plan(env.orc_branch, prompt, roles=roster, dag=True, …)`), replaced by a
  `resolve_flow_plan(...)` seam that injects planner **model / strategy / pattern**
  and accepts a **user-supplied pre-authored plan** (bypass), preserving the #1236
  empty-plan retry behavior.
- Clean break from the removed `FlowPlan/FlowAgent` is respected (`patterns.py:56`
  "plan IS a `list[TaskAssignment]`" contract) — reintroducing them was rejected.

### ADR-0080 — Role→substrate routing (#1210)
- Roles carry an **optional routing target** (no hardcoded provider in shipped
  packs — honors `casts/pack.py` `RoleConfig` default `None` and the
  `test_pack.py:57` compat test). Routing maps onto the **same** executor-provider
  path from ADR-0077 (`RoleConfig.model → build_imodel_from_spec → EndpointRegistry`),
  not a competing mechanism.
- Hardcoding a provider table in `default.yaml` was rejected (breaks the pack
  compatibility test).

---

## Implemented vs design-only

- **Design-only**: ADR-0077, ADR-0078, ADR-0079 (executor interface, remote
  sandbox, configurable planning) — markdown ADRs, no code shipped.
- **Implemented — #1210 thin reference slice (shipped)**: roles carry an optional
  `--pack` routing target consumed by flow/fanout DAG planning.
  - Touched: `lionagi/cli/orchestrate/__init__.py`, `_orchestration.py`, `flow.py`,
    `fanout.py`; test `tests/cli/orchestrate/test_flow_planning.py`.
  - Verified (critic, independently re-run in worktree):
    `uv run pytest tests/cli/orchestrate/test_flow_planning.py tests/casts/test_pack.py
    tests/cli/orchestrate/test_role_config.py` → **30 passed in 6.06s**;
    `uv run ruff check` / `ruff format --check` → clean. Scope guardrails held
    (`default.yaml`, `Role`, casts `Profile` untouched).

---

## Open questions for Ocean

**Executor (0077)**: naming for arbitrary process specs (`process/<profile>`?);
where process profiles live (ADR-0060 resolver vs casts pack); should nonzero
process exits fail the op; is a hardcoded `process` provider import acceptable;
what secrets are allowed in `ExecutionTarget.env`.

**Remote sandbox (0078)**: per-worker vs whole-flow isolation for the first cut;
retention policy for failed Daytona sandboxes; which secrets may be injected into
`DaytonaSandbox.create(env=...)`; whether remote artifacts download into the local
`RunDir`; is Daytona the only approved remote backend this tranche; auto-fail on
nonzero remote exit.

**Planning (0079)**: CLI flag naming (`--planner-model` vs `--planning-model`,
`--plan`); arbitrary pattern files vs allow-list on first release;
`max_width`/`max_depth` fail vs warn-and-retry; should planner model selection also
apply to `fanout` now; does `PlanningEngine` take `PlanningConfig` in the same cut.

**Routing (0080)**: `--pack` filesystem-paths-only vs named packs in the first
slice; should the proposed provider table ship separately; accepted default
providers/model aliases per role; serialization of `execution_target` (nested pack
object vs named ref); whether selected packs change the planner roster's displayed
model labels.

---

## Fix applied this commit (MAJ-1)

The architect's two ADRs were committed (`3d1fd1a86`) as `ADR-0079`/`ADR-0080`,
colliding with architect-2's `ADR-0079`/`ADR-0080` and dangling architect-2's
`[ADR-0077]/[ADR-0078]` links. Surgical resolution per critic:
`git mv` 0079→0077 (executor) and 0080→0078 (sandbox); fixed both H1 lines, the two
inter-ADR `Related` links, and three prose `ADR-0079→ADR-0077` references in the
sandbox ADR. After the rename all cross-references resolve; planning/routing keep
0079/0080. Markdownlint MD018 (two paragraph lines starting `#1197`/`#1210`) fixed
by prefixing "Issue ". Pre-commit (ruff, markdownlint, eof-fixer) all pass.

`git status` confirmed **clean** after the commit (only this untracked `SUMMARY.md`
report remains, which is the coordinator deliverable, not part of the commit).
