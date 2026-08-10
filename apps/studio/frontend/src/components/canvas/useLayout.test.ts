/**
 * StepNode is not a fixed-height box: it grows a row at a time as a run fills
 * in the role, the assignment, and the duration/calls line. dagre is told a
 * height up front, so if that height is a constant it is right for one moment
 * of a node's life and wrong afterwards, and the nodes crowd.
 *
 * getLayoutedElements is exercised through dagre here rather than mocked, so
 * these assert the property that matters (laid-out boxes do not overlap)
 * instead of the arithmetic that happens to produce it.
 */
import { describe, it, expect } from "vitest";
import type { Node, Edge } from "reactflow";
import {
  computeNodeDepths,
  enforceMinRankGap,
  estimateNodeHeight,
  getLayoutedElements,
  maxGraphDepth,
  NODE_HEIGHT,
  rankSepForDepth,
  wrapWideRanks,
  foldWideGraph,
  markContinuationEdges,
  DAG_MAX_ZOOM,
  DAG_FIT_PADDING,
  FIT_ZOOM_FLOOR,
  computeReservedHeight,
} from "./useLayout";
import { transitiveReduceDisplay } from "@/lib/operationGraph";
import { fitZoomFor, MIN_INTERACTIVE_ZOOM } from "./WorkerCanvas";

const bare = (id: string): Node => ({
  id,
  position: { x: 0, y: 0 },
  data: { label: id },
});

const full = (id: string): Node => ({
  id,
  position: { x: 0, y: 0 },
  data: {
    label: id,
    role: "investigator",
    assignment: "codex/gpt-5.6-terra",
    durationSeconds: 147.5,
    toolCallCount: 20,
  },
});

// Deep-chain-with-global-fan-in fixture, modeled on the reported hairball
// shape: several independent roots, one long implementer/tester chain
// hanging off the first root, every node (roots, chain, and a semi-detached
// operator) also feeding one terminal critic, and a node with just a single
// edge in a sea of well-connected ones. 13 nodes / 23 edges. Shared at module
// scope so both the pure-layout fixture and the reduction+layout composition
// fixture (RunDetail's actual "reduce, then lay out" flow) exercise the
// identical shape.
const deepChainRoots = ["r1", "r2", "r3", "r4", "r5", "r6"];
const deepChainChain = ["i1", "i2", "i3", "t1", "t2"];
const deepChainNodeIds = [...deepChainRoots, ...deepChainChain, "critic", "op"];
const deepChainEdges: Edge[] = [
  // Every root feeds the chain's head, not just r1 — a root with only a
  // single, distant edge (to critic) gives dagre's ranker no reason to place
  // it early, and it drifts toward the far rank instead of rank 0.
  ...deepChainRoots.map((r) => ({ id: `${r}-i1`, source: r, target: "i1" })),
  { id: "i1-i2", source: "i1", target: "i2" },
  { id: "i2-i3", source: "i2", target: "i3" },
  { id: "i3-t1", source: "i3", target: "t1" },
  { id: "t1-t2", source: "t1", target: "t2" },
  { id: "r1-op", source: "r1", target: "op" },
  ...deepChainNodeIds
    .filter((id) => id !== "critic")
    .map((id) => ({ id: `${id}-critic`, source: id, target: "critic" })),
];
const deepChainFixture = () => deepChainNodeIds.map(full);

function rectOf(n: Node) {
  return {
    left: n.position.x,
    right: n.position.x + 210,
    top: n.position.y,
    bottom: n.position.y + estimateNodeHeight(n),
  };
}

function assertNoOverlap(nodes: Node[]) {
  for (let i = 0; i < nodes.length; i++) {
    for (let j = i + 1; j < nodes.length; j++) {
      const a = rectOf(nodes[i]);
      const b = rectOf(nodes[j]);
      const overlaps = a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;
      expect(overlaps, `${nodes[i].id} overlaps ${nodes[j].id}`).toBe(false);
    }
  }
}

describe("estimateNodeHeight", () => {
  // The card is a fixed-height box, so its height cannot depend on how far
  // along a run is. This is the invariant the whole layout leans on: two nodes
  // side by side are the same size whether or not either has finished, which is
  // what lets a rank line up. It used to grow a row at a time, and these tests
  // used to assert that growth.
  it("is the same for every node whatever data it carries", () => {
    const roleOnly = { ...bare("a"), data: { label: "a", role: "critic" } };
    const roleAndModel = {
      ...bare("a"),
      data: { label: "a", role: "critic", assignment: "some-model" },
    };
    const zeroed = { ...bare("a"), data: { label: "a", errorCount: 0, toolCallCount: 0 } };
    const heights = [bare("a"), full("a"), roleOnly, roleAndModel, zeroed].map(estimateNodeHeight);
    expect(new Set(heights).size).toBe(1);
  });

  it("matches the card's own declared height, so layout and component cannot drift", () => {
    expect(estimateNodeHeight(full("a"))).toBe(NODE_HEIGHT);
  });

  it("survives a node with no data at all", () => {
    const noData = { id: "a", position: { x: 0, y: 0 } } as unknown as Node;
    expect(() => estimateNodeHeight(noData)).not.toThrow();
    expect(estimateNodeHeight(noData)).toBe(NODE_HEIGHT);
  });
});

describe("getLayoutedElements — populated nodes do not overlap", () => {
  // Siblings off one parent share a dagre rank, so they are stacked along the
  // cross axis. That is exactly where an under-reserved height shows up.
  const parent = "root";
  const siblings = ["s1", "s2", "s3", "s4", "s5"];
  const edges: Edge[] = siblings.map((s) => ({ id: `${parent}-${s}`, source: parent, target: s }));

  function verticalGaps(nodes: Node[]): number[] {
    const laid = siblings
      .map((id) => nodes.find((n) => n.id === id))
      .filter((n): n is Node => Boolean(n))
      .sort((a, b) => a.position.y - b.position.y);
    const gaps: number[] = [];
    for (let i = 1; i < laid.length; i++) {
      const prev = laid[i - 1];
      const cur = laid[i];
      gaps.push(cur.position.y - (prev.position.y + estimateNodeHeight(prev)));
    }
    return gaps;
  }

  it("leaves a positive gap between fully populated siblings", () => {
    const input = [full(parent), ...siblings.map(full)];
    const { nodes } = getLayoutedElements(input, edges, "LR");
    for (const gap of verticalGaps(nodes)) {
      expect(gap).toBeGreaterThan(0);
    }
  });

  it("leaves a positive gap between bare siblings too", () => {
    const input = [bare(parent), ...siblings.map(bare)];
    const { nodes } = getLayoutedElements(input, edges, "LR");
    for (const gap of verticalGaps(nodes)) {
      expect(gap).toBeGreaterThan(0);
    }
  });

  it("spaces siblings identically whether or not they carry data", () => {
    // The inverse of what this used to assert, and the reason ranks line up: a
    // half-finished run and a finished one lay out to the same shape, so the
    // graph does not reflow under the reader as results arrive.
    const populated = getLayoutedElements([full(parent), ...siblings.map(full)], edges, "LR");
    const plain = getLayoutedElements([bare(parent), ...siblings.map(bare)], edges, "LR");
    const span = (nodes: Node[]) => {
      const ys = siblings.map((id) => nodes.find((n) => n.id === id)!.position.y);
      return Math.max(...ys) - Math.min(...ys);
    };
    expect(span(populated.nodes)).toBe(span(plain.nodes));
  });

  it("keeps every node it was given", () => {
    const input = [full(parent), ...siblings.map(full)];
    const { nodes } = getLayoutedElements(input, edges, "LR");
    expect(nodes.map((n) => n.id).sort()).toEqual([parent, ...siblings].sort());
  });
});

