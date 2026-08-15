import { afterEach, describe, expect, it, vi } from "vitest";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { IntlProvider } from "use-intl";
import enMessages from "@/messages/en.json";
import type { AgentRow } from "./fleetReducer";
import { AgentSections } from "./FleetView";

function row(id: string, name: string, invocationKind: string | null): AgentRow {
  return {
    id,
    name,
    status: "running",
    effectiveHealth: null,
    elapsedSec: 12,
    branch_count: 1,
    message_count: 2,
    kind: "run",
    invocation_id: null,
    invocationKind,
  };
}

let root: Root | null = null;
let container: HTMLDivElement | null = null;

afterEach(() => {
  if (root) act(() => root!.unmount());
  container?.remove();
  vi.unstubAllGlobals();
  root = null;
  container = null;
});

function renderRows(rows: AgentRow[]) {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => {
    root!.render(
      <IntlProvider locale="en" messages={enMessages}>
        <AgentSections agents={rows} selectedId={null} onSelectAgent={() => {}} />
      </IntlProvider>,
    );
  });
  return container;
}

describe("Fleet live rows", () => {
  it("separates orchestration roots from single agents without exposing orchestration style", () => {
    const view = renderRows([
      row("flow-root", "Quarterly research", "fanout"),
      row("agent-1", "Draft findings", "agent"),
    ]);

    const orchestrations = view.querySelector('[data-fleet-group="orchestrations"]');
    const agents = view.querySelector('[data-fleet-group="agents"]');

    expect(orchestrations).not.toBeNull();
    expect(agents).not.toBeNull();
    expect(orchestrations?.textContent).toContain("Quarterly research");
    expect(orchestrations?.textContent).not.toContain("Draft findings");
    expect(agents?.textContent).toContain("Draft findings");
    expect(agents?.textContent).not.toContain("Quarterly research");

    expect(view.textContent).not.toContain("fanout");
    expect(view.querySelector('[title="fanout"]')).toBeNull();
  });

  it("keeps every row and preserves order inside each group", () => {
    const view = renderRows([
      row("agent-1", "First agent", "agent"),
      row("flow-1", "First orchestration", "flow"),
      row("agent-2", "Second agent", null),
      row("play-2", "Second orchestration", "show-play"),
    ]);

    const names = (group: string) =>
      Array.from(view.querySelectorAll(`[data-fleet-group="${group}"] button`)).map(
        (button) => button.textContent,
      );

    expect(names("orchestrations")).toEqual([
      expect.stringContaining("First orchestration"),
      expect.stringContaining("Second orchestration"),
    ]);
    expect(names("agents")).toEqual([
      expect.stringContaining("First agent"),
      expect.stringContaining("Second agent"),
    ]);
  });
});
