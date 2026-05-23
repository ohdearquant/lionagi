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
        background: "var(--surface-raised)",
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

    return getLayoutedElements(rawNodes, rawEdges, "LR");
  }, [plays, showMd]);

  return (
    <div style={{ height: 220 }} className="rounded border border-edge bg-surface-base">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        fitView
        fitViewOptions={{ padding: 0.25 }}
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable={false}
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
