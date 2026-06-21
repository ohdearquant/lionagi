/**
 * Host-level regression for the run-detail failure banner. A non-success run whose
 * GET /api/runs/{id} response carries status_reason_* must post a "reason" message
 * even when invocation_id is null, without falling back to GET /api/invocations.
 * Guards the panel wiring the pure runReasonBannerMessage unit test cannot reach.
 *
 * Also guards retarget abort-isolation: clicking a second live run while the first
 * is still streaming must abort the first stream silently, never surfacing the old
 * run's abort as an error on the newly-targeted run.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import * as vscodeMock from "./mocks/vscode.js";
import { RunDetailPanel } from "../src/runs/runDetailPanel.js";
import type { Run } from "../src/api/types.js";

type OpenArgs = Parameters<typeof RunDetailPanel.open>;

function failedRunNoInvocation(): Run {
  return {
    run_id: "r-1",
    id: "r-1",
    name: "failing run",
    playbook_name: null,
    agent_name: null,
    invocation_kind: "agent",
    model: null,
    provider: null,
    effort: null,
    status: "failed",
    started_at: null,
    ended_at: null,
    created_at: 0,
    updated_at: null,
    last_message_at: null,
    effective_health: null,
    branch_count: 0,
    message_count: 0,
    project: null,
    project_source: null,
    invocation_id: null,
    status_reason_code: "run.failed.exit_nonzero",
    status_reason_summary: "worker exited with code 1",
    status_evidence_refs: [{ type: "log", path: "/tmp/run.log" }],
  };
}

describe("RunDetailPanel run-detail reason banner (host)", () => {
  beforeEach(() => {
    vscodeMock.__resetVscodeMock();
  });

  it("posts a reason message from run-detail fields when invocation_id is null", async () => {
    const run = failedRunNoInvocation();
    const getRun = vi.fn(async () => run);
    const getInvocation = vi.fn(async () => {
      throw new Error("getInvocation must not be called when the run carries its own reason");
    });
    const deps = {
      client: { getRun, getInvocation },
      backend: { baseUrl: "http://127.0.0.1:0" },
    } as unknown as OpenArgs[1];
    const context = { extensionPath: "/ext" } as unknown as OpenArgs[0];

    RunDetailPanel.open(context, deps, run);

    const panel = vscodeMock.__lastWebviewPanel;
    expect(panel).toBeTruthy();

    try {
      await vi.waitFor(() => {
        const posted = panel!.webview.postMessage.mock.calls.some(
          (c) => (c[0] as { type?: string })?.type === "reason"
        );
        expect(posted).toBe(true);
      });

      const reasonMsg = panel!.webview.postMessage.mock.calls
        .map((c) => c[0] as { type?: string; code?: string; summary?: string })
        .find((m) => m?.type === "reason");
      expect(reasonMsg?.summary).toBe("worker exited with code 1");
      expect(reasonMsg?.code).toBe("run.failed.exit_nonzero");
      expect(getRun).toHaveBeenCalledOnce();
      // Run carried its own reason → the invocation fallback must never fire.
      expect(getInvocation).not.toHaveBeenCalled();
    } finally {
      panel!.__fireDispose();
    }
  });
});

function liveRun(id: string): Run {
  return {
    run_id: id,
    id,
    name: `live ${id}`,
    playbook_name: null,
    agent_name: null,
    invocation_kind: "agent",
    model: null,
    provider: null,
    effort: null,
    status: "running",
    started_at: null,
    ended_at: null,
    created_at: 0,
    updated_at: null,
    last_message_at: null,
    effective_health: null,
    branch_count: 0,
    message_count: 0,
    project: null,
    project_source: null,
    invocation_id: null,
    status_reason_code: null,
    status_reason_summary: null,
    status_evidence_refs: null,
  };
}

// A fetch that never resolves but rejects with AbortError the moment its signal
// aborts — models a held-open SSE connection cut by a retarget.
function abortingFetch(): ReturnType<typeof vi.fn> {
  return vi.fn((_url: string, init?: { signal?: AbortSignal }) => {
    const signal = init?.signal;
    return new Promise<Response>((_resolve, reject) => {
      const fail = () => reject(new DOMException("The operation was aborted.", "AbortError"));
      if (signal?.aborted) {
        fail();
        return;
      }
      signal?.addEventListener("abort", fail, { once: true });
    });
  });
}

describe("RunDetailPanel retarget abort-isolation (host)", () => {
  let realFetch: typeof globalThis.fetch;
  let fetchStub: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    vscodeMock.__resetVscodeMock();
    realFetch = globalThis.fetch;
    fetchStub = abortingFetch();
    globalThis.fetch = fetchStub as unknown as typeof globalThis.fetch;
  });

  afterEach(() => {
    globalThis.fetch = realFetch;
  });

  it("retargeting mid-stream aborts the old run's stream without posting a spurious error on the new run", async () => {
    const deps = {
      client: { getRun: vi.fn(), getInvocation: vi.fn() },
      backend: { baseUrl: "http://127.0.0.1:0" },
    } as unknown as OpenArgs[1];
    const context = { extensionPath: "/ext" } as unknown as OpenArgs[0];

    // Open a live run → streamLive holds an SSE fetch open (pending).
    RunDetailPanel.open(context, deps, liveRun("r-A"));
    const panel = vscodeMock.__lastWebviewPanel;
    expect(panel).toBeTruthy();

    try {
      await vi.waitFor(() => expect(fetchStub).toHaveBeenCalledTimes(1));

      // Retarget to a different live run: aborts run A's controller, swaps in a
      // fresh one, and starts run B's stream. Run A's fetch now rejects (abort).
      RunDetailPanel.open(context, deps, liveRun("r-B"));
      await vi.waitFor(() => expect(fetchStub).toHaveBeenCalledTimes(2));

      // Drain microtasks so run A's abort-induced catch has executed.
      await Promise.resolve();
      await Promise.resolve();

      const errors = panel!.webview.postMessage.mock.calls
        .map((c) => c[0] as { type?: string })
        .filter((m) => m?.type === "error");
      // The aborted old stream must be swallowed: streamLive tests the controller
      // it *started* with (now aborted), not the panel's current (run B's) one.
      expect(errors).toEqual([]);
    } finally {
      panel!.__fireDispose();
    }
  });
});
