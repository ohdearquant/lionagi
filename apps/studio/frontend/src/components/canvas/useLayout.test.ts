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
import { estimateNodeHeight, getLayoutedElements } from "./useLayout";

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

describe("estimateNodeHeight", () => {
  it("gives a node carrying no optional rows the base height", () => {
    expect(estimateNodeHeight(bare("a"))).toBe(40);
  });

  it("grows as each row is filled in, so a fully populated node is much taller", () => {
    expect(estimateNodeHeight(full("a"))).toBeGreaterThan(estimateNodeHeight(bare("a")));
    // The gap is the whole bug: a single constant cannot describe both.
    expect(estimateNodeHeight(full("a")) - estimateNodeHeight(bare("a"))).toBeGreaterThan(40);
  });

  it("counts each row independently rather than treating any one as a proxy", () => {
    const roleOnly = { ...bare("a"), data: { label: "a", role: "critic" } };
    const roleAndModel = {
      ...bare("a"),
      data: { label: "a", role: "critic", assignment: "codex/gpt-5.6-terra" },
    };
    expect(estimateNodeHeight(roleAndModel)).toBeGreaterThan(estimateNodeHeight(roleOnly));
    expect(estimateNodeHeight(roleOnly)).toBeGreaterThan(estimateNodeHeight(bare("a")));
  });

  it("does not count a zero/absent stats row", () => {
    const zeroed = {
      ...bare("a"),
      data: { label: "a", errorCount: 0, toolCallCount: 0 },
    };
    expect(estimateNodeHeight(zeroed)).toBe(estimateNodeHeight(bare("a")));
  });

  it("survives a node with no data at all", () => {
    const noData = { id: "a", position: { x: 0, y: 0 } } as unknown as Node;
    expect(() => estimateNodeHeight(noData)).not.toThrow();
    expect(estimateNodeHeight(noData)).toBe(40);
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

  it("spaces populated siblings further apart than bare ones, since they are taller", () => {
    const populated = getLayoutedElements([full(parent), ...siblings.map(full)], edges, "LR");
    const plain = getLayoutedElements([bare(parent), ...siblings.map(bare)], edges, "LR");
    const span = (nodes: Node[]) => {
      const ys = siblings.map((id) => nodes.find((n) => n.id === id)!.position.y);
      return Math.max(...ys) - Math.min(...ys);
    };
    expect(span(populated.nodes)).toBeGreaterThan(span(plain.nodes));
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
});