describe("getLayoutedElements — a wide fan-out wraps instead of becoming a strip", () => {
  // The shape from live fleet runs: one orchestrator fanning dozens of
  // workers, all of which feed one sink. dagre puts every worker in one rank,
  // stacked into a single cross-axis column ~4000px tall, which fitView can
  // only show as an unreadable sliver.
  const workers = Array.from({ length: 24 }, (_, i) => `w${i + 1}`);
  const fanEdges: Edge[] = [
    ...workers.map((w) => ({ id: `root-${w}`, source: "root", target: w })),
    ...workers.map((w) => ({ id: `${w}-sink`, source: w, target: "sink" })),
  ];
  const fanNodes = () => [full("root"), ...workers.map(full), full("sink")];

  function rect(n: Node) {
    return {
      left: n.position.x,
      right: n.position.x + 210,
      top: n.position.y,
      bottom: n.position.y + estimateNodeHeight(n),
    };
  }

  it("splits an over-tall rank into several columns", () => {
    const { nodes } = getLayoutedElements(fanNodes(), fanEdges, "LR");
    const xs = new Set(workers.map((id) => nodes.find((n) => n.id === id)!.position.x));
    expect(xs.size).toBeGreaterThan(1);
  });

  it("keeps the wrapped block far shorter than the unwrapped strip", () => {
    const { nodes } = getLayoutedElements(fanNodes(), fanEdges, "LR");
    const ys = workers.map((id) => nodes.find((n) => n.id === id)!.position.y);
    const height = Math.max(...ys) - Math.min(...ys);
    // Unwrapped, 24 populated nodes stack to ~24 × (height + gap) ≈ 3200px.
    expect(height).toBeLessThan(1400);
  });

  it("never overlaps any two nodes, wrapped columns included", () => {
    const { nodes } = getLayoutedElements(fanNodes(), fanEdges, "LR");
    for (let i = 0; i < nodes.length; i++) {
      for (let j = i + 1; j < nodes.length; j++) {
        const a = rect(nodes[i]);
        const b = rect(nodes[j]);
        const overlaps =
          a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;
        expect(overlaps, `${nodes[i].id} overlaps ${nodes[j].id}`).toBe(false);
      }
    }
  });

  it("keeps the downstream rank to the right of every wrapped column", () => {
    const { nodes } = getLayoutedElements(fanNodes(), fanEdges, "LR");
    const sinkX = nodes.find((n) => n.id === "sink")!.position.x;
    for (const id of workers) {
      expect(sinkX).toBeGreaterThan(nodes.find((n) => n.id === id)!.position.x);
    }
  });

  it("leaves a small rank in the single column dagre chose", () => {
    // The existing five-sibling suite above pins the same thing; this arm
    // pins the threshold from the wrap side so lowering it to 1 fails here.
    const few = ["a", "b", "c", "d"];
    const edges: Edge[] = few.map((s) => ({ id: `root-${s}`, source: "root", target: s }));
    const { nodes } = getLayoutedElements([full("root"), ...few.map(full)], edges, "LR");
    const xs = new Set(few.map((id) => nodes.find((n) => n.id === id)!.position.x));
    expect(xs.size).toBe(1);
  });

  it("keeps every node through the wrap", () => {
    const { nodes } = getLayoutedElements(fanNodes(), fanEdges, "LR");
    expect(nodes.map((n) => n.id).sort()).toEqual(["root", ...workers, "sink"].sort());
  });

  it("reports a bounding-box height that tracks the wrapped grid, not the strip", () => {
    const { nodes, height } = getLayoutedElements(fanNodes(), fanEdges, "LR");
    const top = Math.min(...nodes.map((n) => n.position.y));
    const bottom = Math.max(...nodes.map((n) => n.position.y + estimateNodeHeight(n)));
    expect(height).toBeGreaterThanOrEqual(bottom - top);
    expect(height).toBeLessThan(bottom - top + 100);
  });

  it("reports zero height for an empty graph", () => {
    expect(getLayoutedElements([], [], "LR").height).toBe(0);
  });
});

describe("wrapWideRanks — the grid preserves dagre's cross-axis order", () => {
  // A wrapped rank read column-major (columns left to right, each top to
  // bottom) must reproduce the order dagre stacked the strip in. The
  // gap-based arms above sort by y before asserting, so they stay green even
  // if the wrap inverted rows or shuffled siblings — this arm reads the grid
  // in its own order and compares against the input order directly.
  function stripRank(count: number): Node[] {
    return Array.from({ length: count }, (_, i) => ({
      id: `w${i + 1}`,
      position: { x: 300, y: i * 120 },
      data: { label: `w${i + 1}` },
    }));
  }

  function columnMajorIds(nodes: Node[]): string[] {
    const byX = new Map<number, Node[]>();
    for (const n of nodes) {
      const key = Math.round(n.position.x);
      const col = byX.get(key);
      if (col) col.push(n);
      else byX.set(key, [n]);
    }
    return [...byX.entries()]
      .sort(([a], [b]) => a - b)
      .flatMap(([, col]) => [...col].sort((a, b) => a.position.y - b.position.y))
      .map((n) => n.id);
  }

  it("column-major reading of the wrapped grid equals the strip order", () => {
    const wrapped = wrapWideRanks(stripRank(24));
    expect(columnMajorIds(wrapped)).toEqual(Array.from({ length: 24 }, (_, i) => `w${i + 1}`));
  });

  it("holds when the count does not divide evenly into the grid", () => {
    const wrapped = wrapWideRanks(stripRank(23));
    expect(columnMajorIds(wrapped)).toEqual(Array.from({ length: 23 }, (_, i) => `w${i + 1}`));
  });

  it("leaves a rank at the wrap threshold untouched", () => {
    const strip = stripRank(7);
    const wrapped = wrapWideRanks(strip);
    expect(wrapped.map((n) => ({ id: n.id, ...n.position }))).toEqual(
      strip.map((n) => ({ id: n.id, ...n.position })),
    );
  });
});

describe("rankSepForDepth — deep chains taper, shallow graphs are untouched", () => {
  it("is the identity 90 at and below the shallow-depth threshold", () => {
    // The wide fan-out fixture is depth 3 — this is the exact property that
    // keeps its layout, and its unmodified tests, bit-for-bit unchanged.
    expect(rankSepForDepth(0)).toBe(90);
    expect(rankSepForDepth(1)).toBe(90);
    expect(rankSepForDepth(3)).toBe(90);
    expect(rankSepForDepth(5)).toBe(90);
  });

  it("tapers monotonically once depth exceeds the threshold", () => {
    const six = rankSepForDepth(6);
    const ten = rankSepForDepth(10);
    expect(six).toBeLessThan(90);
    expect(ten).toBeLessThan(six);
  });

  it("never drops below the floor, however deep the graph", () => {
    expect(rankSepForDepth(11)).toBe(48);
    expect(rankSepForDepth(50)).toBe(48);
  });

  it("honors custom ranksep/floor arguments", () => {
    expect(rankSepForDepth(3, 120, 60)).toBe(120);
    expect(rankSepForDepth(100, 120, 60)).toBe(60);
  });
});

describe("computeNodeDepths / maxGraphDepth — longest-path rank index", () => {
  it("gives every root depth 0", () => {
    const nodes: Node[] = [bare("a"), bare("b")];
    const depths = computeNodeDepths(nodes, []);
    expect(depths.get("a")).toBe(0);
    expect(depths.get("b")).toBe(0);
  });

  it("takes the longest incoming path, not the first", () => {
    // c is reachable both directly from a (length 1) and via b (length 2) —
    // its rank must be the longer path's, matching dagre's own rank choice.
    const nodes: Node[] = [bare("a"), bare("b"), bare("c")];
    const edges: Edge[] = [
      { id: "a-b", source: "a", target: "b" },
      { id: "b-c", source: "b", target: "c" },
      { id: "a-c", source: "a", target: "c" },
    ];
    const depths = computeNodeDepths(nodes, edges);
    expect(depths.get("a")).toBe(0);
    expect(depths.get("b")).toBe(1);
    expect(depths.get("c")).toBe(2);
    expect(maxGraphDepth(nodes, edges)).toBe(2);
  });

  it("does not hang or throw on a cycle, and does not propagate the cycle's depth", () => {
    const nodes: Node[] = [bare("a"), bare("b"), bare("c")];
    const edges: Edge[] = [
      { id: "a-b", source: "a", target: "b" },
      { id: "b-c", source: "b", target: "c" },
      { id: "c-a", source: "c", target: "a" },
    ];
    expect(() => maxGraphDepth(nodes, edges)).not.toThrow();
    expect(Number.isFinite(maxGraphDepth(nodes, edges))).toBe(true);
  });

  it("ignores edges referencing a node outside the given set", () => {
    const nodes: Node[] = [bare("a")];
    const edges: Edge[] = [{ id: "ghost-a", source: "ghost", target: "a" }];
    expect(computeNodeDepths(nodes, edges).get("a")).toBe(0);
  });

  it("reports 0 for an empty graph", () => {
    expect(maxGraphDepth([], [])).toBe(0);
  });
});

