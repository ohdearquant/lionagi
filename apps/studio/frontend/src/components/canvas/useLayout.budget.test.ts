import { afterEach, describe, expect, it, vi } from "vitest";
import dagre from "dagre";
import type { Edge, Node } from "reactflow";
import {
  estimateNodeHeight,
  FOLD_MAX_ROW_WIDTH,
  FOLD_ROW_GAP,
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

// The fold separates rows by FOLD_ROW_GAP on y, but dagre gives two nodes
// inside one row slightly different y (a chord's dummy offsets), so a rounded y
// is not a row. Group by the gap the fold itself leaves: one entry per row,
// left to right within the row.
function foldRows(nodes: Node[]): Node[][] {
  const sorted = [...nodes].sort((a, b) => a.position.y - b.position.y);
  const rows: Node[][] = [];
  let current: Node[] = [];
  let previousBottom = -Infinity;
  for (const entry of sorted) {
    if (current.length > 0 && entry.position.y - previousBottom >= FOLD_ROW_GAP) {
      rows.push(current);
      current = [];
    }
    current.push(entry);
    previousBottom = Math.max(previousBottom, entry.position.y + estimateNodeHeight(entry));
  }
  if (current.length > 0) rows.push(current);
  return rows.map((row) => [...row].sort((a, b) => a.position.x - b.position.x));
}

// One entry per folded row: the width its cards actually occupy.
function rowWidths(nodes: Node[]) {
  return foldRows(nodes).map((row) => {
    const left = Math.min(...row.map((entry) => entry.position.x));
    const right = Math.max(...row.map((entry) => entry.position.x + NODE_WIDTH));
    return right - left;
  });
}

// The node ids in each folded row, in reading order (left to right), so a
// case can name every row it produces — including a row the rule leaves whole.
function foldRowIds(nodes: Node[]): string[][] {
  return foldRows(nodes).map((row) => row.map((entry) => entry.id));
}

// Assert the fold's promises on a laid-out graph:
//  - an edge marked as a continuation is the single chain link the fold turned
//    around at a taken row break: one out-edge and one in-edge over the drawn
//    edge set, exactly the degree test foldWideGraph uses. A chord is never
//    marked, whatever it does on the canvas;
//  - no unmarked edge runs backwards across a fold: if its target lands in a
//    later row and left of its source, it must be a marked chain link. This is
//    the rule the fold exists to keep — a real dependency is never drawn as a
//    row boundary — checked from the final geometry rather than the fold's own
//    bookkeeping.
// It returns the row widths, so a case can assert which rows fit the bound and
// which were left whole.
function assertFoldInvariants(nodes: Node[], edges: Edge[]) {
  const rowOf = new Map<string, number>();
  foldRows(nodes).forEach((row, index) => row.forEach((entry) => rowOf.set(entry.id, index)));
  const xOf = new Map(nodes.map((entry) => [entry.id, Math.round(entry.position.x)]));
  const outDeg = new Map<string, number>();
  const inDeg = new Map<string, number>();
  for (const entry of edges) {
    outDeg.set(entry.source, (outDeg.get(entry.source) ?? 0) + 1);
    inDeg.set(entry.target, (inDeg.get(entry.target) ?? 0) + 1);
  }
  for (const entry of edges.filter((edgeEntry) => edgeEntry.data?.continuation)) {
    expect(outDeg.get(entry.source), `${entry.id} marked but not a single-out link`).toBe(1);
    expect(inDeg.get(entry.target), `${entry.id} marked but not a single-in link`).toBe(1);
  }
  for (const entry of edges) {
    const sourceRow = rowOf.get(entry.source);
    const targetRow = rowOf.get(entry.target);
    if (sourceRow === undefined || targetRow === undefined) continue;
    if (targetRow <= sourceRow) continue;
    if (xOf.get(entry.target)! >= xOf.get(entry.source)!) continue;
    // Runs backwards across a row: it must be the marked chain link the fold
    // turned around, never an unmarked dependency.
    expect(entry.data?.continuation, `${entry.id} runs backwards across a fold unmarked`).toBe(
      true,
    );
  }
  return rowWidths(nodes);
}

// Read the rows the layout ACTUALLY produced and return each one that is wider
// than FOLD_MAX_ROW_WIDTH while holding an admissible chain-link break inside
// that bound — a break the rule could have taken and did not. An empty result
// means every row wider than the bound is a row the rule left whole or had to
// reach past the bound for, because no admissible break was available inside it.
//
// It reads the rows off the folded output rather than re-walking its own
// recomputed plan, so a row the layout left unfolded is judged as it stands.
// Recomputing the plan and walking that instead made this helper agree with
// itself by construction: an unfolded chain was re-planned into rows that fit
// and reported clean, whatever the layout had done.
//
// Columns are recovered from the folded output: each row's leftmost card sits at
// the shared left margin, a row's internal spacing is its original spacing, and
// the original pitch is the first positive gap any row shows. A row's span is
// the chain position of its first and last card. The rule's own plan is still
// re-derived, because deciding whether a crossing edge runs backwards needs to
// know which row its target lands in.
function rowsWithAdmissibleBreakInside(nodes: Node[], edges: Edge[]): number[] {
  const rows = foldRows(nodes);
  const columnOf = new Map<string, number>();
  let offset = 0;
  rows.forEach((row) => {
    const xs = [...new Set(row.map((entry) => Math.round(entry.position.x)))].sort((a, b) => a - b);
    row.forEach((entry) => {
      columnOf.set(entry.id, offset + xs.indexOf(Math.round(entry.position.x)));
    });
    offset += xs.length;
  });
  const columns = offset;
  let pitch = 0;
  for (const row of rows) {
    for (let j = 1; j < row.length; j++) {
      const gap = Math.round(row[j]!.position.x) - Math.round(row[j - 1]!.position.x);
      if (gap > 0) {
        pitch = gap;
        break;
      }
    }
    if (pitch > 0) break;
  }
  const left = Math.min(...nodes.map((entry) => entry.position.x));
  const columnX = (index: number) => left + index * pitch;
  // Column -> the single node id on it, or several.
  const atColumn = new Map<number, string[]>();
  for (const entry of nodes) {
    const column = columnOf.get(entry.id)!;
    atColumn.set(column, [...(atColumn.get(column) ?? []), entry.id]);
  }
  const outDeg = new Map<string, number>();
  const inDeg = new Map<string, number>();
  for (const entry of edges) {
    outDeg.set(entry.source, (outDeg.get(entry.source) ?? 0) + 1);
    inDeg.set(entry.target, (inDeg.get(entry.target) ?? 0) + 1);
  }
  const edgeSet = new Set(edges.map((entry) => `${entry.source}>${entry.target}`));
  const isChainLink = (cut: number) => {
    const before = atColumn.get(cut);
    const after = atColumn.get(cut + 1);
    if (!before || !after || before.length !== 1 || after.length !== 1) return false;
    return (
      (outDeg.get(before[0]!) ?? 0) === 1 &&
      (inDeg.get(after[0]!) ?? 0) === 1 &&
      edgeSet.has(`${before[0]}>${after[0]}`)
    );
  };
  const nextRowStart = new Array<number>(columns + 1).fill(columns);
  const rowStartIn = (root: number, column: number): number => {
    let start = root;
    while (nextRowStart[start]! <= column) start = nextRowStart[start]!;
    return start;
  };
  const admissible = (cut: number, rowStart: number): boolean => {
    if (!isChainLink(cut)) return false;
    for (const entry of edges) {
      const source = columnOf.get(entry.source);
      const target = columnOf.get(entry.target);
      if (source === undefined || target === undefined) continue;
      if (source < rowStart || source > cut) continue;
      if (target <= cut) continue;
      if (source === cut && target === cut + 1) continue;
      if (source - rowStart > target - rowStartIn(cut + 1, target)) return false;
    }
    return true;
  };
  for (let rowStart = columns - 1; rowStart >= 0; rowStart--) {
    let end = rowStart;
    for (let i = rowStart + 1; i < columns; i++) {
      if (columnX(i) - columnX(rowStart) + NODE_WIDTH > FOLD_MAX_ROW_WIDTH) break;
      end = i;
    }
    if (end === columns - 1) {
      nextRowStart[rowStart] = columns;
      continue;
    }
    let handover = -1;
    for (let cut = end; cut >= rowStart; cut--) {
      if (admissible(cut, rowStart)) {
        handover = cut + 1;
        break;
      }
    }
    if (handover < 0) {
      for (let cut = end + 1; cut < columns - 1; cut++) {
        if (admissible(cut, rowStart)) {
          handover = cut + 1;
          break;
        }
      }
    }
    nextRowStart[rowStart] = handover < 0 ? columns : handover;
  }
  const offenders: number[] = [];
  rows.forEach((row, rowIndex) => {
    const rowStart = Math.min(...row.map((entry) => columnOf.get(entry.id)!));
    const rowEnd = Math.max(...row.map((entry) => columnOf.get(entry.id)!));
    if (columnX(rowEnd) - columnX(rowStart) + NODE_WIDTH <= FOLD_MAX_ROW_WIDTH) return;
    for (let cut = rowStart; cut < rowEnd; cut++) {
      if (columnX(cut) - columnX(rowStart) + NODE_WIDTH > FOLD_MAX_ROW_WIDTH) break;
      if (admissible(cut, rowStart)) {
        offenders.push(rowIndex);
        return;
      }
    }
  });
  return offenders;
}

// The helper above is the oracle every fold assertion in this file rests on,
// so it needs a case it must reject. A ten-node chain left unfolded in one row
// is wide enough to need folding and holds five admissible breaks inside the
// width bound, so the rule could have broken it; a helper that returns no
// offender for it cannot fail, whatever the layout does.
describe("rowsWithAdmissibleBreakInside — it reads the rows the layout produced", () => {
  it("reports an unfolded chain that holds an admissible break inside the width bound", () => {
    const ids = Array.from({ length: 10 }, (_, index) => `c${index}`);
    const nodes = ids.map((id, index) => ({
      ...node(id),
      position: { x: 28 + 272 * index, y: 24 },
    }));
    const edges = ids.slice(1).map((id, index) => edge(ids[index]!, id));
    // 2658 px wide, and every one of c0|c1 .. c4|c5 is an admissible break
    // inside the bound: a plain chain link with no other edge crossing it.
    expect(rowsWithAdmissibleBreakInside(nodes, edges)).toEqual([0]);
  });
});

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
    // cycle, so this takes the fallback and the back edges pile onto the first
    // few columns. Row breaks later in the chain are chain links that no edge
    // would carry backwards once folded, so they are admissible and the fold
    // still breaks. Back edges crossing each in-row chain link instead would
    // leave a row with no admissible break: it would stay whole, no continuation
    // would be marked, and the checks below would run zero times.
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
    // crosses every column boundary, but after folding its target lands far to
    // the right of its source, so it leaves each boundary pointing forward and
    // every chain link inside the width bound is admissible. The row breaks at
    // its width bound and the chord stays an ordinary forward edge, never a row
    // boundary.
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

  it("breaks at the last admissible boundary inside the width bound", () => {
    // A ten-node chain with a chord from c3 to c9. The boundary the row first
    // overflows at (c4|c5) is a chain link, and the chord leaves it pointing
    // forward once folded (c3 sits four columns into the next row, c9 five), so
    // that boundary is admissible and is the last one inside the width bound.
    // The fill takes it rather than an earlier boundary, and the chord is left
    // an ordinary forward edge.
    const ids = Array.from({ length: 10 }, (_, index) => `c${index}`);
    const nodes = ids.map(node);
    const edges = ids.slice(1).map((id, index) => edge(ids[index], id));
    edges.push(edge("c3", "c9"));
    const result = getLayoutedElements(nodes, edges, "LR");
    expect(foldRowIds(result.nodes)).toEqual([
      ["c0", "c1", "c2", "c3", "c4"],
      ["c5", "c6", "c7", "c8", "c9"],
    ]);
    expect(result.edges.filter((entry) => entry.data?.continuation).map((entry) => entry.id)).toEqual(
      ["c4-c5"],
    );
    const chord = result.edges.find((entry) => entry.id === "c3-c9")!;
    expect(chord.data?.continuation).toBeUndefined();
  });

  it("folds a chained graph with a short chord without any row crossing the bound", () => {
    // c3->c6 crosses the boundary the row first overflows at, c4|c5. That
    // boundary is NOT admissible: folding there would put c6 one column into
    // the row beginning at c5 while its source c3 sits three columns into the
    // row above, so the chord would be drawn running backwards. The fill
    // therefore takes the last admissible boundary inside the bound instead,
    // c2|c3 — the chord stays inside the row that boundary starts, so it never
    // crosses the break — and every resulting row fits the bound:
    // [c0 c1 c2] [c3..c7] [c8 c9].
    const ids = Array.from({ length: 10 }, (_, index) => `c${index}`);
    const nodes = ids.map(node);
    const edges = ids.slice(1).map((id, index) => edge(ids[index], id));
    edges.push(edge("c3", "c6"));
    const result = getLayoutedElements(nodes, edges, "LR");
    // The break the row reaches its bound at (c4|c5) is the boundary the chord
    // would fold backwards, so the row does not take it: it breaks at c2|c3 and
    // keeps the chord inside the next row.
    expect(foldRowIds(result.nodes)).toEqual([
      ["c0", "c1", "c2"],
      ["c3", "c4", "c5", "c6", "c7"],
      ["c8", "c9"],
    ]);
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

describe("fold breaks only where the rule admits one", () => {
  // Every input the fold rule must hold on: a chain, plus the chords, parallel
  // edges and self-loops that used to fold a real dependency backwards (A) and
  // the staggered chords, self-loops and back-edge runs that used to leave a
  // row unfolded (B). Each case names the rows it produces, the only edges the
  // fold may mark (the chain link at each break actually taken), and which rows
  // exceed FOLD_MAX_ROW_WIDTH. An over-width row is one the rule leaves whole:
  // it holds no admissible break, so no boundary inside it could be taken
  // without folding one of its own crossing edges backwards.
  const chainIds = (length: number) => Array.from({ length }, (_, index) => `c${index}`);
  const caseEdges = (ids: string[], chords: [string, string][] = [], parallel: [string, string][] = []) => [
    ...ids.slice(1).map((id, index) => edge(ids[index], id)),
    ...chords.map(([source, target]) => edge(source, target)),
    ...parallel.map(([source, target]) => ({ ...edge(source, target), id: `${source}-${target}#2` })),
  ];
  // A backwardChain: a plain chain plus `backEdges` edges from the last node
  // back to the first `backEdges` nodes, the cyclic fallback the fold runs on.
  const backwardChainEdges = (length: number, backEdges: number) => {
    const ids = chainIds(length);
    const edges = ids.slice(1).map((id, index) => edge(ids[index], id));
    for (let index = 0; index < backEdges; index++) edges.push(edge(`c${length - 1}`, `c${index}`));
    return { ids, edges };
  };

  const chainCases: {
    name: string;
    ids: string[];
    chords?: [string, string][];
    parallel?: [string, string][];
    rows: string[][];
    marked: string[];
    over: number[];
  }[] = [
    {
      name: "nested chords",
      ids: chainIds(10),
      chords: [["c0", "c9"], ["c3", "c6"]],
      rows: [["c0", "c1", "c2"], ["c3", "c4", "c5", "c6", "c7"], ["c8", "c9"]],
      marked: ["c2-c3", "c7-c8"],
      over: [],
    },
    {
      name: "interleaved chords",
      ids: chainIds(10),
      chords: [["c0", "c6"], ["c3", "c7"]],
      rows: [["c0", "c1", "c2"], ["c3", "c4", "c5", "c6", "c7"], ["c8", "c9"]],
      marked: ["c2-c3", "c7-c8"],
      over: [],
    },
    {
      name: "adjacent chords",
      ids: chainIds(10),
      chords: [["c0", "c2"], ["c2", "c6"]],
      rows: [["c0", "c1", "c2", "c3"], ["c4", "c5", "c6", "c7", "c8"], ["c9"]],
      marked: ["c3-c4", "c8-c9"],
      over: [],
    },
    {
      name: "adjacent chords that only break past the bound",
      ids: chainIds(10),
      chords: [["c0", "c2"], ["c2", "c5"]],
      rows: [["c0", "c1", "c2", "c3", "c4", "c5"], ["c6", "c7", "c8", "c9"]],
      marked: ["c5-c6"],
      // Row 0 holds no admissible break inside the bound: every chain link up
      // to c4|c5 is crossed by the c2->c5 chord, which would fold backwards
      // there. The row reaches its first admissible break past the bound.
      over: [0],
    },
    {
      name: "self-loop and chord",
      ids: chainIds(10),
      chords: [["c1", "c1"], ["c2", "c6"]],
      rows: [["c0", "c1", "c2", "c3"], ["c4", "c5", "c6", "c7", "c8"], ["c9"]],
      marked: ["c3-c4", "c8-c9"],
      over: [],
    },
    {
      name: "staggered chords",
      ids: chainIds(10),
      chords: [["c0", "c2"], ["c2", "c4"], ["c4", "c6"], ["c6", "c8"]],
      rows: [["c0", "c1", "c2", "c3", "c4", "c5", "c6", "c7", "c8"], ["c9"]],
      marked: ["c8-c9"],
      // The whole span is crossed by the staggered chords, so no boundary
      // inside it is admissible: the row stays whole to its last column.
      over: [0],
    },
    {
      name: "parallel edges",
      ids: chainIds(10),
      parallel: [["c3", "c4"]],
      rows: [["c0", "c1", "c2", "c3", "c4"], ["c5", "c6", "c7", "c8", "c9"]],
      marked: ["c4-c5"],
      over: [],
    },
    {
      name: "self-loops",
      ids: chainIds(10),
      chords: [["c1", "c1"], ["c3", "c3"], ["c5", "c5"]],
      rows: [["c0", "c1", "c2", "c3", "c4", "c5", "c6"], ["c7", "c8", "c9"]],
      marked: ["c6-c7"],
      over: [0],
    },
    {
      name: "three trailing chords",
      ids: chainIds(13),
      chords: [["c6", "c8"], ["c8", "c10"], ["c10", "c12"]],
      rows: [["c0", "c1", "c2", "c3", "c4", "c5"], ["c6", "c7", "c8", "c9", "c10", "c11", "c12"]],
      marked: ["c5-c6"],
      // Row 0 folds at its bound; the tail from c6 is crossed by all three
      // chords, so it holds no admissible break and stays whole.
      over: [1],
    },
    {
      name: "sibling branch",
      ids: [...chainIds(10), "side"],
      chords: [["c2", "side"], ["side", "c6"], ["c0", "c9"]],
      // `side` follows c9 on the chain, so c2 branches out and rejoins at c6
      // while c0->c9 spans the row. Every chain link inside is crossed by a
      // chord, so no boundary is admissible and the row stays whole.
      rows: [["c0", "c1", "c7", "c2", "c8", "c3", "c9", "c4", "side", "c5", "c6"]],
      marked: [],
      over: [0],
    },
    {
      name: "break at the row's first column",
      ids: chainIds(10),
      chords: [["c1", "c3"], ["c3", "c5"], ["c5", "c7"]],
      // The head of the chain is cut off on its own: c0|c1 is a chain link and
      // no chord crosses it, so it is admissible and is the last admissible
      // boundary inside the bound. That break is at the row's first column, so
      // both ends of the c0->c1 link land in the same column and the link runs
      // straight down: it is not marked, because nothing turned it around.
      // Every boundary inside the bound in the row that break starts is crossed
      // by one of the three chords, so none of them is admissible and the row
      // stays whole; the next break, c7|c8, is past the bound and is marked.
      rows: [["c0"], ["c1", "c2", "c3", "c4", "c5", "c6", "c7"], ["c8", "c9"]],
      marked: ["c7-c8"],
      over: [1],
    },
    {
      name: "chord across two breaks",
      ids: chainIds(16),
      chords: [["c4", "c13"]],
      // c4->c13 leaves its own row, crosses the break c9|c10, and lands three
      // columns into the row below that. Deciding whether it runs backwards
      // there needs the row its target lands in, which is not the row the
      // break hands over to but the one after it: c13 sits at column 3 of row
      // 2 against c4's column 0, so the chord still reads forward and both
      // breaks stay admissible.
      rows: [
        ["c0", "c1", "c2", "c3"],
        ["c4", "c5", "c6", "c7", "c8", "c9"],
        ["c10", "c11", "c12", "c13", "c14", "c15"],
      ],
      marked: ["c3-c4", "c9-c10"],
      over: [],
    },
    {
      name: "one spanning chord",
      ids: chainIds(39),
      chords: [["c0", "c38"]],
      rows: [
        ["c0", "c1", "c2", "c3", "c4", "c5"],
        ["c6", "c7", "c8", "c9", "c10", "c11"],
        ["c12", "c13", "c14", "c15", "c16", "c17"],
        ["c18", "c19", "c20", "c21", "c22", "c23"],
        ["c24", "c25", "c26", "c27", "c28", "c29"],
        ["c30", "c31", "c32", "c33", "c34", "c35"],
        ["c36", "c37", "c38"],
      ],
      marked: ["c5-c6", "c11-c12", "c17-c18", "c23-c24", "c29-c30", "c35-c36"],
      over: [],
    },
  ];

  it.each(chainCases)(
    "keeps every fold forward and marks only taken chain links: $name",
    ({ ids, chords, parallel, rows, marked, over }) => {
      const result = getLayoutedElements(
        ids.map(node),
        caseEdges(ids, chords, parallel),
        "LR",
      );
      const widths = assertFoldInvariants(result.nodes, result.edges);
      expect(foldRowIds(result.nodes)).toEqual(rows);
      expect(
        result.edges.filter((entry) => entry.data?.continuation).map((entry) => entry.id).sort(),
      ).toEqual([...marked].sort());
      expect(widths.map((width, index) => (width > FOLD_MAX_ROW_WIDTH ? index : -1)).filter((i) => i >= 0)).toEqual(
        over,
      );
      // Every row that exceeds the bound holds no admissible break within it;
      // a row that fits was broken at its last admissible break inside the bound.
      expect(rowsWithAdmissibleBreakInside(result.nodes, result.edges)).toEqual([]);
      assertNoOverlap(result.nodes);
    },
  );

  it.each([
    {
      name: "40-node chain with 37 back edges",
      length: 40,
      backEdges: 37,
      // The back edges pile onto the first columns, so the head of the chain
      // is crossed by all of them: no boundary there is admissible and the head
      // stays whole. The tail has admissible chain links, so it folds into a
      // second row and exactly one link is marked.
      rows: [chainIds(37).slice(1), ["c37", "c38", "c39", "c0"]],
      whole: [0],
      marked: ["c36-c37"],
    },
    {
      name: "46-node chain with 28 back edges",
      length: 46,
      backEdges: 28,
      rows: [
        chainIds(28).slice(1),
        chainIds(34).slice(28),
        chainIds(40).slice(34),
        chainIds(45).slice(40),
        ["c45", "c0"],
      ],
      whole: [0],
      marked: ["c27-c28", "c33-c34", "c39-c40", "c44-c45"],
    },
  ])(
    "folds a cyclic fallback and marks one chain link per break: $name",
    ({ length, backEdges, rows, whole, marked }) => {
      const { ids, edges } = backwardChainEdges(length, backEdges);
      const result = getLayoutedElements(ids.map(node), edges, "LR");
      expect(foldRowIds(result.nodes)).toEqual(rows);
      const continuations = result.edges.filter((entry) => entry.data?.continuation);
      expect(continuations.map((entry) => entry.id).sort()).toEqual([...marked].sort());
      // One chain link per break taken, and every marked edge is the single
      // link the fold turned around (assertFoldInvariants repeats the degree
      // check over the drawn edge records).
      const widths = assertFoldInvariants(result.nodes, result.edges);
      expect(widths.map((width, index) => (width > FOLD_MAX_ROW_WIDTH ? index : -1)).filter((i) => i >= 0)).toEqual(
        whole,
      );
      expect(rowsWithAdmissibleBreakInside(result.nodes, result.edges)).toEqual([]);
      for (const entry of continuations) {
        expect(result.ranks.get(entry.target)!).toBeGreaterThan(result.ranks.get(entry.source)!);
      }
      assertNoOverlap(result.nodes);
    },
  );

  it("leaves a row with no admissible break whole rather than folding a chord backwards", () => {
    // Staggered chords c0->c2, c2->c4, c4->c6, c6->c8 cross every boundary in
    // the span, so no boundary is admissible: folding at any of them would draw
    // one of the chords as a row boundary running backwards. The row stays
    // whole to its last column instead, and only the final chain link c8|c9 —
    // which no chord crosses — is marked.
    const ids = chainIds(10);
    const edges = caseEdges(ids, [["c0", "c2"], ["c2", "c4"], ["c4", "c6"], ["c6", "c8"]]);
    const result = getLayoutedElements(ids.map(node), edges, "LR");
    expect(foldRowIds(result.nodes)).toEqual([["c0", "c1", "c2", "c3", "c4", "c5", "c6", "c7", "c8"], ["c9"]]);
    expect(result.edges.filter((entry) => entry.data?.continuation).map((entry) => entry.id)).toEqual([
      "c8-c9",
    ]);
    const widths = assertFoldInvariants(result.nodes, result.edges);
    expect(widths[0]!).toBeGreaterThan(FOLD_MAX_ROW_WIDTH);
    expect(widths[1]!).toBeLessThanOrEqual(FOLD_MAX_ROW_WIDTH);
  });
});
