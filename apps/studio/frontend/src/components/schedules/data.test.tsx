import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";

const api = vi.hoisted(() => ({
  listScheduleSummary: vi.fn(),
  listSchedules: vi.fn(),
  listScheduleRuns: vi.fn(),
}));

vi.mock("@/lib/api", () => api);

const { useSchedulesData } = await import("./data");

describe("useSchedulesData — refresh exclusion", () => {
  let container: HTMLDivElement;
  let root: Root | null;
  let refresh = () => {};

  beforeEach(() => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    api.listScheduleSummary.mockReset();
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

  it("loads 100 schedules and their attributable run slices in one request", async () => {
    const schedules = Array.from({ length: 100 }, (_, index) => ({
      id: `schedule-${index}`,
      name: `Schedule ${index}`,
    }));
    const runSummaries = Object.fromEntries(
      schedules.slice(0, 99).map((schedule, index) => [
        schedule.id,
        index === 98
          ? { state: "error", runs: [] }
          : {
              state: "ok",
              runs: [
                {
                  id: `run-${index}`,
                  schedule_id: schedule.id,
                  status: "completed",
                  fired_at: index,
                },
              ],
            },
      ]),
    );
    api.listScheduleSummary.mockResolvedValue({
      summary_version: 1,
      recent_runs_limit: 25,
      schedules,
      run_summaries: runSummaries,
    });
    api.listSchedules.mockResolvedValue({ schedules });
    api.listScheduleRuns.mockResolvedValue({ runs: [] });
    const snapshot: { current: ReturnType<typeof useSchedulesData> | null } = { current: null };

    function SnapshotProbe() {
      snapshot.current = useSchedulesData();
      return null;
    }

    await act(async () => {
      root?.render(<SnapshotProbe />);
      for (let index = 0; index < 8; index += 1) await Promise.resolve();
    });

    expect(api.listScheduleSummary).toHaveBeenCalledOnce();
    expect(api.listScheduleRuns).not.toHaveBeenCalled();
    expect(snapshot.current?.runs).toHaveLength(98);
    expect(snapshot.current?.runSummaryErrors).toEqual(new Set(["schedule-98", "schedule-99"]));
  });

  it("coalesces manual refreshes while the current schedules request is unresolved", async () => {
    const emptySummary = {
      summary_version: 1,
      recent_runs_limit: 25,
      schedules: [],
      run_summaries: {},
    };
    let resolveInitial!: (value: typeof emptySummary) => void;
    api.listScheduleSummary.mockReturnValueOnce(
      new Promise<typeof emptySummary>((resolve) => {
        resolveInitial = resolve;
      }),
    );

    await act(async () => root?.render(<Probe />));
    act(() => {
      refresh();
      refresh();
    });
    expect(api.listScheduleSummary).toHaveBeenCalledOnce();

    api.listScheduleSummary.mockResolvedValueOnce(emptySummary);
    await act(async () => {
      resolveInitial(emptySummary);
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(api.listScheduleSummary).toHaveBeenCalledTimes(2);

    api.listScheduleSummary.mockResolvedValueOnce(emptySummary);
    await act(async () => {
      refresh();
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(api.listScheduleSummary).toHaveBeenCalledTimes(3);
  });
});
