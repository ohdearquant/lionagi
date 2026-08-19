/**
 * SchedulesTable tests, matching the project's existing style for this
 * feature (see SchedulesCalendar.test.tsx): pure logic gets real unit tests;
 * component wiring gets source-contract assertions since this project has
 * no @testing-library/react.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import * as fs from "node:fs";
import * as path from "node:path";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { IntlProvider } from "use-intl";
import SchedulesTable, { sortByNextFire } from "./SchedulesTable";
import { ToastProvider } from "@/components/ui/Toast";
import enMessages from "@/messages/en.json";
import type { ScheduleSummary } from "@/lib/types";

const TABLE_FILE = path.resolve(__dirname, "SchedulesTable.tsx");
const SRC = fs.readFileSync(TABLE_FILE, "utf-8");

const api = vi.hoisted(() => ({
  triggerSchedule: vi.fn(() => Promise.resolve({ run_id: "run-abcdefgh" })),
  disableSchedule: vi.fn(() => Promise.resolve(undefined)),
  enableSchedule: vi.fn(() => Promise.resolve(undefined)),
}));
vi.mock("@/lib/api", () => api);

function schedule(overrides: Partial<ScheduleSummary> = {}): ScheduleSummary {
  return {
    id: "sched-1",
    name: "nightly-build",
    description: null,
    enabled: 1,
    trigger_type: "cron",
    cron_expr: "0 * * * *",
    interval_sec: null,
    github_repo: null,
    poll_interval_sec: null,
    action_kind: "agent",
    action_model: null,
    action_agent: null,
    action_playbook: null,
    action_project: null,
    last_fired_at: null,
    next_fire_at: null,
    missed_fire_policy: "skip",
    overlap_policy: "skip",
    project: null,
    created_at: 0,
    updated_at: 0,
    ...overrides,
  };
}

// ─── sortByNextFire ─────────────────────────────────────────────────────────

describe("sortByNextFire — next-fire column sort", () => {
  it("sorts ascending by next_fire_at (soonest first)", () => {
    const a = schedule({ id: "a", name: "a", next_fire_at: 300 });
    const b = schedule({ id: "b", name: "b", next_fire_at: 100 });
    const c = schedule({ id: "c", name: "c", next_fire_at: 200 });
    expect(sortByNextFire([a, b, c], "asc").map((s) => s.id)).toEqual(["b", "c", "a"]);
  });

  it("sorts descending by next_fire_at when toggled", () => {
    const a = schedule({ id: "a", name: "a", next_fire_at: 300 });
    const b = schedule({ id: "b", name: "b", next_fire_at: 100 });
    const c = schedule({ id: "c", name: "c", next_fire_at: 200 });
    expect(sortByNextFire([a, b, c], "desc").map((s) => s.id)).toEqual(["a", "c", "b"]);
  });

  it("always sorts schedules with no next_fire_at to the bottom, in both directions", () => {
    const scheduled = schedule({ id: "scheduled", name: "z-scheduled", next_fire_at: 100 });
    const unscheduled = schedule({ id: "unscheduled", name: "a-unscheduled", next_fire_at: null });
    expect(sortByNextFire([unscheduled, scheduled], "asc").map((s) => s.id)).toEqual([
      "scheduled",
      "unscheduled",
    ]);
    expect(sortByNextFire([unscheduled, scheduled], "desc").map((s) => s.id)).toEqual([
      "scheduled",
      "unscheduled",
    ]);
  });

  it("breaks ties among unscheduled rows by name", () => {
    const b = schedule({ id: "b", name: "bravo", next_fire_at: null });
    const a = schedule({ id: "a", name: "alpha", next_fire_at: null });
    expect(sortByNextFire([b, a], "asc").map((s) => s.id)).toEqual(["a", "b"]);
  });

  it("does not mutate the input array", () => {
    const list = [
      schedule({ id: "a", next_fire_at: 200 }),
      schedule({ id: "b", next_fire_at: 100 }),
    ];
    const copy = [...list];
    sortByNextFire(list, "asc");
    expect(list).toEqual(copy);
  });

  it("sinks a disabled schedule below enabled ones despite a soon stale next_fire_at", () => {
    const paused = schedule({ id: "paused", name: "a-paused", enabled: 0, next_fire_at: 50 });
    const live = schedule({ id: "live", name: "z-live", enabled: 1, next_fire_at: 500 });
    // Paused sorts last in BOTH directions — its stale timestamp is not a real fire.
    expect(sortByNextFire([paused, live], "asc").map((s) => s.id)).toEqual(["live", "paused"]);
    expect(sortByNextFire([paused, live], "desc").map((s) => s.id)).toEqual(["live", "paused"]);
  });

  it("sinks a disabled schedule with a future stale next_fire_at too", () => {
    const paused = schedule({ id: "paused", name: "paused", enabled: 0, next_fire_at: 9_000 });
    const live = schedule({ id: "live", name: "live", enabled: 1, next_fire_at: 1_000 });
    expect(sortByNextFire([paused, live], "asc").map((s) => s.id)).toEqual(["live", "paused"]);
  });
});

// ─── Source contract — one flat table, real wiring, no kanban ──────────────

describe("SchedulesTable — source contract", () => {
  it("renders exactly one <table>, not per-lane columns", () => {
    expect(SRC.match(/<table/g)?.length).toBe(1);
  });

  it("wires EnabledToggle with stopPropagation so it doesn't also open the row", () => {
    const cellStart = SRC.indexOf("<EnabledToggle");
    const before = SRC.slice(Math.max(0, cellStart - 300), cellStart);
    expect(before).toContain("stopPropagation");
  });

  it("uses StatusPill with the session taxonomy for the last-run cell", () => {
    expect(SRC).toContain('taxonomy="session"');
  });

  it("renders the server's classification and never a raw error_detail", () => {
    expect(SRC).toContain("run.error_class");
    expect(SRC).not.toContain("run.error_detail");
  });

  it("never leaks the raw error_detail into a hover title — only the classified line", () => {
    expect(SRC).not.toMatch(/title=\{run\.error_detail/);
    expect(SRC).toContain("title={errorLine}");
  });

  it("has a sortable Next fire header wired to the sort toggle", () => {
    expect(SRC).toContain("table.colNextFire");
    expect(SRC).toContain("setSortDir");
  });

  it("row click opens the schedule detail via onOpen", () => {
    expect(SRC).toContain("onClick={() => onOpen(schedule.id)}");
  });
});

// ─── Keyboard interaction — mounted, real event bubbling ───────────────────
//
// Enter/Space keydown bubbles up the DOM independently of a nested control's
// own click handling, so the row's key handler must ignore keys that
// originated inside a nested interactive element (button/link/input) —
// otherwise activating a nested control also "opens" the row.

describe("SchedulesTable — keyboard interaction (mounted)", () => {
  let container: HTMLDivElement;
  let root: Root | null;
  let onOpen: ReturnType<typeof vi.fn<(id: string) => void>>;
  let onChanged: ReturnType<typeof vi.fn<() => void>>;

  beforeEach(() => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    api.triggerSchedule.mockClear();
    api.disableSchedule.mockClear();
    api.enableSchedule.mockClear();
    onOpen = vi.fn<(id: string) => void>();
    onChanged = vi.fn<() => void>();
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

  async function mount() {
    await act(async () => {
      root?.render(
        <IntlProvider locale="en" messages={enMessages}>
          <ToastProvider>
            <SchedulesTable
              schedules={[schedule({ id: "sched-1", name: "nightly-build" })]}
              runs={[]}
              nowMs={1_700_000_000_000}
              onChanged={onChanged}
              onOpen={onOpen}
            />
          </ToastProvider>
        </IntlProvider>,
      );
    });
  }

  function keydown(el: Element, key: string) {
    act(() => {
      el.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true }));
    });
  }

  // A real Enter/Space press on a focused native <button> also fires a
  // "click" — jsdom doesn't synthesize that automatically, so tests that
  // assert the nested control's own action ran dispatch it explicitly,
  // exactly like the browser does after the keydown.
  function click(el: Element) {
    act(() => {
      el.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
    });
  }

  async function flush() {
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
  }

  it("Enter and Space on the row itself open the schedule, once each", async () => {
    await mount();
    const row = container.querySelector('tr[aria-label="nightly-build"]');
    expect(row).not.toBeNull();

    keydown(row!, "Enter");
    expect(onOpen).toHaveBeenCalledTimes(1);

    keydown(row!, " ");
    expect(onOpen).toHaveBeenCalledTimes(2);
    expect(onOpen).toHaveBeenNthCalledWith(1, "sched-1");
  });

  it("Enter on Run now triggers the run only — the row does not also open", async () => {
    await mount();
    const runNow = Array.from(container.querySelectorAll("button")).find(
      (b) => b.textContent === "Run now",
    );
    expect(runNow).toBeTruthy();

    keydown(runNow!, "Enter");
    expect(onOpen).not.toHaveBeenCalled();

    click(runNow!);
    await flush();
    expect(api.triggerSchedule).toHaveBeenCalledTimes(1);
    expect(api.triggerSchedule).toHaveBeenCalledWith("sched-1");
    expect(onOpen).not.toHaveBeenCalled();
  });

  it("Enter on the edit action opens the editor once, not twice", async () => {
    await mount();
    const edit = container.querySelector('button[aria-label="Edit"]');
    expect(edit).not.toBeNull();

    keydown(edit!, "Enter");
    expect(onOpen).not.toHaveBeenCalled();

    click(edit!);
    expect(onOpen).toHaveBeenCalledTimes(1);
    expect(onOpen).toHaveBeenCalledWith("sched-1");
  });

  it("Space on the toggle flips it without also opening the row", async () => {
    await mount();
    const toggle = container.querySelector('button[aria-label="Disable schedule"]');
    expect(toggle).not.toBeNull();

    keydown(toggle!, " ");
    expect(onOpen).not.toHaveBeenCalled();

    click(toggle!);
    await flush();
    expect(api.disableSchedule).toHaveBeenCalledTimes(1);
    expect(api.disableSchedule).toHaveBeenCalledWith("sched-1");
    expect(onOpen).not.toHaveBeenCalled();
  });
});
