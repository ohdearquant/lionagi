import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { IntlProvider } from "use-intl";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import dagre from "dagre";
import type { Edge, Node } from "reactflow";
import type { WorkerGraph } from "@/lib/types";
import enMessages from "@/messages/en.json";
import WorkerCanvas from "./WorkerCanvas";
import { computeReservedHeight, getLayoutedElements } from "./useLayout";

const probes = vi.hoisted(() => ({ fitView: vi.fn() }));
vi.mock("./useLayout", async () => {
  const actual = await vi.importActual<typeof import("./useLayout")>("./useLayout");
  return { ...actual, getLayoutedElements: vi.fn(actual.getLayoutedElements) };
});
// Keep the canvas hooks and production layout; replace only the viewport,
// whose measurements are unavailable in jsdom.
vi.mock("reactflow", async () => {
  const actual = await vi.importActual<typeof import("reactflow")>("reactflow");
  return {
    ...actual,
    default: function Viewport({
      nodes,
      edges,
      onInit,
    }: {
      nodes: Node[];
      edges: Edge[];
      onInit: (instance: { fitView: typeof probes.fitView }) => void;
    }) {
      React.useEffect(() => onInit({ fitView: probes.fitView }), [onInit]);
      return (
        <div
          data-viewport
          data-nodes={nodes.length}
          data-edges={edges.length}
          data-positions={JSON.stringify(nodes.map(({ id, position }) => [id, position]))}
        />
      );
    },
    Background: () => null,
    Controls: () => null,
    MiniMap: () => null,
  };
});

const chain = Array.from({ length: 10 }, (_, index) => `chain-${index}`);
const roots = Array.from({ length: 100 }, (_, index) => `early-${index}`);
const graph: WorkerGraph = {
  name: "spanning fan-in",
  description: "",
  nodes: [...chain, ...roots, "join"].map((id) => ({
    id,
    label: id,
    role: "",
    assignment: "",
    prompt: "",
    capacity: 1,
    timeout: null,
    inputs: [],
    outputs: [],
  })),
  edges: [
    ...chain.slice(1).map((target, index) => [chain[index], target]),
    ["chain-9", "join"],
    ...roots.map((source) => [source, "join"]),
  ].map(([source, target]) => ({ id: `${source}-${target}`, source, target, mode: "simple" })),
};

let container: HTMLDivElement;
let root: Root;
let width: number;
const observers = new Set<ResizeObserverStub>();
const frames = new Map<number, FrameRequestCallback>();
let frameId: number;
class ResizeObserverStub {
  constructor(private callback: ResizeObserverCallback) {
    observers.add(this);
  }
  observe(element: Element) {
    Object.defineProperty(element, "clientWidth", { configurable: true, get: () => width });
  }
  disconnect() {
    observers.delete(this);
  }
  resize() {
    this.callback([], this as unknown as ResizeObserver);
  }
}

beforeEach(() => {
  width = 800;
  frameId = 0;
  vi.mocked(getLayoutedElements).mockClear();
  probes.fitView.mockClear();
  vi.stubGlobal("ResizeObserver", ResizeObserverStub);
  vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => {
    frames.set(++frameId, callback);
    return frameId;
  });
  vi.stubGlobal("cancelAnimationFrame", (id: number) => frames.delete(id));
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  observers.clear();
  frames.clear();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

it("reuses bounds on resize and uses bounded placement for repeated expansion mounts", () => {
  const layout = vi.spyOn(dagre, "layout");
  const inlineHeight = vi.fn();
  const expandedHeight = vi.fn();
  function render(expanded = false) {
    act(() => {
      root.render(
        <IntlProvider locale="en" messages={enMessages}>
          <WorkerCanvas key="inline" graph={graph} compact onLayoutHeight={inlineHeight} />
          {expanded && (
            <WorkerCanvas key="expanded" graph={graph} onLayoutHeight={expandedHeight} />
          )}
        </IntlProvider>,
      );
    });
  }
  render();
  expect(getLayoutedElements).toHaveBeenCalledTimes(1);
  expect(layout).not.toHaveBeenCalled();
  const initial = container.querySelector("[data-viewport]")!;
  expect(initial.getAttribute("data-nodes")).toBe("111");
  expect(initial.getAttribute("data-edges")).toBe("110");
  const initialPositions = initial.getAttribute("data-positions");
  const { width: bboxWidth, height: bboxHeight } =
    vi.mocked(getLayoutedElements).mock.results[0].value;
  expect(inlineHeight).toHaveBeenLastCalledWith(
    computeReservedHeight(bboxWidth, bboxHeight, width),
  );

  act(() => {
    width = 10000;
    for (let i = 0; i < 5; i++) observers.forEach((observer) => observer.resize());
    const pending = [...frames.values()];
    frames.clear();
    pending.forEach((callback) => callback(0));
  });
  expect(getLayoutedElements).toHaveBeenCalledTimes(1);
  expect(probes.fitView).toHaveBeenCalledTimes(1);
  expect(inlineHeight).toHaveBeenLastCalledWith(
    computeReservedHeight(bboxWidth, bboxHeight, width),
  );

  render(true);
  expect(getLayoutedElements).toHaveBeenCalledTimes(2);
  render(false);
  render(true);
  expect(getLayoutedElements).toHaveBeenCalledTimes(3);
  expect(layout).not.toHaveBeenCalled();
  for (const viewport of container.querySelectorAll("[data-viewport]")) {
    expect(viewport.getAttribute("data-nodes")).toBe("111");
    expect(viewport.getAttribute("data-edges")).toBe("110");
    expect(viewport.getAttribute("data-positions")).toBe(initialPositions);
  }
});
