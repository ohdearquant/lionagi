/**
 * Host-level regression for the run-detail failure banner. A non-success run whose
 * GET /api/runs/{id} response carries status_reason_* must post a "reason" message
 * even when invocation_id is null, without falling back to GET /api/invocations.
 * Guards the panel wiring the pure runReasonBannerMessage unit test cannot reach.
 */
import { describe, it, expect, beforeEach, vi } from "vitest";
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
