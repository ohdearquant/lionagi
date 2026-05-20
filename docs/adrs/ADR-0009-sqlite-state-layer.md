# ADR-0009: SQLite State Layer for Core Data Model

**Status**: Accepted
**Date**: 2026-05-20
**Supersedes**: ADR-0004 (filesystem-backed data layer) for operational state.
ADR-0004 remains valid for agent/playbook definitions.

## Context

Lion Studio's filesystem-only backend (ADR-0004) works for post-hoc review but
cannot support live monitoring. A `li agent` run doesn't appear in the dashboard
until after completion because `run.json` is written at the end. Polling the
filesystem every 5 seconds gives no task/worker context for in-progress runs.

Separately, lionagi's `Session` / `Branch` / `Message` / `Progression` data
model needs a persistent representation that mirrors the runtime exactly, to
support:

1. Instantaneous monitoring via hooks + WebSocket (not polling).
2. Cross-session message exchange (replacing `li team`'s markdown-file
   coordination with structured inbox/outbox).
3. Branch forking and message sharing without content duplication.
4. Vector similarity search on message embeddings.

aiosqlite is already an optional dependency (`lionagi[sqlite]`).

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
| 1 | `lionagi.protocols.messages.system.System` |
| 2 | `lionagi.protocols.messages.instruction.Instruction` |
| 3 | `lionagi.protocols.messages.assistant_response.AssistantResponse` |
| 4 | `lionagi.protocols.messages.action_request.ActionRequest` |
| 5 | `lionagi.protocols.messages.action_response.ActionResponse` |

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
| `updated_at` | REAL | |

Session is the **substrate**, not the invocation.  Run-level concerns (kind,
task, cwd, worker_name, provider, model, status, lifecycle) are NOT on the
session table — they belong to a higher layer when we get to monitoring.

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

## Consequences

**Positive**
- Session/branch/message model has a persistent representation that mirrors the
  runtime 1:1. Round-trip `to_dict(mode="db")` → INSERT → SELECT → `from_dict()`
  is lossless.
- Foundation for live monitoring (hooks → DB INSERT → WebSocket push) without
  filesystem polling.
- Foundation for cross-session message exchange (structured inbox/outbox via
  recipient/sender fields + progression cursors).
- Branch forking is O(1) with zero message duplication.
- sqlite-vec ready for semantic search on message embeddings.
- Agent/playbook file-based contract preserved.

**Negative**
- New dependency path: `lionagi[sqlite]` required for the state layer.
- JSON array for progression ordering limits query-side operations (no
  `WHERE message_id IN progression` without JSON parsing).
- Schema migrations will be needed as the model evolves.

## Alternatives Considered

| Alternative | Why Rejected |
|-------------|--------------|
| Keep filesystem only (ADR-0004) | Cannot support live monitoring, cross-session exchange, or structured queries |
| khive as backend | Adds service dependency; lionagi should work standalone without a running server |
| Join table for progression ordering | Extra table + extra rows; JSON array is sufficient at current scale |
| Full class path string for lion_class | 60 bytes/row waste; int enum is 4 bytes |
| TEXT for embeddings | Wastes space; BLOB is compact and sqlite-vec compatible |
| Run-level fields (kind, status, task) on session | Session is substrate, not invocation; mixing them violates separation of concerns |

## References

- `lionagi/state/schema.sql` — the schema
- `lionagi/protocols/generic/element.py` — `to_dict(mode="db")` rename
- `lionagi/session/branch.py` — `to_dict()` streamlined serialization
- `lionagi/protocols/messages/manager.py` — `create_message()` extraction
- [ADR-0004](ADR-0004-filesystem-data-layer.md) — predecessor (still valid for agent/playbook files)
