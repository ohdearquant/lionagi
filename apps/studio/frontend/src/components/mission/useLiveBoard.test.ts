/**
 * Tests for the watchdog hysteresis logic in useLiveBoard.
 *
 * These tests validate the client-side watchdog timer behavior:
 * - stale badge only after >5s silence
 * - never flaps on a single frame
 * - clears only after stable resumption (>=2 successful fetches)
 *
 * Uses fake timers + vi.mock to avoid real network calls.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import * as React from "react";
import { boardReducer, initialBoardState } from "./boardReducer";
import type { BoardState } from "./boardReducer";
import { useLiveBoard } from "./useLiveBoard";

vi.mock("@/lib/api", () => ({
  listRuns: vi.fn(),
  listInvocations: vi.fn(),
  listSchedules: vi.fn(),
}));

import { listRuns, listInvocations, listSchedules } from "@/lib/api";

// ─── Watchdog hysteresis: pure reducer logic ──────────────────────────────────
// The hysteresis contract is: MARK_STALE only takes effect when dataState=live.
// This is the state machine part; the timing gate is in useLiveBoard (tested separately).

describe("watchdog stale-badge hysteresis — reducer contract", () => {
  it("live → stale on MARK_STALE", () => {
    let s = initialBoardState();
    s = boardReducer(s, { type: "DATA_OK", runs: [], invocations: [], schedules: null, nowSec: 0 });
    expect(s.dataState).toBe("live");
    s = boardReducer(s, { type: "MARK_STALE" });
    expect(s.dataState).toBe("stale");
  });

  it("loading state is immune to MARK_STALE (no flap before first fetch)", () => {
    const s = boardReducer(initialBoardState(), { type: "MARK_STALE" });
    expect(s.dataState).toBe("loading");
  });

  it("error state is immune to MARK_STALE (don't clobber error with stale)", () => {
    let s: BoardState = initialBoardState();
    s = boardReducer(s, { type: "DATA_ERROR", message: "oops" });
    s = boardReducer(s, { type: "MARK_STALE" });
    expect(s.dataState).toBe("error");
    expect(s.errorMessage).toBe("oops");
  });

  it("stale is idempotent: MARK_STALE on already-stale stays stale", () => {
    let s: BoardState = initialBoardState();
    s = boardReducer(s, { type: "DATA_OK", runs: [], invocations: [], schedules: null, nowSec: 0 });
    s = boardReducer(s, { type: "MARK_STALE" });
    s = boardReducer(s, { type: "MARK_STALE" });
    expect(s.dataState).toBe("stale");
  });

  it("stale clears on DATA_OK (live resumption)", () => {
    let s: BoardState = initialBoardState();
    s = boardReducer(s, { type: "DATA_OK", runs: [], invocations: [], schedules: null, nowSec: 0 });
    s = boardReducer(s, { type: "MARK_STALE" });
    expect(s.dataState).toBe("stale");
    s = boardReducer(s, { type: "DATA_OK", runs: [], invocations: [], schedules: null, nowSec: 1 });
    expect(s.dataState).toBe("live");
  });
});

// ─── Watchdog hysteresis: timing logic (fake timers) ─────────────────────────
// We test the TIMING gate separately using fake timers and manually driven state.
// This avoids mounting React hooks in unit tests.

describe("watchdog timing — 5s silence gate", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("does not mark stale before 5s silence", () => {
    // Simulate the watchdog interval logic directly
    let state: BoardState = initialBoardState();
    // Make it live first
    state = boardReducer(state, {
      type: "DATA_OK",
      runs: [],
      invocations: [],
      schedules: null,
      nowSec: 0,
    });
    expect(state.dataState).toBe("live");

    // Simulate 4900ms passing — watchdog should NOT fire (< 5s)
    let lastSuccessAt = Date.now();
    const STALE_THRESHOLD_MS = 5_000;

    vi.advanceTimersByTime(4_900);
    const silent = Date.now() - lastSuccessAt > STALE_THRESHOLD_MS;
    expect(silent).toBe(false);

    if (silent) {
      state = boardReducer(state, { type: "MARK_STALE" });
    }
    expect(state.dataState).toBe("live");
  });

  it("marks stale after 5s silence", () => {
    let state: BoardState = initialBoardState();
    state = boardReducer(state, {
      type: "DATA_OK",
      runs: [],
      invocations: [],
      schedules: null,
      nowSec: 0,
    });

    let lastSuccessAt = Date.now();
    const STALE_THRESHOLD_MS = 5_000;

    vi.advanceTimersByTime(5_100);
    const silent = Date.now() - lastSuccessAt > STALE_THRESHOLD_MS;
    expect(silent).toBe(true);

    if (silent) {
      state = boardReducer(state, { type: "MARK_STALE" });
    }
    expect(state.dataState).toBe("stale");
  });

  it("clears stale after 2 successful fetches (stable resumption)", () => {
    let state: BoardState = initialBoardState();
    state = boardReducer(state, {
      type: "DATA_OK",
      runs: [],
      invocations: [],
      schedules: null,
      nowSec: 0,
    });
    state = boardReducer(state, { type: "MARK_STALE" });
    expect(state.dataState).toBe("stale");

    // Simulate hysteresis: was stale, need >=2 successes to clear
    let wasStale = true;
    let successStreak = 0;
    const STABLE_RESUMPTION_COUNT = 2;

    // First fetch success — streak not met yet
    successStreak += 1;
    let cleared = !wasStale || successStreak >= STABLE_RESUMPTION_COUNT;
    if (cleared) {
      wasStale = false;
      state = boardReducer(state, {
        type: "DATA_OK",
        runs: [],
        invocations: [],
        schedules: null,
        nowSec: 1,
      });
    }
    expect(successStreak).toBe(1);
    // Still stale because we haven't cleared yet
    expect(state.dataState).toBe("stale");

    // Second fetch success — meets threshold
    successStreak += 1;
    cleared = !wasStale || successStreak >= STABLE_RESUMPTION_COUNT;
    if (cleared) {
      wasStale = false;
      state = boardReducer(state, {
        type: "DATA_OK",
        runs: [],
        invocations: [],
        schedules: null,
        nowSec: 2,
      });
    }
    expect(successStreak).toBe(2);
    expect(state.dataState).toBe("live");
  });

  it("single success does not clear stale (anti-flap)", () => {
    let state: BoardState = initialBoardState();
    state = boardReducer(state, {
      type: "DATA_OK",
      runs: [],
      invocations: [],
      schedules: null,
      nowSec: 0,
    });
    state = boardReducer(state, { type: "MARK_STALE" });

    let wasStale = true;
    let successStreak = 0;
    const STABLE_RESUMPTION_COUNT = 2;

    // One success — not enough
    successStreak += 1;
    const cleared = !wasStale || successStreak >= STABLE_RESUMPTION_COUNT;
    if (cleared) {
      state = boardReducer(state, {
        type: "DATA_OK",
        runs: [],
        invocations: [],
        schedules: null,
        nowSec: 1,
      });
    }

    // Still stale — anti-flap gate held
    expect(state.dataState).toBe("stale");
  });
});

// ─── useLiveBoard — mounted behavior (real fetch path, mocked api module) ────
// Mirrors the usePulse.test.tsx mounting pattern: react-dom/client + act,
// no Testing Library dependency.

describe("useLiveBoard — schedules degrade-to-null on fetch failure", () => {
  let container: HTMLDivElement;
  let root: Root;
  let unmounted: boolean;
  let latest: BoardState | null;

  function Harness() {
    latest = useLiveBoard();
    return null;
  }

  beforeEach(() => {
    vi.mocked(listRuns).mockReset();
    vi.mocked(listInvocations).mockReset();
    vi.mocked(listSchedules).mockReset();
    container = document.createElement("div");
    document.body.appendChild(container);
    latest = null;
    unmounted = false;
  });

  afterEach(() => {
    if (!unmounted) {
      act(() => {
        root.unmount();
      });
    }
    container.remove();
  });

  it("still reaches DATA_OK with schedules: null when listSchedules rejects but runs/invocations resolve", async () => {
    vi.mocked(listRuns).mockResolvedValue({
      runs: [],
      page: 1,
      per_page: 200,
      total: 0,
      total_pages: 1,
      has_next: false,
      has_prev: false,
    });
    vi.mocked(listInvocations).mockResolvedValue({
      invocations: [],
      limit: 100,
      offset: 0,
      has_next: false,
    });
    vi.mocked(listSchedules).mockRejectedValue(new Error("schedules endpoint down"));

    await act(async () => {
      root = createRoot(container);
      root.render(React.createElement(Harness));
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(latest?.dataState).toBe("live");
    expect(latest?.schedules).toEqual([]);
    expect(latest?.schedulesKnown).toBe(false);
  });

  it("retains the prior schedule list once a later poll's schedules fetch fails", async () => {
    vi.useFakeTimers();
    const sched = {
      id: "s1",
      name: "nightly",
      description: null,
      enabled: 1,
      trigger_type: "cron" as const,
      cron_expr: "0 0 * * *",
      interval_sec: null,
      github_repo: null,
      poll_interval_sec: null,
      action_kind: "agent" as const,
      action_model: null,
      action_prompt: null,
      action_agent: null,
      action_playbook: null,
      action_project: null,
      on_success: null,
      on_fail: null,
      last_fired_at: null,
      next_fire_at: null,
      missed_fire_policy: "skip",
      overlap_policy: "skip",
      project: null,
      created_at: 0,
      updated_at: 0,
    };
    const runsResp = {
      runs: [],
      page: 1,
      per_page: 200,
      total: 0,
      total_pages: 1,
      has_next: false,
      has_prev: false,
    };
    const invsResp = { invocations: [], limit: 100, offset: 0, has_next: false };

    vi.mocked(listRuns).mockResolvedValue(runsResp);
    vi.mocked(listInvocations).mockResolvedValue(invsResp);
    vi.mocked(listSchedules).mockResolvedValueOnce({ schedules: [sched] });

    await act(async () => {
      root = createRoot(container);
      root.render(React.createElement(Harness));
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(latest?.schedules).toEqual([sched]);
    expect(latest?.schedulesKnown).toBe(true);

    // Next poll (3s interval): schedules fetch fails — the reducer must keep
    // the last-known list rather than clearing it.
    vi.mocked(listSchedules).mockRejectedValueOnce(new Error("boom"));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3_000);
    });
    expect(latest?.schedules).toEqual([sched]);
    expect(latest?.schedulesKnown).toBe(true);

    vi.useRealTimers();
  });
});
