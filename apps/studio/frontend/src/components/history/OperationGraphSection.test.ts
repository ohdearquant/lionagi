import { describe, expect, it } from "vitest";

import type { OperationNode } from "@/lib/operationGraph";

import { computeLayers } from "./OperationGraphSection";

function node(opId: string, causeOpId: string | null = null): OperationNode {
  return {
    opId,
    name: opId,
    status: "succeeded",
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
});
