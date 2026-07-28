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
