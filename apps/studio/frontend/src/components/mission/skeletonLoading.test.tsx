/**
 * Mission Control loading-skeleton tests.
 *
 * Two layers:
 * - Reducer behavior (real, no mocking needed): "loading" only precedes the
 *   first successful/failed fetch and is never re-entered by a background
 *   refresh — this is the exact gate MissionControl uses to show skeletons
 *   only on first load, never on a poll.
 * - Source contract (matches the existing history/ test style — no
 *   @testing-library/react in this project): the skeleton exports exist,
 *   are wired into MissionControl ahead of the empty/live branches, and are
 *   built from the shared shimmer atom so reduced-motion + theming stay
 *   centralized.
 */

import { describe, it, expect } from "vitest";
import * as fs from "node:fs";
import * as path from "node:path";
import { boardReducer, initialBoardState } from "./boardReducer";
import type { BoardState } from "./boardReducer";

const MISSION_DIR = path.resolve(__dirname);
const GLOBALS_CSS = path.resolve(__dirname, "../../globals.css");

// ─── Reducer: "loading" is a one-time gate, not a recurring state ────────────

describe("dataState — first-load gate never re-fires on refresh", () => {
  it("starts as loading before any fetch settles", () => {
    expect(initialBoardState().dataState).toBe("loading");
  });

  it("first DATA_OK flips loading → live and later polls stay live", () => {
    let s: BoardState = initialBoardState();
    s = boardReducer(s, { type: "DATA_OK", runs: [], invocations: [], schedules: null, nowSec: 1 });
    expect(s.dataState).toBe("live");

    // Simulate several subsequent background polls — none of them should
    // ever push dataState back to "loading".
    for (let i = 2; i <= 5; i++) {
      s = boardReducer(s, {
        type: "DATA_OK",
        runs: [],
        invocations: [],
        schedules: null,
        nowSec: i,
      });
      expect(s.dataState).not.toBe("loading");
    }
  });

  it("a failed first fetch also leaves loading (goes to error, not back to loading)", () => {
    let s: BoardState = initialBoardState();
    s = boardReducer(s, { type: "DATA_ERROR", message: "boom" });
    expect(s.dataState).toBe("error");
    expect(s.dataState).not.toBe("loading");
  });

  it("no reducer action can set dataState back to loading once left", () => {
    let s: BoardState = initialBoardState();
    s = boardReducer(s, { type: "DATA_OK", runs: [], invocations: [], schedules: null, nowSec: 1 });
    s = boardReducer(s, { type: "MARK_STALE" });
    expect(s.dataState).toBe("stale");
    s = boardReducer(s, { type: "DATA_ERROR", message: "refresh failed" });
    expect(s.dataState).toBe("error");
    // Recovery goes to "live", never "loading" — a background hiccup must
    // never re-trigger the first-load skeleton.
    s = boardReducer(s, { type: "DATA_OK", runs: [], invocations: [], schedules: null, nowSec: 2 });
    expect(s.dataState).toBe("live");
  });
});

// ─── Source contract: skeleton exports exist and mirror real shapes ──────────

function read(file: string): string {
  return fs.readFileSync(path.join(MISSION_DIR, file), "utf-8");
}

describe("section skeleton exports — shape-mirroring components", () => {
  const cases: { file: string; symbol: string }[] = [
    { file: "AttentionQueue.tsx", symbol: "AttentionQueueSkeleton" },
    { file: "LiveBoard.tsx", symbol: "LiveBoardSkeleton" },
    { file: "Pulse.tsx", symbol: "PulseSkeleton" },
    { file: "RecentRuns.tsx", symbol: "RecentRunsSkeleton" },
  ];

  for (const { file, symbol } of cases) {
    it(`${file} exports ${symbol}`, () => {
      const src = read(file);
      expect(src).toMatch(new RegExp(`export function ${symbol}`));
    });

    it(`${symbol} is hidden from assistive tech (aria-hidden placeholder, not real content)`, () => {
      const src = read(file);
      const fnMatch = src.match(new RegExp(`export function ${symbol}[\\s\\S]*?\\n}`));
      expect(fnMatch).not.toBeNull();
      expect(fnMatch?.[0]).toMatch(/aria-hidden="true"/);
    });

    it(`${symbol} builds placeholders from the shared shimmer atom`, () => {
      const src = read(file);
      expect(src).toMatch(/from "@\/components\/ui\/Skeleton"/);
      const fnMatch = src.match(new RegExp(`export function ${symbol}[\\s\\S]*?\\n}`));
      expect(fnMatch?.[0]).toMatch(/<Skeleton/);
    });
  }
});

describe("MissionControl.tsx — wires skeletons ahead of empty/live branches", () => {
  const src = read("MissionControl.tsx");

  it("imports all four section skeletons", () => {
    expect(src).toMatch(/AttentionQueueSkeleton/);
    expect(src).toMatch(/LiveBoardSkeleton/);
    expect(src).toMatch(/PulseSkeleton/);
    expect(src).toMatch(/RecentRunsSkeleton/);
  });

  it('gates skeletons on dataState === "loading" (not systemEmpty, not live)', () => {
    expect(src).toMatch(/isInitialLoad = board\.dataState === "loading"/);
  });

  it("checks isInitialLoad before board.systemEmpty (skeleton wins the initial render)", () => {
    const initialLoadIdx = src.indexOf("isInitialLoad ?");
    const systemEmptyIdx = src.indexOf("board.systemEmpty ?");
    expect(initialLoadIdx).toBeGreaterThan(-1);
    expect(systemEmptyIdx).toBeGreaterThan(-1);
    expect(initialLoadIdx).toBeLessThan(systemEmptyIdx);
  });
});

// ─── Shared shimmer atom + reduced-motion guard stay intact ──────────────────

describe("shared skeleton styling — shimmer + reduced-motion + tokens", () => {
  it("ui/Skeleton.tsx marks its box aria-hidden and applies the shared .skeleton class", () => {
    const src = fs.readFileSync(path.resolve(MISSION_DIR, "../ui/Skeleton.tsx"), "utf-8");
    expect(src).toMatch(/aria-hidden="true"/);
    expect(src).toMatch(/skeleton/);
  });

  it("globals.css defines the shimmer keyframe using design-token surfaces", () => {
    const css = fs.readFileSync(GLOBALS_CSS, "utf-8");
    expect(css).toMatch(/@keyframes skeleton-shimmer/);
    expect(css).toMatch(/var\(--surface-overlay\)/);
    expect(css).toMatch(/var\(--surface-input\)/);
  });

  it("globals.css freezes animations under prefers-reduced-motion (static fallback)", () => {
    const css = fs.readFileSync(GLOBALS_CSS, "utf-8");
    expect(css).toMatch(/@media \(prefers-reduced-motion: reduce\)/);
  });
});
