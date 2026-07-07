/**
 * usePulse stale-window guard.
 *
 * Two layers: a source-contract sweep (fast, documents the guard shape) plus
 * behavioral tests that actually mount the hook via `react-dom/client` +
 * `act` (no Testing Library dependency needed) and drive the real
 * stale-window race — a late response from a previous `window_` must never
 * commit into the current one.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import * as fs from "node:fs";
import * as path from "node:path";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import * as React from "react";
import { usePulse } from "./usePulse";
import type { PulseState } from "./usePulse";
import type { ActivityStats, ActivityWindow } from "@/lib/api";

const SRC = fs.readFileSync(path.resolve(__dirname, "usePulse.ts"), "utf-8");

describe("usePulse.ts — stale-window guard (source contract)", () => {
  it("does not use a shared activeRef for request validity", () => {
    expect(SRC).not.toMatch(/activeRef/);
  });

  it("declares an effect-local validity flag inside the window_ effect", () => {
    expect(SRC).toMatch(/let active = true;/);
  });

  it("checks the local flag before committing a successful refresh", () => {
    expect(SRC).toMatch(
      /if \(!active\) return;\s*\n\s*setState\(\{ data, error: null, loading: false \}\);/,
    );
  });

  it("checks the local flag before committing a failed refresh", () => {
    expect(SRC).toMatch(/catch \(err\) \{\s*\n\s*if \(!active\) return;/);
  });

  it("invalidates the flag on effect cleanup", () => {
    expect(SRC).toMatch(/return \(\) => \{\s*\n\s*active = false;/);
  });

  it("re-runs the effect (and resets the flag) when the window changes", () => {
    expect(SRC).toMatch(/\}, \[window_\]\);/);
  });

  it("keeps last-known data and surfaces the error on a failed refresh", () => {
    expect(SRC).toMatch(/data: prev\.data,/);
    expect(SRC).toMatch(/error: err instanceof Error \? err\.message : ""/);
  });
});

vi.mock("@/lib/api", () => ({
  getActivityStats: vi.fn(),
}));

import { getActivityStats } from "@/lib/api";

function makeStats(window: ActivityWindow, total: number): ActivityStats {
  return { window, buckets: [], completion_rate: null, total };
}

/** Deferred promise so the test controls exactly when a fetch resolves. */
function deferred<T>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

describe("usePulse — mounted behavior", () => {
  let container: HTMLDivElement;
  let root: Root;
  let unmounted: boolean;
  let latest: PulseState | null;

  function Harness({ window_ }: { window_: ActivityWindow }) {
    latest = usePulse(window_);
    return null;
  }

  beforeEach(() => {
    vi.mocked(getActivityStats).mockReset();
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
    const d = deferred<ActivityStats>();
    vi.mocked(getActivityStats).mockReturnValueOnce(d.promise);

    act(() => {
      root = createRoot(container);
      root.render(React.createElement(Harness, { window_: "24h" }));
    });
    expect(latest?.loading).toBe(true);

    await act(async () => {
      d.resolve(makeStats("24h", 5));
      await d.promise;
    });

    expect(latest?.data?.window).toBe("24h");
    expect(latest?.data?.total).toBe(5);
    expect(latest?.error).toBeNull();
  });

  it("does not let a slow prior-window response overwrite the current window's state", async () => {
    const slow24h = deferred<ActivityStats>();
    const fast7d = deferred<ActivityStats>();
    vi.mocked(getActivityStats).mockReturnValueOnce(slow24h.promise);

    act(() => {
      root = createRoot(container);
      root.render(React.createElement(Harness, { window_: "24h" }));
    });

    // Toggle to "7d" before the "24h" request resolves — this tears down the
    // "24h" effect (active = false) and starts a fresh one for "7d".
    vi.mocked(getActivityStats).mockReturnValueOnce(fast7d.promise);
    act(() => {
      root.render(React.createElement(Harness, { window_: "7d" }));
    });

    await act(async () => {
      fast7d.resolve(makeStats("7d", 42));
      await fast7d.promise;
    });
    expect(latest?.data?.window).toBe("7d");

    // The stale "24h" request finally resolves. It must be a no-op.
    await act(async () => {
      slow24h.resolve(makeStats("24h", 999));
      await slow24h.promise.catch(() => {});
    });

    expect(latest?.data?.window).toBe("7d");
    expect(latest?.data?.total).toBe(42);
  });

  it("a stale rejection from a torn-down window effect does not surface as the current error", async () => {
    const staleWindow = deferred<ActivityStats>();
    const freshWindow = deferred<ActivityStats>();
    vi.mocked(getActivityStats).mockReturnValueOnce(staleWindow.promise);

    act(() => {
      root = createRoot(container);
      root.render(React.createElement(Harness, { window_: "24h" }));
    });

    vi.mocked(getActivityStats).mockReturnValueOnce(freshWindow.promise);
    act(() => {
      root.render(React.createElement(Harness, { window_: "7d" }));
    });

    await act(async () => {
      freshWindow.resolve(makeStats("7d", 7));
      await freshWindow.promise;
    });
    expect(latest?.data?.total).toBe(7);
    expect(latest?.error).toBeNull();

    await act(async () => {
      staleWindow.reject(new Error("boom - stale 24h failure"));
      await staleWindow.promise.catch(() => {});
    });

    // The torn-down effect's rejection must not clobber the live "7d" state.
    expect(latest?.error).toBeNull();
    expect(latest?.data?.total).toBe(7);
  });

  it("keeps last-known data and surfaces the error message on a failed refresh", async () => {
    const first = deferred<ActivityStats>();
    vi.mocked(getActivityStats).mockReturnValueOnce(first.promise);

    act(() => {
      root = createRoot(container);
      root.render(React.createElement(Harness, { window_: "24h" }));
    });

    await act(async () => {
      first.resolve(makeStats("24h", 3));
      await first.promise;
    });
    expect(latest?.data?.total).toBe(3);

    // Simulate a focus-triggered refresh that fails.
    const second = deferred<ActivityStats>();
    vi.mocked(getActivityStats).mockReturnValueOnce(second.promise);
    await act(async () => {
      window.dispatchEvent(new Event("focus"));
      second.reject(new Error("network down"));
      await second.promise.catch(() => {});
    });

    expect(latest?.error).toBe("network down");
    expect(latest?.data?.total).toBe(3);
    expect(latest?.loading).toBe(false);
  });

  it("removes the focus listener and clears the interval timer on unmount", () => {
    const addSpy = vi.spyOn(window, "addEventListener");
    const removeSpy = vi.spyOn(window, "removeEventListener");
    const clearSpy = vi.spyOn(global, "clearInterval");
    vi.mocked(getActivityStats).mockReturnValue(new Promise(() => {}));

    act(() => {
      root = createRoot(container);
      root.render(React.createElement(Harness, { window_: "24h" }));
    });
    expect(addSpy).toHaveBeenCalledWith("focus", expect.any(Function));

    act(() => {
      root.unmount();
    });
    unmounted = true;
    expect(removeSpy).toHaveBeenCalledWith("focus", expect.any(Function));
    expect(clearSpy).toHaveBeenCalled();

    addSpy.mockRestore();
    removeSpy.mockRestore();
    clearSpy.mockRestore();
  });
});