describe("enforceMinRankGap — minimum vertical gap within a rank", () => {
  it("is a no-op when nodes are already spaced beyond the floor", () => {
    // Spaced off the node height rather than a literal: the floor is
    // height + gap, so a hardcoded y stops clearing it the moment the card
    // grows and this test starts asserting a push while still calling itself
    // a no-op.
    const clear = NODE_HEIGHT + 50;
    const nodes: Node[] = [
      { ...bare("a"), position: { x: 0, y: 0 } },
      { ...bare("b"), position: { x: 0, y: clear } },
    ];
    const out = enforceMinRankGap(nodes, 6);
    expect(out.find((n) => n.id === "a")!.position.y).toBe(0);
    expect(out.find((n) => n.id === "b")!.position.y).toBe(clear);
  });

  it("pushes an overlapping node down until the gap floor is met", () => {
    // Every node measures NODE_HEIGHT regardless of its own data (see
    // estimateNodeHeight), so b at y=10 overlaps a by all but 10px of it.
    const nodes: Node[] = [
      { ...bare("a"), position: { x: 0, y: 0 } },
      { ...bare("b"), position: { x: 0, y: 10 } },
    ];
    const out = enforceMinRankGap(nodes, 6);
    const a = out.find((n) => n.id === "a")!;
    const b = out.find((n) => n.id === "b")!;
    expect(b.position.y).toBeGreaterThanOrEqual(a.position.y + estimateNodeHeight(a) + 6);
  });

  it("treats different (rounded) x as separate ranks — no cross-rank shifting", () => {
    const nodes: Node[] = [
      { ...bare("a"), position: { x: 0, y: 0 } },
      { ...bare("b"), position: { x: 300, y: 0 } }, // same y, different rank
    ];
    const out = enforceMinRankGap(nodes, 6);
    expect(out.find((n) => n.id === "b")!.position.y).toBe(0);
  });

  it("keeps every node through the pass", () => {
    const nodes: Node[] = [bare("a"), bare("b"), bare("c")];
    expect(
      enforceMinRankGap(nodes)
        .map((n) => n.id)
        .sort(),
    ).toEqual(["a", "b", "c"]);
  });
});

describe("getLayoutedElements — exposes a rank map alongside the layout", () => {
  it("returns a rank for every node, matching computeNodeDepths", () => {
    const nodes: Node[] = [full("a"), full("b"), full("c")];
    const edges: Edge[] = [
      { id: "a-b", source: "a", target: "b" },
      { id: "b-c", source: "b", target: "c" },
    ];
    const { ranks } = getLayoutedElements(nodes, edges, "LR");
    expect(ranks.get("a")).toBe(0);
    expect(ranks.get("b")).toBe(1);
    expect(ranks.get("c")).toBe(2);
  });

  it("reports an empty rank map for an empty graph", () => {
    const { ranks } = getLayoutedElements([], [], "LR");
    expect(ranks.size).toBe(0);
  });
});

describe("getLayoutedElements — deep chain with global fan-in (readability fixture)", () => {
  // Modeled on the reported hairball shape: several independent roots, one
  // long implementer/tester chain hanging off the first root, every node
  // (roots, chain, and a semi-detached operator) also feeding one terminal
  // critic, and a node with just a single edge in a sea of well-connected
  // ones. 13 nodes / 23 edges (12 direct root/chain/op -> critic fan-in edges
  // are the ones a display-time transitive reduction — RunDetail's concern,
  // not this layout's — would later collapse against the chain; this layout
  // test asserts the shape it's actually handed lays out without overlap).
  // See "reduced-by-default (RunDetail's actual composition)" below for the
  // reduction+layout composition RunDetail really renders.

  it("has the modeled shape: depth 6, critic as the deepest (fan-in) node", () => {
    const depths = computeNodeDepths(deepChainFixture(), deepChainEdges);
    for (const r of deepChainRoots) expect(depths.get(r)).toBe(0);
    expect(depths.get("i1")).toBe(1);
    expect(depths.get("i2")).toBe(2);
    expect(depths.get("i3")).toBe(3);
    expect(depths.get("t1")).toBe(4);
    expect(depths.get("t2")).toBe(5);
    expect(depths.get("op")).toBe(1);
    expect(depths.get("critic")).toBe(6);
    expect(maxGraphDepth(deepChainFixture(), deepChainEdges)).toBe(6);
  });

  it("tapers ranksep below the shallow-graph identity for this depth", () => {
    expect(rankSepForDepth(6)).toBeLessThan(90);
  });

  it("never overlaps any two nodes", () => {
    const { nodes } = getLayoutedElements(deepChainFixture(), deepChainEdges, "LR");
    assertNoOverlap(nodes);
  });

  it("keeps every node through layout", () => {
    const { nodes } = getLayoutedElements(deepChainFixture(), deepChainEdges, "LR");
    expect(nodes.map((n) => n.id).sort()).toEqual([...deepChainNodeIds].sort());
  });

  it("lays out wider than tall — a chain reads left-to-right, not top-to-bottom", () => {
    const { nodes } = getLayoutedElements(deepChainFixture(), deepChainEdges, "LR");
    const xs = nodes.map((n) => n.position.x);
    const ys = nodes.map((n) => n.position.y + estimateNodeHeight(n));
    const width = Math.max(...xs) - Math.min(...xs) + 210;
    const height = Math.max(...ys) - Math.min(...nodes.map((n) => n.position.y));
    expect(width).toBeGreaterThan(height);
  });

  it("reports a bounding-box height that covers every node", () => {
    const { nodes, height } = getLayoutedElements(deepChainFixture(), deepChainEdges, "LR");
    const top = Math.min(...nodes.map((n) => n.position.y));
    const bottom = Math.max(...nodes.map((n) => n.position.y + estimateNodeHeight(n)));
    expect(height).toBeGreaterThanOrEqual(bottom - top);
  });

  it("the readability floor engages for this graph at a realistic panel size", () => {
    // A 13-node graph where every node also directly edges a terminal sink
    // is wider than a compact embed can show at full legibility — dagre
    // reserves routing room in every intervening rank for those long edges.
    // This is exactly the case the fitViewOptions floor exists for (see
    // WorkerCanvas.test.ts): below this raw number the canvas overflows the
    // panel and pans instead of shrinking illegibly further, and zooming out
    // past the floor stays available for seeing the whole shape. The floor is
    // a clamp on the fit we choose, not a layout guarantee —
    // depth-scaled ranksep and no-overlap (asserted above) are what layout
    // actually owns.
    const { nodes, height } = getLayoutedElements(deepChainFixture(), deepChainEdges, "LR");
    const left = Math.min(...nodes.map((n) => n.position.x));
    const right = Math.max(...nodes.map((n) => n.position.x + 210));
    const width = right - left;
    const padding = 0.15;
    const viewport = { width: 1280, height: 560 };
    const rawFitZoom = Math.min(
      viewport.width / (width * (1 + 2 * padding)),
      viewport.height / (height * (1 + 2 * padding)),
      1,
    );
    expect(rawFitZoom).toBeLessThan(0.65);
  });
});

