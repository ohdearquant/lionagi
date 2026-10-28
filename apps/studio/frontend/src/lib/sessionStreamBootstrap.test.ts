import { beforeEach, describe, expect, it, vi } from "vitest";
import type { SessionDetail, SignalEvent } from "./api";
import { SignalProjection } from "./signalProjection";

vi.mock("./api", async () => ({
  ...(await vi.importActual<typeof import("./api")>("./api")),
  getSession: vi.fn(),
  getSessionSignalPage: vi.fn(),
}));
const { getSession, getSessionSignalPage } = await import("./api");
const { loadSessionLiveBootstrap } = await import("./sessionStreamBootstrap");
const detail = (cut?: number): SessionDetail => ({
  id: "s1",
  name: "session",
  created_at: 1,
  updated_at: 1,
  branches: [],
  ...(cut === undefined
    ? {}
    : { stream_snapshot: { version: 1, message_cursor: "ticket", signal_seq: cut } }),
});
const signal = (
  seq: number,
  kind = "AssistantDelta",
  payload: Record<string, unknown> = {},
): SignalEvent => ({
  id: `signal-${seq}`,
  session_id: "s1",
  seq,
  kind,
  op_id: "child",
  ts: seq * 1_000,
  payload,
});
beforeEach(() => {
  vi.mocked(getSession).mockReset();
  vi.mocked(getSessionSignalPage).mockReset();
});

describe("paired session bootstrap with the existing signal reducer", () => {
  it("folds all bounded pages through the fixed cut, preserving evicted hidden state for later signals", async () => {
    const prefix = [
      signal(1, "AssistantDelta", { text: "before name" }),
      signal(2, "NodeSpawned", { parent_id: "parent", independent: true }),
      ...Array.from({ length: 2_499 }, (_, index) => ({
        ...signal(index + 3, "SessionUpdated"),
        op_id: "",
      })),
      signal(2_502, "NodeStarted", { name: "worker" }),
      signal(2_503, "NodeEscalated", { name: "worker", route: "higher_tier" }),
    ];
    vi.mocked(getSession).mockResolvedValue(detail(2_503));
    vi.mocked(getSessionSignalPage).mockImplementation(async (_id, params) => {
      const rows = prefix
        .filter((row) => row.seq > params.afterSeq && row.seq <= params.throughSeq)
        .slice(0, 500);
      const last = rows.at(-1)?.seq ?? params.afterSeq;
      return {
        signals: rows,
        next_after_seq: last,
        through_seq: params.throughSeq,
        has_more: last < 2_503,
      };
    });
    const bootstrap = await loadSessionLiveBootstrap("s1", () => true);
    expect(getSession).toHaveBeenCalledWith("s1", { liveSnapshot: true });
    expect(getSessionSignalPage).toHaveBeenCalledTimes(6);
    expect(vi.mocked(getSessionSignalPage).mock.calls.map(([, params]) => params)).toEqual(
      [0, 500, 1_000, 1_500, 2_000, 2_500].map((afterSeq) => ({
        afterSeq,
        throughSeq: 2_503,
        limit: 500,
      })),
    );
    const suffix = signal(2_504, "NodeCompleted", { name: "worker", elapsed: 4 });
    bootstrap!.projection.append(suffix);
    const canonical = new SignalProjection();
    for (const event of [...prefix, suffix]) canonical.append(event);
    expect(bootstrap!.projection.snapshot()).toEqual(canonical.snapshot());
    expect(bootstrap!.projection.events).toHaveLength(2_000);
    expect(bootstrap!.projection.totalCount).toBe(2_504);
    expect(bootstrap!.projection.snapshot().nodeActivity.get("worker")?.lastText).toBe(
      "before name",
    );
    expect(bootstrap!.projection.events[0]!.ts).toBe(505_000);
  });

  it("does not request history for an empty cut", async () => {
    vi.mocked(getSession).mockResolvedValue(detail(0));
    const bootstrap = await loadSessionLiveBootstrap("s1", () => true);
    expect(getSessionSignalPage).not.toHaveBeenCalled();
    expect(bootstrap!.snapshot?.signal_seq).toBe(0);
    expect(bootstrap!.projection.totalCount).toBe(0);
  });

  it("supports an older detail without a live snapshot using an empty legacy reducer", async () => {
    vi.mocked(getSession).mockResolvedValue(detail());
    const bootstrap = await loadSessionLiveBootstrap("s1", () => true);
    expect(bootstrap!.snapshot).toBeUndefined();
    expect(bootstrap!.projection.totalCount).toBe(0);
    expect(getSessionSignalPage).not.toHaveBeenCalled();
  });

  it("drops a superseded detail before beginning any prefix read", async () => {
    vi.mocked(getSession).mockResolvedValue(detail(7));
    expect(await loadSessionLiveBootstrap("s1", () => false)).toBeNull();
    expect(getSessionSignalPage).not.toHaveBeenCalled();
  });

  it("drops a superseded prefix page and never asks for its next page", async () => {
    let current = true;
    vi.mocked(getSession).mockResolvedValue(detail(7));
    vi.mocked(getSessionSignalPage).mockImplementation(async () => {
      current = false;
      return { signals: [signal(1)], next_after_seq: 1, through_seq: 7, has_more: true };
    });
    expect(await loadSessionLiveBootstrap("s1", () => current)).toBeNull();
    expect(getSessionSignalPage).toHaveBeenCalledTimes(1);
  });

  it("propagates a failed prefix without publishing an incomplete projection", async () => {
    vi.mocked(getSession).mockResolvedValue(detail(7));
    vi.mocked(getSessionSignalPage).mockRejectedValue(new Error("prefix unavailable"));
    await expect(loadSessionLiveBootstrap("s1", () => true)).rejects.toThrow("prefix unavailable");
  });

  it.each([
    ["a short last page", [signal(1), signal(2)], 2],
    ["an empty last page", [], 0],
  ])("rejects %s that ends below the cut", async (_label, rows, last) => {
    vi.mocked(getSession).mockResolvedValue(detail(7));
    vi.mocked(getSessionSignalPage).mockResolvedValue({
      signals: rows,
      next_after_seq: last,
      through_seq: 7,
      has_more: false,
    });
    await expect(loadSessionLiveBootstrap("s1", () => true)).rejects.toThrow(
      "Signal history ended before the snapshot cut",
    );
  });

  it("rejects a mismatched detail instead of subscribing to another session", async () => {
    vi.mocked(getSession).mockResolvedValue({ ...detail(), id: "other" });
    await expect(loadSessionLiveBootstrap("s1", () => true)).rejects.toThrow(/selected session/);
  });
});
