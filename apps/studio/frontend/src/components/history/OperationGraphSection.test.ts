import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { describe, expect, it, vi } from "vitest";

import type { OperationNode, OperationStatus } from "@/lib/operationGraph";

import OperationGraphSection, { computeLayers } from "./OperationGraphSection";

function node(
  opId: string,
  causeOpId: string | null = null,
  status: OperationStatus = "succeeded",
): OperationNode {
  return {
    opId,
    name: opId,
    status,
    causeOpId,
    elapsed: 0,
    firstTs: 0,
    lastTs: 0,
    eventCount: 1,
  };
}

const layerIds = (layers: OperationNode[][]) => layers.map((l) => l.map((n) => n.opId));

describe("computeLayers", () => {
  it("layers a linear chain by depth", () => {
    const nodes = [node("a"), node("b", "a"), node("c", "b")];
    const edges = [
      { source: "a", target: "b" },
      { source: "b", target: "c" },
    ];
    expect(layerIds(computeLayers(nodes, edges))).toEqual([["a"], ["b"], ["c"]]);
  });

  it("places a fan-in node after all predecessors even when causeOpId is null", () => {
    // synthesis-style join: w1 and w2 both feed j; j.causeOpId is null because
    // parent_id was absent (multiple predecessors live only in depends_on).
    const nodes = [node("w1"), node("w2"), node("j", null)];
    const edges = [
      { source: "w1", target: "j" },
      { source: "w2", target: "j" },
    ];
    const layers = computeLayers(nodes, edges);
    expect(layerIds(layers)).toEqual([["w1", "w2"], ["j"]]);
  });

  it("uses the longest path for a diamond", () => {
    // a→b→d and a→d: d must sit at depth 2 (after b), not depth 1.
    const nodes = [node("a"), node("b", "a"), node("d")];
    const edges = [
      { source: "a", target: "b" },
      { source: "b", target: "d" },
      { source: "a", target: "d" },
    ];
    const layers = computeLayers(nodes, edges);
    expect(layerIds(layers)).toEqual([["a"], ["b"], ["d"]]);
  });

  it("ignores edges referencing unknown ops", () => {
    const nodes = [node("a"), node("b", "a")];
    const edges = [
      { source: "a", target: "b" },
      { source: "ghost", target: "b" },
    ];
    expect(layerIds(computeLayers(nodes, edges))).toEqual([["a"], ["b"]]);
  });

  it("excludes continuations from depth while ordinary dependencies still layer", () => {
    const nodes = [node("origin"), node("retry"), node("dependent")];
    const edges = [
      { source: "origin", target: "retry", continuation: true },
      { source: "origin", target: "dependent" },
    ];

    expect(layerIds(computeLayers(nodes, edges))).toEqual([["origin", "retry"], ["dependent"]]);
  });
});

describe("OperationGraphSection status presentation", () => {
  it("renders escalated with warning tokens while failed keeps error tokens", () => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);

    try {
      act(() => {
        root.render(
          React.createElement(OperationGraphSection, {
            state: {
              nodes: [node("escalated-op", null, "escalated"), node("failed-op", null, "failed")],
              edges: [],
            },
            live: false,
          }),
        );
      });

      const cards = container.querySelectorAll(":scope > div > div > div");
      const escalatedCard = cards[0];
      const failedCard = cards[1];

      expect(escalatedCard?.className).toContain("border-l-status-warning");
      expect(escalatedCard?.className).not.toContain("border-l-status-error");
      expect(escalatedCard?.querySelector(".bg-status-warning")).not.toBeNull();
      expect(escalatedCard?.querySelector(".bg-status-error")).toBeNull();
      expect(failedCard?.className).toContain("border-l-status-error");
      expect(failedCard?.className).not.toContain("border-l-status-warning");
      expect(failedCard?.querySelector(".bg-status-error")).not.toBeNull();
      expect(failedCard?.querySelector(".bg-status-warning")).toBeNull();
    } finally {
      act(() => root.unmount());
      container.remove();
      vi.unstubAllGlobals();
    }
  });
});

describe("OperationGraphSection continuation presentation", () => {
  it("keeps a continuation visible with the established dotted low-weight style", () => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);

    try {
      act(() => {
        root.render(
          React.createElement(OperationGraphSection, {
            state: {
              nodes: [node("origin"), node("retry"), node("dependent")],
              edges: [
                { source: "origin", target: "retry", continuation: true },
                { source: "origin", target: "dependent" },
              ],
            },
            live: false,
          }),
        );
      });

      const paths = container.querySelectorAll("svg path");
      const continuation = container.querySelector('svg path[stroke-dasharray="2 6"]');
      const dependency = Array.from(paths).find((path) => path !== continuation);
      expect(paths).toHaveLength(2);
      expect(continuation?.getAttribute("d")).toBeTruthy();
      expect(continuation?.getAttribute("stroke-dasharray")).toBe("2 6");
      expect(continuation?.getAttribute("stroke-opacity")).toBe("0.35");
      expect(continuation?.getAttribute("stroke-width")).toBe("1.25");
      expect(continuation?.getAttribute("marker-end")).toBeNull();
      expect(dependency?.getAttribute("stroke-dasharray")).toBeNull();
      expect(dependency?.getAttribute("stroke-opacity")).toBe("0.5");
      expect(dependency?.getAttribute("stroke-width")).toBe("1.5");

      const pathCoordinates =
        continuation
          ?.getAttribute("d")
          ?.match(/-?\d+(?:\.\d+)?/g)
          ?.map(Number) ?? [];
      expect(pathCoordinates).toHaveLength(8);
      const [sourceX, , firstControlX, , secondControlX, , targetX] = pathCoordinates;
      expect(targetX).toBe(sourceX);
      expect(firstControlX).toBeGreaterThan(sourceX!);
      expect(secondControlX).toBeGreaterThan(sourceX!);
      expect(Number(container.querySelector("svg")?.getAttribute("width"))).toBeGreaterThan(
        firstControlX!,
      );
    } finally {
      act(() => root.unmount());
      container.remove();
      vi.unstubAllGlobals();
    }
  });

  it("keeps a forward continuation between layers on the ordinary curve", () => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);

    try {
      act(() => {
        root.render(
          React.createElement(OperationGraphSection, {
            state: {
              nodes: [node("source"), node("anchor"), node("target")],
              edges: [
                { source: "source", target: "target", continuation: true },
                { source: "anchor", target: "target" },
              ],
            },
            live: false,
          }),
        );
      });

      const continuation = container.querySelector('svg path[stroke-dasharray="2 6"]');
      const pathCoordinates =
        continuation
          ?.getAttribute("d")
          ?.match(/-?\d+(?:\.\d+)?/g)
          ?.map(Number) ?? [];
      expect(pathCoordinates).toHaveLength(8);
      const [sourceX, , firstControlX, , secondControlX, , targetX] = pathCoordinates;
      expect(targetX).toBeGreaterThan(sourceX!);
      expect(firstControlX).toBeGreaterThan(sourceX!);
      expect(firstControlX).toBeLessThan(targetX!);
      expect(secondControlX).toBe(firstControlX);
    } finally {
      act(() => root.unmount());
      container.remove();
      vi.unstubAllGlobals();
    }
  });
});
