import { afterEach, describe, expect, it, vi } from "vitest";
import dagre from "dagre";
import type { Edge, Node } from "reactflow";
import { estimateNodeHeight, getLayoutedElements, NODE_WIDTH } from "./useLayout";

const node = (id: string): Node => ({
  id,
  position: { x: 0, y: 0 },
  data: { label: id, description: `Content for ${id}` },
});
const edge = (source: string, target: string): Edge => ({
  id: `${source}-${target}`,
  source,
  target,
  data: { condition: "ready" },
});

function chain(length: number) {
  const nodes = Array.from({ length }, (_, index) => node(`chain-${index}`));
  const edges = nodes.slice(1).map((entry, index) => edge(nodes[index].id, entry.id));
  return { nodes, edges };
}

function fanIn() {
  const graph = chain(10);
  const roots = Array.from({ length: 100 }, (_, index) => node(`early-${index}`));
  return {
    nodes: [...graph.nodes, ...roots, node("join")],
    edges: [...graph.edges, edge("chain-9", "join"), ...roots.map((n) => edge(n.id, "join"))],
  };
}

function positions(nodes: Node[]) {
  return Object.fromEntries(nodes.map((n) => [n.id, n.position]));
}

function assertNoOverlap(nodes: Node[]) {
  for (let i = 0; i < nodes.length; i++) {
    const a = nodes[i];
    expect(Number.isFinite(a.position.x) && Number.isFinite(a.position.y)).toBe(true);
    for (let j = i + 1; j < nodes.length; j++) {
      const b = nodes[j];
      const separated =
        a.position.x + NODE_WIDTH <= b.position.x ||
        b.position.x + NODE_WIDTH <= a.position.x ||
        a.position.y + estimateNodeHeight(a) <= b.position.y ||
        b.position.y + estimateNodeHeight(b) <= a.position.y;
      expect(separated, `${a.id} overlaps ${b.id}`).toBe(true);
    }
  }
}

afterEach(() => vi.restoreAllMocks());

describe("bounded graph layout", () => {
  it("keeps dagre at the work limit and bypasses it just above", () => {
    const layout = vi.spyOn(dagre, "layout");
    const { nodes, edges } = chain(64);
    nodes.push(node("loose-0"), node("loose-1"));
    // 66 real nodes + 2 * 63 pinned rank spans = 192 units.
    getLayoutedElements(nodes, edges);
    expect(layout).toHaveBeenCalledTimes(1);
    // Missing endpoints contribute no rank span or phantom layout node.
    getLayoutedElements(nodes, [...edges, edge("missing", "chain-63")]);
    expect(layout).toHaveBeenCalledTimes(2);
    getLayoutedElements([...nodes, node("loose-2")], edges);
    expect(layout).toHaveBeenCalledTimes(2);
  });

  it("bounds isolated nodes even when the span budget is not exhausted", () => {
    const layout = vi.spyOn(dagre, "layout");
    const nodes = Array.from({ length: 128 }, (_, index) => node(`loose-${index}`));
    getLayoutedElements(nodes, []);
    expect(layout).toHaveBeenCalledTimes(1);
    const result = getLayoutedElements([...nodes, node("extra")], []);
    expect(layout).toHaveBeenCalledTimes(1);
    expect(result.nodes).toHaveLength(129);
    assertNoOverlap(result.nodes);
  });

  it.each(["LR", "TB"] as const)(
    "lays out a spanning fan-in without dagre in %s and preserves its records",
    (direction) => {
      const layout = vi.spyOn(dagre, "layout");
      const { nodes, edges } = fanIn();
      const withDangling = [...edges, edge("missing", "join")];
      const result = getLayoutedElements(nodes, withDangling, direction);
      expect(layout).not.toHaveBeenCalled();
      expect(result.nodes.map((n) => n.id).sort()).toEqual(nodes.map((n) => n.id).sort());
      expect(result.edges).toHaveLength(withDangling.length);
      result.edges.forEach((entry, index) => expect(entry).toMatchObject(withDangling[index]));
      expect(
        result.nodes.map((n) => n.data).sort((a, b) => a.label.localeCompare(b.label)),
      ).toEqual(nodes.map((n) => n.data).sort((a, b) => a.label.localeCompare(b.label)));
      expect(result.ranks.size).toBe(111);
      expect(result.ranks.get("join")).toBe(10);
      expect(result.ranks.get("early-0")).toBe(0);
      expect(result.ranks.has("missing")).toBe(false);
      expect(result.width).toBeGreaterThan(0);
      expect(result.height).toBeGreaterThan(0);
      assertNoOverlap(result.nodes);
      if (direction === "TB") {
        const y = new Map(result.nodes.map((n) => [n.id, n.position.y]));
        expect(y.get("early-0")).toBe(y.get("chain-0"));
        for (const e of edges) expect(y.get(e.target)!).toBeGreaterThan(y.get(e.source)!);
      } else {
        // Wide rank zero still wraps; later ranks remain beyond its whole block.
        const rankZero = result.nodes.filter((n) => result.ranks.get(n.id) === 0);
        expect(new Set(rankZero.map((n) => n.position.x)).size).toBeGreaterThan(1);
        const chainOne = result.nodes.find((n) => n.id === "chain-1")!;
        expect(chainOne.position.x).toBeGreaterThan(Math.max(...rankZero.map((n) => n.position.x)));
      }
    },
  );

  it.each(["LR", "TB"] as const)(
    "has stable %s positions when input order changes",
    (direction) => {
      const { nodes, edges } = fanIn();
      const original = getLayoutedElements(nodes, edges, direction);
      const reversed = getLayoutedElements([...nodes].reverse(), [...edges].reverse(), direction);
      expect(positions(reversed.nodes)).toEqual(positions(original.nodes));
    },
  );

  it("keeps chain folding and real continuation edges in the bounded path", () => {
    const layout = vi.spyOn(dagre, "layout");
    const { nodes, edges } = chain(111);
    const result = getLayoutedElements(nodes, edges);
    expect(layout).not.toHaveBeenCalled();
    expect(result.nodes).toHaveLength(111);
    expect(result.edges.map((e) => [e.id, e.source, e.target])).toEqual(
      edges.map((e) => [e.id, e.source, e.target]),
    );
    expect(result.edges.some((e) => e.data?.continuation)).toBe(true);
    expect(result.width).toBeLessThanOrEqual(1556);
    assertNoOverlap(result.nodes);
  });
});
