/**
 * Contract tests for the two SSE clients (src/api/sse.ts, src/api/signals.ts).
 *
 * Both backend routes (sessions.py stream_session_route / stream_signals) emit a
 * `{"type":"done"}` frame and then return before closing, so a *clean* finish is
 * always a `done` event. A transport EOF (reader done) WITHOUT a preceding `done`
 * means the connection dropped — backend restart, proxy close, fetch body cut —
 * and MUST throw so the caller surfaces an error instead of silently freezing the
 * live log/tree on the last received output. These tests lock that contract for
 * both clients (they previously diverged: signals threw, the session log did not).
 */
import { describe, it, expect, vi, afterEach } from "vitest";
import { streamSession } from "../src/api/sse.js";
import { streamSignals } from "../src/api/signals.js";

type StreamFn = (
  baseUrl: string,
  sessionId: string,
  token: string | undefined,
  onEvent: (e: unknown) => void,
  signal: AbortSignal
) => Promise<void>;

// A fetch stub whose response body streams `frames` (each an SSE frame string),
// then reports transport EOF (`{done:true}`) — exactly what the Fetch reader does
// when the connection closes after the last chunk.
function fetchYielding(frames: string[], ok = true): typeof fetch {
  const encoder = new TextEncoder();
  return vi.fn(async () => {
    let i = 0;
    return {
      ok,
      status: ok ? 200 : 502,
      statusText: ok ? "OK" : "Bad Gateway",
      body: {
        getReader() {
          return {
            read: async () =>
              i < frames.length
                ? { done: false, value: encoder.encode(frames[i++]) }
                : { done: true, value: undefined },
            releaseLock() {},
          };
        },
      },
    };
  }) as unknown as typeof fetch;
}

const frame = (obj: unknown) => `data: ${JSON.stringify(obj)}\n\n`;

afterEach(() => {
  vi.restoreAllMocks();
});

// Both clients share the same EOF/done contract — assert it identically for each.
const clients: Array<[string, StreamFn]> = [
  ["streamSession", streamSession as StreamFn],
  ["streamSignals", streamSignals as StreamFn],
];

describe.each(clients)("%s SSE EOF contract", (_name, stream) => {
  it("throws on transport EOF before a `done` event (dropped connection)", async () => {
    globalThis.fetch = fetchYielding([frame({ type: "event", seq: 1 })]);
    const events: unknown[] = [];

    await expect(
      stream("http://x", "s1", undefined, (e) => events.push(e), new AbortController().signal)
    ).rejects.toThrow(/closed before completion/);

    // The pre-EOF event was still delivered before the drop surfaced.
    expect(events).toHaveLength(1);
  });

  it("returns cleanly on a `done` event without throwing (no false-positive)", async () => {
    // A `done` frame followed by the same transport EOF must NOT throw — the
    // client returns from inside the loop the moment it sees `done`.
    globalThis.fetch = fetchYielding([frame({ type: "event", seq: 1 }), frame({ type: "done" })]);
    const events: unknown[] = [];

    await expect(
      stream("http://x", "s1", undefined, (e) => events.push(e), new AbortController().signal)
    ).resolves.toBeUndefined();

    expect(events).toHaveLength(2);
    expect((events[1] as { type: string }).type).toBe("done");
  });

  it("throws a connect error on a non-OK response", async () => {
    globalThis.fetch = fetchYielding([], false);
    await expect(
      stream("http://x", "s1", undefined, () => {}, new AbortController().signal)
    ).rejects.toThrow(/SSE connect failed/);
  });
});
