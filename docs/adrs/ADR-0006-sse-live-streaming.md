# ADR-0006: Live Update Transport — SSE + Interval Refresh

**Status**: Accepted
**Date**: 2026-05-19 (revised 2026-05-20)

## Context

Lion Studio needs live updates for three surfaces: session message streams
(new messages appearing during an active run), show DAG state (play statuses
advancing), and dashboard metrics (counts and recent activity).

All consumers are read-only from the browser's perspective — the UI displays
state but does not send commands. The backend is FastAPI / Starlette (ADR-0002).

## Decision

### Transport policy

| Surface | Transport | Why |
|---------|-----------|-----|
| Session messages | SSE (`/api/sessions/{id}/stream`) | Real-time per-message push; EventSource API is native |
| Show play state | SSE (`/api/shows/{topic}/stream`) | File change detection via 500ms `os.stat()` polling, pushed as SSE |
| Dashboard metrics | Interval refresh (30s `setInterval` on `/api/stats`) | Dashboard is a snapshot, not a live stream; interval is cheaper |

**SSE is the only push transport.** No WebSocket. The browser-to-server direction
uses standard REST endpoints for any mutations (save definition, trigger run).

Note: with shows structural state in SQLite (ADR-0011), the show stream can use `shows.updated_at` / `plays.updated_at` as a cursor instead of filesystem polling. Filesystem polling remains valid as a fallback for shows not yet imported to SQLite, or as a change trigger that causes a SQLite refetch.

### SSE implementation

Starlette `StreamingResponse` with `text/event-stream` media type. Newline-delimited
JSON chunks. The browser uses native `EventSource` API — no client library.

Change detection for show directories uses 500ms polling with `os.stat()`. For
sessions, new messages after a timestamp cursor are queried from SQLite.

### SSE event contract

Both SSE routes use newline-delimited JSON (`data: {...}\n\n`). The tables below
document every event type each route emits, including heartbeat cadence.

#### `/api/sessions/{id}/stream` (sessions router)

| Event type | Payload shape | When emitted |
|------------|--------------|--------------|
| message | `{...session message fields...}` | Each new message after the cursor timestamp |
| `heartbeat` | `{"type":"heartbeat"}` | Every 5 s when no message has been emitted |
| `done` | `{"type":"done"}` | When `sessions.updated_at` is > 60 s old (session quiescent) |

**Heartbeat rationale**: sessions may run for minutes without producing messages
(e.g., waiting for a slow LLM response). Heartbeats prevent proxies and browser
connections from timing out and allow the client to distinguish "live but quiet"
from "server died."

#### `/api/shows/{topic}/stream` (shows service)

| Event type | Payload shape | When emitted |
|------------|--------------|--------------|
| `new` | `{"type":"new","path":"<rel>","size":<n>}` | New file detected under the show directory |
| `change` | `{"type":"change","path":"<rel>","size":<n>}` | Existing file modified (size or mtime changed) |
| `done` | `{"type":"done"}` | Show status is terminal (`completed` or `aborted`) AND no file changed for 60 s |

**No heartbeat on the shows stream (accepted gap)**: the shows stream emits
file-change events (`new`, `change`) and a terminal `done` event, but no periodic
heartbeat. A non-terminal show can be quiet for longer than proxy or browser idle
thresholds (typically 60–90 s) if no files change while plays are waiting or
running. In that case the EventSource will reconnect automatically via its built-in
retry; the server will resume streaming from the current filesystem state on
reconnect. This reconnect behaviour is relied upon instead of a heartbeat.
A heartbeat could be added in a future iteration if reconnect storms become a
problem in practice. The asymmetry with the session stream (which does heartbeat
every 5 s) is an accepted trade-off, not an accident.

### Reconnect behavior

SSE auto-reconnects via `EventSource`. The server sends a `{"type":"done"}` event
when a session is no longer active (no updates for 60s), signaling the client to
stop reconnecting.

## Consequences

**Positive**
- `EventSource` is native in all modern browsers; no client library.
- Starlette `StreamingResponse` integrates with the existing FastAPI stack.
- One-way constraint prevents bidirectional state complexity.
- No WebSocket server, connection upgrade, or ping/pong management.

**Negative**
- SSE is strictly server-to-client. Any client-to-server action requires a
  separate REST endpoint.
- HTTP/1.1 browser connection limits (6 per origin) constrain concurrent SSE
  streams per page.
- 500ms filesystem polling for shows is acceptable at 5-20 files but would need
  replacement at scale. SQLite-backed show state (ADR-0011) reduces reliance on
  filesystem polling.

## Alternatives Considered

| Alternative | Why Rejected |
|-------------|-------------|
| WebSocket | Overkill for one-way streaming; adds reconnect-and-replay logic; no bidirectional need |
| inotify/FSEvents filesystem watcher | Platform-specific daemon dependency; polling at 500ms is sufficient for show dir sizes |
| Long-polling | More client complexity than SSE for equivalent one-way semantics |
| SSE for dashboard | Dashboard metrics are a snapshot, not an event stream; interval refresh is simpler |
