# Marketplace Correctness Sweep — Summary

Branch: `show/lionagi-issue-sweep/marketplace-correctness`
Date: 2026-05-21

## Issues Fixed

| Issue | Title | Commits | Before | After |
|-------|-------|---------|--------|-------|
| #1000 | Stub mcpServers blocks | `c207debe2` | 2 `"type":"stub"` entries | 0 |
| #1001 | Stub plugins in install table | `6736bb025` | 2 stub rows in README table | 0 |
| #1002 | Shell injection in show redo path | `0f7fe1f8d` | 1 unsafe `$PROMPT` interpolation | 0 |
| #1021 | migrate-memory data-loss chain | `75b459705` | 4 sub-defects (ambiguity, no backup, forget_batch, collision) | 0 |
| #1022 | Dead khive verbs | `5a3f94650` | 18 dead verb call sites | 0 |
| #1023 | Ghost CLI subcommands | `69277c945` | 2 ghost `li o flow` invocations | 0 |
| #1024 | Nonexistent model codex/gpt-5.5 | `3692b9eff` | 15 `codex/gpt-5.5` references | 0 |
| #1025 | --yolo without --bypass | `bf8a899ae` | 5 bare `--yolo` sites | 0 |

## Pattern Counts (before → after)

| Pattern | Before | After |
|---------|--------|-------|
| `"type": "stub"` in plugin.json | 2 | 0 |
| `mcp__khive__(memory\|graph\|work)(` | 16 | 0 |
| `mcp__khive__communication(` | 2 | 0 |
| `memory\.(recall\|remember)(` | 11 | 0 |
| `work\.tasks(` | 1 | 0 |
| `li o flow (validate\|run)` | 2 | 0 |
| `codex/gpt-5\.5` | 15 | 0 |
| `--yolo` without `--bypass` | 5 | 0 |
| `forget_batch` | 1 | 0 |

## #1022 — Converted Call Sites

All dead khive verb forms replaced with live direct verbs.

| File | Old Form | New Form |
|------|----------|----------|
| `memory-recall/SKILL.md` | `mcp__khive__memory(action="recall", query=...)` ×5 | `mcp__khive__recall(query=...)` |
| `memory-recall/SKILL.md` | `mcp__khive__memory(action="recall", ..., lambda_id=...)` ×2 | `mcp__khive__recall(..., entity_names=[...])` |
| `memory-recall/SKILL.md` | `mcp__khive__graph(action="search", ...)` | `mcp__khive__search(type="entity", ...)` |
| `memory-recall/SKILL.md` | `mcp__khive__graph(action="link", ...)` | `mcp__khive__link(...)` |
| `memory-recall/SKILL.md` | `memory.recall(` (API reference signature) | `mcp__khive__recall(` |
| `memory-recall/SKILL.md` | `mcp__khive__request('[memory.recall(...), graph.search(...)]')` ×2 | explicit separate verb calls |
| `migrate-memory/SKILL.md` | `mcp__khive__memory(action="remember", ...)` | `mcp__khive__remember(...)` |
| `migrate-memory/SKILL.md` | `forget_batch` reference | `mcp__khive__delete(type="memory", id="...")` per-item |
| `progress-research/SKILL.md` | `memory.recall("...", limit=N)` ×2 | `mcp__khive__recall(query="...", limit=N)` |
| `progress-research/SKILL.md` | `work.tasks(assignee=..., limit=5)` | `mcp__khive__next(assignee=..., limit=5)` |
| `summarize/SKILL.md` | `memory.remember(` ×4 | `mcp__khive__remember(` |
| `summarize/SKILL.md` | `memory.recall(query=...)` | `mcp__khive__recall(query=...)` |
| `status/SKILL.md` | `mcp__khive__work(action="tasks", assignee=...)` ×5 | `mcp__khive__next(assignee=..., limit=10)` |
| `status/SKILL.md` | `mcp__khive__work(action="tasks", filter="inbox")` | `mcp__khive__list(type="work", status="inbox", limit=10)` |
| `status/SKILL.md` | `mcp__khive__work(action="tasks", filter="next")` | `mcp__khive__list(type="work", status="next", limit=10)` |
| `status/SKILL.md` | `mcp__khive__communication(action="list", ..., status="unread")` | `mcp__khive__inbox(status="unread", limit=20)` |
| `status/SKILL.md` | `mcp__khive__communication(action="list", channel="forum", ...)` | `mcp__khive__list(type="comm", channel="forum", limit=20)` |
| `status/SKILL.md` | `work.assign MCP action` (interpretation table) | `mcp__khive__assign` |

## #1023 — Ghost CLI Replacements

| File | Ghost Command | Replaced With |
|------|---------------|---------------|
| `flow-it/SKILL.md` | `li o flow validate -f {path}` | `li o flow -f {path} --dry-run` |
| `flow-it/SKILL.md` | `nohup li o flow run -f tools/flows/{name}.yaml > /tmp/flow_{name}.log 2>&1 &` + `echo "Flow PID: $!"` | `li o flow -f tools/flows/{name}.yaml --background --save .khive/flows/{name} --yolo --bypass` + `echo "Flow saved to: .khive/flows/{name}"` |

## #1021 — Data-Loss Defects Fixed

| Sub-defect | Fix Applied |
|------------|-------------|
| (a) MEMORY_DIR ambiguity | Phase 1 rewrites to resolve to absolute path; collision guard exits with code 2 if both repo-local and `~/.claude/projects/` memory dirs exist without explicit `--target` |
| (b) No backup before delete | Phase 1 always creates timestamped `${MEMORY_DIR}.bak.$(date +%s)` backup; Phase 3 Act refuses destructive ops if backup var is unset |
| (c) `forget_batch` reference | Replaced with `mcp__khive__delete(type="memory", id="...")` per-item loop |
| (d) Path collision | Collision guard: script exits 2 if multiple candidate dirs found and `--target` not supplied |

Added `## Recovery` section with step-by-step restore procedure using the timestamped backup.

## #1002 — Shell Injection Fix

Replaced direct `$PROMPT` variable interpolation in `li play` argument with tmpfile approach:

```bash
REDO_PROMPT_FILE=$(mktemp "$SHOW_DIR/$PLAY/.redo_prompt.XXXXXX")
printf '%s' "$FEEDBACK_AND_ORIGINAL" > "$REDO_PROMPT_FILE"
"$LI" play <playbook> "$(cat "$REDO_PROMPT_FILE")" --yolo --bypass ...
rm -f "$REDO_PROMPT_FILE"
```

This prevents shell metacharacters in `$FEEDBACK` from being interpreted by the shell before `li play` receives them.

## Blockers / Skipped

None. All 8 issues resolved within scope.

One non-scope observation: `lionagi/providers/openai/_config.py:88` contains a `gpt-5.5` string, but that file is in the core library path (NO CLI/CORE TOUCH constraint) and was correctly left untouched. The marketplace fix in #1024 covers only agent `.md` frontmatter files.
