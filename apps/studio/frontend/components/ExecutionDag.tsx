"use client";

import { useState } from "react";
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
  onNodeClick?: (nodeId: string) => void;
  direction?: "vertical" | "horizontal";
}

// Status palette — resolved via CSS vars set in globals.css per theme
const STATUS: Record<StepStatus, { bg: string; border: string; label: string; dot: string }> = {
  pending: {
    bg: "var(--dag-pending-bg)",
    border: "var(--dag-pending-border)",
    label: "var(--dag-pending-label)",
    dot: "var(--dag-pending-dot)",
  },
  running: {
    bg: "var(--dag-running-bg)",
    border: "var(--dag-running-border)",
    label: "var(--dag-running-label)",
    dot: "var(--dag-running-dot)",
  },
  completed: {
    bg: "var(--dag-completed-bg)",
    border: "var(--dag-completed-border)",
    label: "var(--dag-completed-label)",
    dot: "var(--dag-completed-dot)",
  },
  failed: {
    bg: "var(--dag-failed-bg)",
    border: "var(--dag-failed-border)",
    label: "var(--dag-failed-label)",
    dot: "var(--dag-failed-dot)",
  },
};

// Role tags — muted, low-saturation tones only
const ROLE_COLOR: Record<string, string> = {
  researcher: "#5a7a5a",
  explorer: "#5a7a5a",
  implementer: "#4a6080",
  reviewer: "#4a7070",
  critic: "#7a6040",
  analyst: "#5a5880",
  architect: "#3d6878",
  orchestrator: "#6a4a78",
  tester: "#3d7060",
};

const DEFAULT_ROLE_COLOR = "#4a4a4a";

const NODE_W = 180;
const NODE_H = 46;
const GAP_X = 22;
const GAP_Y = 52;
const PAD_X = 28;
const PAD_Y = 20;

// Topological layer assignment (Kahn's algorithm)
function buildLayers(nodes: WorkerStepNode[], edges: WorkerLinkEdge[]): string[][] {
  const adj: Record<string, Set<string>> = {};
  const inDeg: Record<string, number> = {};

  for (const n of nodes) {
    adj[n.id] = new Set();
    inDeg[n.id] = 0;
  }
  for (const e of edges) {
    if (adj[e.source] && !adj[e.source].has(e.target)) {
      adj[e.source].add(e.target);
      inDeg[e.target] = (inDeg[e.target] ?? 0) + 1;
    }
  }

  const layers: string[][] = [];
  const placed = new Set<string>();
  let frontier = nodes.filter((n) => (inDeg[n.id] ?? 0) === 0).map((n) => n.id);

  while (frontier.length > 0 && layers.length < 50) {
    layers.push(frontier);
    frontier.forEach((id) => placed.add(id));

    const next: string[] = [];
    for (const id of frontier) {
      for (const child of Array.from(adj[id] ?? [])) {
        if (!placed.has(child) && !next.includes(child)) {
          const ready = nodes.every(
            (n) => !adj[n.id]?.has(child) || placed.has(n.id) || frontier.includes(n.id),
          );
          if (ready) next.push(child);
        }
      }
    }
    frontier = next;
  }

  // Straggler nodes (cycles or disconnected)
  const unplaced = nodes.filter((n) => !placed.has(n.id)).map((n) => n.id);
  if (unplaced.length > 0) {
    if (layers.length === 0) layers.push([]);
    layers[layers.length - 1].push(...unplaced);
  }

  return layers;
}

// Vertical: layers stack top-to-bottom, members spread horizontally.
function buildPositions(
  layers: string[][],
  totalW: number,
): Record<string, { x: number; y: number }> {
  const pos: Record<string, { x: number; y: number }> = {};
  layers.forEach((layer, li) => {
    const layerW = layer.length * (NODE_W + GAP_X) - GAP_X;
    const offsetX = (totalW - layerW) / 2;
    const y = PAD_Y + li * (NODE_H + GAP_Y);
    layer.forEach((id, ni) => {
      pos[id] = { x: offsetX + ni * (NODE_W + GAP_X), y };
    });
  });
  return pos;
}

// Horizontal: layers stack left-to-right, members spread vertically within each layer.
function buildPositionsHoriz(
  layers: string[][],
  totalH: number,
): Record<string, { x: number; y: number }> {
  const pos: Record<string, { x: number; y: number }> = {};
  layers.forEach((layer, li) => {
    const layerH = layer.length * (NODE_H + GAP_Y) - GAP_Y;
    const offsetY = (totalH - layerH) / 2;
    const x = PAD_X + li * (NODE_W + GAP_X);
    layer.forEach((id, ni) => {
      pos[id] = { x, y: offsetY + ni * (NODE_H + GAP_Y) };
    });
  });
  return pos;
}

