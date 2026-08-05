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

const NODE_SEP = 36;

// A rank taller than this wraps into a grid. dagre stacks every sibling of a
// fan-out into one cross-axis strip, so a run that fans 30 workers off one
// orchestrator becomes a ~4000px column that fitView can only show as a
// sliver of unreadable cards. Wrapping trades edge purity (links into the
// inner columns cross their siblings) for the whole graph being legible at
// once, which is the only trade a monitoring panel can make.
const WRAP_THRESHOLD = 7;
// The embeds this canvas lives in are wide strips (RunDetail's run-dag panel,
// the Fleet session view), so a wrapped block aims for that shape.
const WRAP_TARGET_ASPECT = 2.4;
const WRAP_COL_GAP = 28;

// Re-arrange any over-tall rank of an LR layout into column-major grid
// columns, shifting every rank to its right by the width the wrap added.
// Sibling order within the grid preserves dagre's cross-axis order, so nodes
// dagre placed adjacent stay adjacent.
function wrapWideRanks(nodes: Node[]): Node[] {
  const byRankX = new Map<number, Node[]>();
  for (const node of nodes) {
    const key = Math.round(node.position.x);
    const rank = byRankX.get(key);
    if (rank) rank.push(node);
    else byRankX.set(key, [node]);
  }

  const rankXs = [...byRankX.keys()].sort((a, b) => a - b);
  const out: Node[] = [];
  let xShift = 0;
  const colPitch = NODE_WIDTH + WRAP_COL_GAP;

  for (const rankX of rankXs) {
    const rank = [...(byRankX.get(rankX) ?? [])].sort((a, b) => a.position.y - b.position.y);
    if (rank.length <= WRAP_THRESHOLD) {
      for (const node of rank) {
        out.push({ ...node, position: { x: node.position.x + xShift, y: node.position.y } });
      }
      continue;
    }

    const rowPitch =
      rank.reduce((sum, n) => sum + estimateNodeHeight(n), 0) / rank.length + NODE_SEP;
    const cols = Math.max(
      2,
      Math.ceil(Math.sqrt((WRAP_TARGET_ASPECT * rank.length * rowPitch) / colPitch)),
    );
    const rows = Math.ceil(rank.length / cols);
    // Rounding can leave the last planned column empty; the shift must count
    // the columns actually placed or every rank downstream drifts right.
    const usedCols = Math.ceil(rank.length / rows);

    // Keep the wrapped block vertically centred where dagre centred the rank,
    // so edges from the previous rank stay short.
    const top = rank[0].position.y;
    const bottom = rank[rank.length - 1].position.y + estimateNodeHeight(rank[rank.length - 1]);
    const rankCenter = (top + bottom) / 2;

    for (let col = 0; col * rows < rank.length; col++) {
      const colNodes = rank.slice(col * rows, (col + 1) * rows);
      const colHeight =
        colNodes.reduce((sum, n) => sum + estimateNodeHeight(n), 0) +
        NODE_SEP * (colNodes.length - 1);
      let y = rankCenter - colHeight / 2;
      for (const node of colNodes) {
        out.push({ ...node, position: { x: rankX + xShift + col * colPitch, y } });
        y += estimateNodeHeight(node) + NODE_SEP;
      }
    }

    xShift += (usedCols - 1) * colPitch;
  }

  return out;
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
    nodesep: NODE_SEP,
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

  // The wrap keys ranks by their shared x, which holds only for LR (constant
  // node width). TB ranks share y instead; no caller lays out TB today.
  return { nodes: direction === "LR" ? wrapWideRanks(layoutedNodes) : layoutedNodes, edges };
}

export function useAutoLayout() {
  return useCallback(
    (nodes: Node[], edges: Edge[], direction: "LR" | "TB" = "LR") =>
      getLayoutedElements(nodes, edges, direction),
    [],
  );
}
