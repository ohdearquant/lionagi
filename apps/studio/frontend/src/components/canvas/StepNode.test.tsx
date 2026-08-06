/**
 * The card is read at a glance, in bulk, at whatever zoom fits the graph. That
 * only works if the same fact is always in the same corner, so these tests are
 * about the card keeping its shape rather than about any one string.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import StepNode from "./StepNode";
import type { StepNodeData, NodeExecStatus } from "./StepNode";

// Handle needs a ReactFlow store; the card's own layout is what is under test.
vi.mock("reactflow", () => ({
  Handle: () => null,
  Position: { Left: "left", Right: "right" },
}));

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  // The card asks for the reduced-motion preference on mount; this environment
  // has no matchMedia. Answering "no preference" keeps the running animation on,
  // which is the arm these tests render under.
  vi.stubGlobal("matchMedia", () => ({
    matches: false,
    addEventListener: () => {},
    removeEventListener: () => {},
  }));
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

function renderNode(data: Partial<StepNodeData>) {
  const full: StepNodeData = {
    label: "plan-step",
    role: "critic",
    assignment: "",
    prompt: "",
    capacity: 1,
    timeout: null,
    inputs: [],
    outputs: [],
    ...data,
  };
  act(() => {
    // NodeProps carries more than the card reads; the rest is ReactFlow's.
    root.render(React.createElement(StepNode, { data: full, selected: false } as never));
  });
}

/** The card's two rows, in order. */
function rows(): Element[] {
  return Array.from(container.querySelectorAll(":scope > div > div"));
}

function bottomRightText(): string {
  const bottom = rows()[1];
  const spans = bottom.querySelectorAll("span");
  return spans[spans.length - 1]?.textContent ?? "";
}

describe("StepNode — the bottom-right corner always says something", () => {
  it("shows elapsed time once there is any", () => {
    renderNode({ durationSeconds: 84, execStatus: "completed" });
    expect(bottomRightText()).toBe("1m");
  });

  it("shows the status word before there is a duration, rather than nothing", () => {
    // A corner that can go empty makes the card change shape mid-run, which is
    // exactly when a reader is scanning it.
    for (const [status, word] of [
      ["queued", "queued"],
      ["running", "running"],
      ["failed", "failed"],
      ["awaiting_approval", "approval"],
    ] as [NodeExecStatus, string][]) {
      renderNode({ execStatus: status });
      expect(bottomRightText()).toBe(word);
    }
  });

  it("prefers the duration over the status word when both could apply", () => {
    renderNode({ execStatus: "running", durationSeconds: 3.5 });
    expect(bottomRightText()).toBe("3.5s");
  });

  it("treats a negative duration as no duration, not as a printable number", () => {
    renderNode({ execStatus: "queued", durationSeconds: -1 });
    expect(bottomRightText()).toBe("queued");
  });

  it("is never blank in any status the card can be in", () => {
    const every: NodeExecStatus[] = [
      "pending",
      "queued",
      "running",
      "awaiting_approval",
      "paused",
      "completed",
      "failed",
      "escalated",
    ];
    for (const status of every) {
      renderNode({ execStatus: status });
      expect(bottomRightText().trim().length).toBeGreaterThan(0);
    }
  });
});

describe("StepNode — the card keeps its shape", () => {
  it("renders both rows even when the node carries no role", () => {
    // Held open rather than dropped: a missing role must not move the row above
    // it, because one height is what the layout reserves for every node.
    renderNode({ role: "" });
    expect(rows().length).toBe(2);
  });

  it("renders both rows for a node carrying nothing but a label", () => {
    renderNode({ role: "", execStatus: undefined, durationSeconds: undefined });
    expect(rows().length).toBe(2);
    expect(bottomRightText().trim().length).toBeGreaterThan(0);
  });

  it("puts the error count in the top row beside the state, not in the magnitude corner", () => {
    renderNode({ execStatus: "failed", errorCount: 3, durationSeconds: 12 });
    expect(rows()[0].textContent).toContain("3");
    expect(bottomRightText()).toBe("12s");
  });
});
