/**
 * Tests for launchStore round-trip and the den.retryRun command handler.
 * The retryRun handler is exercised by registering the REAL registerRunsExplorer
 * and invoking the captured callback directly — no reimplementation.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import {
  window,
  commands,
  EventEmitter,
  __resetVscodeMock,
  __getCommand,
} from "./mocks/vscode.js";

// launchStore is module-level state: use distinct ids across tests to avoid cross-test pollution.
import { rememberLaunch, recallLaunch } from "../src/launch/launchStore.js";
import { registerRunsExplorer } from "../src/runs/runsExplorer.js";
import type { LaunchRequest } from "../src/api/types.js";
import type { StudioDeps } from "../src/extension.js";

beforeEach(() => {
  __resetVscodeMock();
});

// ---------------------------------------------------------------------------
// Part 1: launchStore round-trip (pure, no vscode mock needed)
// ---------------------------------------------------------------------------
describe("launchStore", () => {
  it("rememberLaunch then recallLaunch returns the stored request", () => {
    const req: LaunchRequest = {
      action_kind: "agent",
      action_model: "openai/gpt-4.1-mini",
      action_prompt: "hello",
    };
    rememberLaunch("inv-store-1", req);
    expect(recallLaunch("inv-store-1")).toEqual(req);
  });

  it("recallLaunch returns undefined for an unknown id", () => {
    expect(recallLaunch("nope-never-stored")).toBeUndefined();
  });
});

// ---------------------------------------------------------------------------
// Part 2: den.retryRun handler via registerRunsExplorer
// ---------------------------------------------------------------------------
describe("den.retryRun handler", () => {
  // Minimal mock context: subscriptions.push accumulates disposables.
  function makeContext() {
    return { subscriptions: { push(..._args: unknown[]) {} } };
  }

  // Minimal deps: enough for RunsProvider to construct and registerRunsExplorer to wire up.
  function makeDeps(launchMock: ReturnType<typeof vi.fn>) {
    return {
      client: {
        launch: launchMock,
        listRuns: vi.fn().mockResolvedValue({ runs: [], page: 1, per_page: 50, total: 0, total_pages: 0, has_next: false, has_prev: false }),
        listProjectGroups: vi.fn().mockResolvedValue({ projects: [], total: 0 }),
        getRun: vi.fn(),
        cancelLaunch: vi.fn(),
        getInvocation: vi.fn().mockResolvedValue({ id: "", skill: null, status: "pending", sessions: [] }),
      },
      backend: {
        isRunning: vi.fn().mockReturnValue(false),
        onDidChangeState: vi.fn().mockReturnValue({ dispose() {} }),
      },
    } as unknown as StudioDeps;
  }

  it("HIT path: re-posts the cached request and caches the new invocation id", async () => {
    // Cache the original request under "inv-retry-hit".
    const reqA: LaunchRequest = {
      action_kind: "agent",
      action_model: "openai/gpt-4.1-mini",
      action_prompt: "retry me",
    };
    rememberLaunch("inv-retry-hit", reqA);

    const launchMock = vi.fn().mockResolvedValue({
      invocation_id: "inv-retry-hit-new",
      action_kind: "agent",
    });
    const deps = makeDeps(launchMock);

    registerRunsExplorer(makeContext() as unknown as import("vscode").ExtensionContext, deps);

    const retryHandler = __getCommand("den.retryRun");
    expect(retryHandler).toBeDefined();

    // Invoke with a Run-like object that has the original invocation_id.
    await retryHandler!({ invocation_id: "inv-retry-hit" } as unknown);

    // client.launch must have been called exactly once with the original request.
    expect(launchMock).toHaveBeenCalledTimes(1);
    expect(launchMock).toHaveBeenCalledWith(reqA);

    // The new invocation id is cached under the same request.
    expect(recallLaunch("inv-retry-hit-new")).toEqual(reqA);
  });

  it("MISS path: does NOT call client.launch and shows an info message", async () => {
    const launchMock = vi.fn();
    const deps = makeDeps(launchMock);

    registerRunsExplorer(makeContext() as unknown as import("vscode").ExtensionContext, deps);

    const retryHandler = __getCommand("den.retryRun");
    expect(retryHandler).toBeDefined();

    await retryHandler!({ invocation_id: "unknown-id-never-cached" } as unknown);

    expect(launchMock).not.toHaveBeenCalled();
    expect(window.showInformationMessage).toHaveBeenCalled();
  });
});
