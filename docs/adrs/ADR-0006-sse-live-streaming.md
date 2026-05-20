# ADR-0006: Server-Sent Events for Live Streaming

**Status**: Accepted
**Date**: 2026-05-19

## Context

Lion Studio's dashboard needs live updates for two data surfaces: run progress (token-by-token
output as a run executes) and show DAG state (play statuses as a multi-play show advances).

Both consumers are read-only from the browser's perspective — the UI displays state but does not
send commands back over the same channel. The backend is FastAPI / Starlette (see ADR-0002).

## Decision

Use Starlette `StreamingResponse` for all live update streams. Change detection for show
directories uses 500ms polling with `os.stat()` — no filesystem event daemon required.

The run-events endpoint (`GET /api/runs/{run_id}/events`) yields newline-delimited JSON chunks
as SSE. The browser side uses the native `EventSource` API. Any action that triggers a side
effect (e.g., starting a re-run) is a separate REST `POST` endpoint; the SSE channel is
one-way only.

## Consequences

**Positive**
- `EventSource` is native in all modern browsers; no client library required.
- Starlette `StreamingResponse` is synchronous to the existing FastAPI stack — no additional
  dependency or protocol server.
- One-way constraint is a feature for this read-only v1 scope: no reconnect-and-replay
  complexity for bidirectional state.

**Negative**
- SSE is strictly server-to-client. Triggering a re-run or cancelling a run from the UI requires
  a separate REST endpoint; the SSE channel cannot carry commands.
- HTTP/1.1 browser connection limits (6 per origin) constrain the number of concurrent SSE
  streams a single page can hold open.

## Alternatives Considered

| Alternative | Why Rejected |
|-------------|--------------|
| WebSocket | Overkill for one-way streaming; adds reconnect-and-replay logic; no bidirectional need in v1 |
| inotify / FSEvents filesystem watcher | Show directories are small (~5-20 files × ~10 plays); polling at 500ms is sufficient; avoids platform-specific daemon dependency |
| Long-polling | More client complexity than SSE for equivalent one-way semantics |

## References

- `add-shows-pages/_intent.md:65-70` — use `StreamingResponse` (not `EventSourceResponse`)
- `lift-backend/lift_summary.md:73-74` — `GET /api/runs/{run_id}/events` SSE implementation
- `add-shows-pages/_intent.md:69` — polling rationale (show dir size)
- [ADR-0002](ADR-0002-studio-tech-stack.md) — FastAPI/Starlette stack this decision builds on
