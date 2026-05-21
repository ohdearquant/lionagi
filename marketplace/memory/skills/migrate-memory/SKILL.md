---
name: migrate-memory
description: >
  Organize auto-memory space — prune stale files, condense MEMORY.md, optionally
  migrate to khive recall. Suggest when: MEMORY.md exceeds 150 lines, memory files
  accumulate, or user asks to organize/clean memory.
allowed-tools: [Read, Write, Edit, Glob, Grep, Bash, mcp__khive__memory, mcp__khive__request, Agent]
---

# Memory Organization & Migration

Maintain the auto-memory space: prune stale files, condense MEMORY.md index,
and optionally migrate content to khive persistent memory.

## When to Use

- MEMORY.md exceeds 150 lines (200-line truncation limit)
- Memory files accumulate without pruning
- User asks to organize, clean, or consolidate memory
- Switching to khive recall-based architecture (optional)

## Architecture

```
MEMORY.md (index, <200 lines) ← loaded every session
  └→ *.md files (detail) ← loaded on demand via links
  └→ khive memory (semantic search) ← recalled via memory.recall()
```

All three layers coexist. MEMORY.md is the routing table — it tells Leo
what exists and where to find detail. Individual files hold the content.
khive memory is for cross-session semantic search.

## What stays in MEMORY.md (always loaded)

- Recovery protocol (how to bootstrap)
- Health gates (P0 — blocking)
- Core directive
- People table (key relationships + rules)
- Critical life items (immigration, financial, commitments)
- Alignment table + behavioral correction links
- Active work table
- Recall triggers (query templates)

## What goes in individual files

- Detailed project state (receipt numbers, specs, architecture)
- Feedback with context (why + how to apply)
- Reference data (API keys, restaurant lists, device IDs)

## What goes in khive memory (optional)

- Cross-session learnings (KEY_INSIGHT)
- Session summaries (episodic)
- Patterns discovered across projects (semantic)

## Organization Protocol

### Phase 1: Inventory

```bash
MEMORY_DIR="$(find ~/.claude/projects -path '*/memory/MEMORY.md' -exec dirname {} \;)"
ls -lhS "$MEMORY_DIR"/*.md
wc -l "$MEMORY_DIR/MEMORY.md"
```

### Phase 2: Audit

For each file (excluding MEMORY.md):
1. Read frontmatter (name, type, description)
2. Assess: ACTIVE | STALE | CONSOLIDATE
3. Check for dangling references in MEMORY.md

Use an Explore agent for bulk reading if >20 files.

### Phase 3: Act

**Stale files**: Delete + remove from MEMORY.md index.
**Consolidate**: Merge related files → update MEMORY.md link.
**Dangling refs**: Create missing files or remove broken links.
**Bloated MEMORY.md**: Move detail into individual files, keep 1-line pointer.
**Outdated info**: Update dates, day counts, completed milestones.

### Phase 4: Condense MEMORY.md

Target: **<180 lines** (safe margin under 200).

Techniques:
- Move detailed tracking (receipt numbers, timelines) to files
- Group behavioral corrections as compact link lists
- Condense tables (drop redundant columns)
- Remove past milestones that are complete
- Update stale dates to current values

### Phase 5: Verify

- `wc -l MEMORY.md` < 200
- All linked files exist
- No dangling references
- Key info (health, people, commitments) still present

### Phase 6: Optional khive migration

If khive memory is available and desired:

```python
mcp__khive__memory(
  action="remember",
  content="[name]: [content]",
  memory_type="semantic|episodic",
  importance=0.70-0.85,
  source="auto-memory-migration-YYYYMMDD"
)
```

Tag with consistent `source` for rollback via `forget_batch`.

## Quality Gates

- All files read before any deletion
- MEMORY.md stays under 200 lines
- No orphaned files (every .md linked or self-contained)
- Key sections preserved: health, people, alignment, active work