describe("getLayoutedElements — deep-chain fixture reduced-by-default (RunDetail's actual composition)", () => {
  // RunDetail never lays out the raw resolved edge set — it runs
  // transitiveReduceDisplay first (RunDetail.tsx's computeDisplayEdges) and
  // lays out the reduced result, same as WorkerCanvas does once RunDetail
  // hands it graph.edges (see WorkerCanvas.tsx's toFlowEdges/attachRankDistance,
  // which are agnostic to which caller — authored or runtime — supplied the
  // edges). This exercises that composition end to end on the fixture above:
  // every root/chain/op node also edges the terminal critic directly, on top
  // of a path that already reaches it through the chain — exactly the
  // "ancestor already covered by a longer surviving path" redundancy
  // transitive reduction exists to collapse.
  const { kept: reducedEdges, hidden: hiddenEdges } = transitiveReduceDisplay(deepChainEdges);

  it("collapses the direct-to-critic fan-in edges the chain already covers; keeps the chain and the two irreducible sink edges", () => {
    // Full: 6 root->i1 + 4 chain + 1 root->op + 12 fan-in-to-critic = 23.
    expect(deepChainEdges).toHaveLength(23);
    // Every node that ALSO reaches critic via a longer surviving path loses
    // its direct edge: the 6 roots (via i1) and i1/i2/i3/t1 (via the next
    // chain link). t2->critic and op->critic are each their source's ONLY
    // edge, so there is no alternative path and they survive.
    expect(reducedEdges).toHaveLength(13);
    expect(hiddenEdges).toHaveLength(10);
    expect(hiddenEdges.map((e) => e.id).sort()).toEqual(
      [
        "r1-critic",
        "r2-critic",
        "r3-critic",
        "r4-critic",
        "r5-critic",
        "r6-critic",
        "i1-critic",
        "i2-critic",
        "i3-critic",
        "t1-critic",
      ].sort(),
    );
    expect(reducedEdges.some((e) => e.id === "t2-critic")).toBe(true);
    expect(reducedEdges.some((e) => e.id === "op-critic")).toBe(true);
  });

  it("lays out the reduced set without overlap, keeping every node and the same wider-than-tall shape", () => {
    const { nodes, height } = getLayoutedElements(deepChainFixture(), reducedEdges, "LR");
    assertNoOverlap(nodes);
    expect(nodes.map((n) => n.id).sort()).toEqual([...deepChainNodeIds].sort());

    const left = Math.min(...nodes.map((n) => n.position.x));
    const right = Math.max(...nodes.map((n) => n.position.x + 210));
    const width = right - left;
    const top = Math.min(...nodes.map((n) => n.position.y));
    const bottom = Math.max(...nodes.map((n) => n.position.y + estimateNodeHeight(n)));
    expect(width).toBeGreaterThan(bottom - top);
    expect(height).toBeGreaterThanOrEqual(bottom - top);
    // Reduction drops edges, not nodes — the longest surviving path into
    // critic (through the chain) is untouched, so the rank structure (and
    // therefore the depth-scaled ranksep from Cause B) is identical.
    expect(maxGraphDepth(deepChainFixture(), reducedEdges)).toBe(6);
  });

  it("fit zoom for the reduced layout at a realistic panel size — the clamp still engages", () => {
    const { nodes, height } = getLayoutedElements(deepChainFixture(), reducedEdges, "LR");
    const left = Math.min(...nodes.map((n) => n.position.x));
    const right = Math.max(...nodes.map((n) => n.position.x + 210));
    const width = right - left;
    const viewport = { width: 1280, height: 560 };
    // minZoom=0: raw (unclamped) arithmetic — fitZoomFor's default minZoom is
    // now FIT_ZOOM_FLOOR (see the WorkerCanvas.tsx fix), which would mask
    // exactly the under-floor case this test demonstrates.
    const reducedZoom = fitZoomFor(width, height, viewport.width, viewport.height, 0.15, 1, 0);
    // Fewer edges make the picture legible (Cause A) and the taper (Cause B)
    // narrows the bounding box, but neither changes rank COUNT for this
    // fixture's shape, so the raw fit zoom at this compact a panel still
    // falls under the floor — same story as the unreduced fixture above.
    // The fitViewOptions floor is what actually guarantees legibility of the
    // opening view; the layout/reduction work minimizes how far under the
    // floor a real run lands, and how often the clamp needs to engage at all.
    expect(reducedZoom).toBeGreaterThan(0);
    expect(reducedZoom).toBeLessThan(FIT_ZOOM_FLOOR);
  });
});

// ── Acceptance fixtures: the exact 30-sibling / 18-node shapes ────────────────
//
// qa_checklist.md flags that the existing wide-fan-out coverage above uses 24
// workers (not the acceptance-specified 30) and the deep-chain coverage above
// uses 13 nodes (not 18), and that no fixture asserts reduced-vs-full edge
// counts or a bounding-box aspect together with overlap/zoom. This section
// adds the exact shapes without touching the existing 24-worker/13-node
// fixtures above (acceptance item 4 requires those stay green and unmodified).
//
// "Computed fit zoom >= floor": the raw arithmetic (fitZoomFor / the manual
// formula used above) legitimately lands BELOW FIT_ZOOM_FLOOR for both
// fixtures at a realistic compact-panel size (measured: ~0.557 for the wide
// fixture, ~0.386 for the deep chain), and the "reduced layout... fit zoom"
// test above pins the same fact. The floor is a clamp applied to the fit
// (fitViewOptions, covered by WorkerCanvas.test.ts's source-contract tests),
// not something raw layout arithmetic alone guarantees. Asserting the CLAMPED
// value clears the floor would be vacuous, so the tests assert the two things
// that can actually fail: the raw fit is under the floor (the trade is real),
// and the interactive zoom range still reaches it (the graph stays viewable).
// The clamped-value arithmetic below is kept because it documents the real
// installed ReactFlow formula — computed
// with the REAL installed ReactFlow formula (see the `fitZoomFor` defect
// block further below: production `fitZoomFor` uses `1 + 2*padding` where
// the installed `reactflow` package's `getViewportForBounds` uses
// `1 + padding`, and does not clamp to minZoom at all).
function realReactFlowFitZoom(
  width: number,
  height: number,
  viewport: { width: number; height: number },
  padding: number,
  minZoom: number,
  maxZoom: number,
): number {
  const xZoom = viewport.width / (width * (1 + padding));
  const yZoom = viewport.height / (height * (1 + padding));
  return Math.min(Math.max(Math.min(xZoom, yZoom), minZoom), maxZoom);
}

function boundingBox(nodes: Node[]) {
  const left = Math.min(...nodes.map((n) => n.position.x));
  const right = Math.max(...nodes.map((n) => n.position.x + 210));
  const top = Math.min(...nodes.map((n) => n.position.y));
  const bottom = Math.max(...nodes.map((n) => n.position.y + estimateNodeHeight(n)));
  return { width: right - left, height: bottom - top };
}

