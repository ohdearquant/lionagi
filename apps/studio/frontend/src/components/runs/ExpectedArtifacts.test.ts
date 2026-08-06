/** Render tests for live, recorded, and absent artifact-verification states. */
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import ExpectedArtifacts from "./ExpectedArtifacts";
import type { ArtifactContract, ArtifactVerification } from "@/lib/types";

const CONTRACT: ArtifactContract = {
  expected: [
    { id: "report", path: "REPORT.md", required: true },
    { id: "notes", path: "NOTES.md", required: false },
  ],
};

let root: Root | null = null;
let container: HTMLDivElement | null = null;

function renderExpectedArtifacts(verification: ArtifactVerification | null) {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => {
    root?.render(createElement(ExpectedArtifacts, { contract: CONTRACT, verification }));
  });
  return container;
}

afterEach(() => {
  if (root) {
    act(() => root?.unmount());
  }
  container?.remove();
  root = null;
  container = null;
});

describe("ExpectedArtifacts", () => {
  it("shows written progress and keeps unwritten live artifacts pending", () => {
    const view = renderExpectedArtifacts({
      status: "failed",
      checked_at: 10,
      missing_required: [],
      missing_optional: [{ id: "notes", path: "NOTES.md", required: false }],
      produced: [{ id: "report", path: "REPORT.md", size: 5, present: true }],
      provisional: true,
    });

    expect(view.textContent).toContain("1 of 2 written");
    expect(view.textContent).toContain("OK (5 B)");
    expect(view.textContent).toContain("PENDING");
    expect(view.textContent).not.toContain("MISSING");
  });

  it("shows missing only when a recorded verdict says it is missing", () => {
    const view = renderExpectedArtifacts({
      status: "failed",
      checked_at: 20,
      missing_required: [{ id: "report", path: "REPORT.md", required: true }],
      missing_optional: [],
      produced: [],
    });

    expect(view.textContent).toContain("Verified: failed");
    expect(view.textContent).toContain("MISSING");
  });

  it("keeps a live null verdict pending", () => {
    const view = renderExpectedArtifacts(null);

    expect(view.textContent?.match(/PENDING/g)).toHaveLength(2);
    expect(view.textContent).not.toContain("Verification not recorded");
    expect(view.textContent).not.toContain("NOT RECORDED");
  });

  it("renders a terminal null verdict as not recorded instead of pending", () => {
    const view = renderExpectedArtifacts({ status: "not_recorded" });

    expect(view.textContent).toContain("Verification not recorded");
    expect(view.textContent?.match(/NOT RECORDED/g)).toHaveLength(2);
    expect(view.textContent).not.toContain("PENDING");
    expect(view.textContent).not.toContain("MISSING");
  });

  it("shows when a recorded verdict was taken instead of presenting it as current", () => {
    const view = renderExpectedArtifacts({
      status: "passed",
      checked_at: 1700000000,
      missing_required: [],
      missing_optional: [],
      produced: [
        { id: "report", path: "REPORT.md", size: 5, present: true },
        { id: "notes", path: "NOTES.md", size: 3, present: true },
      ],
    });

    expect(view.textContent).toContain("verified at completion,");
    // A provisional (mid-run) reading is not a completion snapshot and must
    // not claim one.
    const provisionalView = renderExpectedArtifacts({
      status: "failed",
      checked_at: 1700000000,
      missing_required: [],
      missing_optional: [{ id: "notes", path: "NOTES.md", required: false }],
      produced: [{ id: "report", path: "REPORT.md", size: 5, present: true }],
      provisional: true,
    });
    expect(provisionalView.textContent).not.toContain("verified at completion,");
  });

  it("does not claim staleness for a fresh recorded verdict", () => {
    const view = renderExpectedArtifacts({
      status: "passed",
      checked_at: 1700000000,
      missing_required: [],
      missing_optional: [],
      produced: [{ id: "report", path: "REPORT.md", size: 5, present: true }],
    });

    expect(view.textContent).not.toContain("no longer present");
    expect(view.textContent).not.toContain("files changed since verification");
    expect(view.textContent).not.toContain("NO LONGER PRESENT");
  });

  it("flags a produced artifact whose file changed after verification", () => {
    const view = renderExpectedArtifacts({
      status: "passed",
      checked_at: 1700000000,
      missing_required: [],
      missing_optional: [],
      produced: [{ id: "report", path: "REPORT.md", size: 5, present: true }],
      changed_since_verification: ["report"],
    });

    expect(view.textContent).toContain("files changed since verification");
    expect(view.textContent).toContain("changed since verification");
  });

  it("flags a produced artifact whose file is no longer present", () => {
    const view = renderExpectedArtifacts({
      status: "passed",
      checked_at: 1700000000,
      missing_required: [],
      missing_optional: [],
      produced: [{ id: "report", path: "REPORT.md", size: 5, present: true }],
      absent_since_verification: ["report"],
    });

    expect(view.textContent).toContain("no longer present");
    expect(view.textContent).toContain("NO LONGER PRESENT");
    expect(view.textContent).not.toContain("OK (5 B)");
  });
});
