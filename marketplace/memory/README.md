# memory

Memory recall, MEMORY.md hygiene, and auto-memory bootstrap for Claude Code sessions.

## What's inside

- **skills/memory-recall** — Automatically searches memory when session context
  suggests relevant prior experience. Triggered silently: assess relevance → search
  memory → surface insights → connect to current context.
- **skills/migrate-memory** — Maintains the auto-memory space: prunes stale files,
  condenses the MEMORY.md index (kept under 200 lines), and optionally migrates
  content to khive persistent memory. Quality gates: all files read before deletion,
  no orphaned files, key sections preserved.

## Install

```
claude /plugin marketplace add ohdearquant/lionagi
claude /plugin install memory@lionagi
```

## Quick start

```
/memory-recall
```

`memory-recall` is typically auto-triggered at session start. Run `/migrate-memory`
periodically (or when MEMORY.md exceeds 200 lines) to prune and condense.

## See also

- ADR-0003 (docs/adrs/ADR-0003-claude-code-marketplace.md) — marketplace pattern
