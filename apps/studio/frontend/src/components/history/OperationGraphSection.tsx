import type { OperationGraphState, OperationNode, OperationStatus } from "@/lib/operationGraph";

// ── Status tokens (match RunDetail LANE_TONE) ─────────────────────────────────

const STATUS_BORDER: Record<OperationStatus, string> = {
  queued: "border-l-content-muted",
  running: "border-l-status-running",
  awaiting_approval: "border-l-status-warn",
  succeeded: "border-l-status-success",
  failed: "border-l-status-error",
  escalated: "border-l-status-error",
};

const STATUS_DOT: Record<OperationStatus, string> = {
  queued: "bg-content-muted",
  running: "bg-status-running",
  awaiting_approval: "bg-status-warn",
  succeeded: "bg-status-success",
  failed: "bg-status-error",
  escalated: "bg-status-error",
};

// ── Node card ─────────────────────────────────────────────────────────────────

function NodeCard({ node, live }: { node: OperationNode; live: boolean }) {
  const isPulsing = live && node.status === "running";

  return (
    <div
      className={`flex min-w-0 flex-col gap-1 rounded border border-edge border-l-2 bg-surface-raised px-2.5 py-2 shadow-card transition-opacity duration-150 ${STATUS_BORDER[node.status]}`}
    >
      <div className="flex items-center gap-1.5">
        <span className="relative flex h-2 w-2 shrink-0">
          {isPulsing && (
            <span
              className={`absolute inline-flex h-full w-full animate-ping rounded-full opacity-75 ${STATUS_DOT[node.status]}`}
            />
          )}
          <span
            className={`relative inline-flex h-2 w-2 rounded-full ${STATUS_DOT[node.status]}`}
          />
        </span>
        <span className="min-w-0 truncate text-[length:var(--t-xs)] font-semibold text-content-primary">
          {node.name || node.opId.slice(0, 8)}
        </span>
      </div>

      <div className="flex items-center gap-2">
        <span className="font-mono text-[length:var(--t-xs)] text-content-muted">
          {node.opId.slice(0, 8)}
        </span>
        {node.elapsed > 0 && (
          <span className="rounded bg-surface-overlay px-1 font-mono text-[length:var(--t-xs)] text-content-secondary">
            {node.elapsed < 1
              ? `${Math.round(node.elapsed * 1000)}ms`
              : `${node.elapsed.toFixed(1)}s`}
          </span>
        )}
      </div>
    </div>
  );
}

// ── Layer computation (longest-path layering over edges) ──────────────────────

// Depth is the longest path from any root, computed over ALL edges — not just a
// node's single causeOpId. Fan-in nodes (multiple predecessors, causeOpId null
// because parent_id was absent) must land after every predecessor, else their
// edges render backwards through the cards.
export function computeLayers(
  nodes: OperationNode[],
  edges: OperationGraphState["edges"],
): OperationNode[][] {
  if (nodes.length === 0) return [];

  const known = new Set(nodes.map((n) => n.opId));
  const predsByOp = new Map<string, string[]>();
  for (const e of edges) {
    if (!known.has(e.source) || !known.has(e.target)) continue;
    (predsByOp.get(e.target) ?? predsByOp.set(e.target, []).get(e.target)!).push(e.source);
  }

  const depthCache = new Map<string, number>();
  const onStack = new Set<string>();
  const depthOf = (op: string): number => {
    const cached = depthCache.get(op);
    if (cached !== undefined) return cached;
    const preds = predsByOp.get(op);
    if (!preds || preds.length === 0) {
      depthCache.set(op, 0);
      return 0;
    }
    if (onStack.has(op)) return 0; // cycle guard — graph should be acyclic
    onStack.add(op);
    let d = 0;
    for (const p of preds) d = Math.max(d, depthOf(p) + 1);
    onStack.delete(op);
    depthCache.set(op, d);
    return d;
  };

  const depths = nodes.map((n) => depthOf(n.opId));
  const maxDepth = Math.max(...depths);
  const layers: OperationNode[][] = Array.from({ length: maxDepth + 1 }, () => []);
  for (let i = 0; i < nodes.length; i++) {
    layers[depths[i]!]!.push(nodes[i]!);
  }
  return layers;
}

