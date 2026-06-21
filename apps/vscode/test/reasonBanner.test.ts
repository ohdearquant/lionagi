/**
 * Unit tests for reasonBannerMessage (src/runs/runDetailPanel.ts) — the gating
 * decision behind the run-detail reason banner. Regression-guards the
 * red-banner-on-success defect: a succeeded/completed run must never produce a
 * (red-toned) reason banner, even when its invocation carries a reason.
 */
import { describe, it, expect } from "vitest";
import { reasonBannerMessage } from "../src/runs/runDetailPanel.js";

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
