/**
 * LiveBoard RunCard staleness — effective_health drives the "quiet — check?"
 * dead-run render, never run duration.
 *
 * Pure logic (isDeadHealth) covers the classification directly; a mounted
 * render (react-dom/client + act, IntlProvider, no Testing Library — see
 * usePulse.test.tsx / NoDaemonGate.test.tsx for the established pattern)
 * covers what actually reaches the DOM: StatusDot status + stale label.
 */

import { describe, it, expect, afterEach, beforeEach, vi } from "vitest";
import * as React from "react";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { IntlProvider } from "use-intl";
import enMessages from "@/messages/en.json";
import type { RunSummary } from "@/lib/types";
import type { InvocationSummary } from "@/lib/api";

// jsdom's built-in `window.localStorage` in this vitest environment is a
// stub with no Storage methods (no --localstorage-file wired up) — mounting
// LiveBoard now reads a view preference from it, so tests need a working
// implementation, same as a real browser provides.
function installLocalStorageStub() {
  const store = new Map<string, string>();
  Object.defineProperty(window, "localStorage", {
    configurable: true,
    value: {
      getItem: (key: string) => (store.has(key) ? store.get(key)! : null),
      setItem: (key: string, value: string) => {
        store.set(key, String(value));
      },
      removeItem: (key: string) => store.delete(key),
      clear: () => store.clear(),
    },
  });
}

// LiveBoard cards route through <Link> for deep-linking, which needs a full
// RouterProvider tree to resolve `useLinkProps`. These tests only assert on
// StatusDot classes and label text, so a plain anchor stands in for it.
vi.mock("@tanstack/react-router", () => ({
  Link: ({ children, className }: { children?: React.ReactNode; className?: string }) => (
    <div className={className}>{children}</div>
  ),
}));

const {
  default: LiveBoard,
  DEAD_HEALTH,
  isDeadHealth,
  isUnknownHealth,
} = await import("./LiveBoard");

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

  beforeEach(() => {
    installLocalStorageStub();
  });

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

// ─── Board rendering: interleaved order, view toggle, persistence ────────────

function invocation(overrides: Partial<InvocationSummary> = {}): InvocationSummary {
  return {
    id: "inv-0000000000000001",
    skill: "code-review",
    plugin: null,
    prompt: null,
    started_at: 0,
    ended_at: null,
    status: "running",
    session_count: 1,
    created_at: 0,
    updated_at: 0,
    node_metadata: null,
    ...overrides,
  };
}

describe("isUnknownHealth", () => {
  it("is true only for the literal 'unknown' verdict", () => {
    expect(isUnknownHealth("unknown")).toBe(true);
    expect(isUnknownHealth("healthy")).toBe(false);
    expect(isUnknownHealth("stale")).toBe(false);
    expect(isUnknownHealth(null)).toBe(false);
    expect(isUnknownHealth(undefined)).toBe(false);
  });
});

describe("LiveBoard — InvocationCard health rendering", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    installLocalStorageStub();
  });

  afterEach(() => {
    act(() => {
      root.unmount();
    });
    container.remove();
  });

  function mount(invocations: InvocationSummary[]) {
    container = document.createElement("div");
    document.body.appendChild(container);
    act(() => {
      root = createRoot(container);
      root.render(
        <IntlProvider locale="en" messages={enMessages}>
          <LiveBoard activeRuns={[]} activeInvocations={invocations} nowSec={100} />
        </IntlProvider>,
      );
    });
  }

  it("renders a live pulsing dot with no stale label when health=healthy", () => {
    mount([invocation({ health: "healthy" })]);
    const dot = container.querySelector('[aria-hidden="true"].rounded-full');
    expect(dot?.className).toContain("live-pulse-dot");
    expect(container.textContent).not.toContain("quiet — check?");
  });

  it("never renders an unconditional 'running' pulsing dot when health=orphaned", () => {
    mount([invocation({ health: "orphaned" })]);
    const dot = container.querySelector('[aria-hidden="true"].rounded-full');
    expect(dot).not.toBeNull();
    expect(dot?.className).not.toContain("live-pulse-dot");
    expect(container.textContent).toContain("quiet — check?");
  });

  it("renders a static, non-pulsing dot with no false stale/healthy claim when health=unknown", () => {
    mount([invocation({ health: "unknown" })]);
    const dot = container.querySelector('[aria-hidden="true"].rounded-full');
    expect(dot).not.toBeNull();
    expect(dot?.className).not.toContain("live-pulse-dot");
    // Not the dead label either — "unknown" is not a confirmed-dead verdict.
    expect(container.textContent).not.toContain("quiet — check?");
  });
});

