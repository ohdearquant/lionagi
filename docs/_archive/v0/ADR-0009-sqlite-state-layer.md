# ADR-0009: SQLite State Layer for Core Data Model

**Status**: Accepted
**Date**: 2026-05-20

---

> **Related update**: This ADR establishes SQLite as the current persistent state layer. [ADR-0033](ADR-0033-unified-entity-state-model.md) introduces `NormalizedState` (lifecycle × health × delivery) as the contract for entity state; the SQLite schema here is the current materialization of that contract. [ADR-0034](ADR-0034-frontend-data-and-state-architecture.md) §"Backend-frontend sync contract" specifies how this layer's reads flow to the frontend. The schema extensions for the new dimensions are in ADR-0033 Appendix A. SQLite-specific details remain authoritative; future stores (Postgres, distributed) will materialize the same NormalizedState model differently.

---

## Context

Lion Studio's filesystem-only backend (ADR-0004) works for post-hoc review but
cannot support live monitoring. A `li agent` run doesn't appear in the dashboard
until after completion because `run.json` is written at the end. Polling the
filesystem every 5 seconds gives no task/agent context for in-progress runs.

Separately, lionagi's `Session` / `Branch` / `Message` / `Progression` data
model needs a persistent representation that mirrors the runtime exactly, to
support:

1. Instantaneous monitoring via hooks + SSE push (not filesystem polling).
2. Cross-session message exchange (replacing `li team`'s markdown-file
   coordination with structured inbox/outbox).
3. Branch forking and message sharing without content duplication.
4. Vector similarity search on message embeddings.

aiosqlite is a mandatory dependency (lightweight, ~15KB).

## Decision

Introduce `~/.lionagi/state.db` (SQLite, WAL mode) with four core tables that
map 1:1 to lionagi's runtime data model.  The schema lives at
`lionagi/state/schema.sql`.

### Data model — four tables

**`messages`** — Atomic content.  Independent entities referenced by
progressions, not owned by a branch or session.  Fields match
`Message.model_dump()`:

| Column | Type | Notes |
|--------|------|-------|
| `id` | TEXT PK | Message UUID |
| `created_at` | REAL | unix timestamp |
| `node_metadata` | JSON | `{lion_class: ...}`; renamed from runtime `metadata` to avoid SQL collision |
| `content` | JSON | shape varies by message type |
| `embedding` | BLOB | packed float32 vector; sqlite-vec indexes these when available |
| `sender` | TEXT | UUID of branch/session/external |
| `recipient` | TEXT | UUID for exchange routing |
| `channel` | TEXT | named channel |
| `role` | TEXT | `user`, `assistant`, `system`, `tool`, ... |
| `lion_class` | INTEGER FK | references `message_types(type_id)` — int enum for space efficiency |

**`message_types`** — lookup table mapping integer to full Python class path:

| type_id | lion_class |
|---------|-----------|
| 0 | `__unknown__` (sentinel — see below) |
| 1 | `lionagi.protocols.messages.system.System` |
| 2 | `lionagi.protocols.messages.instruction.Instruction` |
| 3 | `lionagi.protocols.messages.assistant_response.AssistantResponse` |
| 4 | `lionagi.protocols.messages.action_request.ActionRequest` |
| 5 | `lionagi.protocols.messages.action_response.ActionResponse` |

> **Type ID 0 sentinel:** the `__unknown__` row is a typed sentinel returned by
> `StateDB._resolve_lion_class("")` when an incoming message dict carries no
> `lion_class` string in its `node_metadata`. This keeps `insert_message()` total
> (it never raises on missing class info) at the cost of an opaque row that
> downstream readers cannot map back to a runtime class. New type strings
> continue to be allocated their own auto-incremented IDs on first insert.

**`progressions`** — `Progression[Message]`.  An ordered sequence of message
IDs stored as a JSON array in `collection`.  Both sessions and branches own a
progression.

| Column | Type | Notes |
|--------|------|-------|
| `id` | TEXT PK | Progression UUID |
| `created_at` | REAL | |
| `collection` | TEXT | JSON array of message id strings, ordered |

**`sessions`** — The scope boundary.  A session owns a progression (the
session-level message pool) and zero or more branches.  Maps 1:1 to
`lionagi.session.Session`.

| Column | Type | Notes |
|--------|------|-------|
| `id` | TEXT PK | Session UUID |
| `created_at` | REAL | |
| `node_metadata` | JSON | |
| `name` | TEXT | |
| `user` | TEXT | |
| `progression_id` | TEXT FK | session's message pool ordering |
| `first_msg_id` | TEXT FK | convenience bookmark |
| `last_msg_id` | TEXT FK | convenience bookmark |
| `updated_at` | REAL | defaults to `now()` on insert; callers can override for lossless `li state import` / backfill |

