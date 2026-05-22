# memory

MEMORY.md hygiene and auto-memory bootstrap for Claude Code sessions.

## What's inside

- **skills/migrate-memory** — Maintains the auto-memory space: prunes stale files,
  condenses the MEMORY.md index (kept under 200 lines), and optionally migrates
  content to khive persistent memory. Quality gates: all files read before deletion,
  no orphaned files, key sections preserved.

## Deferred (v2.1)

- **skills/memory-recall** — Deferred to v2.1. Will be rewritten from scratch against
  `~/.lionagi/runs/` and Studio APIs with no external dependencies. The previous
  implementation required `mcp__khive__recall` / `mcp__khive__search` (khive MCP),
  which conflicts with the marketplace v2 goal of no extra dependencies for the 5
  catalog plugins. Removed from Phase 0 entirely.

## Install

```
claude /plugin marketplace add ohdearquant/lionagi
claude /plugin install memory@lionagi
```

## Quick start

Run `/migrate-memory` periodically (or when MEMORY.md exceeds 200 lines) to prune
and condense your auto-memory space.

## See also

- ADR-0003 (docs/adrs/ADR-0003-claude-code-marketplace.md) — marketplace pattern
