"use client";

import { useEffect, useRef } from "react";
import type { WorkerStepNode, WorkerLinkEdge } from "@/lib/types";

type StepStatus = "pending" | "running" | "completed" | "failed";

export interface ExecutionStep {
  step: string;
  status: StepStatus;
  result?: Record<string, unknown>;
  timestamp?: number | null;
}

interface Props {
  nodes: WorkerStepNode[];
  edges: WorkerLinkEdge[];
  executionSteps: ExecutionStep[];
  currentStep: string | null;
}

const STATUS_COLORS: Record<StepStatus, { fill: string; stroke: string; text: string }> = {
  pending: { fill: "#1a1a2e", stroke: "#333", text: "#555" },
  running: { fill: "#1a1a3e", stroke: "#60a5fa", text: "#93c5fd" },
  completed: { fill: "#0a2e1a", stroke: "#22c55e", text: "#86efac" },
  failed: { fill: "#2e0a0a", stroke: "#ef4444", text: "#fca5a5" },
};

const ROLE_COLORS: Record<string, string> = {
  researcher: "#22c55e",
  implementer: "#a855f7",
  reviewer: "#14b8a6",
  critic: "#f59e0b",
};

export default function ExecutionDag({ nodes, edges, executionSteps, currentStep }: Props) {
  const svgRef = useRef<SVGSVGElement>(null);

  const completedSteps = new Set(
    executionSteps.filter((s) => s.status === "completed").map((s) => s.step),
  );

  function getStatus(nodeId: string): StepStatus {
    if (nodeId === currentStep) return "running";
    if (completedSteps.has(nodeId)) return "completed";
    const failed = executionSteps.find((s) => s.step === nodeId && s.status === "failed");
    if (failed) return "failed";
    return "pending";
  }

  const NODE_W = 200;
  const NODE_H = 70;
  const GAP_X = 80;
  const GAP_Y = 30;
  const PAD = 40;

  const adj: Record<string, Set<string>> = {};
  const inDeg: Record<string, number> = {};
  for (const n of nodes) {
    adj[n.id] = new Set();
    inDeg[n.id] = 0;
  }
  for (const e of edges) {
    if (adj[e.source] && !adj[e.source].has(e.target)) {
      adj[e.source].add(e.target);
      inDeg[e.target] = (inDeg[e.target] || 0) + 1;
    }
  }

  const layers: string[][] = [];
  const placed = new Set<string>();
  let frontier = nodes.filter((n) => (inDeg[n.id] || 0) === 0).map((n) => n.id);
  while (frontier.length > 0) {
    layers.push(frontier);
    frontier.forEach((id) => placed.add(id));
    const next: string[] = [];
    for (const id of frontier) {
      for (const child of Array.from(adj[id] || [])) {
        if (!placed.has(child) && !next.includes(child)) {
          const allParentsPlaced = nodes.every(
            (n) => !adj[n.id]?.has(child) || placed.has(n.id) || frontier.includes(n.id),
          );
          if (allParentsPlaced) next.push(child);
        }
      }
    }
    frontier = next;
    if (layers.length > 50) break;
  }
  for (const n of nodes) {
    if (!placed.has(n.id)) {
      if (layers.length === 0) layers.push([]);
      layers[layers.length - 1].push(n.id);
    }
  }

  const pos: Record<string, { x: number; y: number }> = {};
  const maxLayerH = Math.max(...layers.map((l) => l.length));
  const totalW = layers.length * (NODE_W + GAP_X) - GAP_X + PAD * 2;
  const totalH = maxLayerH * (NODE_H + GAP_Y) - GAP_Y + PAD * 2;

  layers.forEach((layer, li) => {
    const lx = PAD + li * (NODE_W + GAP_X);
    const layerH = layer.length * (NODE_H + GAP_Y) - GAP_Y;
    const offsetY = (totalH - layerH) / 2;
    layer.forEach((id, ni) => {
      pos[id] = { x: lx, y: offsetY + ni * (NODE_H + GAP_Y) };
    });
  });

  const svgW = Math.max(totalW, 400);
  const svgH = Math.max(totalH, 200);

  return (
    <div className="overflow-auto rounded border border-neutral-800 bg-neutral-950">
      <svg
        ref={svgRef}
        viewBox={`0 0 ${svgW} ${svgH}`}
        width={svgW}
        height={svgH}
        className="block"
      >
        <defs>
          <marker id="exec-arrow" markerWidth="8" markerHeight="6" refX="8" refY="3" orient="auto">
            <polygon points="0 0, 8 3, 0 6" fill="#555" />
          </marker>
          <marker
            id="exec-arrow-active"
            markerWidth="8"
            markerHeight="6"
            refX="8"
            refY="3"
            orient="auto"
          >
            <polygon points="0 0, 8 3, 0 6" fill="#22c55e" />
          </marker>
        </defs>

        {edges.map((e, i) => {
          const from = pos[e.source];
          const to = pos[e.target];
          if (!from || !to) return null;

          const sourceCompleted = completedSteps.has(e.source);
          const x1 = from.x + NODE_W;
          const y1 = from.y + NODE_H / 2;
          const x2 = to.x;
          const y2 = to.y + NODE_H / 2;

          const mx = (x1 + x2) / 2;
          const path = `M ${x1} ${y1} C ${mx} ${y1}, ${mx} ${y2}, ${x2} ${y2}`;

          return (
            <path
              key={i}
              d={path}
              fill="none"
              stroke={sourceCompleted ? "#22c55e" : "#333"}
              strokeWidth={sourceCompleted ? 2 : 1}
              strokeDasharray={e.mode === "code" ? "6 4" : undefined}
              markerEnd={sourceCompleted ? "url(#exec-arrow-active)" : "url(#exec-arrow)"}
              style={{ transition: "stroke 0.5s, stroke-width 0.5s" }}
            />
          );
        })}

        {nodes.map((node) => {
          const p = pos[node.id];
          if (!p) return null;
          const status = getStatus(node.id);
          const colors = STATUS_COLORS[status];
          const roleColor = ROLE_COLORS[node.role] || "#666";

          return (
            <g key={node.id} style={{ transition: "opacity 0.3s" }}>
              <rect
                x={p.x}
                y={p.y}
                width={NODE_W}
                height={NODE_H}
                rx={6}
                fill={colors.fill}
                stroke={colors.stroke}
                strokeWidth={status === "running" ? 2 : 1}
              />

              {status === "running" && (
                <rect
                  x={p.x}
                  y={p.y}
                  width={NODE_W}
                  height={NODE_H}
                  rx={6}
                  fill="none"
                  stroke={colors.stroke}
                  strokeWidth={2}
                  opacity={0.5}
                >
                  <animate
                    attributeName="opacity"
                    values="0.5;0.1;0.5"
                    dur="1.5s"
                    repeatCount="indefinite"
                  />
                </rect>
              )}

              {status === "completed" && (
                <text
                  x={p.x + NODE_W - 16}
                  y={p.y + 16}
                  fontSize={14}
                  fill="#22c55e"
                  textAnchor="middle"
                >
                  ✓
                </text>
              )}

              {status === "failed" && (
                <text
                  x={p.x + NODE_W - 16}
                  y={p.y + 16}
                  fontSize={14}
                  fill="#ef4444"
                  textAnchor="middle"
                >
                  ✗
                </text>
              )}

              <text
                x={p.x + 10}
                y={p.y + 20}
                fontSize={13}
                fontWeight={600}
                fontFamily="monospace"
                fill={colors.text}
              >
                {node.label}
              </text>

              {node.role && (
                <rect
                  x={p.x + NODE_W - 70}
                  y={p.y + 28}
                  width={60}
                  height={16}
                  rx={8}
                  fill={roleColor}
                  opacity={0.2}
                />
              )}
              {node.role && (
                <text
                  x={p.x + NODE_W - 40}
                  y={p.y + 40}
                  fontSize={9}
                  fill={roleColor}
                  textAnchor="middle"
                  fontFamily="monospace"
                >
                  {node.role}
                </text>
              )}

              <text
                x={p.x + 10}
                y={p.y + 45}
                fontSize={10}
                fontFamily="monospace"
                fill={status === "pending" ? "#444" : "#888"}
              >
                {node.assignment}
              </text>

              {status === "completed" && (
                <text x={p.x + 10} y={p.y + 60} fontSize={9} fontFamily="monospace" fill="#4a9">
                  done
                </text>
              )}
            </g>
          );
        })}
      </svg>
    </div>
  );
}
