import type { WorkflowSpec, WorkflowNode, WorkflowEdge, WorkflowEngineConfig } from "@/lib/api";

export interface ValidationError {
  rule: string;
  message: string;
}

/** Toposort a DAG. Returns sorted ids, or null if a cycle exists. */
export function toposort(nodeIds: string[], edges: WorkflowEdge[]): string[] | null {
  const inDegree = new Map<string, number>(nodeIds.map((id) => [id, 0]));
  const adj = new Map<string, string[]>(nodeIds.map((id) => [id, []]));

  for (const e of edges) {
    if (!inDegree.has(e.from) || !inDegree.has(e.to)) continue;
    adj.get(e.from)!.push(e.to);
    inDegree.set(e.to, inDegree.get(e.to)! + 1);
  }

  const queue: string[] = [];
  for (const [id, deg] of inDegree) {
    if (deg === 0) queue.push(id);
  }

  const sorted: string[] = [];
  while (queue.length > 0) {
    const n = queue.shift()!;
    sorted.push(n);
    for (const next of adj.get(n) ?? []) {
      const deg = inDegree.get(next)! - 1;
      inDegree.set(next, deg);
      if (deg === 0) queue.push(next);
    }
  }

  return sorted.length === nodeIds.length ? sorted : null;
}

/** Returns true if the graph has a cycle. */
export function hasCycle(nodeIds: string[], edges: WorkflowEdge[]): boolean {
  return toposort(nodeIds, edges) === null;
}

function isEngineConfig(c: unknown): c is WorkflowEngineConfig {
  return (
    typeof c === "object" &&
    c !== null &&
    typeof (c as WorkflowEngineConfig).engine_def_id === "string"
  );
}

/** V1 spec validation: returns a list of errors (empty = valid). */
export function validateSpec(
  spec: WorkflowSpec,
  knownEngineDefIds?: Set<string>,
): ValidationError[] {
  const errors: ValidationError[] = [];
  const nodeIds = new Set(spec.nodes.map((n) => n.id));

  // Rule 1 — at least one input node
  const inputNodes = spec.nodes.filter((n) => n.kind === "input");
  if (inputNodes.length === 0) {
    errors.push({ rule: "no-input", message: "Workflow must have at least one input node." });
  }

  // Rule 2 — no cycles
  if (hasCycle([...nodeIds], spec.edges)) {
    errors.push({ rule: "cycle", message: "Workflow graph must be acyclic." });
  }

  // Rule 3 — no disconnected nodes (each node must have at least one edge, unless it's the only node)
  if (spec.nodes.length > 1) {
    const connected = new Set<string>();
    for (const e of spec.edges) {
      connected.add(e.from);
      connected.add(e.to);
    }
    for (const n of spec.nodes) {
      if (!connected.has(n.id)) {
        errors.push({
          rule: "disconnected",
          message: `Node "${n.label || n.id}" is disconnected.`,
        });
      }
    }
  }

  // Rule 4 — engine nodes must have known engine_def_id (when registry is provided)
  if (knownEngineDefIds) {
    for (const n of spec.nodes) {
      if (n.kind === "engine") {
        if (!isEngineConfig(n.config)) {
          errors.push({
            rule: "engine-no-config",
            message: `Engine node "${n.label || n.id}" is missing engine_def_id.`,
          });
        } else if (!knownEngineDefIds.has(n.config.engine_def_id)) {
          errors.push({
            rule: "engine-unknown-def",
            message: `Engine node "${n.label || n.id}" references unknown engine def "${n.config.engine_def_id}".`,
          });
        }
      }
    }
  }

  return errors;
}

/** Build an empty spec with a single input node. */
export function emptySpec(): WorkflowSpec {
  return {
    version: 1,
    nodes: [{ id: "n1", kind: "input", label: "Input", pos: { x: 64, y: 120 } }],
    edges: [],
    inputs: [],
    outputs: [],
  };
}

export type { WorkflowSpec, WorkflowNode, WorkflowEdge };
