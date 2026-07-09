/**
 * LiveBoard RunCard staleness — effective_health drives the "quiet — check?"
 * dead-run render, never run duration.
 *
 * Pure logic (isDeadHealth) covers the classification directly; a mounted
 * render (react-dom/client + act, IntlProvider, no Testing Library — see
 * usePulse.test.tsx / NoDaemonGate.test.tsx for the established pattern)
 * covers what actually reaches the DOM: StatusDot status + stale label.
 */

import { describe, it, expect, afterEach, vi } from "vitest";
import * as React from "react";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { IntlProvider } from "use-intl";
import enMessages from "@/messages/en.json";
import type { RunSummary } from "@/lib/types";

// LiveBoard cards route through <Link> for deep-linking, which needs a full
// RouterProvider tree to resolve `useLinkProps`. These tests only assert on
// StatusDot classes and label text, so a plain anchor stands in for it.
vi.mock("@tanstack/react-router", () => ({
  Link: ({ children, className }: { children?: React.ReactNode; className?: string }) => (
    <div className={className}>{children}</div>
  ),
}));

const { default: LiveBoard, DEAD_HEALTH, isDeadHealth } = await import("./LiveBoard");

describe("isDeadHealth", () => {
  it("is true for every DEAD_HEALTH member", () => {
    for (const health of DEAD_HEALTH) {
      expect(isDeadHealth(health)).toBe(true);
    }
  });

  it("is false for healthy and idle", () => {
    expect(isDeadHealth("healthy")).toBe(false);
    expect(isDeadHealth("idle")).toBe(false);
  });

  it("is false for null and undefined (a fresh run has no health verdict yet)", () => {
    expect(isDeadHealth(null)).toBe(false);
    expect(isDeadHealth(undefined)).toBe(false);
  });
});

function run(overrides: Partial<RunSummary> = {}): RunSummary {
  return {
    run_id: "run-0000000000000001",
    status: "running",
    started_at: 0,
    ...overrides,
  };
}

describe("LiveBoard — RunCard dead-health rendering", () => {
  let container: HTMLDivElement;
  let root: Root;

  afterEach(() => {
    act(() => {
      root.unmount();
    });
    container.remove();
  });

  function mount(runs: RunSummary[]) {
    container = document.createElement("div");
    document.body.appendChild(container);
    act(() => {
      root = createRoot(container);
      root.render(
        <IntlProvider locale="en" messages={enMessages}>
          <LiveBoard activeRuns={runs} activeInvocations={[]} nowSec={100} />
        </IntlProvider>,
      );
    });
  }

  for (const health of ["stale", "orphaned", "zombie", "unresponsive"] as const) {
    it(`renders a static dead dot + stale label for effective_health="${health}"`, () => {
      mount([run({ effective_health: health })]);
      const dot = container.querySelector('[aria-hidden="true"].rounded-full');
      expect(dot).not.toBeNull();
      expect(dot?.className).not.toContain("live-pulse-dot");
      expect(container.textContent).toContain("quiet — check?");
    });
  }

  it("renders a live pulsing dot with no stale label for effective_health=healthy", () => {
    mount([run({ effective_health: "healthy" })]);
    const dot = container.querySelector('[aria-hidden="true"].rounded-full');
    expect(dot?.className).toContain("live-pulse-dot");
    expect(container.textContent).not.toContain("quiet — check?");
  });

  it("renders a live pulsing dot with no stale label when effective_health is null (fresh run)", () => {
    mount([run({ effective_health: null })]);
    const dot = container.querySelector('[aria-hidden="true"].rounded-full');
    expect(dot?.className).toContain("live-pulse-dot");
    expect(container.textContent).not.toContain("quiet — check?");
  });
});