describe("getLayoutedElements — 30-sibling wide fan-out (acceptance fixture)", () => {
  // The acceptance-specified sibling count (qa_checklist.md item 2), distinct
  // from the pre-existing 24-worker wrap-behavior fixture above. Adds one
  // redundant root->sink edge (root already reaches sink through every
  // worker) so this fixture also exercises reduced-vs-full edge counts —
  // the plain fan-out/fan-in shape above has nothing to reduce.
  const workers30 = Array.from({ length: 30 }, (_, i) => `w${i + 1}`);
  const fullEdges: Edge[] = [
    ...workers30.map((w) => ({ id: `root-${w}`, source: "root", target: w })),
    ...workers30.map((w) => ({ id: `${w}-sink`, source: w, target: "sink" })),
    { id: "root-sink", source: "root", target: "sink" }, // redundant: root->w_i->sink for every i
  ];
  const nodes30 = () => [full("root"), ...workers30.map(full), full("sink")];

  it("has exactly 30 siblings and one redundant root->sink edge", () => {
    expect(workers30).toHaveLength(30);
    expect(fullEdges).toHaveLength(61);
  });

  it("reduces the redundant root->sink edge and nothing else", () => {
    const { kept, hidden } = transitiveReduceDisplay(fullEdges);
    expect(kept).toHaveLength(60);
    expect(hidden).toHaveLength(1);
    expect(hidden[0].id).toBe("root-sink");
    // Every worker's two edges (its only path in and out) must survive —
    // reduction must never touch an edge with no alternative path.
    for (const w of workers30) {
      expect(kept.some((e) => e.id === `root-${w}`)).toBe(true);
      expect(kept.some((e) => e.id === `${w}-sink`)).toBe(true);
    }
  });

  it("lays out all 32 nodes without overlap, full and reduced alike", () => {
    const { kept } = transitiveReduceDisplay(fullEdges);
    for (const edgeSet of [fullEdges, kept]) {
      const { nodes } = getLayoutedElements(nodes30(), edgeSet, "LR");
      expect(nodes).toHaveLength(32);
      assertNoOverlap(nodes);
    }
  });

  it("wraps the 30-wide rank into a grid substantially wider than tall (WRAP_TARGET_ASPECT-shaped)", () => {
    const { nodes } = getLayoutedElements(nodes30(), fullEdges, "LR");
    const { width, height } = boundingBox(nodes);
    // Unwrapped, 30 populated nodes would stack ~30 * ~80px =~ 2400px tall in
    // a single column; the wrap keeps the block far shorter than that and the
    // overall picture wider than tall, same property the 24-worker fixture
    // above pins directly on the worker rank's height.
    expect(height).toBeLessThan(1400);
    expect(width).toBeGreaterThan(height);
  });

  it("stays reachable by zoom-out even though it cannot fit legibly", () => {
    const { nodes } = getLayoutedElements(nodes30(), fullEdges, "LR");
    const { width, height } = boundingBox(nodes);
    const viewport = { width: 1280, height: 560 };

    // What the graph would need to fit whole, with no floor applied. This is
    // genuinely below the readability floor (~0.56 at this size), so the fit
    // opens clamped and overflowing: that is the deliberate trade, legible by
    // default rather than whole by default. Asserting the CLAMPED value clears
    // the floor would prove nothing — the clamp guarantees it for any layout.
    const rawFit = realReactFlowFitZoom(width, height, viewport, 0.15, 0, 1);
    expect(rawFit).toBeLessThan(FIT_ZOOM_FLOOR);

    // The half that protects the user: whatever the fit picks, the zoom
    // control must still reach far enough out to show the graph whole. Putting
    // the readability floor on the ReactFlow root breaks exactly this.
    expect(MIN_INTERACTIVE_ZOOM).toBeLessThanOrEqual(rawFit);
  });
});

describe("getLayoutedElements — 18-node deep chain with global fan-in (acceptance fixture)", () => {
  // The acceptance-specified shape (qa_checklist.md item 2): "~6 roots, a 5-6
  // deep implementer/tester chain, every node also edging to a terminal
  // critic, one semi-detached operator" totaling 18 nodes — distinct from
  // the pre-existing 13-node fixture above (6 roots + 5-chain + critic + op),
  // which this extends rather than replaces. To reach 18 while keeping the
  // roots count and chain depth exactly as specified, the extra 5 nodes are
  // additional standalone (depth-0) roles that — like the semi-detached
  // operator — only edge into critic and never join the main chain: this
  // models the report's "huge empty space" (multiple loosely-connected
  // nodes, not only the one named operator) rather than inventing an
  // unrelated shape. This choice is called out explicitly here per the
  // ambiguity in the acceptance text (it specifies roots/chain/critic/op
  // counts that only total 13-14, leaving 4-5 nodes unassigned to reach 18).
  const roots18 = ["r1", "r2", "r3", "r4", "r5", "r6"];
  const chain18 = ["i1", "i2", "i3", "t1", "t2"];
  const peripheral18 = ["explorer", "analyst", "reviewer", "synthesizer", "coordinator"];
  const nodeIds18 = [...roots18, ...chain18, "op", ...peripheral18, "critic"];
  const edges18: Edge[] = [
    ...roots18.map((r) => ({ id: `${r}-i1`, source: r, target: "i1" })),
    { id: "i1-i2", source: "i1", target: "i2" },
    { id: "i2-i3", source: "i2", target: "i3" },
    { id: "i3-t1", source: "i3", target: "t1" },
    { id: "t1-t2", source: "t1", target: "t2" },
    { id: "r1-op", source: "r1", target: "op" },
    ...nodeIds18
      .filter((id) => id !== "critic")
      .map((id) => ({ id: `${id}-critic`, source: id, target: "critic" })),
  ];
  const nodes18 = () => nodeIds18.map(full);

  it("has exactly 18 nodes and the modeled edge count", () => {
    expect(nodeIds18).toHaveLength(18);
    // Structural: 6 root->i1 + 4 chain-internal + 1 r1->op = 11.
    // Global fan-in: every one of the 17 non-critic nodes -> critic = 17.
    expect(edges18).toHaveLength(28);
  });

  it("reduces to 18 kept / 10 hidden: every node with a longer surviving path to critic loses its direct edge", () => {
    const { kept, hidden } = transitiveReduceDisplay(edges18);
    expect(kept).toHaveLength(18);
    expect(hidden).toHaveLength(10);
    // The 6 roots and the first 4 chain links all reach critic via the chain
    // (ending t2->critic), so their direct edges are redundant.
    expect(hidden.map((e) => e.id).sort()).toEqual(
      [
        "r1-critic",
        "r2-critic",
        "r3-critic",
        "r4-critic",
        "r5-critic",
        "r6-critic",
        "i1-critic",
        "i2-critic",
        "i3-critic",
        "t1-critic",
      ].sort(),
    );
    // t2, op, and every peripheral node have no alternative path — each
    // ->critic edge is that node's only route, so it survives.
    for (const id of ["t2", "op", ...peripheral18]) {
      expect(kept.some((e) => e.id === `${id}-critic`)).toBe(true);
    }
  });

  it("has the modeled depth: chain reaches depth 5, critic (fan-in) is the deepest node at depth 6", () => {
    const depths = computeNodeDepths(nodes18(), edges18);
    for (const r of roots18) expect(depths.get(r)).toBe(0);
    for (const p of peripheral18) expect(depths.get(p)).toBe(0);
    expect(depths.get("i3")).toBe(3);
    expect(depths.get("t2")).toBe(5);
    expect(depths.get("op")).toBe(1);
    expect(depths.get("critic")).toBe(6);
    expect(maxGraphDepth(nodes18(), edges18)).toBe(6);
  });

  it("lays out full and reduced edge sets without overlap, keeping every node", () => {
    const { kept } = transitiveReduceDisplay(edges18);
    for (const edgeSet of [edges18, kept]) {
      const { nodes } = getLayoutedElements(nodes18(), edgeSet, "LR");
      expect(nodes.map((n) => n.id).sort()).toEqual([...nodeIds18].sort());
      assertNoOverlap(nodes);
    }
  });

  it("bounding-box aspect is wider than tall for both the full and reduced edge sets", () => {
    const { kept } = transitiveReduceDisplay(edges18);
    for (const edgeSet of [edges18, kept]) {
      const { nodes } = getLayoutedElements(nodes18(), edgeSet, "LR");
      const { width, height } = boundingBox(nodes);
      expect(width).toBeGreaterThan(height);
    }
  });

  it("stays reachable by zoom-out at a realistic panel size, full and reduced alike", () => {
    const { kept } = transitiveReduceDisplay(edges18);
    const viewport = { width: 1280, height: 560 };
    for (const edgeSet of [edges18, kept]) {
      const { nodes } = getLayoutedElements(nodes18(), edgeSet, "LR");
      const { width, height } = boundingBox(nodes);

      // This fixture is ~1968x1692, so height binds and it needs ~0.29 to fit
      // whole — well under the readability floor. Opening it at the floor
      // therefore shows roughly the top half of the graph, and the zoom-out
      // range is the ONLY way to see the rest: a root minZoom set to the
      // readability floor leaves this graph permanently half-visible in a
      // compact embed, which has no minimap either.
      const rawFit = realReactFlowFitZoom(width, height, viewport, 0.15, 0, 1);
      expect(MIN_INTERACTIVE_ZOOM).toBeLessThanOrEqual(rawFit);
    }
  });
});

