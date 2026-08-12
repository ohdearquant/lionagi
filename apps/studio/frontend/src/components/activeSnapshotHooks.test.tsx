import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useFleet } from "./fleet/useFleet";
import { useLiveBoard } from "./mission/useLiveBoard";

vi.mock("@/lib/api", () => ({
  getActiveSnapshot: vi.fn(),
  listRuns: vi.fn(),
  listInvocations: vi.fn(),
  listSchedules: vi.fn().mockResolvedValue({ schedules: [] }),
  listAttentionDispositions: vi.fn().mockResolvedValue({}),
  listGatedPlays: vi.fn().mockResolvedValue([]),
}));

import {
  getActiveSnapshot,
  listAttentionDispositions,
  listGatedPlays,
  listInvocations,
  listRuns,
  listSchedules,
} from "@/lib/api";
import type { ActiveSnapshotResponse } from "@/lib/api";

const snapshot: ActiveSnapshotResponse = {
  snapshot_version: "active:5:2",
  snapshot_at: 100,
  active_runs: [
    {
      run_id: "run-live",
      status: "running",
      started_at: 1,
      effective_health: "healthy" as const,
      project: "org/alpha",
    },
  ],
  active_run_total: 5,
  active_run_omitted: 4,
  active_invocations: [
    {
      id: "inv-live",
      skill: "review",
      plugin: null,
      prompt: null,
      started_at: 1,
      ended_at: null,
      status: "running",
      session_count: 1,
      created_at: 1,
      updated_at: 1,
      node_metadata: null,
      health: "healthy" as const,
    },
  ],
  active_invocation_total: 2,
  active_invocation_omitted: 1,
  recent_runs: [{ run_id: "run-done", status: "completed", started_at: 0 }],
  recent_run_has_more: false,
  recent_invocations: [
    {
      id: "inv-failed",
      skill: "failed-review",
      plugin: null,
      prompt: null,
      started_at: 0,
      ended_at: 1,
      status: "failed",
      session_count: 0,
      created_at: 0,
      updated_at: 1,
      node_metadata: null,
    },
  ],
  recent_invocation_has_more: false,
  complete: false,
};

describe("Fleet and Mission consume one bounded active snapshot", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    vi.mocked(getActiveSnapshot).mockReset().mockResolvedValue(snapshot);
    vi.mocked(listRuns).mockReset();
    vi.mocked(listInvocations).mockReset();
    vi.mocked(listSchedules).mockClear();
    vi.mocked(listAttentionDispositions).mockClear();
    vi.mocked(listGatedPlays).mockClear();
    container = document.createElement("div");
    document.body.appendChild(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
  });

  it("Fleet sends its scope once and retains exact omission metadata", async () => {
    let latest: ReturnType<typeof useFleet> | null = null;
    function Harness() {
      latest = useFleet({ project: "org/alpha", search: "review" });
      return null;
    }

    await act(async () => {
      root = createRoot(container);
      root.render(<Harness />);
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(getActiveSnapshot).toHaveBeenCalledWith({
      run_limit: 200,
      invocation_limit: 200,
      recent_limit: 200,
      project: "org/alpha",
      project_null: false,
      search: "review",
    });
    expect(listRuns).not.toHaveBeenCalled();
    expect(listInvocations).not.toHaveBeenCalled();
    const fleetState = latest as unknown as ReturnType<typeof useFleet>;
    expect(fleetState.activeRunTotal).toBe(5);
    expect(fleetState.activeRunOmitted).toBe(4);
    expect(fleetState.activeInvocationTotal).toBe(2);
    expect(fleetState.activeInvocationOmitted).toBe(1);
    expect(fleetState.snapshotVersion).toBe("active:5:2");
  });

  it("Mission uses snapshot rows for live and recent projections", async () => {
    let latest: ReturnType<typeof useLiveBoard> | null = null;
    function Harness() {
      latest = useLiveBoard();
      return null;
    }

    await act(async () => {
      root = createRoot(container);
      root.render(<Harness />);
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(getActiveSnapshot).toHaveBeenCalledWith({
      run_limit: 200,
      invocation_limit: 100,
      recent_limit: 200,
    });
    expect(listRuns).not.toHaveBeenCalled();
    expect(listInvocations).not.toHaveBeenCalled();
    const missionState = latest as unknown as ReturnType<typeof useLiveBoard>;
    expect(missionState.activeRuns.map((row) => row.run_id)).toEqual(["run-live"]);
    expect(missionState.recentRuns.map((row) => row.run_id)).toEqual(["run-done"]);
    expect(missionState.activeRunTotal).toBe(5);
    expect(missionState.activeInvocationTotal).toBe(2);
  });
});