// ── Main component ────────────────────────────────────────────────────────────

export default function OperationGraphSection({
  state,
  live,
}: {
  state: OperationGraphState;
  live: boolean;
}) {
  const { nodes, edges } = state;

  if (nodes.length === 0) return null;

  const hasEdges = edges.length > 0;

  if (!hasEdges) {
    return (
      <div className="flex flex-wrap gap-2">
        {nodes.map((n) => (
          <div key={n.opId} className="w-44">
            <NodeCard node={n} live={live} />
          </div>
        ))}
      </div>
    );
  }

  const layers = computeLayers(nodes, edges);
  const colWidth = 176;
  const colGap = 40;
  const cardHeight = 60;
  const rowGap = 8;
  const totalWidth = layers.length * colWidth + Math.max(0, layers.length - 1) * colGap;
  const maxRows = Math.max(...layers.map((l) => l.length), 1);
  const totalHeight = maxRows * cardHeight + Math.max(0, maxRows - 1) * rowGap;

  const cardCenterX = (colIdx: number) => colIdx * (colWidth + colGap) + colWidth / 2;
  const cardCenterY = (rowIdx: number, totalRows: number) => {
    const totalUsed = totalRows * cardHeight + Math.max(0, totalRows - 1) * rowGap;
    const startY = (totalHeight - totalUsed) / 2;
    return startY + rowIdx * (cardHeight + rowGap) + cardHeight / 2;
  };

  const opToLayerCol = new Map<string, { col: number; row: number }>();
  layers.forEach((layer, col) => {
    layer.forEach((node, row) => {
      opToLayerCol.set(node.opId, { col, row });
    });
  });

  return (
    <div className="overflow-x-auto">
      <div className="relative inline-block" style={{ width: totalWidth, height: totalHeight }}>
        <svg
          className="pointer-events-none absolute inset-0"
          width={totalWidth}
          height={totalHeight}
          viewBox={`0 0 ${totalWidth} ${totalHeight}`}
        >
          {edges.map((edge) => {
            const src = opToLayerCol.get(edge.source);
            const tgt = opToLayerCol.get(edge.target);
            if (!src || !tgt) return null;
            const srcLayer = layers[src.col];
            const tgtLayer = layers[tgt.col];
            if (!srcLayer || !tgtLayer) return null;
            const x1 = cardCenterX(src.col) + colWidth / 2;
            const y1 = cardCenterY(src.row, srcLayer.length);
            const x2 = cardCenterX(tgt.col) - colWidth / 2;
            const y2 = cardCenterY(tgt.row, tgtLayer.length);
            const mx = (x1 + x2) / 2;
            return (
              <path
                key={`${edge.source}-${edge.target}`}
                d={`M ${x1} ${y1} C ${mx} ${y1}, ${mx} ${y2}, ${x2} ${y2}`}
                fill="none"
                stroke="currentColor"
                strokeWidth={1.5}
                className="text-edge"
                strokeOpacity={0.5}
              />
            );
          })}
        </svg>

        {layers.map((layer, col) =>
          layer.map((node, row) => {
            const x = col * (colWidth + colGap);
            const totalUsed = layer.length * cardHeight + Math.max(0, layer.length - 1) * rowGap;
            const startY = (totalHeight - totalUsed) / 2;
            const y = startY + row * (cardHeight + rowGap);
            return (
              <div
                key={node.opId}
                className="absolute"
                style={{ left: x, top: y, width: colWidth, height: cardHeight }}
              >
                <NodeCard node={node} live={live} />
              </div>
            );
          }),
        )}
      </div>
    </div>
  );
}
