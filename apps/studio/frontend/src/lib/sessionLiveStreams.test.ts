import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const stops: Array<() => void> = [];
const signal = (seq: number) => ({
  id: `signal-${seq}`, session_id: "session-1", seq, kind: "NodeStarted",
  op_id: "op-1", ts: 2, payload: { name: "worker" },
});
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
  status, headers: { "Content-Type": "application/json" },
});
const frames = (data: string) => new Response(data, {
  status: 200, headers: { "Content-Type": "text/event-stream" },
});
const snapshot = { version: 1, message_cursor: "opaque+bootstrap/one", signal_seq: 7 };

beforeEach(() => { vi.resetModules(); vi.useFakeTimers(); });
afterEach(() => {
  for (const stop of stops.splice(0)) stop();
  vi.clearAllTimers(); vi.useRealTimers(); vi.unstubAllGlobals();
  delete (window as Window & { __STUDIO_AUTH_TOKEN__?: string }).__STUDIO_AUTH_TOKEN__;
});

describe("session live bootstrap and explicit signal pages", () => {
  it("opts into paired detail without changing ordinary or older-page requests", async () => {
    const fetchMock = vi.fn((_url: RequestInfo | URL, _init?: RequestInit) => Promise.resolve(json({ id: "session-1", stream_snapshot: snapshot })));
    vi.stubGlobal("fetch", fetchMock);
    const { getSession } = await import("./api");
    await getSession("session-1", { liveSnapshot: true });
    await getSession("session-1");
    await getSession("session-1", { messageCursor: "older-only" });
    const queries = fetchMock.mock.calls.map(([url]) => new URL(String(url), "http://studio.test").searchParams);
    expect(queries[0]!.get("live_snapshot")).toBe("true");
    expect(queries[0]!.get("message_cursor")).toBeNull();
    expect([...queries[1]!]).toEqual([]);
    expect([...queries[2]!]).toEqual([["message_cursor", "older-only"]]);
  });

  it.each([
    ["null", null], ["version", { ...snapshot, version: 2 }],
    ["empty-ticket", { ...snapshot, message_cursor: "" }],
    ["negative-cut", { ...snapshot, signal_seq: -1 }],
  ])("rejects a present invalid %s snapshot", async (_name, value) => {
    vi.stubGlobal("fetch", vi.fn((_url: RequestInfo | URL, _init?: RequestInit) => Promise.resolve(json({ id: "session-1", stream_snapshot: value }))));
    const { getSession } = await import("./api");
    await expect(getSession("session-1", { liveSnapshot: true })).rejects.toThrow(/snapshot/);
  });

  it("requests the fixed prefix in capped pages and normalizes each timestamp once", async () => {
    (window as Window & { __STUDIO_AUTH_TOKEN__?: string }).__STUDIO_AUTH_TOKEN__ = "page-token";
    const fetchMock = vi.fn((_url: RequestInfo | URL, _init?: RequestInit) => Promise.resolve(json({
      signals: [signal(7)], next_after_seq: 7, through_seq: 10, has_more: false,
    })));
    vi.stubGlobal("fetch", fetchMock);
    const { getSessionSignalPage } = await import("./api");
    const page = await getSessionSignalPage("session-1", { afterSeq: 4, throughSeq: 10, limit: 900 });
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(new URL(String(url), "http://studio.test").pathname).toBe("/api/sessions/session-1/signals/page");
    expect([...new URL(String(url), "http://studio.test").searchParams]).toEqual([
      ["after_seq", "4"], ["through_seq", "10"], ["limit", "500"],
    ]);
    expect(new Headers((init as RequestInit).headers).get("authorization")).toBe("Bearer page-token");
    expect(page.signals[0]!.ts).toBe(2_000);
    expect(page.next_after_seq).toBe(7);
  });

  it.each([
    ["past-cut", { signals: [signal(11)], next_after_seq: 11, through_seq: 10, has_more: false }],
    ["stalled", { signals: [], next_after_seq: 4, through_seq: 10, has_more: true }],
    ["foreign-session", { signals: [{ ...signal(7), session_id: "other" }], next_after_seq: 7, through_seq: 10, has_more: false }],
    ["descending", { signals: [signal(8), signal(7)], next_after_seq: 7, through_seq: 10, has_more: false }],
  ])("rejects a %s history page rather than starting an incomplete live reducer", async (_name, page) => {
    vi.stubGlobal("fetch", vi.fn((_url: RequestInfo | URL, _init?: RequestInit) => Promise.resolve(json(page))));
    const { getSessionSignalPage } = await import("./api");
    await expect(getSessionSignalPage("session-1", { afterSeq: 4, throughSeq: 10 })).rejects.toThrow();
  });
});

