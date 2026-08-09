---
name: show
description: >
  Orchestrate multi-play shows: decompose a complex goal into sequential plays
  (each a play.submit run through the plugin's MCP server), gate each output
  for quality, adapt the plan based on results, and merge work into an
  integration branch. Shows are rows in Studio's state database, readable over
  its /api/shows endpoints. Studio's retired /shows route redirects to Fleet;
  there is no dedicated show page.
allowed-tools: [Bash, Read, Write, Glob, Grep]
---

# show

Orchestrate a complex goal as a sequence of gated plays. Each play is one run
of a saved playbook, fired through the plugin's MCP server and executed in its
own git worktree. The show coordinates: plan → fire → gate → merge → adapt →
repeat.

Firing and observing plays go through the `mcp__plugin_orchestrate_lion__request`
tool this plugin ships (see [procedure.md](procedure.md) for the exact calls).
Everything about git — worktrees, branches, merges — is plain git and always
runs as Bash; there is no MCP verb for it and none is needed. If you are
working inside a lionagi checkout with `li` on PATH, `li play` and `li agent`
fire the same work; `li wait` blocks until a run reaches a terminal state, the
way repeated `job.wait` calls do; and `li monitor`, `li runs` and `li kill`
observe and stop it. Treat the CLI as a local convenience, not the primary path.

## Shows vs flows — pick the right tool

| Dimension | `flow.submit` | show (this skill) |
|---|---|---|
| Execution | One dependency DAG | Multiple gated playbook runs |
| Unit of work | Planned assignment | Full playbook run (`play.submit`) |
| Branching | Single DAG | Each play gets its own worktree + branch |
| Human gate | Optional critic node | Between every play |
| Replanning | Worker-triggered reactive spawn requests | After every play based on verdict |
| Studio surface | Fleet | `/api/shows` endpoints; Fleet has no dedicated show page |

Use a show when: the goal requires multiple independent bodies of work, each needing
its own branch and quality gate, with a plan that adapts based on intermediate results.

Use a flow when: one dependency-aware run can plan and execute the work as a DAG.

## The 8-step procedure

1. **Plan** — Write `_show.md` (goal, plays, deps). Create the integration branch off `main`.
2. **Pick** — Select the next play: `pending` status, all `depends_on` plays are `merged`.
3. **Worktree** — `git worktree add -b show/<topic>/<play>` off the integration branch.
4. **Fire** — Call `play.submit` for the playbook; record the returned `run_id`.
5. **Gate** — Call `agent.submit` with the acceptance criteria; write `_verdict.json`.
6. **Decide** — `gate_passed=true` → merge; attempt 1 fail → redo; attempt 2 fail → escalate.
7. **Adapt** — Update `_show.md` decisions; adjust downstream `_intent.md` if outputs changed.
8. **Final gate** — When all plays are terminal, run show-level gate; open PR if passed.

For the complete procedure with the exact MCP calls, see [procedure.md](procedure.md).

## Common mistakes

```
❌ Creating a show for a task one flow can finish — overkill, use flow.submit
❌ Not checking each op's `ok` in the reply — a partial batch can carry a failed op silently
❌ Calling help and ops in the same request — the server refuses that; ask help first, separately
❌ Treating one bounded job.wait call as completion — repeat until all_terminal is true
❌ Using git merge without --no-ff — loses play boundary in git history
❌ Skipping gate on attempt 2 — gate is mandatory before escalate decision
❌ Firing plays before integration branch exists — plays have nowhere to merge
❌ Touching _ABORT after all plays have launched — it only blocks new launches; use job.kill for running ones
❌ Keeping play worktrees after merge — they accumulate; remove after merge
❌ Not updating _show.md decisions after each play — that file is the readable record, so a stale one misleads everyone after you
```

## Companion files

- [data-model.md](data-model.md) — `shows`/`plays` table schema, status enums, reading a show from Studio
- [procedure.md](procedure.md) — full 8-step procedure with the MCP calls, workspace layout, `_show.md` format
- [gate-protocol.md](gate-protocol.md) — gate agents, verdict JSON, decision logic, abort/resume

## Source code reference

| File | Purpose |
|---|---|
| `lionagi/state/schema.sql` (`CREATE TABLE shows`, `CREATE TABLE plays`) | `shows` and `plays` table DDL |
| `lionagi/studio/services/shows.py` | List, detail, import, SSE watcher, and the `/api/shows` routes |
| `lionagi/studio/registry.py` | Registers that service with the app |
| `lionagi/studio/config.py` | `LIONAGI_SHOWS_ROOT` and the root it falls back to |
| `apps/studio/frontend/src/components/shows/PlayDag.tsx` | DAG visualization component; present in the tree, imported by nothing |
| `lionagi/mcp/server.py`, `lionagi/mcp/verbs.py` | MCP verb registry — `play.submit`, `job.*` |
