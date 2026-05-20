import type { WorkerStepNode } from "@/lib/types";
import Badge from "@/components/Badge";

const ROLE_TONE: Record<string, "ok" | "running" | "failed" | "pending" | "blocked" | "default"> = {
  researcher: "running",
  implementer: "ok",
  reviewer: "blocked",
  critic: "failed",
  suggester: "pending",
  analyst: "running",
  tester: "ok",
};

function roleTone(
  role: string | undefined | null,
): "ok" | "running" | "failed" | "pending" | "blocked" | "default" {
  if (!role) return "default";
  return ROLE_TONE[role.toLowerCase()] ?? "default";
}

export interface StepPanelProps {
  node: WorkerStepNode;
}

export default function StepPanel({ node }: StepPanelProps) {
  return (
    <div className="flex flex-col gap-3 border border-neutral-800 bg-neutral-950 p-4">
      <div className="flex items-start justify-between gap-2">
        <h3 className="font-semibold text-neutral-100">{node.label || node.id}</h3>
        {node.role ? (
          <Badge tone={roleTone(node.role)} className="shrink-0">
            {node.role}
          </Badge>
        ) : null}
      </div>

      {node.assignment ? (
        <div>
          <span className="text-xs uppercase text-neutral-500">assignment</span>
          <p className="mt-0.5 font-mono text-sm text-neutral-300">{node.assignment}</p>
        </div>
      ) : null}

      {node.prompt ? (
        <div>
          <span className="text-xs uppercase text-neutral-500">prompt template</span>
          <pre className="mt-1 max-h-48 overflow-auto rounded border border-neutral-800 bg-neutral-900 p-3 text-xs leading-5 text-neutral-400 whitespace-pre-wrap">
            {node.prompt}
          </pre>
        </div>
      ) : null}

      <div className="flex flex-wrap gap-4 text-xs text-neutral-500">
        {node.capacity !== undefined ? (
          <span>
            <span className="text-neutral-600">capacity:</span>{" "}
            <span className="text-neutral-300">{node.capacity}</span>
          </span>
        ) : null}
        {node.timeout !== null && node.timeout !== undefined ? (
          <span>
            <span className="text-neutral-600">timeout:</span>{" "}
            <span className="text-neutral-300">{node.timeout}s</span>
          </span>
        ) : null}
        {node.inputs?.length > 0 ? (
          <span>
            <span className="text-neutral-600">inputs:</span>{" "}
            <span className="text-neutral-300">{node.inputs.join(", ")}</span>
          </span>
        ) : null}
        {node.outputs?.length > 0 ? (
          <span>
            <span className="text-neutral-600">outputs:</span>{" "}
            <span className="text-neutral-300">{node.outputs.join(", ")}</span>
          </span>
        ) : null}
      </div>
    </div>
  );
}
