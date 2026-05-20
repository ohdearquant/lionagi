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