describe("LiveBoard — combined card order and view switching", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    installLocalStorageStub();
  });

  afterEach(() => {
    act(() => {
      root.unmount();
    });
    container.remove();
  });

  function mount(runs: RunSummary[], invocations: InvocationSummary[] = []) {
    container = document.createElement("div");
    document.body.appendChild(container);
    act(() => {
      root = createRoot(container);
      root.render(
        <IntlProvider locale="en" messages={enMessages}>
          <LiveBoard activeRuns={runs} activeInvocations={invocations} nowSec={1000} />
        </IntlProvider>,
      );
    });
  }

  it("interleaves runs and invocations into one creation-ordered board, not two separate groups", () => {
    mount(
      [
        run({ run_id: "run-a", started_at: 300, playbook_name: "run-a" }),
        run({ run_id: "run-b", started_at: 100, playbook_name: "run-b" }),
      ],
      [invocation({ id: "inv-a", started_at: 200, skill: "inv-a" })],
    );
    // Grid renders a flat sequence of cards — the DOM order is the read
    // order. Sorted by started_at: run-b (100), inv-a (200), run-a (300).
    // A card query that grabs each card's <span> name in document order
    // proves the merge, not two concatenated per-kind lists.
    const names = container.querySelectorAll(".group span.truncate");
    const text = Array.from(names).map((n) => n.textContent);
    expect(text[0]).toBe("run-b");
    expect(text.some((_, i) => text[i] === "inv-a")).toBe(true);
    const order = ["run-b", "inv-a", "run-a"];
    const positions = order.map((name) => text.indexOf(name));
    expect(positions).toEqual([...positions].sort((a, b) => a - b));
  });

  it("defaults to the cards view and switches to the table view on click", () => {
    mount([run({ run_id: "r1", started_at: 100, playbook_name: "solo-run" })]);
    expect(container.querySelector("table")).toBeNull();
    const tableButton = Array.from(container.querySelectorAll("button")).find(
      (b) => b.textContent === "Table",
    );
    expect(tableButton).toBeDefined();
    act(() => {
      tableButton!.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    expect(container.querySelector("table")).not.toBeNull();
    expect(container.textContent).toContain("solo-run");
  });

  it("table view renders one row per card with name, status, uptime, last-activity, kind, and id", () => {
    mount([run({ run_id: "run-abcdef0123456789", started_at: 900, playbook_name: "table-run" })]);
    const tableButton = Array.from(container.querySelectorAll("button")).find(
      (b) => b.textContent === "Table",
    );
    act(() => {
      tableButton!.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    const rows = container.querySelectorAll("tbody tr");
    expect(rows).toHaveLength(1);
    const cells = rows[0].querySelectorAll("td");
    expect(cells).toHaveLength(6);
    expect(rows[0].textContent).toContain("table-run");
    expect(rows[0].textContent).toContain("run");
    expect(rows[0].textContent).toContain("run-abcdef0123456789".slice(-16));
  });

  it("table view and card view resolve the same run to the same name — no more mirror-surface gap", () => {
    const theRun = run({
      run_id: "run-abcdef0123456789",
      started_at: 900,
      playbook_name: "table-run",
      agent_name: "implementer",
    });
    mount([theRun]);
    const cardText = container.textContent ?? "";
    expect(cardText).toContain("table-run");

    const tableButton = Array.from(container.querySelectorAll("button")).find(
      (b) => b.textContent === "Table",
    );
    act(() => {
      tableButton!.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    const rows = container.querySelectorAll("tbody tr");
    expect(rows[0].textContent).toContain("table-run");
  });

  it("persists the view choice across remounts via localStorage", () => {
    mount([run({ run_id: "r1", started_at: 100 })]);
    const tableButton = Array.from(container.querySelectorAll("button")).find(
      (b) => b.textContent === "Table",
    );
    act(() => {
      tableButton!.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    expect(window.localStorage.getItem("studio:mission-board-view")).toBe("table");

    act(() => {
      root.unmount();
    });
    container.remove();
    // Remount fresh — the stored preference (not the component default)
    // decides what's on screen first.
    mount([run({ run_id: "r1", started_at: 100 })]);
    expect(container.querySelector("table")).not.toBeNull();
  });

  it("bounds the board's footprint with an internally-scrolling container in both views", () => {
    mount([run({ run_id: "r1", started_at: 100 })]);
    const cardsRegion = container.querySelector('[class*="max-h-"]');
    expect(cardsRegion).not.toBeNull();
    expect(cardsRegion?.className).toContain("overflow-y-auto");

    const tableButton = Array.from(container.querySelectorAll("button")).find(
      (b) => b.textContent === "Table",
    );
    act(() => {
      tableButton!.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    const tableRegion = container.querySelector('[class*="max-h-"]');
    expect(tableRegion).not.toBeNull();
    expect(tableRegion?.className).toMatch(/overflow-(y-)?auto/);
  });

  it("empty board shows no cards, no table, and no view toggle", () => {
    mount([], []);
    expect(container.querySelector("table")).toBeNull();
    expect(container.querySelectorAll("button")).toHaveLength(0);
    expect(container.textContent).toContain("No active runs or invocations.");
  });
});
