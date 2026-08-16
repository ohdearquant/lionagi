/**
 * The Files panel inside Run Detail shows what the server allowed, in the
 * server's spelling.
 *
 * Two things have to hold at once. The allowlist arrives as artifact-root
 * relative paths and the tool arguments hold whatever the agent passed, so the
 * two are only comparable through aliases built from the trusted side. And a
 * session with no summary at all is not a session whose summary allows
 * nothing: both used to reach this component as an empty array, and an empty
 * allowlist means show nothing, so the absent case silently emptied the panel.
 *
 * These render the card rather than reading its source, because both defects
 * are in what the panel ends up displaying.
 */
import * as React from "react";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { IntlProvider } from "use-intl";
import { describe, expect, it, vi } from "vitest";
import RunStepCard from "./RunStepCard";
import enMessages from "@/messages/en.json";
import type { RunStep } from "@/lib/types";

vi.mock("@/components/ui/Markdown", () => ({
  default: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));

const ARTIFACT_ROOT = "/Users/dev/.lionagi/runs/run-1";

const STEP: RunStep = {
  step: "worker",
  status: "completed",
  timestamp: 1,
  messages: [
    {
      role: "action",
      function: "Read",
      // Absolute, as an agent actually writes it. The server returns the same
      // file as "src/app.py".
      arguments: { file_path: `${ARTIFACT_ROOT}/src/app.py` },
      timestamp: 1,
    },
    {
      role: "action",
      function: "Write",
      // Outside the artifact root: the server withholds this one, so whatever
      // the allowlist says, it must not reach the panel.
      arguments: { file_path: "/Users/dev/.ssh/id_ed25519" },
      timestamp: 2,
    },
  ],
};

function renderFilesPanel(runFiles: string[] | undefined) {
  const container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);
  act(() => {
    root.render(
      <IntlProvider locale="en" messages={enMessages}>
        <RunStepCard
          step={STEP}
          defaultExpanded
          runId="run-1"
          artifactRoot={ARTIFACT_ROOT}
          runFiles={runFiles}
        />
      </IntlProvider>,
    );
  });
  const tab = container.querySelector<HTMLElement>("#step-worker-tab-files");
  expect(tab, "the Files tab must exist for this test to mean anything").toBeTruthy();
  act(() => tab!.click());
  const panel = container.querySelector<HTMLElement>("#step-worker-panel-files");
  expect(panel, "the Files panel must render once its tab is selected").toBeTruthy();
  return panel!.textContent ?? "";
}

describe("RunStepCard file panel — server allowlist", () => {
  it("shows the raw paths when no allowlist was supplied", () => {
    // A standalone card outside Run Detail has no server summary to defer to.
    // This is also the control for the two cases below: without it, an
    // allowlist that filters everything is indistinguishable from a panel that
    // never had anything to show.
    const text = renderFilesPanel(undefined);
    expect(text).toContain("app.py");
    expect(text).toContain("id_ed25519");
  });

  it("shows an allowed file under the server's path, not the agent's", () => {
    const text = renderFilesPanel(["src/app.py"]);
    expect(text).toContain("src/app.py");
    // The absolute form is what the tool argument held. Echoing it here would
    // put a host path back on a surface that exists to keep them off it.
    expect(text).not.toContain(ARTIFACT_ROOT);
    expect(text).not.toContain("id_ed25519");
  });

  it("shows nothing when the allowlist allows nothing", () => {
    // Distinct from the no-allowlist case above, and this is the pair that
    // matters: if an absent summary is turned into an empty array on the way
    // in, the first test's output becomes this one's.
    const text = renderFilesPanel([]);
    expect(text).not.toContain("app.py");
    expect(text).not.toContain("id_ed25519");
  });
});
