import { afterEach, describe, expect, it, vi } from "vitest";
import dagre from "dagre";
import type { Edge, Node } from "reactflow";
import {
  estimateNodeHeight,
  getLayoutedElements,
  NODE_HEIGHT,
  NODE_WIDTH,
} from "./useLayout";

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

function backwardChain(length: number, backEdges: number) {
  const nodes = Array.from({ length }, (_, index) => node(`c${index}`));
  return {
    nodes,
    edges: [
      ...nodes.slice(1).map((entry, index) => edge(nodes[index].id, entry.id)),
      ...nodes.slice(0, backEdges).map((entry) => edge(nodes[length - 1].id, entry.id)),
    ],
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

// One entry per folded row: the width its cards actually occupy. Rows are
// separated on y by the fold, so a rounded y identifies a row.
function rowWidths(nodes: Node[]) {
  const rows = new Map<number, Node[]>();
  for (const entry of nodes) {
    const key = Math.round(entry.position.y);
    rows.set(key, [...(rows.get(key) ?? []), entry]);
  }
  return [...rows.values()].map((row) => {
    const left = Math.min(...row.map((entry) => entry.position.x));
    const right = Math.max(...row.map((entry) => entry.position.x + NODE_WIDTH));
    return right - left;
  });
}

afterEach(() => vi.restoreAllMocks());

describe("bounded graph layout", () => {
  it.each([
    [40, 37],
    [46, 28],
  ])("bypasses dagre for a %i-node chain with %i back edges", (length, backEdges) => {
    const layout = vi.spyOn(dagre, "layout");
    const { nodes, edges } = backwardChain(length, backEdges);
    const result = getLayoutedElements(nodes, edges);
    expect(layout).not.toHaveBeenCalled();
    expect(result.nodes).toHaveLength(length);
    expect(result.edges.map((entry) => [entry.id, entry.source, entry.target])).toEqual(
      edges.map((entry) => [entry.id, entry.source, entry.target]),
    );
    assertNoOverlap(result.nodes);
  });

  it.each(["LR", "TB"] as const)(
    "has stable %s cyclic fallback ranks and positions under input permutations",
    (direction) => {
      const { nodes, edges } = backwardChain(46, 28);
      nodes.push(...Array.from({ length: 84 }, (_, index) => node(`loose-${index}`)));
      const interlocked = [node("a"), node("b"), node("c")];
      const paired = interlocked.flatMap((source) =>
        interlocked
          .filter((target) => source !== target)
          .map((target) => edge(source.id, target.id)),
      );
      const layout = vi.spyOn(dagre, "layout");
      for (const graph of [
        { nodes, edges },
        { nodes: interlocked, edges: paired },
      ]) {
        const original = getLayoutedElements(graph.nodes, graph.edges, direction);
        const reversed = getLayoutedElements(
          [...graph.nodes].reverse(),
          [...graph.edges].reverse(),
          direction,
        );
        expect(Object.fromEntries(reversed.ranks)).toEqual(Object.fromEntries(original.ranks));
        expect(positions(reversed.nodes)).toEqual(positions(original.nodes));
        assertNoOverlap(original.nodes);
      }
      expect(layout).not.toHaveBeenCalled();
    },
  );

  it("charges distinct directed pairs while preserving all parallel edge records", () => {
    const layout = vi.spyOn(dagre, "layout");
    const nodes = [node("a"), node("b")];
    const edges = Array.from({ length: 100 }, (_, index) => ({
      ...edge("a", "b"),
      id: `parallel-${index}`,
      data: { condition: `condition-${index}` },
    }));
    const result = getLayoutedElements(nodes, edges);
    expect(layout).toHaveBeenCalledTimes(1);
    expect(result.edges).toEqual(edges);
    expect(positions(result.nodes)).toEqual(
      positions(getLayoutedElements(nodes, [edge("a", "b")]).nodes),
    );
    expect(layout).toHaveBeenCalledTimes(2);
    getLayoutedElements(nodes, [...edges, edge("b", "a")]);
    expect(layout).toHaveBeenCalledTimes(2);
    const loops = Array.from({ length: 100 }, (_, index) => ({
      ...edge("a", "a"),
      id: `self-${index}`,
    }));
    expect(getLayoutedElements([node("a")], loops).edges).toEqual(loops);
    expect(layout).toHaveBeenCalledTimes(3);
  });

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

  it("keeps visual wrap and fold boundaries independent from dagre admission", () => {
    const layout = vi.spyOn(dagre, "layout");
    for (const count of [7, 8]) {
      const leaves = Array.from({ length: count }, (_, index) => node(`leaf-${index}`));
      const result = getLayoutedElements(
        [node("root"), ...leaves],
        leaves.map((entry) => edge("root", entry.id)),
      );
      const columns = new Set(
        result.nodes.filter((entry) => entry.id !== "root").map((entry) => entry.position.x),
      );
      expect(columns.size).toBe(count === 7 ? 1 : 3);
    }
    const short = chain(5);
    const longer = chain(6);
    const beforeFold = getLayoutedElements(short.nodes, short.edges);
    const afterFold = getLayoutedElements(longer.nodes, longer.edges);
    expect(new Set(beforeFold.nodes.map((entry) => entry.position.y)).size).toBe(1);
    expect(new Set(afterFold.nodes.map((entry) => entry.position.y)).size).toBe(2);
    expect(beforeFold.edges.some((entry) => entry.data?.continuation)).toBe(false);
    expect(afterFold.edges.some((entry) => entry.data?.continuation)).toBe(true);
    expect(layout).toHaveBeenCalledTimes(4);
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

  it("marks only a genuine fold boundary on a cyclic fallback", () => {
    // A 40-node chain with back edges from c39 to c0..c6. The edges make a
    // cycle, so this takes the fallback, and the tail from c7 onward is spanned
    // by none of them, so the chain still has boundaries the fold can break at.
    // Back edges covering the rows below instead would leave a row with no
    // breakable boundary, it would stay whole, and the two loops below would
    // run zero times.
    const layout = vi.spyOn(dagre, "layout");
    const nodes = Array.from({ length: 40 }, (_, index) => node(`c${index}`));
    const edges = [
      ...nodes.slice(1).map((entry, index) => edge(nodes[index].id, entry.id)),
      ...nodes.slice(0, 7).map((entry) => edge("c39", entry.id)),
    ];
    const result = getLayoutedElements(nodes, edges);
    expect(layout).not.toHaveBeenCalled();
    const rows = new Set(result.nodes.map((entry) => Math.round(entry.position.y))).size;
    expect(rows).toBeGreaterThan(1);
    const marked = result.edges.filter((entry) => entry.data?.continuation);
    expect(marked.length).toBeGreaterThan(0);
    // A dependency that points backwards on its own is not a fold continuation.
    for (const entry of marked) {
      expect(result.ranks.get(entry.target)!).toBeGreaterThan(result.ranks.get(entry.source)!);
    }
    const y = new Map(result.nodes.map((entry) => [entry.id, entry.position.y]));
    for (const entry of marked) {
      expect(y.get(entry.target)!).toBeGreaterThan(y.get(entry.source)!);
    }
    expect(marked).toHaveLength(rows - 1);
  });

  it("marks no continuation in a top-to-bottom layout", () => {
    const layout = vi.spyOn(dagre, "layout");
    const leaves = Array.from({ length: 128 }, (_, index) => node(`leaf-${index}`));
    const result = getLayoutedElements(
      [node("root"), ...leaves],
      leaves.map((entry) => edge("root", entry.id)),
      "TB",
    );
    expect(layout).not.toHaveBeenCalled();
    expect(result.edges.some((entry) => entry.data?.continuation)).toBe(false);
    assertNoOverlap(result.nodes);
  });

  it("folds a long chain that one chord spans end to end, without marking the chord", () => {
    // A 39-node chain plus one edge from the first node to the last. That chord
    // crosses every column boundary, so no boundary is a safe break; the row
    // would run on without limit and the graph would stay a single strip far
    // wider than the fold bound. The fallback breaks at the row's last chain
    // link instead, leaving the chord an ordinary forward edge rather than a
    // row boundary.
    const ids = Array.from({ length: 39 }, (_, index) => `c${index}`);
    const nodes = ids.map(node);
    const edges = ids.slice(1).map((id, index) => edge(ids[index], id));
    edges.push(edge("c0", "c38"));
    const result = getLayoutedElements(nodes, edges, "LR");
    const rowTops = new Set(result.nodes.map((entry) => Math.round(entry.position.y)));
    expect(rowTops.size).toBeGreaterThan(1);
    for (const w of rowWidths(result.nodes)) expect(w).toBeLessThanOrEqual(1500);
    const chord = result.edges.find((entry) => entry.id === "c0-c38")!;
    expect(chord.data?.continuation).toBeUndefined();
  });

  it("breaks before a chord at the last safe boundary inside the width bound", () => {
    // A ten-node chain with a chord from c3 to c9. The boundary the row first
    // overflows at (c4|c5) is crossed by the chord, but the safe boundary before
    // the chord (c2|c3) still lies inside the width bound, so the fill goes back
    // to it rather than running the row past the bound.
    const ids = Array.from({ length: 10 }, (_, index) => `c${index}`);
    const nodes = ids.map(node);
    const edges = ids.slice(1).map((id, index) => edge(ids[index], id));
    edges.push(edge("c3", "c9"));
    const result = getLayoutedElements(nodes, edges, "LR");
    const y = new Map(result.nodes.map((entry) => [entry.id, entry.position.y]));
    expect(y.get("c2")).not.toBe(y.get("c3"));
    const firstRow = result.nodes
      .filter((entry) => entry.position.y === y.get("c0"))
      .map((entry) => entry.id)
      .sort();
    expect(firstRow).toEqual(["c0", "c1", "c2"]);
    const chord = result.edges.find((entry) => entry.id === "c3-c9")!;
    expect(chord.data?.continuation).toBeUndefined();
  });

  it("folds a chained graph with a short chord without any row crossing the bound", () => {
    // c3->c6 crosses the boundaries around the middle of the row, so the first
    // row reaches its bound at a boundary the chord spans. The fill falls back
    // to the last safe break before the chord; every resulting row must fit.
    const ids = Array.from({ length: 10 }, (_, index) => `c${index}`);
    const nodes = ids.map(node);
    const edges = ids.slice(1).map((id, index) => edge(ids[index], id));
    edges.push(edge("c3", "c6"));
    const result = getLayoutedElements(nodes, edges, "LR");
    const rowTops = new Set(result.nodes.map((entry) => Math.round(entry.position.y)));
    expect(rowTops.size).toBeGreaterThan(1);
    for (const w of rowWidths(result.nodes)) expect(w).toBeLessThanOrEqual(1500);
    assertNoOverlap(result.nodes);
  });

  it("lays out one slot per node id and does not overwrite the kept record", () => {
    // 200 extra records for the same id. The layout hands dagre one node per
    // distinct id, so the id is set once by the layout rather than once per
    // duplicate (dagre's own graph build sets it once more), and the height it
    // reserves is the kept record's. Setting the record per input duplicate
    // instead would let a later duplicate overwrite the height it reserved.
    const setNode = vi.spyOn(dagre.graphlib.Graph.prototype, "setNode");
    const nodes = [node("root"), ...Array.from({ length: 200 }, () => node("dup"))];
    const edges = [edge("root", "dup")];
    getLayoutedElements(nodes, edges, "LR");
    // The layout builds one graph, then dagre builds its own from it; take the
    // first instance, which is the layout's, and look only at the calls that
    // pass a label (graphlib also calls setNode with no label to create an
    // edge's endpoints). One slot per id means the two ids are sized twice
    // here, not once per input record.
    const graph = setNode.mock.instances[0];
    const sized = setNode.mock.calls.filter(
      (call, i) =>
        setNode.mock.instances[i] === graph && typeof call[1] === "object" && call[1] !== null,
    );
    expect(sized.map((call) => call[0])).toEqual(["root", "dup"]);
    expect((sized[1]![1] as { height: number }).height).toBe(NODE_HEIGHT);
  });

  it("keeps one slot per node id, so a duplicated record cannot overlap in TB", () => {
    // The canvas can hand the layout two records for one id. Both would take a
    // slot of their own, and the gap pass would then push the second one down
    // into the neighbouring column on top of another card. The first record is
    // the one kept, so the 150 records collapse to the 149 distinct ids and no
    // two cards overlap.
    const ids = Array.from({ length: 149 }, (_, index) => `n|${index}`);
    ids[0] = "é|0";
    const nodes = [...ids.map(node), node(ids[0])];
    const edges = ids.slice(1).map((id, index) => edge(ids[index], id));
    const result = getLayoutedElements(nodes, edges, "TB");
    assertNoOverlap(result.nodes);
    expect(result.nodes.map((entry) => entry.id).sort()).toEqual([...ids].sort());
    expect(new Set(result.nodes.map((entry) => entry.id)).size).toBe(result.nodes.length);
  });

  it("keeps one slot per node id when a generated graph duplicates a record", () => {
    // The same property on a graph whose ids mix label shapes and whose parents
    // spread across ranks, so the duplicate lands in a rank that already holds
    // other cards rather than on a chain's end.
    let state = 1;
    const next = () => {
      state = (state + 0x6d2b79f5) | 0;
      let t = Math.imul(state ^ (state >>> 15), 1 | state);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
    const labels = ["é", "a", "Z", "Ω", "漢字", "😀", "~", "2", "10", "1", "b", "c", "d", "e"];
    const ids = Array.from(
      { length: 149 },
      (_, index) => `${labels[Math.floor(next() * labels.length)]}|${index}`,
    );
    ids[0] = "é|0";
    const nodes = [...ids.map(node), node(ids[0])];
    const edges = ids.slice(1).map((id, index) => edge(ids[Math.floor(next() * index)], id));
    const result = getLayoutedElements(nodes, edges, "TB");
    assertNoOverlap(result.nodes);
    expect(result.nodes.map((entry) => entry.id).sort()).toEqual([...ids].sort());
    expect(new Set(result.nodes.map((entry) => entry.id)).size).toBe(result.nodes.length);
  });

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