> **Provenance columns:** `playbook_name`, `agent_name`, `invocation_kind`,
> `show_topic`, `show_play_name`, `artifacts_path`, and `source_kind` are
> first-class columns on `sessions`. See ADR-0012 for the enrichment decision.
> These columns are nullable lightweight hints for display and filtering —
> they are not authoritative execution state. `invocation_kind` and
> `source_kind` carry schema-level `CHECK` constraints mirrored by Python
> validators in `db.py` (closed vocabularies, ADR-0012).

Session is the **substrate**, not the invocation. Heavy run-level concerns
(full manifest, cwd, provider, model, play-level lifecycle) are NOT on the
session table. Minimal lifecycle columns (`status`, `started_at`, `ended_at`)
are first-class on `sessions` for dashboard and runs-list queries (see
ADR-0017). However, minimal provenance columns (`playbook_name`,
`invocation_kind`, `show_topic`, `show_play_name`, `agent_name`,
`artifacts_path`, `source_kind`) are also on sessions for execution lineage
queries (see ADR-0012). These are lightweight hints for display and filtering,
not authoritative execution state.

**`branches`** — A progression with identity.  A branch IS a progression (an
ordered cursor over the session's messages) with attached agent configuration.
Branch config (provider, model, system_prompt, tools, effort) lives in
`node_metadata` JSON.

| Column | Type | Notes |
|--------|------|-------|
| `id` | TEXT PK | Branch UUID |
| `created_at` | REAL | |
| `node_metadata` | JSON | agent config lives here |
| `user` | TEXT | |
| `name` | TEXT | |
| `session_id` | TEXT FK | owning session |
| `progression_id` | TEXT FK | branch's message ordering |
| `system_msg_id` | TEXT FK | system prompt pointer — see below |

> **Fork columns:** `parent_branch_id` (TEXT) and `forked_at_ord` (INTEGER) are
> stored in `node_metadata` JSON on the branch row, not as top-level columns. A
> dedicated branch fork protocol may promote these to first-class columns in a
> future migration if query patterns require it.
>
> **`system_msg_id` is first-class.** Unlike fork pointers, the system prompt
> reference is promoted to a top-level column because (a) every live persist
> path writes it during branch initialization, (b) the Studio inbox/branch view
> needs a constant-time lookup of the system message without parsing
> `node_metadata`, and (c) sessions that resume should be able to detect "system
> message already persisted" without round-tripping the JSON blob. The actual
> message body remains in `messages`; this column is just an `O(1)` pointer.

### Key design decisions

**1. Fork model: tree pointers, no message duplication.**
A fork records `parent_branch_id` + `forked_at_ord` on the new branch.  Full
history is reconstructed by walking up the ancestry tree.  Fork cost is one
INSERT.  Messages are never copied.

**2. No join table for branch→message ordering.**
The progression's `collection` JSON array holds the ordered list of message IDs.
At our scale (< 500 messages per branch), JSON append is fast and avoids an
extra table.  The trade-off: no SQL-level reverse index ("which progressions
contain message X?").  If needed later, a join table can be derived from the
JSON arrays.

**3. `node_metadata` instead of `metadata`.**
The runtime field is `metadata` but `METADATA` is reserved in some SQL contexts.
`Element.to_dict(mode="db")` renames it to `node_metadata` automatically
(commit 7ac31109).

**4. `lion_class` as integer enum.**
Full class paths like `lionagi.protocols.messages.instruction.Instruction` are
~60 bytes per row.  The `message_types` lookup table maps them to 4-byte
integers.  New message types get the next ID on INSERT.

**5. Embedding as BLOB, not TEXT.**
Packed float32 vectors are compact and ready for sqlite-vec virtual table
indexing.  The vec extension is optional — the core schema is plain SQLite.

**6. Agent and playbook definitions stay as files.**
`~/.lionagi/agents/*.md` and `~/.lionagi/playbooks/*.yaml` remain the source of
truth.  They are the contract with external tools (Claude Code, codex CLI, vim,
grep, git, symlinks).  ADR-0004 remains valid for these.

### Supporting code changes (same session)

**`Branch.to_dict()` streamlined** (commits 7ac31109, a36b5542):

- Added `mode` parameter: `"python"` (default), `"json"`, `"db"`.
- `mode="db"` renames `metadata` → `node_metadata`.
- Optional flags: `include_logs`, `include_log_config`,
  `include_processor_config`, `include_request_options`.
- Metadata serializer handles `clone_from` branch references.
- `parse_model` only included when it differs from `chat_model`.

**`create_message()` extracted** (commit 8f6c6d94):

- `MessageManager.create_message()` is now a `@staticmethod` — can be used
  without a MessageManager instance.
- Standalone `create_message()` function exported from
  `lionagi.protocols.messages` and `lionagi.__init__`.
- `add_message()` delegates to `create_message()` then handles progression
  insertion and system message replacement.

**`iModel.to_dict()` slimmed** (commits 7ac31109, a36b5542):

- `request_options` excluded by default (bulk, not useful for persistence).
- `processor_config` excluded by default.

### Operational commands (`li state`)

The state.db is a long-lived file that grows monotonically without
intervention. To keep the file manageable and to give operators a single
introspection surface, the CLI ships maintenance subcommands under
`li state`:

| Command | Purpose |
|---------|---------|
| `li state import` | Backfill from `~/.lionagi/runs/` filesystem snapshots (idempotent). |
| `li state ls [--limit N] [--status S]` | Paginated session listing with optional status filter. |
| `li state stats` | DB + WAL size, per-table row counts, status distribution, PRAGMAs (journal_mode / wal_autocheckpoint / busy_timeout / synchronous / foreign_keys). |
| `li state checkpoint [--mode TRUNCATE\|PASSIVE\|RESTART\|FULL]` | Force `PRAGMA wal_checkpoint(...)`. Default `TRUNCATE` reclaims the `state.db-wal` file when no readers are active. |
| `li state vacuum` | Run `VACUUM` to rebuild the DB file and reclaim pages freed by `prune`. Holds an exclusive lock for the duration. |
| `li state prune --keep-days N --keep-n M [--dry-run]` | Delete sessions older than `--keep-days` (default 30), always preserving the `--keep-n` most-recent (default 100). Branches cascade via FK (`branches.session_id ... ON DELETE CASCADE`). Messages are swept only if NO progression references them — see ADR-0017 §"Pruning gaps" for the deliberate orphan-progression behavior. |

The DB layer sets `PRAGMA wal_autocheckpoint = 1000` explicitly so the
policy is visible (matches the SQLite default of 1000 frames). Operators
who hit unbounded WAL growth (long-lived readers, never-closed connections
on legacy versions) can drop the autocheckpoint setting or run
`li state checkpoint --mode TRUNCATE` manually.

## Consequences

**Positive**

- Session/branch/message model has a persistent representation that mirrors the
  runtime 1:1. Round-trip `to_dict(mode="db")` → INSERT → SELECT → `from_dict()`
  is lossless.
- Foundation for live monitoring (hooks → DB INSERT → SSE push) without
  filesystem polling.
- Foundation for cross-session message exchange (structured inbox/outbox via
  recipient/sender fields + progression cursors).
- Branch forking is O(1) with zero message duplication.
- sqlite-vec ready for semantic search on message embeddings.
- Agent/playbook file-based contract preserved.

**Negative**

- `aiosqlite` is now a mandatory dependency (promoted from `lionagi[sqlite]` optional).
- JSON array for progression ordering limits query-side operations (no
  `WHERE message_id IN progression` without JSON parsing).
- Schema migrations will be needed as the model evolves. **Migration protocol:**
  this PR ships v1 as the collapsed initial release schema (no `v1→v2`
  migration runner yet). Forward-only column reconciliation is handled in
  `StateDB._reconcile_columns()` — it `PRAGMA table_info`s every table named
  in `_MIGRATION_COLUMNS` and `ALTER TABLE ... ADD COLUMN`s anything that
  exists in the current schema but not on disk. This safely upgrades
  pre-release `state.db` files that were written before late provenance /
  lifecycle columns landed without requiring a numbered migration step. When
  the schema needs a true v2 (column removal, type change, table split), the
  numbered `_migrate()` runner described above will be added and `schema_meta`
  will bump.

## Alternatives Considered

| Alternative | Why Rejected |
|-------------|--------------|
| Keep filesystem only (ADR-0004) | Cannot support live monitoring, cross-session exchange, or structured queries |
| khive as backend | Adds service dependency; lionagi should work standalone without a running server |
| Join table for progression ordering | Extra table + extra rows; JSON array is sufficient at current scale |
| Full class path string for lion_class | 60 bytes/row waste; int enum is 4 bytes |
| TEXT for embeddings | Wastes space; BLOB is compact and sqlite-vec compatible |
| Heavy run-level fields (full manifest, cwd, provider) on session | Session is substrate, not invocation; keep provenance lightweight (ADR-0012 adds minimal hints only) |

## References

- `lionagi/state/schema.sql` — the schema
- `lionagi/protocols/generic/element.py` — `to_dict(mode="db")` rename
- `lionagi/session/branch.py` — `to_dict()` streamlined serialization
- `lionagi/protocols/messages/manager.py` — `create_message()` extraction
- [ADR-0004](ADR-0004-filesystem-data-layer.md) — predecessor (still valid for agent/playbook files)