// ── fitZoomFor vs. the installed ReactFlow formula ─────────────────────────────
//
// qa_checklist.md item 2b / review.md's second HIGH finding: production
// `fitZoomFor` (WorkerCanvas.tsx) computes each axis as
// `viewport / (graph * (1 + 2 * padding))`. The installed `reactflow`
// package's own `getViewportForBounds` (node_modules/@reactflow/core) uses
// `viewport / (bounds * (1 + padding))` — a single padding term, not double —
// and additionally clamps the result to [minZoom, maxZoom], which
// `fitZoomFor` does not do at all (it has no minZoom parameter). This test
// pins the installed package's real formula directly against `fitZoomFor`'s
// output for the same inputs; it is expected to FAIL against the current
// implementation and is intentionally not weakened to match the bug — see
// focused_test_results.md for the defect this documents.
describe("fitZoomFor — must match the installed ReactFlow getViewportForBounds formula", () => {
  it("agrees with the installed reactflow package's own fit-zoom arithmetic", async () => {
    // Import the actual installed reactflow implementation directly, so this
    // assertion tracks whatever version is in node_modules rather than a
    // hand-copied formula that could itself drift out of sync.
    const { getViewportForBounds } = await import("reactflow");
    const cases: Array<{ width: number; height: number; vw: number; vh: number; padding: number }> =
      [
        { width: 2000, height: 800, vw: 1280, vh: 560, padding: 0.15 },
        { width: 3000, height: 300, vw: 1280, vh: 560, padding: 0.15 },
        { width: 600, height: 200, vw: 1280, vh: 560, padding: 0.15 },
      ];
    for (const { width, height, vw, vh, padding } of cases) {
      const real = getViewportForBounds(
        { x: 0, y: 0, width, height },
        vw,
        vh,
        FIT_ZOOM_FLOOR,
        1,
        padding,
      ).zoom;
      const production = fitZoomFor(width, height, vw, vh, padding, 1);
      expect(
        production,
        `fitZoomFor(${width}x${height} in ${vw}x${vh} @padding ${padding}) = ${production}, ` +
          `but the installed reactflow's own getViewportForBounds computes ${real} for the same inputs`,
      ).toBeCloseTo(real, 5);
    }
  });
});

describe("getLayoutedElements — a long sequential chain folds into readable rows", () => {
  // The wide fan-out above is one failure shape; this is the other. A run whose
  // work is mostly sequential produces one node per rank, so the graph grows
  // only along x: measured before this fold, an 18-step chain laid out
  // 4596x98 (aspect 46.9) and a 30-step chain 7692x98 (aspect 78.5). The
  // second matters most — its fit zoom was 0.128, BELOW the 0.2 interactive
  // zoom floor, so no zoom gesture could show it whole. The pre-existing
  // "18-node deep chain" fixture is not this shape at all: its global fan-in
  // makes it 1968x1692, aspect 1.16, which is why these cases needed their own
  // coverage rather than being assumed covered.
  const chainOf = (n: number) => {
    const ids = Array.from({ length: n }, (_, i) => `s${i}`);
    const edges: Edge[] = ids
      .slice(0, -1)
      .map((id, i) => ({ id: `${id}-${ids[i + 1]}`, source: id, target: ids[i + 1] }));
    return { nodes: () => ids.map(full), edges, ids };
  };

  const boxOf = (nodes: Node[]) => {
    const left = Math.min(...nodes.map((n) => n.position.x));
    const right = Math.max(...nodes.map((n) => n.position.x + 210));
    const top = Math.min(...nodes.map((n) => n.position.y));
    const bottom = Math.max(...nodes.map((n) => n.position.y + estimateNodeHeight(n)));
    return { width: right - left, height: bottom - top };
  };

  // The same arithmetic the canvas uses to pick a fit zoom, against a realistic
  // compact-embed viewport.
  const fitFor = (nodes: Node[]) => {
    const { width, height } = boxOf(nodes);
    const padding = 0.15;
    return Math.min(1280 / (width * (1 + 2 * padding)), 560 / (height * (1 + 2 * padding)), 1);
  };

  it("brings an 18-step chain up to a fit zoom that clears the readability floor", () => {
    const c = chainOf(18);
    const { nodes } = getLayoutedElements(c.nodes(), c.edges, "LR");
    expect(fitFor(nodes)).toBeGreaterThanOrEqual(FIT_ZOOM_FLOOR);
  });

  it("brings a 30-step chain back above the interactive zoom floor it used to sit under", () => {
    // This is the case the zoom floor alone could not rescue: unfolded, the
    // whole graph only fit at 0.128 and MIN_INTERACTIVE_ZOOM is 0.2.
    const c = chainOf(30);
    const { nodes } = getLayoutedElements(c.nodes(), c.edges, "LR");
    expect(fitFor(nodes)).toBeGreaterThan(MIN_INTERACTIVE_ZOOM);
  });

  it("stacks the chain into more than one row instead of one strip", () => {
    const c = chainOf(18);
    const { nodes } = getLayoutedElements(c.nodes(), c.edges, "LR");
    const distinctRows = new Set(nodes.map((n) => Math.round(n.position.y / 50)));
    expect(distinctRows.size).toBeGreaterThan(1);
    expect(boxOf(nodes).width).toBeLessThan(4596);
  });

  it("keeps every node and never overlaps two of them after folding", () => {
    const c = chainOf(30);
    const { nodes } = getLayoutedElements(c.nodes(), c.edges, "LR");
    expect(nodes.map((n) => n.id).sort()).toEqual([...c.ids].sort());
    assertNoOverlap(nodes);
  });

  it("leaves a chain that already fits on one line alone", () => {
    // Folding a 4-step pipeline into a 2x2 block would be a regression: it
    // reads perfectly well as one line and already fits at near-full zoom.
    const c = chainOf(4);
    const { nodes } = getLayoutedElements(c.nodes(), c.edges, "LR");
    const rows = new Set(nodes.map((n) => Math.round(n.position.y)));
    expect(rows.size).toBe(1);
  });

  it("leaves the wide-BUT-TALL fan-in fixture untouched — folding only helps flat graphs", () => {
    // The guard that keeps this from being a blanket "fold anything wide".
    // This fixture is past the row-width limit, so width alone would fold it;
    // what saves it is having real cross-axis content (measured ~1692px tall,
    // far past the flat-height limit). Folding would cost it the one thing a
    // left-to-right graph guarantees — downstream sitting right of its source
    // — to buy nothing, because its rows already carry meaning.
    const laid = getLayoutedElements(deepChainFixture(), deepChainEdges, "LR").nodes;
    expect(boxOf(laid).width).toBeGreaterThan(1500);
    expect(boxOf(laid).height).toBeGreaterThan(240);
    // Unfolded, the deepest node still sits to the RIGHT of every root.
    const critic = laid.find((n) => n.id === "critic")!;
    const roots = laid.filter((n) => deepChainRoots.includes(n.id));
    for (const r of roots) expect(critic.position.x).toBeGreaterThan(r.position.x);
  });

  it("leaves an already-wrapped wide fan-out to wrapWideRanks, sink still rightmost", () => {
    // A fan-out that wrapWideRanks has gridded is wide (~1900px) but has four
    // rows of workers, so it is not flat and must not be folded on top of that
    // treatment — the sink would land on a new row, left of the workers.
    const workers = Array.from({ length: 24 }, (_, i) => `w${i + 1}`);
    const fan: Edge[] = [
      ...workers.map((w) => ({ id: `root-${w}`, source: "root", target: w })),
      ...workers.map((w) => ({ id: `${w}-sink`, source: w, target: "sink" })),
    ];
    const { nodes } = getLayoutedElements(
      [full("root"), ...workers.map(full), full("sink")],
      fan,
      "LR",
    );
    expect(boxOf(nodes).height).toBeGreaterThan(240);
    const sinkX = nodes.find((n) => n.id === "sink")!.position.x;
    for (const w of workers) {
      expect(sinkX).toBeGreaterThan(nodes.find((n) => n.id === w)!.position.x);
    }
  });
});

