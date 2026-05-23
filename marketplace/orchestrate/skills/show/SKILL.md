---
name: show
description: >
  Orchestrate multi-play shows: decompose a complex goal into sequential plays
  (each a li play invocation), gate each output for quality, adapt the plan based
  on results, and merge work into an integration branch. Shows are first-class
  entities in Lion Studio with dedicated UI at /shows.
allowed-tools: [Bash, Read, Write, Glob, Grep]
---

# show

Orchestrate a complex goal as a sequence of gated plays. Each play is one `li play`
invocation running in its own git worktree. The show coordinates: plan → fire →
gate → merge → adapt → repeat.

## Shows vs flows — pick the right tool

| Dimension | `li o flow` | show (this skill) |
|---|---|---|
| Duration | 5-30 min | 1-4 hours |
| Unit of work | Single agent op | Full `li play` invocation |
| Branching | Single DAG | Each play gets its own worktree + branch |
| Human gate | Optional critic node | Between every play |
| Replanning | Control-node triggered | After every play based on verdict |
| Studio UI | `/runs` | `/shows` (dedicated PlayDag view) |

Use a show when: the goal requires multiple independent bodies of work, each needing
its own branch and quality gate, with a plan that adapts based on intermediate results.

Use a flow when: a single orchestrator can plan and execute everything in one session.

## The 8-step procedure

1. **Plan** — Write `_show.md` (goal, plays, deps). Create the integration branch off `main`.
2. **Pick** — Select the next play: `pending` status, all `depends_on` plays are `merged`.
3. **Worktree** — `git worktree add -b show/<topic>/<play>` off the integration branch.
4. **Fire** — Run `li play <playbook> <prompt> --yolo --bypass --invocation $INV_ID`.
5. **Gate** — Run `li agent -a reviewer` with the acceptance criteria; write `_verdict.json`.
6. **Decide** — `gate_passed=true` → merge; attempt 1 fail → redo; attempt 2 fail → escalate.
7. **Adapt** — Update `_show.md` decisions; adjust downstream `_intent.md` if outputs changed.
8. **Final gate** — When all plays are terminal, run show-level gate; open PR if passed.

For the complete procedure with code examples, see [procedure.md](procedure.md).

## Common mistakes

```
❌ Creating a show for a task one flow can finish — overkill, use li o flow
❌ Forgetting --yolo --bypass on li play — interactive prompts stall the play
❌ Using git merge without --no-ff — loses play boundary in git history
❌ Skipping gate on attempt 2 — gate is mandatory before escalate decision
❌ Firing plays before integration branch exists — plays have nowhere to merge
❌ Touching _ABORT after all plays have launched — it won't stop them (use kill)
❌ Keeping play worktrees after merge — they accumulate; remove after merge
❌ Not updating _show.md decisions after each play — Studio shows stale plan
```

## Companion files

- [data-model.md](data-model.md) — `shows`/`plays` table schema, status enums, Studio pages
- [procedure.md](procedure.md) — full 8-step procedure with code, workspace layout, `_show.md` format
- [gate-protocol.md](gate-protocol.md) — gate agents, verdict JSON, decision logic, abort/resume

## Source code reference

| File | Purpose |
|---|---|
| `lionagi/state/schema.sql` line ~218 | `shows` and `plays` table DDL |
| `apps/studio/server/services/shows.py` | List, detail, import, SSE watcher |
| `apps/studio/server/routers/shows.py` | REST + SSE endpoints |
| `apps/studio/frontend/app/shows/page.tsx` | Show list page |
| `apps/studio/frontend/app/shows/[topic]/page.tsx` | Show detail page |
| `apps/studio/frontend/app/shows/[topic]/components/PlayDag.tsx` | DAG visualization |
| `apps/studio/server/config.py` | `LIONAGI_SHOWS_ROOT` env var |
| `lionagi/cli/invoke.py` | `li invoke start/end/list` |
