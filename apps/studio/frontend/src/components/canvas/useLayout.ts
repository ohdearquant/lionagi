import { useCallback } from "react";
import dagre from "dagre";
import type { Node, Edge } from "reactflow";

const NODE_WIDTH = 210;

// StepNode grows a row at a time as a run fills its data in: the role badge,
// the assignment (the model it ran on), then the duration/calls line. A single
// constant height therefore describes the node at exactly one moment of its
// life. Feeding dagre that constant once the other rows exist makes it reserve
// less vertical room than the node occupies, and the boxes crowd into each
// other — worst on a graph where every node has been assigned, which is any
// graph worth looking at.
//
// These track StepNode's own padding and per-row heights. They are estimates:
// exact only matters to the extent that ranks stay clear of each other, and
// over-reserving is harmless where under-reserving overlaps.
const NODE_BASE_HEIGHT = 40; // vertical padding + the label row
const ROW_ROLE = 22;
const ROW_ASSIGNMENT = 17;
const ROW_STATS = 19;

export function estimateNodeHeight(node: Node): number {
  const data = (node.data ?? {}) as {
    role?: unknown;
    assignment?: unknown;
    durationSeconds?: number | null;
    errorCount?: number | null;
    toolCallCount?: number | null;
  };
  let height = NODE_BASE_HEIGHT;
  if (data.role) height += ROW_ROLE;
  if (data.assignment) height += ROW_ASSIGNMENT;
  if (
    (data.durationSeconds != null && data.durationSeconds >= 0) ||
    (data.errorCount ?? 0) > 0 ||
    (data.toolCallCount ?? 0) > 0
  ) {
    height += ROW_STATS;
  }
  return height;
}

export function getLayoutedElements(
  nodes: Node[],
  edges: Edge[],
  direction: "LR" | "TB" = "LR",
): { nodes: Node[]; edges: Edge[] } {
  const g = new dagre.graphlib.Graph();
  g.setDefaultEdgeLabel(() => ({}));
  g.setGraph({
    rankdir: direction,
    nodesep: 36,
    ranksep: 90,
    marginx: 28,
    marginy: 24,
  });

  for (const node of nodes) {
    g.setNode(node.id, { width: NODE_WIDTH, height: estimateNodeHeight(node) });
  }
  for (const edge of edges) {
    g.setEdge(edge.source, edge.target);
  }

  dagre.layout(g);

  const layoutedNodes = nodes.map((node) => {
    const pos = g.node(node.id);
    // dagre reports a centre, so each node is offset by its OWN height. Using a
    // shared constant here would re-introduce the overlap from the other side.
    return {
      ...node,
      position: {
        x: pos.x - NODE_WIDTH / 2,
        y: pos.y - estimateNodeHeight(node) / 2,
      },
    };
  });

  return { nodes: layoutedNodes, edges };
}

export function useAutoLayout() {
  return useCallback(
    (nodes: Node[], edges: Edge[], direction: "LR" | "TB" = "LR") =>
      getLayoutedElements(nodes, edges, direction),
    [],
  );
}