describe("foldWideGraph — unit behaviour", () => {
  it("is an identity for an empty graph and for a graph inside the width limit", () => {
    expect(foldWideGraph([])).toEqual([]);
    const narrow = [
      { ...full("a"), position: { x: 0, y: 0 } },
      { ...full("b"), position: { x: 300, y: 0 } },
    ];
    expect(foldWideGraph(narrow)).toBe(narrow);
  });

  it("preserves left-to-right order within each row it produces", () => {
    const wide = Array.from({ length: 12 }, (_, i) => ({
      ...full(`n${i}`),
      position: { x: i * 260, y: 0 },
    }));
    const folded = foldWideGraph(wide);
    const byRow = new Map<number, Node[]>();
    for (const n of folded) {
      const row = Math.round(n.position.y / 50);
      byRow.set(row, [...(byRow.get(row) ?? []), n]);
    }
    expect(byRow.size).toBeGreaterThan(1);
    for (const row of byRow.values()) {
      const sortedByX = [...row].sort((a, b) => a.position.x - b.position.x);
      const indices = sortedByX.map((n) => Number(n.id.slice(1)));
      // Within a row, reading left to right must still read the chain in order.
      expect(indices).toEqual([...indices].sort((a, b) => a - b));
    }
  });
});

describe("markContinuationEdges — the fold's return sweep is not a dependency", () => {
  const at = (id: string, x: number): Node => ({ id, position: { x, y: 0 }, data: { label: id } });
  const link = (source: string, target: string): Edge => ({
    id: `${source}-${target}`,
    source,
    target,
  });

  it("marks an edge whose target was folded onto the row below its source", () => {
    const marked = markContinuationEdges([at("a", 1400), at("b", 0)], [link("a", "b")]);
    expect(marked[0].data).toMatchObject({ continuation: true });
  });

  it("leaves a forward edge alone, which is every edge in an unfolded LR graph", () => {
    const edges = [link("a", "b")];
    const marked = markContinuationEdges([at("a", 0), at("b", 300)], edges);
    expect(marked[0].data?.continuation).toBeUndefined();
  });

  it("does not mark an edge between two nodes at the same x", () => {
    // Equal x is not backwards. Marking it would put a continuation inside a
    // wrapped rank's grid column, where nothing wrapped.
    const marked = markContinuationEdges([at("a", 500), at("b", 500)], [link("a", "b")]);
    expect(marked[0].data?.continuation).toBeUndefined();
  });

  it("returns the very same array when nothing is marked", () => {
    // Identity, not just equality: the canvas re-lays out on every run tick and
    // a fresh array each time would invalidate memoized edges for no reason.
    const edges = [link("a", "b")];
    const nodes = [at("a", 0), at("b", 300)];
    expect(markContinuationEdges(nodes, edges)).toBe(edges);
  });

  it("keeps the data a marked edge already carried", () => {
    const edges: Edge[] = [{ ...link("a", "b"), data: { mode: "code", rankDistance: 4 } }];
    const marked = markContinuationEdges([at("a", 1400), at("b", 0)], edges);
    expect(marked[0].data).toMatchObject({ mode: "code", rankDistance: 4, continuation: true });
  });

  it("says nothing about an edge whose endpoints the layout never placed", () => {
    // An unplaced endpoint has no x, so it has no direction either. Guessing
    // one would mark edges in the editor's fresh-connect path, which never folded.
    const marked = markContinuationEdges([at("a", 1400)], [link("a", "ghost")]);
    expect(marked[0].data?.continuation).toBeUndefined();
  });

  it("does not mutate the edges it was given", () => {
    const edges = [link("a", "b")];
    markContinuationEdges([at("a", 1400), at("b", 0)], edges);
    expect(edges[0].data).toBeUndefined();
  });
});

describe("getLayoutedElements — a folded chain marks exactly its row boundaries", () => {
  const chain = (n: number) => {
    const ids = Array.from({ length: n }, (_, i) => `s${i}`);
    const edges: Edge[] = ids
      .slice(0, -1)
      .map((id, i) => ({ id: `${id}-${ids[i + 1]}`, source: id, target: ids[i + 1] }));
    return { nodes: () => ids.map(full), edges };
  };

  it("marks a continuation on a chain long enough to fold", () => {
    const c = chain(30);
    const { edges } = getLayoutedElements(c.nodes(), c.edges, "LR");
    expect(edges.filter((e) => e.data?.continuation).length).toBeGreaterThan(0);
  });

  it("marks one continuation per row boundary and no more", () => {
    // A fold into R rows breaks the chain in R-1 places, so anything above that
    // means ordinary dependencies are being drawn as wrapped text.
    const c = chain(30);
    const { nodes, edges } = getLayoutedElements(c.nodes(), c.edges, "LR");
    const rowTops = new Set(nodes.map((n) => Math.round(n.position.y)));
    const marked = edges.filter((e) => e.data?.continuation);
    expect(marked).toHaveLength(rowTops.size - 1);
  });

  it("marks only edges that really do run backwards on the canvas", () => {
    const c = chain(30);
    const { nodes, edges } = getLayoutedElements(c.nodes(), c.edges, "LR");
    const xById = new Map(nodes.map((n) => [n.id, n.position.x]));
    for (const e of edges.filter((x) => x.data?.continuation)) {
      expect(xById.get(e.target)!).toBeLessThan(xById.get(e.source)!);
    }
  });

  it("marks nothing on a chain short enough that no fold happens", () => {
    // The control. Without it every assertion above is satisfied by a function
    // that marks whatever it likes on graphs that never wrapped.
    const c = chain(4);
    const { nodes, edges } = getLayoutedElements(c.nodes(), c.edges, "LR");
    expect(new Set(nodes.map((n) => Math.round(n.position.y))).size).toBe(1);
    expect(edges.filter((e) => e.data?.continuation)).toHaveLength(0);
  });
});

