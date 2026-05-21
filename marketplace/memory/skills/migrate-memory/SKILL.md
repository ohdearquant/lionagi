---
name: migrate-memory
description: >
  Organize auto-memory space — prune stale files, condense MEMORY.md, optionally
  migrate to khive recall. Suggest when: MEMORY.md exceeds 150 lines, memory files
  accumulate, or user asks to organize/clean memory.
argument-hint: '[--target /absolute/path/to/memory]'
allowed-tools: [Read, Write, Edit, Glob, Grep, Bash, mcp__khive__remember, mcp__khive__recall, mcp__khive__delete, Agent]
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
  └→ khive memory (semantic search) ← recalled via mcp__khive__recall()
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

Resolve `MEMORY_DIR` once, to an absolute path, before reading or mutating any
memory files. If more than one candidate exists, or if both a repo-local
`memory/` directory and Claude Code's `~/.claude/projects/.../memory/` directory
exist, stop and require `/migrate-memory --target /absolute/path/to/memory`.

```bash
# Set TARGET from --target if provided; leave empty otherwise.
TARGET="${TARGET:-}"
REPO_MEMORY_DIR="$(pwd -P)/memory"
mapfile -t CLAUDE_MEMORY_DIRS < <(
  find "$HOME/.claude/projects" -path '*/memory/MEMORY.md' -exec dirname {} \; 2>/dev/null | sort -u
)

if [ -n "$TARGET" ]; then
  MEMORY_DIR="$(cd "$TARGET" && pwd -P)"
elif [ -d "$REPO_MEMORY_DIR" ] && [ "${#CLAUDE_MEMORY_DIRS[@]}" -gt 0 ]; then
  printf 'ERROR: both repo-local memory/ and ~/.claude/projects/.../memory/ exist.\n' >&2
  printf 'Re-run with --target /absolute/path/to/memory.\n' >&2
  printf 'Candidates:\n  %s\n' "$REPO_MEMORY_DIR" "${CLAUDE_MEMORY_DIRS[@]}" >&2
  exit 2
elif [ "${#CLAUDE_MEMORY_DIRS[@]}" -eq 1 ]; then
  MEMORY_DIR="$(cd "${CLAUDE_MEMORY_DIRS[0]}" && pwd -P)"
elif [ "${#CLAUDE_MEMORY_DIRS[@]}" -gt 1 ]; then
  printf 'ERROR: multiple ~/.claude/projects/.../memory/ directories found.\n' >&2
  printf 'Re-run with --target /absolute/path/to/memory.\n' >&2
  printf 'Candidates:\n' >&2
  printf '  %s\n' "${CLAUDE_MEMORY_DIRS[@]}" >&2
  exit 2
else
  printf 'ERROR: no MEMORY.md found under ~/.claude/projects.\n' >&2
  exit 1
fi

case "$MEMORY_DIR" in
  "$HOME/.claude/projects/"*/memory|"$REPO_MEMORY_DIR") ;;
  *) printf 'ERROR: refusing unexpected memory target: %s\n' "$MEMORY_DIR" >&2; exit 2 ;;
esac

BACKUP_DIR="${MEMORY_DIR}.bak.$(date +%s)"
cp -r "$MEMORY_DIR" "$BACKUP_DIR"
printf 'Backup created: %s\n' "$BACKUP_DIR"

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

Before any delete, move, merge, or rewrite, confirm the backup exists:

```bash
[ -n "${BACKUP_DIR:-}" ] && [ -d "$BACKUP_DIR" ] || {
  echo "ERROR: backup missing; refusing destructive memory operation" >&2
  exit 3
}
```

**Stale files**: Delete + remove from MEMORY.md index only after the backup check passes.
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
mcp__khive__remember(
  content="[name]: [content]",
  memory_type="semantic|episodic",
  importance=0.70-0.85,
  source="auto-memory-migration-YYYYMMDD"
)
```

Tag with consistent `source` for rollback. There is no `forget_batch` verb; to undo a khive migration, recall by the migration `source`, record the returned IDs, and delete those IDs with `mcp__khive__delete(type="memory", id="...")`.

## Recovery

The inventory phase creates `BACKUP_DIR="${MEMORY_DIR}.bak.$(date +%s)"` before
any destructive action. To restore from a backup:

```bash
# 1. Pick the backup created for this run.
BACKUP_DIR="/absolute/path/to/memory.bak.<timestamp>"

# 2. Confirm both paths before replacing anything.
test -d "$BACKUP_DIR"
test -d "$MEMORY_DIR"
printf 'Restore %s -> %s\n' "$BACKUP_DIR" "$MEMORY_DIR"

# 3. Preserve the failed migrated state, then restore the backup.
FAILED_DIR="${MEMORY_DIR}.failed.$(date +%s)"
mv "$MEMORY_DIR" "$FAILED_DIR"
cp -r "$BACKUP_DIR" "$MEMORY_DIR"

# 4. Verify the restored index and links.
wc -l "$MEMORY_DIR/MEMORY.md"
find "$MEMORY_DIR" -maxdepth 1 -name '*.md' -print | sort
```

If khive memories were migrated too, recall by the migration source and delete
only those returned memory IDs:

```python
mcp__khive__recall(query="auto-memory-migration-YYYYMMDD", source="auto-memory-migration-YYYYMMDD", limit=100)
mcp__khive__delete(type="memory", id="<returned-memory-id>")
```

## Quality Gates

- All files read before any deletion
- MEMORY.md stays under 200 lines
- No orphaned files (every .md linked or self-contained)
- Key sections preserved: health, people, alignment, active work
