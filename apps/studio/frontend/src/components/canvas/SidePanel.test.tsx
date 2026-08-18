import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import SidePanel from "./SidePanel";
import type { StepNodeData } from "./StepNode";

const node: StepNodeData = {
  label: "Research",
  role: "researcher",
  assignment: "question -> findings",
  prompt: "Investigate {question}",
  capacity: 1,
  timeout: null,
  inputs: ["question"],
  outputs: ["findings"],
};

describe("SidePanel controls", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  });

  it("uses a non-submitting, contextual delete control for a node", async () => {
    const onDelete = vi.fn();
    await act(async () => {
      root.render(
        <SidePanel
          selection={{ type: "node", id: "research", data: node }}
          editable
          roles={["researcher"]}
          onDelete={onDelete}
        />,
      );
    });

    const deleteButton = container.querySelector<HTMLButtonElement>(
      'button[aria-label="Delete Research"]',
    );
    expect(deleteButton?.type).toBe("button");
    await act(async () => deleteButton?.click());
    expect(onDelete).toHaveBeenCalledWith("node", "research");
  });

  it("exposes the selected link mode and never submits its surrounding form", async () => {
    const onUpdate = vi.fn();
    await act(async () => {
      root.render(
        <SidePanel
          selection={{
            type: "edge",
            id: "research-review",
            data: { mode: "simple", condition: "approved" },
          }}
          editable
          roles={[]}
          onEdgeUpdate={onUpdate}
        />,
      );
    });

    const group = container.querySelector('[role="group"][aria-label="Link mode"]');
    const buttons = Array.from(group?.querySelectorAll<HTMLButtonElement>("button") ?? []);
    expect(buttons.map((button) => [button.textContent, button.type, button.ariaPressed])).toEqual([
      ["Simple", "button", "true"],
      ["Code", "button", "false"],
    ]);

    await act(async () => buttons[1]?.click());
    expect(onUpdate).toHaveBeenCalledWith("research-review", { mode: "code" });
  });
});
