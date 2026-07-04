/**
 * Project a WorkflowSpec onto the FlowModel shape that FlowCanvas expects.
 * Node positions come from spec.nodes[].pos (user-authored, no auto-layout).
 * Signals are derived from edge labels; unlabeled edges use the neutral handoff color.
 */
import type { WorkflowSpec } from "@/lib/api";
import type { FlowModel, FlowNode, FlowEdge } from "@/lib/designer/flow";
import { SIGNAL_PALETTE } from "@/lib/designer/flow";
import { bezierPath } from "./bezier";

const NODE_W = 192;
const NODE_H = 64;

/** Map from workflow node kind to a display type label. */
const KIND_LABEL: Record<string, string> = {
  input: "input",
  chat: "chat",
  parse: "parse",
  fanout: "fanout",
  engine: "engine",
  gate: "gate",
};

export function specToFlowModel(spec: WorkflowSpec): FlowModel {
  if (!spec.nodes.length) {
    return {
      nodes: [],
      edges: [],
      signals: [],
      width: 400,
      height: 300,
      spawnRules: {},
      observes: {},
      signalColor: {},
    };
  }

  // Collect unique edge labels to assign signal colors
  const labelOrder: string[] = [];
  for (const e of spec.edges) {
    if (e.label && !labelOrder.includes(e.label)) labelOrder.push(e.label);
  }
  const signalColor: Record<string, string> = {};
  labelOrder.forEach((l, i) => {
    signalColor[l] = SIGNAL_PALETTE[i % SIGNAL_PALETTE.length];
  });

  const nodes: FlowNode[] = spec.nodes.map((n) => ({
    kind: "op" as const,
    id: n.id,
    stages: [],
    typeLabel: KIND_LABEL[n.kind] ?? n.kind,
    x: n.pos.x,
    y: n.pos.y,
    w: NODE_W,
    h: NODE_H,
    layer: 0,
    row: 0,
    inPorts: [],
    outPorts: [],
  }));

  const nodeById = new Map(nodes.map((n) => [n.id, n]));

  const edges: FlowEdge[] = spec.edges.map((e, i) => {
    const a = nodeById.get(e.from);
    const b = nodeById.get(e.to);
    const color = e.label ? (signalColor[e.label] ?? "var(--edge-strong)") : "var(--edge-strong)";

    if (!a || !b) {
      return {
        id: `e${i}`,
        from: e.from,
        to: e.to,
        kind: "forward" as const,
        signal: e.label,
        color,
        path: "",
        chip: { x: 0, y: 0 },
        arrow: { x: 0, y: 0, dir: "right" as const },
      };
    }

    const x1 = a.x + a.w;
    const y1 = a.y + a.h / 2;
    const x2 = b.x;
    const y2 = b.y + b.h / 2;
    const path = bezierPath(x1, y1, x2, y2);
    const chipX = (x1 + x2) / 2;
    const chipY = (y1 + y2) / 2 - 9;

    return {
      id: `e${i}`,
      from: e.from,
      to: e.to,
      kind: "forward" as const,
      signal: e.label,
      color,
      path,
      chip: { x: chipX, y: chipY },
      arrow: { x: x2, y: y2, dir: "right" as const },
    };
  });

  // Compute canvas extents from node positions
  let maxX = 400;
  let maxY = 300;
  for (const n of nodes) {
    maxX = Math.max(maxX, n.x + n.w + 80);
    maxY = Math.max(maxY, n.y + n.h + 80);
  }

  const signals = labelOrder.map((name) => ({
    name,
    color: signalColor[name],
    emitters: spec.edges.filter((e) => e.label === name).map((e) => e.from),
    observers: spec.edges.filter((e) => e.label === name).map((e) => e.to),
  }));

  return {
    nodes,
    edges,
    signals,
    width: maxX,
    height: maxY,
    spawnRules: {},
    observes: {},
    signalColor,
  };
}
