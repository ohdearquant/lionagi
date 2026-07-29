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
});
