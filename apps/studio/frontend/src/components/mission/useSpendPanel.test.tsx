/**
 * useSpendPanel — mirrors usePulse.test.tsx's structure: a source-contract
 * sweep (documents the same stale-window guard shape) plus behavioral tests
 * that mount the hook and drive a real stale-window race.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import * as fs from "node:fs";
import * as path from "node:path";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import * as React from "react";
import { useSpendPanel } from "./useSpendPanel";
import type { SpendPanelState } from "./useSpendPanel";
import type { SpendStats, ActivityWindow } from "@/lib/api";

const SRC = fs.readFileSync(path.resolve(__dirname, "useSpendPanel.ts"), "utf-8");

describe("useSpendPanel.ts — stale-window guard (source contract)", () => {
  it("declares an effect-local validity flag inside the window_ effect", () => {
    expect(SRC).toMatch(/let active = true;/);
  });

  it("checks the local flag before committing a successful refresh", () => {
    expect(SRC).toMatch(
      /if \(!active\) return;\s*\n\s*setState\(\{ data, error: null, loading: false \}\);/,
    );
  });

  it("invalidates the flag on effect cleanup", () => {
    expect(SRC).toMatch(/return \(\) => \{\s*\n\s*active = false;/);
  });

  it("re-runs the effect (and resets the flag) when the window changes", () => {
    expect(SRC).toMatch(/\}, \[window_\]\);/);
  });
});

vi.mock("@/lib/api", () => ({
  getSpendStats: vi.fn(),
}));

import { getSpendStats } from "@/lib/api";

function makeSpend(window: ActivityWindow, overrides: Partial<SpendStats> = {}): SpendStats {
  return {
    window,
    reported_usd: 12.5,
    reported_count: 3,
    unreported_count: 1,
    total_count: 4,
    coverage: 0.75,
    ...overrides,
  };
}

function deferred<T>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

describe("useSpendPanel — mounted behavior", () => {
  let container: HTMLDivElement;
  let root: Root;
  let unmounted: boolean;
  let latest: SpendPanelState | null;

  function Harness({ window_ }: { window_: ActivityWindow }) {
    latest = useSpendPanel(window_);
    return null;
  }

  beforeEach(() => {
    vi.mocked(getSpendStats).mockReset();
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

  it("commits data once the current-window request resolves", async () => {
    const d = deferred<SpendStats>();
    vi.mocked(getSpendStats).mockReturnValueOnce(d.promise);

    act(() => {
      root = createRoot(container);
      root.render(React.createElement(Harness, { window_: "24h" }));
    });
    expect(latest?.loading).toBe(true);

    await act(async () => {
      d.resolve(makeSpend("24h"));
      await d.promise;
    });

    expect(latest?.data?.window).toBe("24h");
    expect(latest?.data?.reported_usd).toBe(12.5);
    expect(latest?.error).toBeNull();
  });

  it("preserves a null reported_usd (all-unreported window) rather than treating it as falsy/absent", async () => {
    const d = deferred<SpendStats>();
    vi.mocked(getSpendStats).mockReturnValueOnce(d.promise);

    act(() => {
      root = createRoot(container);
      root.render(React.createElement(Harness, { window_: "24h" }));
    });

    await act(async () => {
      d.resolve(
        makeSpend("24h", {
          reported_usd: null,
          reported_count: 0,
          unreported_count: 2,
          coverage: 0,
        }),
      );
      await d.promise;
    });

    expect(latest?.data?.reported_usd).toBeNull();
    expect(latest?.data?.unreported_count).toBe(2);
  });

  it("does not let a slow prior-window response overwrite the current window's state", async () => {
    const slow24h = deferred<SpendStats>();
    const fast7d = deferred<SpendStats>();
    vi.mocked(getSpendStats).mockReturnValueOnce(slow24h.promise);

    act(() => {
      root = createRoot(container);
      root.render(React.createElement(Harness, { window_: "24h" }));
    });

    vi.mocked(getSpendStats).mockReturnValueOnce(fast7d.promise);
    act(() => {
      root.render(React.createElement(Harness, { window_: "7d" }));
    });

    await act(async () => {
      fast7d.resolve(makeSpend("7d", { reported_usd: 99 }));
      await fast7d.promise;
    });
    expect(latest?.data?.window).toBe("7d");

    await act(async () => {
      slow24h.resolve(makeSpend("24h", { reported_usd: 1 }));
      await slow24h.promise;
    });

    // The late "24h" response must not clobber the committed "7d" state.
    expect(latest?.data?.window).toBe("7d");
    expect(latest?.data?.reported_usd).toBe(99);
  });

  it("keeps last-known data and surfaces the error on a failed same-window refresh", async () => {
    const first = deferred<SpendStats>();
    vi.mocked(getSpendStats).mockReturnValueOnce(first.promise);

    act(() => {
      root = createRoot(container);
      root.render(React.createElement(Harness, { window_: "24h" }));
    });

    await act(async () => {
      first.resolve(makeSpend("24h"));
      await first.promise;
    });
    expect(latest?.data?.reported_usd).toBe(12.5);

    // A focus-triggered refresh of the same window that fails — the window
    // prop itself never changes, so the effect's data-reset never re-fires.
    const second = deferred<SpendStats>();
    vi.mocked(getSpendStats).mockReturnValueOnce(second.promise);
    await act(async () => {
      window.dispatchEvent(new Event("focus"));
      second.reject(new Error("network down"));
      await second.promise.catch(() => {});
    });

    expect(latest?.data?.reported_usd).toBe(12.5); // last-known value retained
    expect(latest?.error).toBe("network down");
    expect(latest?.loading).toBe(false);
  });
});
