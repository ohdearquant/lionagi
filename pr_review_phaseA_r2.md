# Delta Review: PR #1718 Phase A Round 2

Scope: reviewed only `5c81be9f8..e830a6544` plus resolution of the four round-1 blockers in `pr_review_phaseA.md`.

Verdict: REQUEST_CHANGES
Findings: 1 Blocker, 0 High, 0 Medium, 0 Low

## Blockers

### 1. Project-scoped Operations still silently hides Script and Flow sources

Evidence: `apps/studio/frontend/src/lib/run-model.ts:288` sets `includeGlobalSources = !project`, and `apps/studio/frontend/src/lib/run-model.ts:300-311` replaces shows and engine runs with empty arrays whenever a project is selected. That is a reasonable phase-A containment strategy for sources with no project column, but it does not emit any scope metadata or warning. The Operations source filter still always offers `Script` and `Flow` at `apps/studio/frontend/src/routes/index.tsx:292-305`; the degraded banner only renders `sourceErrors` at `apps/studio/frontend/src/routes/index.tsx:341-344`; and the empty state at `apps/studio/frontend/src/routes/index.tsx:357-361` only mentions partial data when `sourceErrors` is non-empty. Since scoped suppression is intentional and not recorded as `sourceErrors`, a user can select a project and `Script` or `Flow` and get "No runs match the current filters" even though those sources are being withheld until "All projects".

Why this matters: ADR-0093's "project lens scopes Operations fully" can be satisfied by hiding unscopable global sources under an active project, but only if the UI makes the scope boundary explicit. The current behavior gives a false complete-view signal and still intersects the round-1 partial-data blocker: operators cannot distinguish "there are no script/flow runs" from "this project lens intentionally omits global script/flow sources."

Suggested fix: When `project` is active, either remove/disable `Script` and `Flow` source filter options with clear copy, or return scope notices from `aggregateRuns` and render a visible banner/empty-state message such as "Script and Flow runs have no project field and are shown only under All projects." The notice should appear even when no source request failed.

## Blocker Resolution Check

- Blocker 1, reachable "invocation" copy: resolved for reachable phase-A surfaces. The still-reachable detail route now renders `Run` title/error headers at `apps/studio/frontend/src/routes/invocations/$id.tsx:53` and `apps/studio/frontend/src/routes/invocations/$id.tsx:60`, "Sessions in this run" at `apps/studio/frontend/src/routes/invocations/$id.tsx:103`, and "No sessions spawned under this run yet" at `apps/studio/frontend/src/routes/invocations/$id.tsx:121`. The engine launch success card says "run" and "Operations" at `apps/studio/frontend/src/routes/engines/index.tsx:354-356`, and `errors.loadInvocation` says "Failed to load run" at `apps/studio/frontend/src/lib/copy.ts:105`. The remaining `Invocations` list copy is behind unconditional `/invocations/` and `/runs/` `beforeLoad` redirects, or is internal API/model plumbing.
- Blocker 2, full project scoping: partially resolved. The data leak is fixed by excluding shows and engine runs under project scope (`apps/studio/frontend/src/lib/run-model.ts:288-311`), but the omission is silent, so the blocker remains in the narrower form above.
- Blocker 3, bounded fan-out: resolved. `mapWithConcurrency` preserves result order by index and propagates uncaught rejections through `Promise.all` at `apps/studio/frontend/src/lib/run-model.ts:77-91`. The aggregation now caps schedules to 30, schedule runs to 20 per schedule, shows to 20, and fan-out concurrency to 5 at `apps/studio/frontend/src/lib/run-model.ts:270-273`, `apps/studio/frontend/src/lib/run-model.ts:323-338`, and `apps/studio/frontend/src/lib/run-model.ts:343-357`.
- Blocker 4, partial-failure surfacing: resolved for actual source failures. `aggregateRuns` returns `{ runs, sourceErrors }`, records per-source failures at `apps/studio/frontend/src/lib/run-model.ts:290-311`, `apps/studio/frontend/src/lib/run-model.ts:327-340`, and `apps/studio/frontend/src/lib/run-model.ts:351-360`, and the Operations page mounts the partial-data banner at `apps/studio/frontend/src/routes/index.tsx:341-344`. Live poll hard failures keep the prior data and show a stale banner at `apps/studio/frontend/src/routes/index.tsx:146-157` and `apps/studio/frontend/src/routes/index.tsx:347-351`.

## Commands Run

- `rg -n -i "invocation" apps/studio/frontend/src apps/studio/frontend/public apps/studio/frontend/index.html`
- `npm run typecheck && npm run lint && npm run test && npm run build` from the worktree root: failed immediately with `ENOENT` because there is no root `package.json`.
- `npm run typecheck && npm run lint && npm run test && npm run build` from `apps/studio/frontend`: passed. Lint exited 0 with 6 warnings; Vitest passed 86/86; production build passed with existing dynamic-import and chunk-size warnings.

Domain utility: LOW - khive recall returned the same ADR-0093 review principle from round 1, and knowledge.suggest returned no composed domains.

VERDICT: REQUEST_CHANGES - blocker 1