describe("getLayoutedElements — folding a mid-run branch+join stays edge-aware", () => {
  const chain = (n: number) => {
    const ids = Array.from({ length: n }, (_, i) => `s${i}`);
    const edges: Edge[] = ids
      .slice(0, -1)
      .map((id, i) => ({ id: `${id}-${ids[i + 1]}`, source: id, target: ids[i + 1] }));
    return { nodes: () => ids.map(full), edges };
  };

  // The shape a purely geometric fold gets wrong: a long LR chain with a
  // branch (one node, two successors) and a join (one node, two
  // predecessors) partway through. s0..s4 sequential, s4 branches to
  // sBa/sBb, both join at sJ, then a0..a9 continue sequentially. Wide and
  // flat enough that the pre-fix fold splits a row right at the join, which
  // marked the real sBa-sJ/sBb-sJ dependencies as continuation.
  const before = Array.from({ length: 5 }, (_, i) => `s${i}`);
  const after = Array.from({ length: 10 }, (_, i) => `a${i}`);
  const branchJoinIds = [...before, "sBa", "sBb", "sJ", ...after];
  const branchJoinEdges: Edge[] = [
    ...before
      .slice(0, -1)
      .map((id, i) => ({ id: `${id}-${before[i + 1]}`, source: id, target: before[i + 1] })),
    { id: "s4-sBa", source: "s4", target: "sBa" },
    { id: "s4-sBb", source: "s4", target: "sBb" },
    { id: "sBa-sJ", source: "sBa", target: "sJ" },
    { id: "sBb-sJ", source: "sBb", target: "sJ" },
    { id: "sJ-a0", source: "sJ", target: "a0" },
    ...after
      .slice(0, -1)
      .map((id, i) => ({ id: `${id}-${after[i + 1]}`, source: id, target: after[i + 1] })),
  ];
  const branchJoinAuthoredIds = new Set([
    "s4-sBa",
    "s4-sBb",
    "sBa-sJ",
    "sBb-sJ",
    "sJ-a0",
    ...before.slice(0, -1).map((id, i) => `${id}-${before[i + 1]}`),
    ...after.slice(0, -1).map((id, i) => `${id}-${after[i + 1]}`),
  ]);

  it("never marks a branch or join dependency as a continuation", () => {
    const { nodes, edges } = getLayoutedElements(branchJoinIds.map(full), branchJoinEdges, "LR");
    // Sanity: this fixture actually folds into more than one row.
    const rowCount = new Set(nodes.map((n) => Math.round(n.position.y / 50))).size;
    expect(rowCount).toBeGreaterThan(1);

    const wronglyMarked = edges.filter(
      (e) => ["s4-sBa", "s4-sBb", "sBa-sJ", "sBb-sJ"].includes(e.id) && e.data?.continuation,
    );
    expect(wronglyMarked.map((e) => e.id)).toEqual([]);
  });

  it("marks continuation on nothing but authored edges (no synthetic edge sneaks in)", () => {
    const { edges } = getLayoutedElements(branchJoinIds.map(full), branchJoinEdges, "LR");
    for (const e of edges.filter((x) => x.data?.continuation)) {
      expect(branchJoinAuthoredIds.has(e.id)).toBe(true);
    }
  });

  it("keeps every node and never overlaps two of them", () => {
    const { nodes } = getLayoutedElements(branchJoinIds.map(full), branchJoinEdges, "LR");
    expect(nodes.map((n) => n.id).sort()).toEqual([...branchJoinIds].sort());
    assertNoOverlap(nodes);
  });

  it("the truly-sequential 30-node chain still folds into the previously-measured band", () => {
    // Pins the readability win survives the edge-aware rewrite: every
    // boundary in a pure chain is a safe (1-predecessor/1-successor) break,
    // so this must fold exactly as before, modulo the row height itself.
    // Measured pre-fix band at NODE_HEIGHT=56: ~1528x504, raw fit ~0.644 at a
    // 1280x560 panel. NODE_HEIGHT rose to 88 for the live-activity row (ADR-
    // 0113 row 6, see useLayout.ts), which raises every row's height and so
    // this band's too — remeasured then at ~1500x664. It rose again when
    // NODE_HEIGHT stopped being a literal and became the sum of the rows the
    // card draws, which came out 11px taller than the literal had claimed:
    // remeasured at ~719 tall. Drift with the row height is expected and is
    // why the width and fit stay bands; a strip (unfolded ~7692-wide) is the
    // regression this guards against.
    const c = chain(30);
    const { nodes } = getLayoutedElements(c.nodes(), c.edges, "LR");
    const left = Math.min(...nodes.map((n) => n.position.x));
    const right = Math.max(...nodes.map((n) => n.position.x + 210));
    const top = Math.min(...nodes.map((n) => n.position.y));
    const bottom = Math.max(...nodes.map((n) => n.position.y + estimateNodeHeight(n)));
    const width = right - left;
    const height = bottom - top;
    const padding = 0.15;
    const rawFit = Math.min(
      1280 / (width * (1 + 2 * padding)),
      560 / (height * (1 + 2 * padding)),
      1,
    );

    expect(width).toBeGreaterThan(1300);
    expect(width).toBeLessThan(1700);
    expect(height).toBeCloseTo(719, -1);
    expect(rawFit).toBeGreaterThan(0.55);
    expect(rawFit).toBeLessThan(0.75);
  });
});

describe("getLayoutedElements — reports a bounding-box width alongside height", () => {
  it("reports zero width for an empty graph", () => {
    expect(getLayoutedElements([], [], "LR").width).toBe(0);
  });

  it("reports a positive width for a populated graph", () => {
    const { width } = getLayoutedElements(
      [full("a"), full("b")],
      [{ id: "a-b", source: "a", target: "b" }],
      "LR",
    );
    expect(width).toBeGreaterThan(0);
  });
});

describe("computeReservedHeight — width-constrained rendered-height reservation", () => {
  // The dead-height bug: getLayoutedElements reports the UNSCALED bbox
  // height, but a wide graph renders width-constrained at a smaller zoom —
  // so a container reserving the raw bbox height reserves far more than the
  // graph ever draws. computeReservedHeight must report what will actually
  // render, given the bbox and the real container width.

  it("shrinks the reserved height for a width-constrained (very wide) graph", () => {
    // 3000px-wide bbox into a 700px container is far below zoom 1.
    const reserved = computeReservedHeight(3000, 600, 700);
    expect(reserved).toBeLessThan(600);
    expect(reserved).toBeGreaterThan(0);
  });

  it("matches the fit-zoom arithmetic exactly for a width-constrained graph", () => {
    // Numbers chosen so the unclamped fit zoom clears FIT_ZOOM_FLOOR (0.65)
    // but stays below DAG_MAX_ZOOM (1) — genuinely width-constrained, not
    // floor- or ceiling-clamped.
    const bboxWidth = 1200;
    const bboxHeight = 600;
    const containerWidth = 1000;
    const expectedZoom = containerWidth / (bboxWidth * 1.15);
    expect(computeReservedHeight(bboxWidth, bboxHeight, containerWidth)).toBeCloseTo(
      bboxHeight * expectedZoom,
      5,
    );
  });

  it("reserves the full reported height for a graph that fits entirely (zoom would clamp to 1)", () => {
    // A narrow, short bbox well inside the container never needs to shrink —
    // fitView's maxZoom of 1 caps it there, not beyond.
    const reserved = computeReservedHeight(200, 150, 1200);
    expect(reserved).toBeCloseTo(150, 5);
  });

  it("is height-constrained (not width-constrained) for a tall narrow graph — full reported height, zoom 1", () => {
    const reserved = computeReservedHeight(150, 900, 1200);
    expect(reserved).toBeCloseTo(900, 5);
  });

  it("never scales below the readability zoom floor the canvas actually clamps to (FIT_ZOOM_FLOOR), however wide the graph", () => {
    // An extremely wide graph into a narrow container: fitView cannot zoom
    // out past FIT_ZOOM_FLOOR (WorkerCanvas's own clamp), so the panel must
    // not reserve less than that — reserving at a lower floor leaves most of
    // what the canvas actually renders outside the panel.
    const bboxWidth = 100_000;
    const bboxHeight = 500;
    const containerWidth = 400;
    const reserved = computeReservedHeight(bboxWidth, bboxHeight, containerWidth);
    expect(reserved).toBeCloseTo(bboxHeight * FIT_ZOOM_FLOOR, 5);
  });

  it("matches the canvas's real fit height for an under-fit graph (10,000x10,000 in a 711px panel)", () => {
    // Regression for the reservation/canvas floor divergence: the reservation
    // must render the same height the canvas actually fits to, not a smaller
    // one computed against a different, lower floor.
    const reserved = computeReservedHeight(10_000, 10_000, 711);
    const canvasZoom = fitZoomFor(10_000, 10_000, 711, Infinity, DAG_FIT_PADDING, DAG_MAX_ZOOM);
    expect(reserved).toBeCloseTo(10_000 * canvasZoom, 5);
  });

  it("never scales above DAG_MAX_ZOOM even for a tiny bbox in a huge container", () => {
    const reserved = computeReservedHeight(50, 40, 5000);
    expect(reserved).toBeCloseTo(40 * DAG_MAX_ZOOM, 5);
  });

  it("degrades to the raw height rather than dividing by zero on a degenerate bbox/container", () => {
    expect(computeReservedHeight(0, 500, 700)).toBe(500);
    expect(computeReservedHeight(500, 0, 700)).toBe(0);
    expect(computeReservedHeight(500, 500, 0)).toBe(500);
  });

  it("end-to-end: a real wrapped fan-out's reserved height is materially smaller than its raw height at a realistic panel width", () => {
    const workers = Array.from({ length: 24 }, (_, i) => `w${i + 1}`);
    const nodes: Node[] = [
      { id: "root", position: { x: 0, y: 0 }, data: { label: "root", role: "analyst" } },
      ...workers.map((id) => ({
        id,
        position: { x: 0, y: 0 },
        data: { label: id, role: "analyst" },
      })),
      { id: "sink", position: { x: 0, y: 0 }, data: { label: "sink", role: "analyst" } },
    ];
    const edges: Edge[] = [
      ...workers.map((w) => ({ id: `root-${w}`, source: "root", target: w })),
      ...workers.map((w) => ({ id: `${w}-sink`, source: w, target: "sink" })),
    ];
    const { height, width } = getLayoutedElements(nodes, edges, "LR");
    const reserved = computeReservedHeight(width, height, 711); // measured RunDetail panel width
    expect(reserved).toBeLessThan(height);
  });
});