export default function ExecutionDag({
  nodes,
  edges,
  executionSteps,
  currentStep,
  onNodeClick,
  direction = "vertical",
}: Props) {
  const [hoveredNode, setHoveredNode] = useState<string | null>(null);
  const [selectedNode, setSelectedNode] = useState<string | null>(null);
  const [tooltipNode, setTooltipNode] = useState<{ id: string; x: number; y: number } | null>(null);

  const completedSet = new Set(
    executionSteps.filter((s) => s.status === "completed").map((s) => s.step),
  );
  const failedSet = new Set(executionSteps.filter((s) => s.status === "failed").map((s) => s.step));

  function getStatus(nodeId: string): StepStatus {
    if (nodeId === currentStep) return "running";
    if (completedSet.has(nodeId)) return "completed";
    if (failedSet.has(nodeId)) return "failed";
    return "pending";
  }

  // Compact single-node layout: full SVG with viewBox stretching wastes
  // horizontal space when there is only one step. Render a focused card.
  if (nodes.length === 1) {
    const node = nodes[0];
    const status = getStatus(node.id);
    const colors = STATUS[status];
    const roleColor = ROLE_COLOR[node.role] ?? DEFAULT_ROLE_COLOR;
    const isClickable = !!onNodeClick;

    return (
      <div className="rounded border border-edge bg-surface-base p-3">
        <div className="flex items-center gap-3">
          <button
            type="button"
            disabled={!isClickable}
            onClick={() => onNodeClick?.(node.id)}
            className="flex min-w-[200px] max-w-[280px] items-center justify-between rounded-md border px-3 py-2 text-left transition-colors disabled:cursor-default"
            style={{
              backgroundColor: colors.bg,
              borderColor: colors.border,
              cursor: isClickable ? "pointer" : "default",
            }}
          >
            <div className="flex min-w-0 flex-col">
              <span
                className="truncate font-mono text-body font-medium"
                style={{ color: colors.label }}
              >
                {node.label}
              </span>
              {node.role && (
                <span
                  className="truncate font-mono text-meta"
                  style={{ color: roleColor, opacity: 0.9 }}
                >
                  {node.role}
                </span>
              )}
            </div>
            <span
              className="ml-3 inline-block h-2 w-2 shrink-0 rounded-full"
              style={{
                backgroundColor: colors.dot,
                opacity: status === "pending" ? 0.35 : 0.9,
              }}
            />
          </button>
          <span className="text-meta uppercase tracking-[0.06em] text-content-muted">
            Single step · {status}
          </span>
        </div>
      </div>
    );
  }

  const layers = buildLayers(nodes, edges);
  const maxLayerCount = Math.max(...layers.map((l) => l.length), 1);
  const isHoriz = direction === "horizontal";
  const svgW = isHoriz
    ? Math.max(layers.length * (NODE_W + GAP_X) - GAP_X + PAD_X * 2, 280)
    : Math.max(maxLayerCount * (NODE_W + GAP_X) - GAP_X + PAD_X * 2, 280);
  const svgH = isHoriz
    ? Math.max(maxLayerCount * (NODE_H + GAP_Y) - GAP_Y + PAD_Y * 2, 80)
    : Math.max(layers.length * (NODE_H + GAP_Y) - GAP_Y + PAD_Y * 2, 100);
  const pos = isHoriz ? buildPositionsHoriz(layers, svgH) : buildPositions(layers, svgW);

  // Running edge: dashes for in-progress source→target
  const isEdgeActive = (e: WorkerLinkEdge) => e.source === currentStep || e.target === currentStep;

  return (
    <div
      className="rounded border border-edge bg-surface-base"
      style={{
        maxHeight: isHoriz ? svgH + 4 : 350,
        overflowY: "auto",
        overflowX: "auto",
        position: "relative",
      }}
    >
      <svg
        viewBox={`0 0 ${svgW} ${svgH}`}
        width={isHoriz ? svgW : "100%"}
        height={svgH}
        style={{ display: "block", minWidth: svgW }}
      >
        <defs>
          {/* Pending edge arrowhead */}
          <marker id="arr-pending" markerWidth="7" markerHeight="6" refX="6" refY="3" orient="auto">
            <path d="M0,0 L7,3 L0,6 Z" fill="var(--dag-edge-pending)" />
          </marker>
          {/* Completed edge arrowhead */}
          <marker id="arr-done" markerWidth="7" markerHeight="6" refX="6" refY="3" orient="auto">
            <path d="M0,0 L7,3 L0,6 Z" fill="var(--dag-edge-done)" opacity="0.7" />
          </marker>
          {/* Running edge arrowhead */}
          <marker id="arr-running" markerWidth="7" markerHeight="6" refX="6" refY="3" orient="auto">
            <path d="M0,0 L7,3 L0,6 Z" fill="var(--dag-running-border)" />
          </marker>
        </defs>

        {/* Edges */}
        {edges.map((e, i) => {
          const from = pos[e.source];
          const to = pos[e.target];
          if (!from || !to) return null;

          const done = completedSet.has(e.source);
          const active = isEdgeActive(e);
          let path: string;
          if (isHoriz) {
            const x1 = from.x + NODE_W;
            const y1 = from.y + NODE_H / 2;
            const x2 = to.x;
            const y2 = to.y + NODE_H / 2;
            const cx = (x1 + x2) / 2;
            path = `M ${x1} ${y1} C ${cx} ${y1}, ${cx} ${y2}, ${x2} ${y2}`;
          } else {
            const x1 = from.x + NODE_W / 2;
            const y1 = from.y + NODE_H;
            const x2 = to.x + NODE_W / 2;
            const y2 = to.y;
            const cy = (y1 + y2) / 2;
            path = `M ${x1} ${y1} C ${x1} ${cy}, ${x2} ${cy}, ${x2} ${y2}`;
          }

          const stroke = active
            ? "var(--dag-running-border)"
            : done
              ? "var(--dag-edge-done)"
              : "var(--dag-edge-pending)";
          const strokeWidth = done || active ? 2 : 1.5;
          const markerEnd = active
            ? "url(#arr-running)"
            : done
              ? "url(#arr-done)"
              : "url(#arr-pending)";

          return (
            <path
              key={i}
              d={path}
              fill="none"
              stroke={stroke}
              strokeWidth={strokeWidth}
              strokeOpacity={done ? 0.65 : 1}
              strokeDasharray={active ? "5 3" : undefined}
              markerEnd={markerEnd}
            >
              {active && (
                <animate
                  attributeName="strokeDashoffset"
                  from="0"
                  to="-16"
                  dur="0.6s"
                  repeatCount="indefinite"
                />
              )}
            </path>
          );
        })}

        {/* Nodes */}
        {nodes.map((node) => {
          const p = pos[node.id];
          if (!p) return null;

          const status = getStatus(node.id);
          const colors = STATUS[status];
          const isHovered = hoveredNode === node.id;
          const isSelected = selectedNode === node.id;
          const roleColor = ROLE_COLOR[node.role] ?? DEFAULT_ROLE_COLOR;

          // Label: wider node → more room, cap at 22 chars
          const maxLabelChars = 22;
          const label =
            node.label.length > maxLabelChars
              ? node.label.slice(0, maxLabelChars - 1) + "…"
              : node.label;

          // Status indicator: small filled circle, top-right corner
          const DOT_R = 4;
          const dotCX = p.x + NODE_W - DOT_R - 6;
          const dotCY = p.y + DOT_R + 5;

          // Status icon text (✓ / × / … inside dot area)
          const statusGlyph = status === "completed" ? "✓" : status === "failed" ? "×" : null;

          return (
            <g
              key={node.id}
              style={{
                cursor: onNodeClick ? "pointer" : "default",
                transform: isHovered || isSelected ? `translate(0px, -1px)` : undefined,
                transition: "transform 0.1s ease",
              }}
              onClick={() => {
                setSelectedNode(node.id);
                onNodeClick?.(node.id);
              }}
              onMouseEnter={(evt) => {
                setHoveredNode(node.id);
                const svg = (evt.currentTarget as SVGGElement).closest("svg");
                const svgRect = svg?.getBoundingClientRect();
                setTooltipNode(svgRect ? { id: node.id, x: p.x + NODE_W / 2, y: p.y } : null);
              }}
              onMouseLeave={() => {
                setHoveredNode(null);
                setTooltipNode(null);
              }}
            >
              {/* Drop shadow on hover/select */}
              {(isHovered || isSelected) && (
                <rect
                  x={p.x + 1}
                  y={p.y + 2}
                  width={NODE_W}
                  height={NODE_H}
                  rx={6}
                  fill="rgba(0,0,0,0.25)"
                />
              )}

              {/* Node body */}
              <rect
                x={p.x}
                y={p.y}
                width={NODE_W}
                height={NODE_H}
                rx={6}
                fill={colors.bg}
                stroke={
                  isSelected
                    ? "var(--dag-hover-border)"
                    : isHovered
                      ? "var(--dag-hover-border)"
                      : colors.border
                }
                strokeWidth={isSelected ? 2 : isHovered ? 1.5 : 1}
              />

              {/* Running pulse overlay */}
              {status === "running" && (
                <rect
                  x={p.x}
                  y={p.y}
                  width={NODE_W}
                  height={NODE_H}
                  rx={6}
                  fill="none"
                  stroke={colors.border}
                  strokeWidth={1.5}
                  opacity={0.3}
                >
                  <animate
                    attributeName="opacity"
                    values="0.3;0.05;0.3"
                    dur="1.6s"
                    repeatCount="indefinite"
                  />
                </rect>
              )}

              {/* Step name — primary, mono, left-padded */}
              <text
                x={p.x + 12}
                y={p.y + 18}
                fontSize={11}
                fontWeight={500}
                fontFamily="ui-monospace, 'Cascadia Code', 'Fira Code', Menlo, monospace"
                fill={colors.label}
              >
                {label}
              </text>

              {/* Role tag — small, color-coded, secondary line */}
              {node.role && (
                <text
                  x={p.x + 12}
                  y={p.y + 32}
                  fontSize={9}
                  fontFamily="ui-monospace, 'Cascadia Code', 'Fira Code', Menlo, monospace"
                  fill={roleColor}
                  opacity={0.9}
                >
                  {node.role}
                </text>
              )}

              {/* Status dot — top-right corner, filled circle */}
              <circle
                cx={dotCX}
                cy={dotCY}
                r={DOT_R}
                fill={colors.dot}
                opacity={status === "pending" ? 0.35 : 0.9}
              >
                {status === "running" && (
                  <animate
                    attributeName="r"
                    values={`${DOT_R};${DOT_R + 1.5};${DOT_R}`}
                    dur="1.2s"
                    repeatCount="indefinite"
                  />
                )}
              </circle>

              {/* Status glyph inside dot (✓ / ×) */}
              {statusGlyph && (
                <text
                  x={dotCX}
                  y={dotCY + 3.5}
                  fontSize={6}
                  fontWeight={700}
                  textAnchor="middle"
                  fill="rgba(0,0,0,0.65)"
                  style={{ pointerEvents: "none", userSelect: "none" }}
                >
                  {statusGlyph}
                </text>
              )}
            </g>
          );
        })}

        {/* Tooltip — rendered last so it's on top */}
        {tooltipNode &&
          (() => {
            const node = nodes.find((n) => n.id === tooltipNode.id);
            const p = node ? pos[node.id] : null;
            if (!node || !p) return null;
            const assignTail = node.assignment ? (node.assignment.split("/").pop() ?? "") : null;
            const tipW = 160;
            const tipH = assignTail ? 46 : 34;
            const tipX = Math.min(p.x, svgW - tipW - 4);
            const tipY = p.y - tipH - 6;
            return (
              <g style={{ pointerEvents: "none" }}>
                <rect
                  x={tipX}
                  y={Math.max(tipY, 2)}
                  width={tipW}
                  height={tipH}
                  rx={4}
                  fill="var(--surface-overlay, #1e1e2e)"
                  stroke="var(--dag-hover-border)"
                  strokeWidth={1}
                  opacity={0.97}
                />
                <text
                  x={tipX + 8}
                  y={Math.max(tipY, 2) + 14}
                  fontSize={10}
                  fontWeight={600}
                  fontFamily="ui-monospace, 'Cascadia Code', monospace"
                  fill="var(--content-primary, #e2e2e9)"
                >
                  {node.label}
                </text>
                {node.role && (
                  <text
                    x={tipX + 8}
                    y={Math.max(tipY, 2) + 27}
                    fontSize={9}
                    fontFamily="ui-monospace, 'Cascadia Code', monospace"
                    fill={ROLE_COLOR[node.role] ?? DEFAULT_ROLE_COLOR}
                    opacity={0.9}
                  >
                    {node.role}
                  </text>
                )}
                {assignTail && (
                  <text
                    x={tipX + 8}
                    y={Math.max(tipY, 2) + 40}
                    fontSize={8}
                    fontFamily="ui-monospace, 'Cascadia Code', monospace"
                    fill="var(--content-muted, #888)"
                  >
                    {assignTail.length > 20 ? assignTail.slice(0, 19) + "…" : assignTail}
                  </text>
                )}
              </g>
            );
          })()}
      </svg>
    </div>
  );
}
