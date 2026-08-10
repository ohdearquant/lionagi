/**
 * Rank assignment is ours, not dagre's (ADR-0113 D2).
 *
 * A node's rank is its longest path from any source, so nodes that can start
 * together share a rank. dagre keeps the jobs it is good at: ordering nodes
 * within a rank to reduce crossings, and routing.
 *
 * The distinction these tests are built around: `computeNodeDepths` has always
 * computed the right numbers, but it fed only rank separation and edge
 * styling. dagre ranked the graph independently, and dagre optimizes total
 * edge length rather than concurrency. So asserting on the rank function alone
 * passes whether or not the canvas draws the ranks the function returned. The
 * assertion has to be on the laid-out positions.
 *
 * The graph below is the one the decision was measured on: `a`, `b`, `c` have
 * no dependencies; `d`, `e`, `f` depend on `a`; `g` depends on `d`, `e`, `f`;
 * `h` depends on `b`, `c`, `g`. `b` and `c` start at the same moment as `a`,
 * but their results are not consumed until `h`, so an edge-length optimizer
 * pulls them rightward to sit beside `g` — drawing them as though they ran
 * late. That is the specific false statement this replaces.
 */
import { describe, it, expect } from "vitest";
import type { Node, Edge } from "reactflow";
import { computeNodeDepths, getLayoutedElements } from "./useLayout";

const ids = ["a", "b", "c", "d", "e", "f", "g", "h"] as const;

const nodes: Node[] = ids.map((id) => ({
  id,
  position: { x: 0, y: 0 },
  data: { label: id },
}));

const edges: Edge[] = (
  [
    ["a", "d"],
    ["a", "e"],
    ["a", "f"],
    ["d", "g"],
    ["e", "g"],
    ["f", "g"],
    ["b", "h"],
    ["c", "h"],
    ["g", "h"],
  ] as [string, string][]
).map(([source, target]) => ({ id: `${source}-${target}`, source, target }));

/** Group node ids by the column they were actually drawn in. An LR layout
 *  gives every node in a rank the same x, so distinct x values are the
 *  columns, left to right. */
function drawnColumns(laidOut: Node[]): string[][] {
  const xById = new Map(laidOut.map((n) => [n.id, Math.round(n.position.x)]));
  const columns = [...new Set(xById.values())].sort((p, q) => p - q);
  return columns.map((x) =>
    [...xById.entries()]
      .filter(([, nodeX]) => nodeX === x)
      .map(([id]) => id)
      .sort(),
  );
}

describe("canvas/useLayout.ts — rank assignment is ASAP", () => {
  it("ranks every node at its longest path from a source", () => {
    const ranks = computeNodeDepths(nodes, edges);

    // Independent work shares rank 0 even when its result is consumed late.
    expect(ranks.get("a")).toBe(0);
    expect(ranks.get("b")).toBe(0);
    expect(ranks.get("c")).toBe(0);

    expect(ranks.get("d")).toBe(1);
    expect(ranks.get("e")).toBe(1);
    expect(ranks.get("f")).toBe(1);
    expect(ranks.get("g")).toBe(2);
    expect(ranks.get("h")).toBe(3);
  });

  // This is the one that discriminates. The assertion above passes against a
  // canvas that ignores the rank function entirely.
  it("draws nodes in the columns the rank function assigned", () => {
    const { nodes: laidOut } = getLayoutedElements(nodes, edges, "LR");

    expect(drawnColumns(laidOut)).toEqual([["a", "b", "c"], ["d", "e", "f"], ["g"], ["h"]]);
  });

  it("never draws a node in an earlier column than something it depends on", () => {
    const { nodes: laidOut } = getLayoutedElements(nodes, edges, "LR");
    const xById = new Map(laidOut.map((n) => [n.id, Math.round(n.position.x)]));

    for (const edge of edges) {
      const sourceX = xById.get(edge.source)!;
      const targetX = xById.get(edge.target)!;
      expect(
        targetX,
        `${edge.source} -> ${edge.target} runs backwards or sits in the same column`,
      ).toBeGreaterThan(sourceX);
    }
  });

  it("draws a node with no edges in the first column rather than inventing a position for it", () => {
    // An operation that entered the graph unattached depends on nothing, and
    // the honest drawing says so. Placing it beside its apparent siblings
    // would imply a relationship the graph does not carry.
    const detached: Node = { id: "loose", position: { x: 0, y: 0 }, data: { label: "loose" } };
    const withDetached = [...nodes, detached];

    expect(computeNodeDepths(withDetached, edges).get("loose")).toBe(0);

    // The rank assertion above is the non-discriminating half — it passes
    // against a canvas that ignores the rank function entirely. The detached
    // node has to be DRAWN in rank 0's column, beside the other work that
    // depends on nothing.
    const { nodes: laidOut } = getLayoutedElements(withDetached, edges, "LR");
    expect(drawnColumns(laidOut)[0]).toEqual(["a", "b", "c", "loose"]);
  });

  it("lays out a graph whose edge names an endpoint that never arrived", () => {
    // An edge can reference a node the graph never received — an escalation
    // child whose parent is absent, for instance. Such an edge has no rank gap
    // to pin, and the layout still has to draw the nodes it does have. Passing
    // dagre an explicit `undefined` label for this edge crashes the whole
    // layout, so this covers every node on the canvas, not just the dangling one.
    const dangling: Edge[] = [...edges, { id: "ghost-h", source: "ghost", target: "h" }];

    expect(() => getLayoutedElements(nodes, dangling, "LR")).not.toThrow();

    const { nodes: laidOut } = getLayoutedElements(nodes, dangling, "LR");
    expect(laidOut.map((n) => n.id).sort()).toEqual([...ids].sort());
    for (const node of laidOut) {
      expect(Number.isFinite(node.position.x), `${node.id} has no finite x`).toBe(true);
      expect(Number.isFinite(node.position.y), `${node.id} has no finite y`).toBe(true);
    }
  });
});
