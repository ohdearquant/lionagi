# ADR-0016: Definition Write Path and Versioning

**Status**: Accepted
**Date**: 2026-05-20
**Extends**: ADR-0009 (SQLite state layer), ADR-0010 (plugin editability matrix), ADR-0014 (CLI-primary)

## Context

Studio is not the execution control plane (ADR-0014), but it is allowed to
mutate local definitions — agent profiles and playbook YAML. This is the only
sanctioned write path in Studio besides the convenience Run button. The
invariants of this write path need a durable contract.

The definitions API is specified below. The agent and playbook write routes
currently return 501 (unimplemented); the definitions SQLite table and
versioning logic are part of the implementation phases in ADR-0012. This ADR
formalizes the semantics: what is editable, what is the source of truth, how
versioning works, and what rollback means.

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

1. Validate `kind` and `name` (see "Route parameter validation" below).
2. Acquire the per-`(kind, name)` in-process lock (`_DEFINITION_LOCKS`).
3. Insert a new row in `definitions` with the content, incremented version number,
   and timestamp.
4. Write the new content to the file on disk.
5. Release the lock.

The DB write happens **before** the disk write. If the DB write fails, the
exception propagates and the disk file is **not** modified. If the disk write
fails after a successful DB write, the DB row exists but the disk file may be
stale — the next save will reconcile them.

When a file is edited outside Studio (in a text editor, by the CLI, by git):
the disk version is current. Studio's version history may be behind — this is
acceptable. The next Studio edit will create a new version from whatever is
on disk.

### Route parameter validation

All definition routes validate `kind` and `name` before any filesystem or DB
operation. Invalid values return **422 Unprocessable Entity**.

`kind` must be one of the supported definition kinds: `agent`, `playbook`.

`name` must be a non-empty single path component. The following are rejected:

- Path separators: `/`, `\`
- NUL byte: `\x00`
- Dot components: `.`, `..`
- Glob metacharacters: `*`, `?`, `[`, `]`, `{`, `}`, `~`
- Empty string or whitespace-only strings

The validation is implemented in `services/_path_safety.py:validate_name_component()`.
It is called at the top of `get_definition()`, `get_version()`, `save_definition()`,
and `rollback_definition()`.

Note: symlinked definition files (e.g. `~/.lionagi/agents/*.md` → `firm/agents/`)
are fully supported. Security relies on validating the route `name` parameter,
not on restricting symlink targets.

### Save semantics

```text
POST /api/definitions/{kind}/{name}
  body: { content: string, message?: string }
  →
  1. validate_name_component(kind), validate_name_component(name)
  2. acquire _DEFINITION_LOCKS[(kind, name)]
  3. INSERT INTO definitions (kind, name, path, content, version, created_at, message)
     VALUES (kind, name, path, content, max(version)+1, now(), message)
     -- DB failure raises; disk is NOT modified
  4. write content to disk path (mkdir -p if needed)
  5. release lock
  6. Return { version: N, saved_at: timestamp, message: string|null }
```

`_DEFINITION_LOCKS` is an in-process `asyncio.Lock` per `(kind, name)` pair.
It serialises concurrent saves within a single uvicorn worker. It is **not** a
cross-process lock; multiple uvicorn worker processes or external CLI writes can
still race. For the current single-user, localhost deployment this is acceptable.

Version numbers are monotonic per `(kind, name)`. Current version = `MAX(version)`.

Definition identity is `(kind, name)`. This is unique because marketplace agents and playbooks are discovered at paths that resolve to the same namespace as local definitions (the plugin scanner contributes to the same agent/playbook registry). If a marketplace and local definition share the same name, the local definition takes precedence (closer to user). Collision detection is deferred — at current scale (20 playbooks, 17 agents), name conflicts have not occurred.

### Rollback semantics

```text
POST /api/definitions/{kind}/{name}/rollback?version=N
  →
  1. SELECT content FROM definitions WHERE kind=? AND name=? AND version=?
  2. Call save_definition() with the restored content — DB write first, then disk
  3. Return {
       version: N+1,            -- new version number assigned to the restored content
       saved_at: <float>,       -- unix timestamp of the restore write (same as a save)
       rolled_back_from: <int>, -- version that was current before the rollback
       rolled_back_to: N,       -- version whose content was restored
       message: <string|null>   -- auto-set to "rollback to vN"; mirrors save message field
     }
```

Rollback delegates to `save_definition()` and therefore inherits DB-first semantics:
if the DB write fails, the disk file is not modified. Rollback creates a NEW version
(not a delete). The version history is append-only. Version N+1's content equals
version N's content, but it is a new entry with a new timestamp.

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
  Mitigation: DB write first (data integrity gate). If the DB write fails the
  exception propagates and disk is not touched. If the disk write fails after a
  successful DB commit, the DB row exists without a matching file update — the
  next save will reconcile. SQLite is the version-history record; disk is the
  canonical runtime file.
- No notification when an external edit makes SQLite history stale. The next
  Studio load shows disk content, which may differ from the latest SQLite version.
- `_DEFINITION_LOCKS` serialises concurrent saves only within a single process.
  Multiple uvicorn workers or external CLI writers are not covered.

## Alternatives Considered

| Alternative | Why Rejected |
|-------------|-------------|
| SQLite as source of truth (disk is derived) | Breaks the CLI/editor/git workflow — external tools expect to read/write files |
| No version history (just save to disk) | Rollback is valuable; 1 INSERT per save is negligible cost |
| File locking during edits | Single-user tool; locking adds complexity for a race condition that doesn't happen |
| Make skills editable too | Skills are Claude Code instructions, not lionagi definitions; different authoring model |
