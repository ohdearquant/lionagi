"use client";

import { useId, useMemo } from "react";
import type { WorkerLinkEdge, WorkerStepNode } from "@/lib/types";

export type WorkerDagProps = {
  nodes: WorkerStepNode[];
  edges: WorkerLinkEdge[];
};

// Layout constants
const NODE_W = 220;
const NODE_H = 72;
const X_GAP = 80;
const Y_GAP = 28;
const PAD = 32;

type PlacedNode = WorkerStepNode & { x: number; y: number };
type PlacedEdge = { source: PlacedNode; target: PlacedNode; edge: WorkerLinkEdge };

const ROLE_COLORS: Record<string, { bg: string; text: string; border: string }> = {
  researcher: { bg: "#172554", text: "#93c5fd", border: "#1e40af" },
  implementer: { bg: "#052e24", text: "#6ee7b7", border: "#065f46" },
  reviewer: { bg: "#4a044e", text: "#f0abfc", border: "#86198f" },
  critic: { bg: "#450a0a", text: "#fca5a5", border: "#991b1b" },
  suggester: { bg: "#451a03", text: "#fcd34d", border: "#92400e" },
  analyst: { bg: "#0c1a2e", text: "#7dd3fc", border: "#0369a1" },
  tester: { bg: "#052e16", text: "#86efac", border: "#166534" },
};

const DEFAULT_ROLE_COLOR = { bg: "#0a0a0a", text: "#a3a3a3", border: "#404040" };

function roleColor(role: string | undefined | null) {
  if (!role) return DEFAULT_ROLE_COLOR;
  return ROLE_COLORS[role.toLowerCase()] ?? DEFAULT_ROLE_COLOR;
}

function trim(value: string, max: number): string {
  return value.length > max ? `${value.slice(0, max - 1)}…` : value;
}

// Compute topological layers for top-to-bottom layout
function computeLayers(nodes: WorkerStepNode[], edges: WorkerLinkEdge[]): WorkerStepNode[][] {
  const ids = new Set(nodes.map((n) => n.id));
  const validEdges = edges.filter((e) => ids.has(e.source) && ids.has(e.target));

  const inDegree = new Map(nodes.map((n) => [n.id, 0]));
  const outAdj = new Map(nodes.map((n) => [n.id, [] as string[]]));

  validEdges.forEach((e) => {
    outAdj.get(e.source)?.push(e.target);
    inDegree.set(e.target, (inDegree.get(e.target) ?? 0) + 1);
  });

  const layerOf = new Map<string, number>();
  const queue = nodes.filter((n) => (inDegree.get(n.id) ?? 0) === 0).map((n) => n.id);
  const visited = new Set<string>();

  while (queue.length) {
    const id = queue.shift() as string;
    visited.add(id);
    const currentLayer = layerOf.get(id) ?? 0;
    (outAdj.get(id) ?? []).forEach((to) => {
      layerOf.set(to, Math.max(layerOf.get(to) ?? 0, currentLayer + 1));
      inDegree.set(to, (inDegree.get(to) ?? 1) - 1);
      if (inDegree.get(to) === 0) queue.push(to);
    });
  }

  // Fallback for nodes in cycles or disconnected
  const maxLayer = Math.max(0, ...Array.from(layerOf.values()));
  nodes.forEach((n) => {
    if (!visited.has(n.id)) layerOf.set(n.id, maxLayer + 1);
  });

  const layerCount = Math.max(0, ...Array.from(layerOf.values())) + 1;
  const layers: WorkerStepNode[][] = Array.from({ length: layerCount }, () => []);
  nodes.forEach((n) => {
    const l = layerOf.get(n.id) ?? 0;
    layers[l]?.push(n);
  });

  return layers.filter((l) => l.length > 0);
}

function layoutNodes(
  nodes: WorkerStepNode[],
  edges: WorkerLinkEdge[],
): { placed: Map<string, PlacedNode>; width: number; height: number } {
  if (nodes.length === 0) {
    return { placed: new Map(), width: 0, height: 0 };
  }

  const layers = computeLayers(nodes, edges);
  const cols = layers.length;
  const maxRows = Math.max(...layers.map((l) => l.length));

  const width = PAD * 2 + cols * NODE_W + Math.max(0, cols - 1) * X_GAP;
  const height = PAD * 2 + maxRows * NODE_H + Math.max(0, maxRows - 1) * Y_GAP;

  const placed = new Map<string, PlacedNode>();

  layers.forEach((layerNodes, colIndex) => {
    const colHeight = layerNodes.length * NODE_H + Math.max(0, layerNodes.length - 1) * Y_GAP;
    const startY = (height - colHeight) / 2;

    layerNodes.forEach((node, rowIndex) => {
      placed.set(node.id, {
        ...node,
        x: PAD + colIndex * (NODE_W + X_GAP),
        y: startY + rowIndex * (NODE_H + Y_GAP),
      });
    });
  });

  return { placed, width, height };
}

