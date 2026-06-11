import { useCallback, useMemo } from "react";
import ReactFlow, { Background, Controls, MarkerType } from "reactflow";
import type { Edge, Node, NodeMouseHandler } from "reactflow";
import "reactflow/dist/style.css";
import { getLayoutedElements } from "@/components/canvas/useLayout";
import type { ShowDetail } from "@/lib/types";

type Play = ShowDetail["plays"][number];

interface PlayDagProps {
  plays: Play[];
  showMd?: string | null;
  onNodeClick?: (playName: string) => void;
}

function parseShowMdDeps(showMd: string | null | undefined): Map<string, string[]> {
  const deps = new Map<string, string[]>();
  if (!showMd) return deps;

  let currentPlay: string | null = null;
  for (const line of showMd.split("\n")) {
    const playMatch = line.match(/\*\*([^*]+)\*\*/);
    if (playMatch) {
      currentPlay = playMatch[1].trim();
    }
    if (currentPlay) {
      const depsMatch = line.match(/deps:\s*\[([^\]]*)\]/);
      if (depsMatch) {
        const list = depsMatch[1]
          .split(",")
          .map((d) => d.trim())
          .filter(Boolean);
        deps.set(currentPlay, list);
        currentPlay = null;
      }
    }
  }
  return deps;
}

function statusBackground(status: string): string {
  if (
    status === "merged" ||
    status === "completed" ||
    status === "done" ||
    status === "director-managed-complete"
  ) {
    return "var(--status-success-bg)";
  }
  if (status === "running" || status === "director-managed") {
    return "var(--status-running-bg)";
  }
  if (status === "failed" || status === "error") return "var(--status-error-bg)";
  return "var(--surface-raised)";
}

function statusColor(status: string): string {
  if (
    status === "merged" ||
    status === "completed" ||
    status === "done" ||
    status === "director-managed-complete"
  ) {
    return "var(--status-success)";
  }
  if (status === "running" || status === "pending" || status === "director-managed") {
    return "var(--status-running)";
  }
  if (status === "failed" || status === "error") return "var(--status-error)";
  return "var(--edge-strong)";
}

/** Returns the set of edge IDs on the longest path (by hop count) through the DAG. */
function criticalPathEdgeIds(nodes: Node[], edges: Edge[]): Set<string> {
  const outgoing = new Map<string, string[]>();
  const inDegree = new Map<string, number>();
  for (const n of nodes) {
    outgoing.set(n.id, []);
    inDegree.set(n.id, 0);
  }
  for (const e of edges) {
    outgoing.get(e.source)?.push(e.target);
    inDegree.set(e.target, (inDegree.get(e.target) ?? 0) + 1);
  }

  const queue: string[] = [];
  const deg = new Map(inDegree);
  deg.forEach((d, id) => {
    if (d === 0) queue.push(id);
  });
  const topo: string[] = [];
  while (queue.length > 0) {
    const u = queue.shift()!;
    topo.push(u);
    for (const v of outgoing.get(u) ?? []) {
      const nd = (deg.get(v) ?? 0) - 1;
      deg.set(v, nd);
      if (nd === 0) queue.push(v);
    }
  }

  const dist = new Map<string, number>(nodes.map((n) => [n.id, 0]));
  const prev = new Map<string, string | null>(nodes.map((n) => [n.id, null]));
  for (const u of topo) {
    for (const v of outgoing.get(u) ?? []) {
      if ((dist.get(u) ?? 0) + 1 > (dist.get(v) ?? 0)) {
        dist.set(v, (dist.get(u) ?? 0) + 1);
        prev.set(v, u);
      }
    }
  }

  let maxDist = -1;
  let endNode = "";
  dist.forEach((d, id) => {
    if (d > maxDist) {
      maxDist = d;
      endNode = id;
    }
  });

  const edgeByKey = new Map(edges.map((e) => [`${e.source}→${e.target}`, e.id]));

  const critical = new Set<string>();
  let cur = endNode;
  let p = prev.get(cur);
  while (p) {
    const eid = edgeByKey.get(`${p}→${cur}`);
    if (eid) critical.add(eid);
    cur = p;
    p = prev.get(cur);
  }
  return critical;
}

export default function PlayDag({ plays, showMd, onNodeClick }: PlayDagProps) {
  const { nodes, edges } = useMemo(() => {
    const depsMap = parseShowMdDeps(showMd);
    const playNames = new Set(plays.map((p) => p.name));

    const rawNodes: Node[] = plays.map((play) => ({
      id: play.name,
      type: "default",
      position: { x: 0, y: 0 },
      data: { label: play.name },
      style: {
        background: statusBackground(play.meta.status),
        border: `1px solid ${statusColor(play.meta.status)}`,
        color: "var(--content-primary)",
        fontSize: 10,
        fontFamily: "monospace",
        width: 180,
        borderRadius: 5,
        padding: "4px 8px",
        boxShadow: "0 1px 3px rgba(0,0,0,0.12)",
      },
    }));

    let rawEdges: Edge[];
    const hasDeps = depsMap.size > 0;

    if (hasDeps) {
      rawEdges = [];
      plays.forEach((play) => {
        const deps = depsMap.get(play.name) ?? [];
        for (const dep of deps) {
          if (playNames.has(dep)) {
            rawEdges.push({
              id: `e-${dep}-${play.name}`,
              source: dep,
              target: play.name,
              markerEnd: { type: MarkerType.ArrowClosed },
              style: { stroke: "var(--edge-default)" },
            });
          }
        }
      });
    } else {
      rawEdges = plays.slice(0, -1).map((play, i) => ({
        id: `e-${i}`,
        source: play.name,
        target: plays[i + 1].name,
        markerEnd: { type: MarkerType.ArrowClosed },
        style: { stroke: "#444" },
      }));
    }

    const criticalIds = criticalPathEdgeIds(rawNodes, rawEdges);
    const highlightedEdges = rawEdges.map((e) =>
      criticalIds.has(e.id)
        ? { ...e, style: { ...e.style, stroke: "var(--status-running)", strokeWidth: 2 } }
        : e,
    );

    return getLayoutedElements(rawNodes, highlightedEdges, "LR");
  }, [plays, showMd]);

  const handleNodeClick: NodeMouseHandler = useCallback(
    (_event, node) => {
      onNodeClick?.(node.id);
    },
    [onNodeClick],
  );

  return (
    <div
      style={{ height: 300 }}
      className={`rounded border border-edge bg-surface-base${onNodeClick ? " [&_.react-flow__node]:cursor-pointer" : ""}`}
    >
      <ReactFlow
        nodes={nodes}
        edges={edges}
        fitView
        fitViewOptions={{ padding: 0.25 }}
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable={false}
        onNodeClick={onNodeClick ? handleNodeClick : undefined}
        proOptions={{ hideAttribution: true }}
        className="bg-surface-base"
      >
        <Background color="var(--edge-subtle)" gap={20} size={1} />
        <Controls
          showInteractive={false}
          className="!bg-surface-raised !border-edge !shadow-none [&>button]:!bg-surface-raised [&>button]:!border-edge [&>button]:!text-content-secondary [&>button:hover]:!bg-surface-overlay [&>button:hover]:!text-content-primary"
        />
      </ReactFlow>
    </div>
  );
}
