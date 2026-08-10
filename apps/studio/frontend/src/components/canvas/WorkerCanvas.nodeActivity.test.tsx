/**
 * These tests start where the app starts — raw signal events as they arrive off
 * the wire — and end at the text a node card actually renders, running the
 * production path in between: the wire normalizer, the correlation, the canvas
 * projection, and the real StepNode.
 *
 * That span is the point. The activity fold was fully unit-tested and green
 * while nothing in the app called it and no card carried live data, so an
 * assertion that stops at the fold cannot tell a wired projection from an
 * unwired one. Both failure modes this covers are silent in exactly that way:
 * an unwired projection renders the plain status word, and a timestamp left in
 * the backend's seconds renders every signalled node as "stalled" — neither
 * throws, and both look like a run that simply has nothing to report.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { IntlProvider } from "use-intl";
import enMessages from "@/messages/en.json";
import WorkerCanvas from "./WorkerCanvas";
import { normalizeSignalEvent } from "@/lib/api";
import type { SignalEvent } from "@/lib/api";
import { buildNodeActivityByName } from "@/lib/nodeActivity";
import type { NodeActivitySnapshot } from "@/lib/nodeActivity";
import type { WorkerGraph } from "@/lib/types";

// ReactFlow's viewport measures its container against a zustand store, which in
// jsdom (every element 0x0) re-measures itself into an infinite update loop —
// the reason the other tests here mock it away entirely. This replaces ONLY the
// viewport renderer and its decorations, with a stand-in that draws every node
// through the real nodeTypes map. Everything under test stays real: the
// canvas's own node state (useNodesState is a plain useState wrapper), the
// layout, the projection effect, and StepNode itself. What is not covered is
// therefore stated rather than implied — panning, zoom, culling and node
// measurement are ReactFlow's, and no assertion here depends on them.
vi.mock("reactflow", async () => {
  const actual = await vi.importActual<typeof import("reactflow")>("reactflow");
  type StubNode = { id: string; type?: string; data: unknown };
  type NodeComponent = React.ComponentType<{ id: string; data: unknown; selected: boolean }>;
  return {
    ...actual,
    default: ({
      nodes,
      nodeTypes,
    }: {
      nodes: StubNode[];
      nodeTypes: Record<string, NodeComponent>;
    }) => (
      <div>
        {nodes.map((n) => {
          const NodeComponent = nodeTypes[n.type ?? ""];
          return (
            <div key={n.id} className="react-flow__node" data-node-id={n.id}>
              {NodeComponent ? <NodeComponent id={n.id} data={n.data} selected={false} /> : null}
            </div>
          );
        })}
      </div>
    ),
    Background: () => null,
    Controls: () => null,
    MiniMap: () => null,
    Handle: () => null,
  };
});

let container: HTMLDivElement;
let root: Root;

class StubObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
  takeRecords() {
    return [];
  }
}

beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.stubGlobal("matchMedia", () => ({
    matches: false,
    addEventListener: () => {},
    removeEventListener: () => {},
  }));
  // ReactFlow measures its container and StepNode asks whether it is on screen;
  // neither exists in jsdom. Observing nothing leaves the card in its default
  // state, which is the one under test.
  vi.stubGlobal("ResizeObserver", StubObserver);
  vi.stubGlobal("IntersectionObserver", StubObserver);
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
});

const GRAPH: WorkerGraph = {
  name: "demo",
  description: "",
  nodes: [
    {
      id: "plan",
      label: "plan",
      role: "orchestrator",
      assignment: "",
      prompt: "",
      capacity: 1,
      timeout: null,
      inputs: [],
      outputs: [],
    },
    {
      id: "silent",
      label: "silent",
      role: "implementer",
      assignment: "",
      prompt: "",
      capacity: 1,
      timeout: null,
      inputs: [],
      outputs: [],
    },
  ],
  edges: [],
};

// One event exactly as the backend emits it: a Unix timestamp in SECONDS, and
// the authored step id under payload.name (the runtime op_id is a UUID the
// planned graph has never heard of).
function rawEvent(overrides: Partial<SignalEvent> = {}): SignalEvent {
  return {
    id: "e1",
    session_id: "s1",
    seq: 1,
    kind: "NodeStarted",
    op_id: "0f9d6c2e-0000-4000-8000-000000000001",
    ts: Date.now() / 1000,
    payload: { name: "plan" },
    ...overrides,
  };
}

function renderCanvas(nodeActivity?: Map<string, NodeActivitySnapshot>) {
  act(() => {
    root.render(
      <IntlProvider locale="en" messages={enMessages}>
        <WorkerCanvas
          graph={GRAPH}
          editable={false}
          nodeStatuses={{ plan: "running", silent: "running" }}
          nodeActivity={nodeActivity}
          live
        />
      </IntlProvider>,
    );
  });
}

// The card for a node, located by the label it renders.
function cardTextFor(label: string): string {
  const nodes = Array.from(container.querySelectorAll<HTMLElement>(".react-flow__node"));
  const match = nodes.find((n) => n.textContent?.includes(label));
  if (!match) throw new Error(`no rendered node card contains "${label}"`);
  return match.textContent ?? "";
}

describe("WorkerCanvas — live activity reaches the card it describes", () => {
  it("renders what a node is doing, from raw events through the production path", () => {
    const events = [
      rawEvent({ payload: { name: "plan", tool_name: "grep" }, kind: "ToolCallStarted" }),
    ].map(normalizeSignalEvent);

    renderCanvas(buildNodeActivityByName(events));

    // "tool: grep" is only reachable if the fold ran, the correlation matched
    // the authored id, and the canvas wrote the result into the card's data.
    expect(cardTextFor("plan")).toContain("tool: grep");
  });

  it("shows the assistant's latest text on the card", () => {
    const events = [
      rawEvent({ kind: "AssistantDelta", payload: { name: "plan", text: "reading the ADR" } }),
    ].map(normalizeSignalEvent);

    renderCanvas(buildNodeActivityByName(events));

    expect(cardTextFor("plan")).toContain("reading the ADR");
  });

  it("leaves a node with no signal exactly as it was — no activity, not a blank", () => {
    const events = [rawEvent()].map(normalizeSignalEvent);

    renderCanvas(buildNodeActivityByName(events));

    // "plan" is thinking; "silent" was never correlated, so it keeps the plain
    // status word rather than borrowing its neighbour's activity or rendering
    // an empty row where the word belongs.
    expect(cardTextFor("plan")).toContain("thinking");
    expect(cardTextFor("silent")).toContain("running");
    expect(cardTextFor("silent")).not.toContain("thinking");
  });

  it("does not read a card as stalled when its event just arrived", () => {
    // The unit defect, stated where it shows: the backend stamps seconds and
    // the card's stall clock is Date.now() in milliseconds. Normalized, a
    // fresh event is fresh. Un-normalized it is ~1.7 trillion ms old, so
    // every node carrying a signal would render "stalled" the instant it got
    // one — the arm below proves that is what the normalizer prevents.
    const raw = [rawEvent()];

    renderCanvas(buildNodeActivityByName(raw.map(normalizeSignalEvent)));
    expect(cardTextFor("plan")).toContain("thinking");
    expect(cardTextFor("plan")).not.toContain("stalled");

    renderCanvas(buildNodeActivityByName(raw));
    expect(cardTextFor("plan")).toContain("stalled");
  });

  it("renders the plain status word when nothing projects activity at all", () => {
    // The shape of the original defect: everything below the canvas worked and
    // the canvas was simply never handed the data.
    renderCanvas(undefined);

    expect(cardTextFor("plan")).toContain("running");
    expect(cardTextFor("plan")).not.toContain("thinking");
  });
});
