import { useCallback } from "react";
import dagre from "dagre";
import type { Node, Edge } from "reactflow";

// The card's own geometry, imported by StepNode so there is one copy of each
// number rather than a layout constant and a stylesheet that agree until one
// of them is edited.
export const NODE_WIDTH = 210;

// StepNode is a fixed three-row card: name and state on top, role and
// elapsed second, and a live-activity row (agent's latest text, current
// activity, event/token counter) along the bottom — all three rows always
// rendered. So one constant describes it at every moment of a run, and dagre
// reserves exactly what the node occupies.
//
// It used to grow a row at a time as a run filled its data in, which meant the
// height depended on how far along the run was, ranks came out ragged, and this
// function had to guess. Fixing the card removed the guess: two nodes side by
// side are the same size whether or not either has finished, and — since the
// live-activity row was added — whether or not either has produced any
// assistant text yet. The row's text is clipped to a fixed line count rather
// than allowed to grow the card, which is what keeps this constant honest.
export const NODE_HEIGHT = 88;

/** The height of a rendered node. Constant by construction — see NODE_HEIGHT.
 *  Kept as a function because the layout passes call it per node and a future
 *  variable-size node type would land here. */
export function estimateNodeHeight(_node: Node): number {
  return NODE_HEIGHT;
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
export function wrapWideRanks(nodes: Node[]): Node[] {
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

// Past this width a graph cannot be read at its fit zoom in the embeds this
// canvas lives in: a ~1280px panel with the standard 15% fit padding drops
// under the 0.65 readability floor once the graph passes roughly this many
// pixels, so everything beyond it is a strip you can only pan across. Folding
// trades one unbroken left-to-right line for several rows that each read
// left-to-right, which is the trade a paragraph of text already makes.
const FOLD_MAX_ROW_WIDTH = 1500;
// ...and fold only a graph that is FLAT, meaning it has no vertical structure
// of its own to lose. Folding invents rows, and a downstream node placed on
// the next row sits to the LEFT of its source — the one thing a left-to-right
// graph otherwise guarantees. That price is worth paying for a chain, whose
// rows are empty of meaning anyway, and not worth paying for a graph that
// already reads in two dimensions. The cut-off is two full node-rows plus the
// separator between them, so a graph taller than this has real cross-axis
// content: a wrapped fan-out is deliberately out of scope here, because
// wrapWideRanks above already owns that shape and made its own trade.
//
// Derived rather than written as a number so that changing the card's height
// cannot silently widen what counts as flat. At 56px per row that is 148px,
// where a literal 240 would have quietly started folding three-row graphs.
const FOLD_MAX_FLAT_HEIGHT = 2 * NODE_HEIGHT + NODE_SEP;
const FOLD_ROW_GAP = 56;

// A row may only break between two adjacent columns when exactly one edge
// crosses that boundary and it is a plain 1-predecessor/1-successor link —
// the same "maximal chain segment" a purely sequential run is made of. A
// branch (a node with more than one successor) or a join (a node with more
// than one predecessor) anchors a segment boundary instead: breaking a row
// there would put a real dependency behind the fold's continuation styling,
// the exact thing the fold must never do. Degree is computed over the WHOLE
// edge set (not just edges between adjacent columns), so a long-range edge
// that skips columns also correctly blocks a break at either of its ends.
function computeDegrees(
  nodes: Node[],
  edges: Edge[],
): { outDeg: Map<string, string[]>; inDeg: Map<string, string[]> } {
  const known = new Set(nodes.map((n) => n.id));
  const outDeg = new Map<string, string[]>();
  const inDeg = new Map<string, string[]>();
  for (const e of edges) {
    if (!known.has(e.source) || !known.has(e.target)) continue;
    (outDeg.get(e.source) ?? outDeg.set(e.source, []).get(e.source)!).push(e.target);
    (inDeg.get(e.target) ?? inDeg.set(e.target, []).get(e.target)!).push(e.source);
  }
  return { outDeg, inDeg };
}

// Fold a wide, flat graph into stacked rows, each row still reading
// left-to-right. Within a row the geometry is dagre's own, translated as a
// block, so rank spacing and sibling order survive untouched. The cost is the
// edge that leaves the end of one row and re-enters at the start of the next:
// it sweeps back across the canvas the way a wrapped line of text does — but
// only at a safe (single predecessor/successor) boundary; see computeDegrees.
export function foldWideGraph(nodes: Node[], edges: Edge[] = []): Node[] {
  if (nodes.length === 0) return nodes;

  const left = Math.min(...nodes.map((n) => n.position.x));
  const right = Math.max(...nodes.map((n) => n.position.x + NODE_WIDTH));
  const top = Math.min(...nodes.map((n) => n.position.y));
  const bottom = Math.max(...nodes.map((n) => n.position.y + estimateNodeHeight(n)));
  const width = right - left;
  const height = bottom - top;

  if (width <= FOLD_MAX_ROW_WIDTH) return nodes;
  if (height > FOLD_MAX_FLAT_HEIGHT) return nodes;

  // After wrapWideRanks every column shares one x, so columns are the unit
  // that moves. Folding by column keeps a wrapped fan-out's grid intact.
  const byX = new Map<number, Node[]>();
  for (const node of nodes) {
    const key = Math.round(node.position.x);
    const col = byX.get(key);
    if (col) col.push(node);
    else byX.set(key, [node]);
  }
  const xs = [...byX.keys()].sort((a, b) => a - b);

  // No edges given means no dependency to misrepresent — every column
  // boundary is safe, matching the pre-edge-aware behavior exactly.
  const edgeAware = edges.length > 0;
  const { outDeg, inDeg } = computeDegrees(nodes, edges);
  const isSafeBreak = (beforeX: number, afterX: number): boolean => {
    const beforeNodes = byX.get(beforeX) ?? [];
    const afterNodes = byX.get(afterX) ?? [];
    if (beforeNodes.length !== 1 || afterNodes.length !== 1) return false;
    const a = beforeNodes[0]!;
    const b = afterNodes[0]!;
    const aOut = outDeg.get(a.id) ?? [];
    const bIn = inDeg.get(b.id) ?? [];
    return aOut.length === 1 && aOut[0] === b.id && bIn.length === 1 && bIn[0] === a.id;
  };

  const rows: number[][] = [];
  let current: number[] = [];
  let rowStartX = 0;
  let prevX: number | null = null;
  for (const x of xs) {
    if (current.length === 0) {
      current = [x];
      rowStartX = x;
      prevX = x;
      continue;
    }
    const overWidth = x - rowStartX + NODE_WIDTH > FOLD_MAX_ROW_WIDTH;
    if (overWidth && (!edgeAware || isSafeBreak(prevX!, x))) {
      rows.push(current);
      current = [x];
      rowStartX = x;
    } else {
      current.push(x);
    }
    prevX = x;
  }
  if (current.length > 0) rows.push(current);
  if (rows.length <= 1) return nodes;

  const out: Node[] = [];
  let yOffset = 0;
  for (const row of rows) {
    const rowX0 = row[0];
    let rowTop = Infinity;
    let rowBottom = -Infinity;
    for (const x of row) {
      for (const node of byX.get(x) ?? []) {
        rowTop = Math.min(rowTop, node.position.y);
        rowBottom = Math.max(rowBottom, node.position.y + estimateNodeHeight(node));
      }
    }
    for (const x of row) {
      for (const node of byX.get(x) ?? []) {
        out.push({
          ...node,
          position: { x: left + (x - rowX0), y: node.position.y - rowTop + yOffset },
        });
      }
    }
    yOffset += rowBottom - rowTop + FOLD_ROW_GAP;
  }

  return out;
}

// dagre's ranksep is one constant paid at EVERY rank boundary, so a 10-rank
// chain pays it 9 times over — the deeper the graph, the more that constant
// alone inflates the bounding box fitView has to shrink to fit. Shallow
// graphs (D <= SHALLOW_DEPTH) keep the original 90 unchanged — the wide
// fan-out fixture is depth 3, so this is an identity for it; deeper graphs
// taper linearly down to a floor. The floor is not a node-overlap guarantee
// by itself — ranksep only spaces ranks apart, not siblings within a rank —
// that guarantee is enforceMinRankGap below.
const RANKSEP_SHALLOW_DEPTH = 5;
const RANKSEP_TAPER_PER_RANK = 7;

export function rankSepForDepth(maxDepth: number, ranksep = 90, minRanksep = 48): number {
  if (maxDepth <= RANKSEP_SHALLOW_DEPTH) return ranksep;
  return Math.max(
    minRanksep,
    ranksep - RANKSEP_TAPER_PER_RANK * (maxDepth - RANKSEP_SHALLOW_DEPTH),
  );
}

// Longest-path depth of every node from its roots, over the RESOLVED edge
// set (post display-time reduction, if any — callers pass whatever edges
// they intend to lay out). This is both the rank index edges use for
// long-range routing (ConditionEdge's rankDistance) and the input to
// rankSepForDepth. A cycle should not happen in a depends_on graph, but a
// defensive on-stack guard keeps a malformed input from recursing forever —
// it simply stops deepening along the cyclic edge rather than erroring, the
// same policy operationGraph's reducer uses for cycles.
export function computeNodeDepths(nodes: Node[], edges: Edge[]): Map<string, number> {
  const known = new Set(nodes.map((n) => n.id));
  const preds = new Map<string, string[]>();
  for (const e of edges) {
    if (!known.has(e.source) || !known.has(e.target)) continue;
    const list = preds.get(e.target);
    if (list) list.push(e.source);
    else preds.set(e.target, [e.source]);
  }

  const depth = new Map<string, number>();
  const onStack = new Set<string>();
  const depthOf = (id: string): number => {
    const cached = depth.get(id);
    if (cached !== undefined) return cached;
    const p = preds.get(id);
    if (!p || p.length === 0) {
      depth.set(id, 0);
      return 0;
    }
    if (onStack.has(id)) return 0; // cycle guard
    onStack.add(id);
    let d = 0;
    for (const src of p) d = Math.max(d, depthOf(src) + 1);
    onStack.delete(id);
    depth.set(id, d);
    return d;
  };
  for (const n of nodes) depthOf(n.id);
  return depth;
}

export function maxGraphDepth(nodes: Node[], edges: Edge[]): number {
  const depths = computeNodeDepths(nodes, edges);
  return depths.size ? Math.max(...depths.values()) : 0;
}

// dagre's nodesep spaces siblings within a rank, but its estimate of a rank's
// cross-axis extent comes from the SAME per-node height estimate that drives
// ranksep math — close enough for well-behaved graphs, not a hard guarantee
// once a rank mixes very different node heights (a deep chain's fan-in rank,
// e.g.). This pass runs last and is a straightforward sweep, not a dagre
// re-layout: group by shared rank (x, for LR), sort by y, and push down any
// node whose gap to its predecessor is under the floor. Nodes in the same
// rank are never linked by an edge that passes between them (dagre only
// edges across ranks), so widening the gap never crosses an edge.
export function enforceMinRankGap(nodes: Node[], minGap = 6): Node[] {
  const byX = new Map<number, Node[]>();
  for (const n of nodes) {
    const key = Math.round(n.position.x);
    const rank = byX.get(key);
    if (rank) rank.push(n);
    else byX.set(key, [n]);
  }

  const out: Node[] = [];
  for (const [, rank] of byX) {
    const sorted = [...rank].sort((a, b) => a.position.y - b.position.y);
    let cursor = -Infinity;
    for (const node of sorted) {
      const top = Math.max(node.position.y, cursor + minGap);
      const shifted =
        top === node.position.y ? node : { ...node, position: { ...node.position, y: top } };
      out.push(shifted);
      cursor = top + estimateNodeHeight(shifted);
    }
  }
  return out;
}

// An LR layout puts every target to the right of its source, so an edge that
// runs backwards can only be one the fold created: it leaves the end of a row
// and re-enters at the start of the next. That edge is still a real dependency
// in the graph, but on screen it is doing what the wrap at the end of a line of
// text does, and drawing it like the others makes the reader look for a
// dependency behind them. Detected from the final geometry rather than from the
// fold's own bookkeeping, so it describes what the edge actually does on the
// canvas whichever pass moved the nodes.
export function markContinuationEdges(nodes: Node[], edges: Edge[]): Edge[] {
  const xById = new Map<string, number>();
  for (const node of nodes) xById.set(node.id, node.position.x);

  let changed = false;
  const out = edges.map((edge) => {
    const sourceX = xById.get(edge.source);
    const targetX = xById.get(edge.target);
    // An endpoint the layout never placed says nothing about direction.
    if (sourceX === undefined || targetX === undefined) return edge;
    if (targetX >= sourceX) return edge;
    changed = true;
    return { ...edge, data: { ...(edge.data ?? {}), continuation: true } };
  });

  return changed ? out : edges;
}

export interface LayoutedGraph {
  nodes: Node[];
  edges: Edge[];
  height: number;
  /** node id -> longest-path depth (rank index), for edge rank-distance
   * styling (ConditionEdge's long-range smooth-step routing). */
  ranks: Map<string, number>;
  /** UNSCALED bounding-box width, for computeReservedHeight callers. */
  width: number;
}

// fitView is width-constrained for any graph wider than its container (true
// of almost every real run graph), so the zoom actually applied is clamped
// to what dagre's own container-fit contract already uses in WorkerCanvas:
// padding 0.15, max 1 (never zoomed in past 1:1 to fit). The floor is
// FIT_ZOOM_FLOOR below — defined here (not in WorkerCanvas.tsx, which
// imports from this module) so the reservation arithmetic and the canvas's
// actual fitView clamp can never diverge onto two different floors.
export const DAG_FIT_PADDING = 0.15;
export const DAG_MAX_ZOOM = 1;

// ─── Readability floor ───────────────────────────────────
//
// fitView shrinks the whole graph to fit the container, with no regard for
// whether the result is still legible. StepNode's smallest text (label,
// role, assignment, stats rows) all render at --t-xs (11px, theme.css) —
// ConditionEdge's condition chip matches. Below a 7px screen size even
// anti-aliased text stops being legible, so the floor is the zoom at which
// an 11px glyph lands on 7px: 7 / 11 = 0.636, rounded up to 0.65 for a small
// margin. Below the floor the canvas overflows its container instead of
// shrinking further; ReactFlow's own pan/zoom-out takes over from there.
export const FIT_ZOOM_FLOOR = 0.65;

// getLayoutedElements (below) reports the graph's UNSCALED bounding-box
// size. A container that reserves height at that value reserves for a shape
// the graph never actually draws: at the zoom fitView will actually apply —
// width-constrained and clamped to the same floor/ceiling WorkerCanvas uses
// — the rendered height is smaller. Given the bbox and the container width
// the panel will actually have, this returns the height that will actually
// render, so a wide graph does not leave the rest of its reserved panel
// empty.
export function computeReservedHeight(
  bboxWidth: number,
  bboxHeight: number,
  containerWidth: number,
): number {
  if (bboxWidth <= 0 || bboxHeight <= 0 || containerWidth <= 0) return bboxHeight;
  const fitZoom = containerWidth / (bboxWidth * (1 + DAG_FIT_PADDING));
  const zoom = Math.min(DAG_MAX_ZOOM, Math.max(FIT_ZOOM_FLOOR, fitZoom));
  return bboxHeight * zoom;
}

export function getLayoutedElements(
  nodes: Node[],
  edges: Edge[],
  direction: "LR" | "TB" = "LR",
): LayoutedGraph {
  const ranks = computeNodeDepths(nodes, edges);
  const maxDepth = ranks.size ? Math.max(...ranks.values()) : 0;

  const g = new dagre.graphlib.Graph();
  g.setDefaultEdgeLabel(() => ({}));
  g.setGraph({
    rankdir: direction,
    nodesep: NODE_SEP,
    ranksep: rankSepForDepth(maxDepth),
    marginx: 28,
    marginy: 24,
  });

  for (const node of nodes) {
    g.setNode(node.id, { width: NODE_WIDTH, height: estimateNodeHeight(node) });
  }
  // Rank assignment is ours (ADR-0113 D2), not dagre's: `ranks` above already
  // computed each node's ASAP depth. dagre's own rankers — network-simplex
  // included, which is the untouched default here — minimize total edge
  // length instead, which is a different objective and disagrees with ASAP
  // whenever a node's result is consumed much later than it is produced (the
  // P2 case). Rather than fight that objective with a `ranker` option (the
  // ADR is explicit that no ranker choice changes the outcome), pin every
  // edge's minlen to the exact ASAP rank gap between its endpoints. ASAP then
  // satisfies every constraint with equality, so its total edge span equals
  // the sum of the minlens, which is the lower bound any feasible assignment
  // can reach — and reaching it requires every edge to be tight, which only
  // ASAP is. dagre still does the ordering and routing, just not the ranking.
  //
  // Note what that argument rests on: ASAP is the unique MINIMUM-COST
  // assignment here, not the unique feasible one. Slack-free constraints do
  // not by themselves pin a solution — on the graph the decision was measured
  // on, {b:1, h:4} with the rest unchanged satisfies all nine constraints at a
  // total span of 15 against ASAP's 13. It loses on cost, not on legality. So
  // this depends on the ranker minimizing total edge length, which every dagre
  // ranker does. A future ranker with a different objective would need this
  // revisited rather than trusted.
  for (const edge of edges) {
    const sourceRank = ranks.get(edge.source);
    const targetRank = ranks.get(edge.target);
    if (sourceRank !== undefined && targetRank !== undefined) {
      g.setEdge(edge.source, edge.target, { minlen: Math.max(1, targetRank - sourceRank) });
      continue;
    }
    // An endpoint is missing from `ranks`, which happens exactly when the edge
    // names a node that never arrived: computeNodeDepths ranks every node it
    // was given and skips edges pointing outside that set. Skip the edge here
    // too, so the layout and the rank map describe the same graph.
    //
    // Handing it to dagre instead is worse than useless. graphlib's setEdge
    // CREATES an endpoint it has not seen, so the absent node gets a rank and
    // a slot of its own, and dagre pushes the real node that depends on it out
    // of the rank this function just computed — positions contradicting the
    // `ranks` map returned beside them, on behalf of a node nothing draws. It
    // also costs real time at scale, since dagre lays out every phantom.
    //
    // If a future change does need one of these to reach dagre, call setEdge
    // with no label rather than passing `undefined` for one: graphlib stores an
    // explicit `undefined` AS the label, and dagre dereferences it while
    // routing — "Cannot set properties of undefined (setting 'points')", which
    // takes the whole layout down.
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
  const wrappedNodes = direction === "LR" ? wrapWideRanks(layoutedNodes) : layoutedNodes;
  // Fold after wrapping, so a wrapped fan-out moves as one block rather than
  // having its grid columns split across two rows.
  const foldedNodes = direction === "LR" ? foldWideGraph(wrappedNodes, edges) : wrappedNodes;
  const finalNodes = enforceMinRankGap(foldedNodes);

  // Bounding-box height of the laid-out graph (post-wrap, post-gap), so
  // containers can size to what the layout actually needs — a linear
  // pipeline is one rank tall no matter how many nodes it has, and a wrapped
  // fan-out is exactly as tall as its grid.
  let top = Infinity;
  let bottom = -Infinity;
  let left = Infinity;
  let right = -Infinity;
  for (const node of finalNodes) {
    top = Math.min(top, node.position.y);
    bottom = Math.max(bottom, node.position.y + estimateNodeHeight(node));
    left = Math.min(left, node.position.x);
    right = Math.max(right, node.position.x + NODE_WIDTH);
  }
  const height = finalNodes.length === 0 ? 0 : bottom - top + 2 * 24;
  const width = finalNodes.length === 0 ? 0 : right - left + 2 * 28;

  return {
    nodes: finalNodes,
    edges: markContinuationEdges(finalNodes, edges),
    height,
    width,
    ranks,
  };
}

export function useAutoLayout() {
  return useCallback(
    (nodes: Node[], edges: Edge[], direction: "LR" | "TB" = "LR") =>
      getLayoutedElements(nodes, edges, direction),
    [],
  );
}
