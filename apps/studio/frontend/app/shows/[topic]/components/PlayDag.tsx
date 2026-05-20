"use client";

import { useMemo } from "react";
import ReactFlow, { Background, Controls, MarkerType } from "reactflow";
import type { Edge, Node } from "reactflow";
import "reactflow/dist/style.css";
import { getLayoutedElements } from "@/components/canvas/useLayout";
import type { ShowDetail } from "@/lib/types";

type Play = ShowDetail["plays"][number];

interface PlayDagProps {
  plays: Play[];
  showMd?: string | null;
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

function statusColor(status: string): string {
  if (status === "merged" || status === "completed" || status === "done") return "#22c55e";
  if (status === "running" || status === "pending") return "#60a5fa";
  if (status === "failed" || status === "error") return "#ef4444";
  return "#555";
}

export default function PlayDag({ plays, showMd }: PlayDagProps) {
  const { nodes, edges } = useMemo(() => {
    const depsMap = parseShowMdDeps(showMd);
    const playNames = new Set(plays.map((p) => p.name));

    const rawNodes: Node[] = plays.map((play) => ({
      id: play.name,
      type: "default",
      position: { x: 0, y: 0 },
      data: { label: play.name },
      style: {
        background: "#111",
        border: `1px solid ${statusColor(play.meta.status)}`,
        color: "#ccc",
        fontSize: 11,
        fontFamily: "monospace",
        width: 200,
        borderRadius: 6,
        padding: "6px 10px",
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
              style: { stroke: "#444" },
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

    return getLayoutedElements(rawNodes, rawEdges, "LR");
  }, [plays, showMd]);

  return (
    <div style={{ height: 280 }} className="rounded border border-neutral-800 bg-neutral-950">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        fitView
        fitViewOptions={{ padding: 0.3 }}
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable={false}
        proOptions={{ hideAttribution: true }}
        className="bg-neutral-950"
      >
        <Background color="#222" gap={20} size={1} />
        <Controls
          showInteractive={false}
          className="!bg-neutral-900 !border-neutral-700 !shadow-none [&>button]:!bg-neutral-800 [&>button]:!border-neutral-700 [&>button]:!text-neutral-400"
        />
      </ReactFlow>
    </div>
  );
}
