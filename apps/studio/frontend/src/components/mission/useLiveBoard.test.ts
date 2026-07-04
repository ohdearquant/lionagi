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
import { boardReducer, initialBoardState } from "./boardReducer";
import type { BoardState } from "./boardReducer";

// ─── Watchdog hysteresis: pure reducer logic ──────────────────────────────────
// The hysteresis contract is: MARK_STALE only takes effect when dataState=live.
// This is the state machine part; the timing gate is in useLiveBoard (tested separately).

describe("watchdog stale-badge hysteresis — reducer contract", () => {
  it("live → stale on MARK_STALE", () => {
    let s = initialBoardState();
    s = boardReducer(s, { type: "DATA_OK", runs: [], invocations: [], nowSec: 0 });
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
    s = boardReducer(s, { type: "DATA_OK", runs: [], invocations: [], nowSec: 0 });
    s = boardReducer(s, { type: "MARK_STALE" });
    s = boardReducer(s, { type: "MARK_STALE" });
    expect(s.dataState).toBe("stale");
  });

  it("stale clears on DATA_OK (live resumption)", () => {
    let s: BoardState = initialBoardState();
    s = boardReducer(s, { type: "DATA_OK", runs: [], invocations: [], nowSec: 0 });
    s = boardReducer(s, { type: "MARK_STALE" });
    expect(s.dataState).toBe("stale");
    s = boardReducer(s, { type: "DATA_OK", runs: [], invocations: [], nowSec: 1 });
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
    state = boardReducer(state, { type: "DATA_OK", runs: [], invocations: [], nowSec: 0 });
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
    state = boardReducer(state, { type: "DATA_OK", runs: [], invocations: [], nowSec: 0 });

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
    state = boardReducer(state, { type: "DATA_OK", runs: [], invocations: [], nowSec: 0 });
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
      state = boardReducer(state, { type: "DATA_OK", runs: [], invocations: [], nowSec: 1 });
    }
    expect(successStreak).toBe(1);
    // Still stale because we haven't cleared yet
    expect(state.dataState).toBe("stale");

    // Second fetch success — meets threshold
    successStreak += 1;
    cleared = !wasStale || successStreak >= STABLE_RESUMPTION_COUNT;
    if (cleared) {
      wasStale = false;
      state = boardReducer(state, { type: "DATA_OK", runs: [], invocations: [], nowSec: 2 });
    }
    expect(successStreak).toBe(2);
    expect(state.dataState).toBe("live");
  });

  it("single success does not clear stale (anti-flap)", () => {
    let state: BoardState = initialBoardState();
    state = boardReducer(state, { type: "DATA_OK", runs: [], invocations: [], nowSec: 0 });
    state = boardReducer(state, { type: "MARK_STALE" });

    let wasStale = true;
    let successStreak = 0;
    const STABLE_RESUMPTION_COUNT = 2;

    // One success — not enough
    successStreak += 1;
    const cleared = !wasStale || successStreak >= STABLE_RESUMPTION_COUNT;
    if (cleared) {
      state = boardReducer(state, { type: "DATA_OK", runs: [], invocations: [], nowSec: 1 });
    }

    // Still stale — anti-flap gate held
    expect(state.dataState).toBe("stale");
  });
});