describe("session live applied positions and resync", () => {
  it("keeps the initial message ticket over a failed consumer, then advances on success", async () => {
    const urls: string[] = [];
    vi.stubGlobal("fetch", vi.fn((url: string) => {
      urls.push(url);
      return Promise.resolve(frames('id: delivery-one\ndata: {"id":"m1","branch_id":"b1","role":"assistant","timestamp":1}\n\n'));
    }));
    const { streamSession } = await import("./api");
    let applied = 0;
    stops.push(streamSession("session-1", () => {
      if (++applied === 1) throw new Error("not applied");
    }, undefined, { mode: "live", initialCursor: snapshot.message_cursor }));
    await vi.advanceTimersByTimeAsync(0);
    await vi.advanceTimersByTimeAsync(2_000);
    await vi.advanceTimersByTimeAsync(2_000);
    expect(urls.map((url) => new URL(url, "http://studio.test").searchParams.get("cursor"))).toEqual([
      snapshot.message_cursor, snapshot.message_cursor, "delivery-one",
    ]);
    expect(urls.every((url) => new URL(url, "http://studio.test").searchParams.get("mode") === "live")).toBe(true);
  });

  it("retains the signal snapshot on reconnect and advances seq only after application", async () => {
    const urls: string[] = [];
    vi.stubGlobal("fetch", vi.fn((url: string) => {
      urls.push(url);
      return Promise.resolve(frames(`data: ${JSON.stringify(signal(8))}\n\n`));
    }));
    const { streamSignals } = await import("./api");
    const timestamps: number[] = [];
    let deliveries = 0;
    stops.push(streamSignals("session-1", (event) => {
      if ("type" in event) return;
      if (++deliveries === 1) throw new Error("not applied");
      timestamps.push(event.ts);
    }, undefined, { mode: "live", initialSnapshot: snapshot.message_cursor, initialAfterSeq: 7 }));
    await vi.advanceTimersByTimeAsync(0);
    await vi.advanceTimersByTimeAsync(2_000);
    await vi.advanceTimersByTimeAsync(2_000);
    const queries = urls.map((url) => new URL(url, "http://studio.test").searchParams);
    expect(queries.map((query) => query.get("after_seq"))).toEqual(["7", "7", "8"]);
    expect(queries.map((query) => query.get("snapshot"))).toEqual(Array(3).fill(snapshot.message_cursor));
    expect(timestamps).toEqual([2_000]);
  });

  it.each(["messages", "signals"] as const)("%s overflow reports the applied position, not the enqueued marker", async (channel) => {
    const control = channel === "messages"
      ? { type: "resync_required", stream: channel, reason: "overflow", resume_cursor: "newest-enqueued" }
      : { type: "resync_required", stream: channel, reason: "overflow", resume_after_seq: 99 };
    const data = channel === "messages"
      ? 'id: applied-message\ndata: {"id":"m1","branch_id":"b1","role":"assistant","timestamp":1}\n\n'
      : `data: ${JSON.stringify(signal(8))}\n\n`;
    const fetchMock = vi.fn((_url: RequestInfo | URL, _init?: RequestInit) => Promise.resolve(frames(data + `id: control-is-not-a-cursor\ndata: ${JSON.stringify(control)}\n\n`)));
    vi.stubGlobal("fetch", fetchMock);
    const api = await import("./api");
    const resync = vi.fn(); const states: string[] = []; const applied = vi.fn();
    stops.push(channel === "messages"
      ? api.streamSession("session-1", applied, (state) => states.push(state), {
          mode: "live", initialCursor: snapshot.message_cursor, onResyncRequired: resync,
        })
      : api.streamSignals("session-1", applied, (state) => states.push(state), {
          mode: "live", initialSnapshot: snapshot.message_cursor, initialAfterSeq: 7, onResyncRequired: resync,
        }));
    await vi.advanceTimersByTimeAsync(0);
    await vi.advanceTimersByTimeAsync(10_000);
    expect(resync).toHaveBeenCalledTimes(1);
    expect(resync).toHaveBeenCalledWith(control, channel === "messages" ? { cursor: "applied-message" } : { afterSeq: 8 });
    expect(applied).toHaveBeenCalledTimes(1);
    expect(states).toEqual(["connecting", "open", "disconnected"]);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it.each(["messages", "signals"] as const)("%s 409 requests resnapshot without timer replay or done", async (channel) => {
    const fetchMock = vi.fn((_url: RequestInfo | URL, _init?: RequestInit) => Promise.resolve(json({
      detail: { code: "stream_resync_required", reason: "foreign_epoch" },
    }, 409)));
    vi.stubGlobal("fetch", fetchMock);
    const api = await import("./api"); const resync = vi.fn(); const applied = vi.fn();
    stops.push(channel === "messages"
      ? api.streamSession("session-1", applied, undefined, { mode: "live", initialCursor: snapshot.message_cursor, onResyncRequired: resync })
      : api.streamSignals("session-1", applied, undefined, { mode: "live", initialSnapshot: snapshot.message_cursor, initialAfterSeq: 7, onResyncRequired: resync }));
    await vi.advanceTimersByTimeAsync(0); await vi.advanceTimersByTimeAsync(10_000);
    expect(resync).toHaveBeenCalledTimes(1);
    expect(resync).toHaveBeenCalledWith(
      { type: "resync_required", stream: channel, reason: "foreign_epoch" },
      channel === "messages" ? { cursor: snapshot.message_cursor } : { afterSeq: 7 },
    );
    expect(applied).not.toHaveBeenCalled(); expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("cancels a pending response body when its live subscription closes", async () => {
    const cancel = vi.fn();
    const body = new ReadableStream<Uint8Array>({ cancel });
    vi.stubGlobal("fetch", vi.fn((_url: RequestInfo | URL, _init?: RequestInit) =>
      Promise.resolve(new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream" } })),
    ));
    const { streamSignals } = await import("./api");
    const close = streamSignals("session-1", vi.fn(), undefined, {
      mode: "live", initialSnapshot: snapshot.message_cursor, initialAfterSeq: 7,
    });
    stops.push(close);
    await vi.advanceTimersByTimeAsync(0);
    close();
    await vi.advanceTimersByTimeAsync(0);
    expect(cancel).toHaveBeenCalledTimes(1);
  });

  it("keeps three-argument signal subscriptions cursorless and without live scope", async () => {
    const fetchMock = vi.fn((_url: RequestInfo | URL, _init?: RequestInit) => Promise.resolve(frames('data: {"type":"done"}\n\n')));
    vi.stubGlobal("fetch", fetchMock); const { streamSignals } = await import("./api");
    stops.push(streamSignals("session-1", vi.fn())); await vi.advanceTimersByTimeAsync(0);
    const query = new URL(String(fetchMock.mock.calls[0]![0]), "http://studio.test").searchParams;
    expect([...query]).toEqual([["after_seq", "0"]]);
  });
});

describe("new live frame validation", () => {
  it("does not apply an unresumable message before retrying its accepted position", async () => {
    const urls: string[] = [];
    const payload = '{"id":"m1","branch_id":"b1","role":"assistant","timestamp":1}';
    vi.stubGlobal("fetch", vi.fn((url: string) => {
      urls.push(url);
      return Promise.resolve(frames(urls.length === 1 ? `data: ${payload}\n\n` : `id: valid-cursor\ndata: ${payload}\n\n`));
    }));
    const { streamSession } = await import("./api"); const consumer = vi.fn();
    stops.push(streamSession("session-1", consumer, undefined, { mode: "live", initialCursor: snapshot.message_cursor }));
    await vi.advanceTimersByTimeAsync(0);
    expect(consumer).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(2_000);
    expect(consumer).toHaveBeenCalledTimes(1);
    expect(urls.map((url) => new URL(url, "http://studio.test").searchParams.get("cursor"))).toEqual([
      snapshot.message_cursor, snapshot.message_cursor,
    ]);
    await vi.advanceTimersByTimeAsync(2_000);
    expect(new URL(urls[2]!, "http://studio.test").searchParams.get("cursor")).toBe("valid-cursor");
  });

  it.each([
    ["unsafe-seq", { ...signal(8), seq: Number.MAX_SAFE_INTEGER + 1 }],
    ["negative-seq", { ...signal(8), seq: -1 }],
    ["foreign-session", { ...signal(8), session_id: "another" }],
    ["nonfinite-ts", { ...signal(8), ts: null }],
    ["normalized-overflow", { ...signal(8), ts: 1e308 }],
  ])("rejects %s before a live signal consumer or applied sequence advances", async (_name, event) => {
    const urls: string[] = [];
    vi.stubGlobal("fetch", vi.fn((url: string) => {
      urls.push(url);
      const encoded = _name === "nonfinite-ts"
        ? JSON.stringify(event).replace('"ts":null', '"ts":1e309') : JSON.stringify(event);
      return Promise.resolve(frames(`data: ${encoded}\n\ndata: ${JSON.stringify(signal(9))}\n\n`));
    }));
    const { streamSignals } = await import("./api"); const consumer = vi.fn();
    stops.push(streamSignals("session-1", consumer, undefined, {
      mode: "live", initialSnapshot: snapshot.message_cursor, initialAfterSeq: 7,
    }));
    await vi.advanceTimersByTimeAsync(0); await vi.advanceTimersByTimeAsync(2_000);
    expect(consumer).not.toHaveBeenCalled();
    expect(urls).toHaveLength(2);
    expect(urls.map((url) => new URL(url, "http://studio.test").searchParams.get("after_seq"))).toEqual(["7", "7"]);
  });

  it("drops duplicate signal sequences without treating them as accepted progress", async () => {
    const fetchMock = vi.fn((_url: RequestInfo | URL, _init?: RequestInit) => Promise.resolve(frames(
      `data: ${JSON.stringify(signal(7))}\n\ndata: ${JSON.stringify(signal(8))}\n\n`,
    )));
    vi.stubGlobal("fetch", fetchMock);
    const { streamSignals } = await import("./api"); const consumer = vi.fn();
    stops.push(streamSignals("session-1", consumer, undefined, {
      mode: "live", initialSnapshot: snapshot.message_cursor, initialAfterSeq: 7,
    }));
    await vi.advanceTimersByTimeAsync(0);
    expect(consumer).toHaveBeenCalledTimes(1); expect(consumer.mock.calls[0]![0].seq).toBe(8);
    await vi.advanceTimersByTimeAsync(2_000);
    const query = new URL(String(fetchMock.mock.calls[1]![0]), "http://studio.test").searchParams;
    expect(query.get("after_seq")).toBe("8"); expect(consumer).toHaveBeenCalledTimes(1);
  });

  it("keeps legacy message callbacks without frame IDs and legacy signal normalization lax", async () => {
    const fetchMock = vi.fn((url: RequestInfo | URL, _init?: RequestInit) => Promise.resolve(frames(
      String(url).includes("/signals?")
        ? `data: ${JSON.stringify({ ...signal(8), session_id: "legacy-other" })}\n\ndata: {"type":"done"}\n\n`
        : 'data: {"id":"legacy-message"}\n\ndata: {"type":"done"}\n\n',
    )));
    vi.stubGlobal("fetch", fetchMock);
    const { streamSession, streamSignals } = await import("./api");
    const messages = vi.fn(); const signals = vi.fn();
    stops.push(streamSession("session-1", messages)); stops.push(streamSignals("session-1", signals));
    await vi.advanceTimersByTimeAsync(0);
    expect(messages.mock.calls[0]![0]).toEqual({ id: "legacy-message" });
    expect(signals.mock.calls[0]![0]).toMatchObject({ session_id: "legacy-other", ts: 2_000 });
  });
});
