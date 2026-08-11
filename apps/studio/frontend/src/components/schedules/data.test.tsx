import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";

const api = vi.hoisted(() => ({
  listSchedules: vi.fn(),
  listScheduleRuns: vi.fn(),
}));

vi.mock("@/lib/api", () => api);

const { mapSettledWithConcurrency, useSchedulesData } = await import("./data");

describe("mapSettledWithConcurrency", () => {
  it("preserves result order while never exceeding the requested concurrency", async () => {
    let active = 0;
    let peak = 0;
    const releases: Array<() => void> = [];
    const work = mapSettledWithConcurrency([0, 1, 2, 3, 4, 5, 6], 3, async (value) => {
      active += 1;
      peak = Math.max(peak, active);
      await new Promise<void>((resolve) => releases.push(resolve));
      active -= 1;
      if (value === 4) throw new Error("expected failure");
      return value * 10;
    });

    expect(active).toBe(3);
    while (releases.length > 0) {
      releases.shift()?.();
      await Promise.resolve();
    }
    const settled = await work;

    expect(peak).toBe(3);
    expect(settled.map((result) => result.status)).toEqual([
      "fulfilled",
      "fulfilled",
      "fulfilled",
      "fulfilled",
      "rejected",
      "fulfilled",
      "fulfilled",
    ]);
    expect(settled[6]).toEqual({ status: "fulfilled", value: 60 });
  });
});

describe("useSchedulesData — refresh exclusion", () => {
  let container: HTMLDivElement;
  let root: Root | null;
  let refresh = () => {};

  beforeEach(() => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    api.listSchedules.mockReset();
    api.listScheduleRuns.mockReset();
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    if (root) act(() => root?.unmount());
    root = null;
    container.remove();
    vi.unstubAllGlobals();
  });

  function Probe() {
    refresh = useSchedulesData().refresh;
    return null;
  }

  it("coalesces manual refreshes while the current schedules request is unresolved", async () => {
    let resolveInitial!: (value: { schedules: [] }) => void;
    api.listSchedules.mockReturnValueOnce(
      new Promise<{ schedules: [] }>((resolve) => {
        resolveInitial = resolve;
      }),
    );

    await act(async () => root?.render(<Probe />));
    act(() => {
      refresh();
      refresh();
    });
    expect(api.listSchedules).toHaveBeenCalledOnce();

    api.listSchedules.mockResolvedValueOnce({ schedules: [] });
    await act(async () => {
      resolveInitial({ schedules: [] });
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(api.listSchedules).toHaveBeenCalledTimes(2);

    api.listSchedules.mockResolvedValueOnce({ schedules: [] });
    await act(async () => {
      refresh();
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(api.listSchedules).toHaveBeenCalledTimes(3);
  });

  it("queues exactly one trailing refresh when data mutates during the run fanout", async () => {
    const before = { id: "schedule-before", name: "Before" };
    const after = { id: "schedule-after", name: "After" };
    let resolveInitialRuns!: (value: { runs: [] }) => void;
    api.listSchedules
      .mockResolvedValueOnce({ schedules: [before] })
      .mockResolvedValueOnce({ schedules: [after] });
    api.listScheduleRuns.mockImplementation((scheduleId: string) => {
      if (scheduleId === before.id) {
        return new Promise<{ runs: [] }>((resolve) => {
          resolveInitialRuns = resolve;
        });
      }
      return Promise.resolve({ runs: [] });
    });

    await act(async () => {
      root?.render(<Probe />);
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(api.listScheduleRuns).toHaveBeenCalledWith(before.id, expect.anything());

    act(() => {
      refresh();
      refresh();
    });
    resolveInitialRuns({ runs: [] });
    await act(async () => {
      for (let i = 0; i < 6; i += 1) await Promise.resolve();
    });

    expect(api.listSchedules).toHaveBeenCalledTimes(2);
    expect(api.listScheduleRuns).toHaveBeenLastCalledWith(after.id, expect.anything());
  });
});
