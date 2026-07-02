import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";
import * as vscode from "vscode";
import { __resetVscodeMock } from "./mocks/vscode.js";
import { registerRunsExplorer } from "../src/runs/runsExplorer.js";

// Regression guard for the live-update bootstrap. The runs tree refreshes the
// count/status by polling /api/runs every 4s — but only while there are active
// runs. Polling used to be (re)evaluated synchronously right after refresh(),
// which merely *schedules* the async load, so hasActiveRuns() read the stale
// empty set and polling never started when the view was already visible and the
// backend already running (the common case). The fix fires onDidLoad AFTER the
// load populates _activeRuns and keys polling off that. These tests pin both
// directions: it starts when a load reveals an active run, and it does not when
// there are none.

const POLL_INTERVAL_MS = 4_000;

interface Captured {
  provider: vscode.TreeDataProvider<unknown> & {
    getChildren(e?: unknown): Promise<unknown[]>;
  };
  refreshCount: number;
}

function makeRun(over: Record<string, unknown> = {}) {
  return {
    id: "sess-1",
    run_id: "sess-1",
    name: "live session",
    status: "running",
    project: "acme/widget",
    message_count: 3,
    branch_count: 1,
    started_at: 1_782_000_000,
    created_at: 1_782_000_000,
    ...over,
  };
}

/**
 * Wire registerRunsExplorer against a visible tree view + a backend that is
 * already running (no state transition fires — that's the path the bug lived
 * on). Returns the captured provider and a live refresh counter (each poll tick
 * fires onDidChangeTreeData).
 */
function setup(opts: {
  runningRuns: unknown[];
  projects: unknown[];
}): Captured {
  const stateEmitter = new vscode.EventEmitter<string>();
  const visibilityEmitter = new vscode.EventEmitter<{ visible: boolean }>();
  const backend = {
    isRunning: () => true,
    onDidChangeState: stateEmitter.event,
  };
  const client = {
    listRuns: vi.fn(async () => ({
      runs: opts.runningRuns,
      has_next: false,
      page: 1,
      per_page: 100,
      total: opts.runningRuns.length,
    })),
    listProjectGroups: vi.fn(async () => ({
      projects: opts.projects,
      total: opts.projects.length,
    })),
  };
  const deps = { backend, client } as never;

  const captured = {} as Captured;
  (vscode.window.createTreeView as ReturnType<typeof vi.fn>).mockImplementation(
    (_id: string, viewOpts: { treeDataProvider: unknown }) => {
      captured.provider = viewOpts.treeDataProvider as Captured["provider"];
      return {
        visible: true,
        onDidChangeVisibility: visibilityEmitter.event,
        dispose() {},
      };
    }
  );

  const context = { subscriptions: [] } as never;
  registerRunsExplorer(context, deps);

  captured.refreshCount = 0;
  captured.provider.onDidChangeTreeData?.(() => {
    captured.refreshCount += 1;
  });
  return captured;
}

describe("runs explorer live-update polling bootstrap", () => {
  beforeEach(() => {
    __resetVscodeMock();
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("starts polling once a load reveals an active run (view already visible, backend already running)", async () => {
    const c = setup({
      runningRuns: [makeRun()],
      projects: [{ project: "acme/widget", count: 1, last_activity: 1_782_000_000 }],
    });

    // The initial render: VS Code calls getChildren on the root. This populates
    // _activeRuns and — with the fix — fires onDidLoad → starts the poll timer.
    await c.provider.getChildren();
    expect(c.refreshCount).toBe(0); // the load itself does not fire a refresh

    // Three poll intervals must produce three refreshes. Before the fix this
    // stayed 0: polling never bootstrapped from a load, only a frozen snapshot.
    await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS * 3);
    expect(c.refreshCount).toBeGreaterThanOrEqual(3);
  });

  it("does not keep polling when no runs are active", async () => {
    const c = setup({
      runningRuns: [],
      projects: [{ project: "acme/widget", count: 2, last_activity: 1_782_000_000 }],
    });

    await c.provider.getChildren();
    await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS * 3);
    expect(c.refreshCount).toBe(0); // nothing active → no live polling churn
  });
});
