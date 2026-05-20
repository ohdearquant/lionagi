# ADR-0016: Definition Write Path and Versioning

**Status**: Accepted
**Date**: 2026-05-20
**Extends**: ADR-0009 (SQLite state layer), ADR-0010 (plugin editability matrix), ADR-0014 (CLI-primary)

## Context

Studio is not the execution control plane (ADR-0014), but it is allowed to
mutate local definitions — agent profiles and playbook YAML. This is the only
sanctioned write path in Studio besides the convenience Run button. The
invariants of this write path need a durable contract.

The definitions API exists in code (`/api/definitions/`, `definitions` SQLite
table) but its semantics are not formalized. Specifically: what is editable,
what is the source of truth, how versioning works, and what rollback means.

## Decision

### What is editable

| Item | Editable? | Where? |
|------|-----------|--------|
| Marketplace/local agent definitions (`*.md`) | Yes | Agents page |
| Marketplace/local playbook definitions (`*.yaml`) | Yes | Playbooks page |
| Skills (`SKILL.md`) | No | Read-only in Plugins page |
| Third-party plugin components | No | Read-only everywhere |
| Runtime artifacts (sessions, messages, shows, plays) | No | Read-only |
| Plugin metadata (`plugin.json`, `hooks.json`, `.mcp.json`) | No | Read-only |

### Source of truth

**Disk content is canonical.** The file on disk (`~/.lionagi/agents/analyst.md`,
`~/.lionagi/playbooks/review-flow.yaml`) is the authoritative version. SQLite's
`definitions` table stores version history (content snapshots + metadata), not
the current state.

When Studio saves a definition:
1. Write the new content to the file on disk.
2. Insert a new row in `definitions` with the content, incremented version number,
   and timestamp.

When a file is edited outside Studio (in a text editor, by the CLI, by git):
the disk version is current. Studio's version history may be behind — this is
acceptable. The next Studio edit will create a new version from whatever is
on disk.

### Save semantics

```
POST /api/definitions/{kind}/{name}
  body: { content: string, message?: string }
  →
  1. Write content to disk path
  2. INSERT INTO definitions (kind, name, path, content, version, created_at, message)
     VALUES (kind, name, path, content, max(version)+1, now(), message)
  3. Return { version: N, saved_at: timestamp }
```

Version numbers are monotonic per `(kind, name)`. Current version = `MAX(version)`.

### Rollback semantics

```
POST /api/definitions/{kind}/{name}/rollback?version=N
  →
  1. SELECT content FROM definitions WHERE kind=? AND name=? AND version=?
  2. Write that content to disk
  3. INSERT new definitions row with version = max(version)+1 and the restored content
  4. Return { version: N+1, rolled_back_from: current, rolled_back_to: N }
```

Rollback creates a NEW version (not a delete). The version history is append-only.
Version N+1's content equals version N's content, but it is a new entry with a
new timestamp.

### Conflict posture

Single-user, localhost-only. No merge UI, no multi-writer conflict model, no
file locking. If two tabs save the same definition concurrently, last write
wins on disk and both versions exist in SQLite history.

This is acceptable because: (1) one primary user, (2) CLI edits are rare while
Studio is open, (3) git provides the ultimate conflict resolution layer.

## Consequences

**Positive**
- Clear boundary: definitions are the only things Studio writes. Everything else
  is read-only or import-only.
- Disk-as-truth means git, grep, vim, and Claude Code all see the same file.
- Append-only version history enables rollback without data loss.
- Simple CRUD surface — no complex state machine or workflow engine.

**Negative**
- Disk and SQLite can drift if a write to one succeeds and the other fails.
  Mitigation: disk write first (canonical), SQLite second (history). If SQLite
  fails, the file is still saved — just version history is incomplete.
- No notification when an external edit makes SQLite history stale. The next
  Studio load shows disk content, which may differ from the latest SQLite version.

## Alternatives Considered

| Alternative | Why Rejected |
|-------------|-------------|
| SQLite as source of truth (disk is derived) | Breaks the CLI/editor/git workflow — external tools expect to read/write files |
| No version history (just save to disk) | Rollback is valuable; 1 INSERT per save is negligible cost |
| File locking during edits | Single-user tool; locking adds complexity for a race condition that doesn't happen |
| Make skills editable too | Skills are Claude Code instructions, not lionagi definitions; different authoring model |