function edgePath(from: PlacedNode, to: PlacedNode): string {
  const x1 = from.x + NODE_W;
  const y1 = from.y + NODE_H / 2;
  const x2 = to.x;
  const y2 = to.y + NODE_H / 2;
  const bend = Math.max(40, Math.abs(x2 - x1) / 2);
  return `M ${x1} ${y1} C ${x1 + bend} ${y1}, ${x2 - bend} ${y2}, ${x2} ${y2}`;
}

export default function WorkerDag({ nodes, edges }: WorkerDagProps) {
  const markerId = useId().replace(/:/g, "");
  const dashedMarkerId = `${markerId}_d`;

  const { placed, width, height, placedEdges } = useMemo(() => {
    const layout = layoutNodes(nodes, edges);
    const pe: PlacedEdge[] = edges
      .map((edge) => {
        const src = layout.placed.get(edge.source);
        const tgt = layout.placed.get(edge.target);
        if (!src || !tgt) return null;
        return { source: src, target: tgt, edge };
      })
      .filter((e): e is PlacedEdge => e !== null);
    return { ...layout, placedEdges: pe };
  }, [nodes, edges]);

  if (nodes.length === 0) {
    return (
      <div className="border border-neutral-800 p-6 text-sm text-neutral-500">
        No steps defined.
      </div>
    );
  }

  return (
    <div className="overflow-x-auto border border-neutral-800 bg-neutral-950">
      <svg
        width={width}
        height={height}
        viewBox={`0 0 ${width} ${height}`}
        aria-label="Worker step DAG"
      >
        <defs>
          {/* Solid arrowhead for simple links */}
          <marker id={markerId} markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto">
            <path d="M 0 0 L 8 4 L 0 8 z" fill="#3b82f6" />
          </marker>
          {/* Dashed arrowhead for code links */}
          <marker
            id={dashedMarkerId}
            markerWidth="8"
            markerHeight="8"
            refX="7"
            refY="4"
            orient="auto"
          >
            <path d="M 0 0 L 8 4 L 0 8 z" fill="#f97316" />
          </marker>
        </defs>

        {/* Edges */}
        {placedEdges.map(({ source, target, edge }) => {
          const isCode = edge.mode === "code";
          const d = edgePath(source, target);
          const labelX = (source.x + NODE_W + target.x) / 2;
          const labelY = (source.y + target.y) / 2 + NODE_H / 2 - 6;

          return (
            <g key={edge.id}>
              <path
                d={d}
                fill="none"
                stroke={isCode ? "#f97316" : "#3b82f6"}
                strokeWidth="1.5"
                strokeDasharray={isCode ? "5,3" : undefined}
                markerEnd={`url(#${isCode ? dashedMarkerId : markerId})`}
              />
              {!isCode && edge.condition ? (
                <text x={labelX} y={labelY} fill="#6b7280" fontSize="10" textAnchor="middle">
                  {trim(edge.condition, 24)}
                </text>
              ) : null}
            </g>
          );
        })}

        {/* Nodes */}
        {Array.from(placed.values()).map((node) => {
          const colors = roleColor(node.role);
          const assignmentText = node.assignment ? trim(node.assignment, 28) : "";

          return (
            <g key={node.id} transform={`translate(${node.x} ${node.y})`}>
              <rect
                width={NODE_W}
                height={NODE_H}
                rx="6"
                fill="#111827"
                stroke="#374151"
                strokeWidth="1"
              />
              {/* Header bar */}
              <rect width={NODE_W} height="24" rx="6" fill="#1f2937" stroke="none" />
              <rect width={NODE_W} height="8" y="16" fill="#1f2937" />

              {/* Step name */}
              <text x="10" y="16" fill="#e5e7eb" fontSize="12" fontWeight="600">
                {trim(node.label || node.id, 22)}
              </text>

              {/* Role badge (top right) */}
              {node.role ? (
                <g>
                  <rect
                    x={NODE_W - 10 - Math.min(node.role.length, 12) * 6 - 8}
                    y="2"
                    width={Math.min(node.role.length, 12) * 6 + 8}
                    height="16"
                    rx="8"
                    fill={colors.bg}
                    stroke={colors.border}
                  />
                  <text
                    x={NODE_W - 14 - Math.min(node.role.length, 12) * 3}
                    y="14"
                    fill={colors.text}
                    fontSize="9"
                    fontWeight="500"
                    textAnchor="middle"
                  >
                    {trim(node.role, 12)}
                  </text>
                </g>
              ) : null}

              {/* Assignment */}
              <text x="10" y="44" fill="#9ca3af" fontSize="11">
                {assignmentText}
              </text>

              {/* Capacity / timeout indicators */}
              {node.capacity > 1 ? (
                <text x="10" y="62" fill="#6b7280" fontSize="9">
                  {`cap:${node.capacity}`}
                </text>
              ) : null}
              {node.timeout !== null ? (
                <text x={node.capacity > 1 ? 52 : 10} y="62" fill="#6b7280" fontSize="9">
                  {`t/o:${node.timeout}s`}
                </text>
              ) : null}
            </g>
          );
        })}
      </svg>
    </div>
  );
}
