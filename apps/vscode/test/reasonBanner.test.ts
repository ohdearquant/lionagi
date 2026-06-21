/**
 * Unit tests for reasonBannerMessage (src/runs/runDetailPanel.ts) — the gating
 * decision behind the run-detail reason banner. Regression-guards the
 * red-banner-on-success defect: a succeeded/completed run must never produce a
 * (red-toned) reason banner, even when its invocation carries a reason.
 */
import { describe, it, expect } from "vitest";
import {
  reasonBannerMessage,
  runReasonBannerMessage,
} from "../src/runs/runDetailPanel.js";

const fullInv = {
  status_reason_code: "run.failed.exception",
  status_reason_summary: "RuntimeError: boom",
  status_evidence_refs: [{ kind: "session", id: "s-1" }],
};

describe("reasonBannerMessage", () => {
  it("returns null for a succeeded run even when the invocation carries a reason", () => {
    expect(reasonBannerMessage("succeeded", "inv-1", fullInv)).toBeNull();
  });

  it("returns null for a completed run even when the invocation carries a reason", () => {
    expect(reasonBannerMessage("completed", "inv-1", fullInv)).toBeNull();
  });

  it("treats the success check case-insensitively (SUCCEEDED → null)", () => {
    expect(reasonBannerMessage("SUCCEEDED", "inv-1", fullInv)).toBeNull();
  });

  it("returns the reason payload for a failed run with a reason", () => {
    expect(reasonBannerMessage("failed", "inv-1", fullInv)).toEqual({
      type: "reason",
      code: "run.failed.exception",
      summary: "RuntimeError: boom",
      evidenceRefs: [{ kind: "session", id: "s-1" }],
    });
  });

  it("returns a reason payload for a cancelled run", () => {
    expect(reasonBannerMessage("cancelled", "inv-1", fullInv)?.type).toBe("reason");
  });

  it("returns null when there is no invocation id", () => {
    expect(reasonBannerMessage("failed", null, fullInv)).toBeNull();
  });

  it("returns null when the invocation is missing", () => {
    expect(reasonBannerMessage("failed", "inv-1", null)).toBeNull();
  });

  it("returns null when the invocation has neither code nor summary", () => {
    expect(
      reasonBannerMessage("failed", "inv-1", {
        status_reason_code: null,
        status_reason_summary: null,
        status_evidence_refs: null,
      })
    ).toBeNull();
  });

  it("renders a banner when only the code is present (no summary)", () => {
    const msg = reasonBannerMessage("failed", "inv-1", {
      status_reason_code: "run.failed.exception",
      status_reason_summary: null,
      status_evidence_refs: null,
    });
    expect(msg).not.toBeNull();
    expect(msg?.code).toBe("run.failed.exception");
  });

  it("passes evidence refs through, leaving null as null", () => {
    const msg = reasonBannerMessage("failed", "inv-1", {
      status_reason_code: "run.failed.exception",
      status_reason_summary: null,
      status_evidence_refs: null,
    });
    expect(msg?.evidenceRefs).toBeNull();
  });
});

// The run-detail counterpart: GET /api/runs/{id} now carries the session's own
// reason fields, so a failed run produces a banner WITHOUT any invocation — the
// gap that left invocation-less failed runs with no banner at all.
const failedRun = {
  status: "failed",
  status_reason_code: "run.failed.exit_nonzero",
  status_reason_summary: "worker exited with code 1",
  status_evidence_refs: [{ type: "log", path: "/tmp/run.log" }],
};

describe("runReasonBannerMessage (run-detail source)", () => {
  it("builds a banner from a failed run's own fields — no invocation needed", () => {
    expect(runReasonBannerMessage(failedRun)).toEqual({
      type: "reason",
      code: "run.failed.exit_nonzero",
      summary: "worker exited with code 1",
      evidenceRefs: [{ type: "log", path: "/tmp/run.log" }],
    });
  });

  it("returns null for a succeeded run even with reason fields set", () => {
    expect(runReasonBannerMessage({ ...failedRun, status: "succeeded" })).toBeNull();
  });

  it("returns null for a completed run (success synonym)", () => {
    expect(runReasonBannerMessage({ ...failedRun, status: "completed" })).toBeNull();
  });

  it("treats the success check case-insensitively (SUCCEEDED → null)", () => {
    expect(runReasonBannerMessage({ ...failedRun, status: "SUCCEEDED" })).toBeNull();
  });

  it("returns null for a failed run with no reason code or summary", () => {
    expect(
      runReasonBannerMessage({
        status: "failed",
        status_reason_code: null,
        status_reason_summary: null,
        status_evidence_refs: null,
      })
    ).toBeNull();
  });

  it("treats absent (undefined) reason fields like null — a clean list-row degrade", () => {
    expect(runReasonBannerMessage({ status: "failed" })).toBeNull();
  });

  it("renders a banner when only the code is present (no summary)", () => {
    const msg = runReasonBannerMessage({
      status: "failed",
      status_reason_code: "run.failed.exception",
      status_reason_summary: null,
      status_evidence_refs: null,
    });
    expect(msg?.code).toBe("run.failed.exception");
    expect(msg?.summary).toBeNull();
    expect(msg?.evidenceRefs).toBeNull();
  });

  it("returns a reason payload for a cancelled run", () => {
    expect(runReasonBannerMessage({ ...failedRun, status: "cancelled" })?.type).toBe(
      "reason"
    );
  });
});
