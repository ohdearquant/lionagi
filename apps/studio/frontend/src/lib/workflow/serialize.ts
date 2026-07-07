import { parse as parseYaml, stringify as stringifyYaml } from "yaml";
import { parse as parseToml, stringify as stringifyToml } from "smol-toml";
import type { WorkflowSpec, WorkflowNode, WorkflowEdge, WorkflowNodeKind } from "@/lib/api";

const NODE_KINDS: readonly WorkflowNodeKind[] = [
  "input",
  "chat",
  "parse",
  "fanout",
  "engine",
  "gate",
];

export interface ParseResult {
  spec: WorkflowSpec | null;
  errors: string[];
}

/**
 * Order keys explicitly so serialized documents diff cleanly regardless of
 * how the in-memory object was assembled.
 */
function orderedSpec(spec: WorkflowSpec): Record<string, unknown> {
  return {
    version: spec.version,
    nodes: spec.nodes.map((n) => ({
      id: n.id,
      kind: n.kind,
      label: n.label,
      pos: { x: n.pos.x, y: n.pos.y },
      ...(n.config && Object.keys(n.config).length > 0 ? { config: n.config } : {}),
    })),
    edges: spec.edges.map((e) => ({
      id: e.id,
      from: e.from,
      to: e.to,
      ...(e.label ? { label: e.label } : {}),
      ...(e.condition && e.condition.trim() ? { condition: e.condition } : {}),
    })),
    inputs: spec.inputs,
    outputs: spec.outputs,
  };
}

export function specToYaml(spec: WorkflowSpec): string {
  return stringifyYaml(orderedSpec(spec), { indent: 2, lineWidth: 100 });
}

export function specToToml(spec: WorkflowSpec): string {
  return stringifyToml(orderedSpec(spec));
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

/**
 * Coerce a parsed document into a WorkflowSpec, collecting shape errors.
 * Unknown top-level keys are rejected loudly rather than dropped silently.
 */
export function coerceSpec(data: unknown): ParseResult {
  const errors: string[] = [];
  const doc = asRecord(data);
  if (!doc) return { spec: null, errors: ["document must be a mapping (object) at top level"] };

  const known = new Set(["version", "nodes", "edges", "inputs", "outputs"]);
  for (const key of Object.keys(doc)) {
    if (!known.has(key)) errors.push(`unknown top-level key "${key}"`);
  }

  const nodesRaw = Array.isArray(doc.nodes) ? doc.nodes : null;
  if (!nodesRaw) errors.push('"nodes" must be a list');

  const nodes: WorkflowNode[] = [];
  const seenNodeIds = new Set<string>();
  for (const [i, raw] of (nodesRaw ?? []).entries()) {
    const n = asRecord(raw);
    if (!n) {
      errors.push(`node ${i}: not a mapping`);
      continue;
    }
    const id = typeof n.id === "string" ? n.id : "";
    if (!id) errors.push(`node ${i}: missing "id"`);
    else if (seenNodeIds.has(id)) errors.push(`node ${i}: duplicate id "${id}"`);
    seenNodeIds.add(id);

    const kind = typeof n.kind === "string" ? n.kind : "";
    if (!NODE_KINDS.includes(kind as WorkflowNodeKind)) {
      errors.push(`node "${id || i}": kind must be one of ${NODE_KINDS.join(", ")}`);
    }
    const pos = asRecord(n.pos);
    const x = typeof pos?.x === "number" ? pos.x : 0;
    const y = typeof pos?.y === "number" ? pos.y : 0;
    const config = asRecord(n.config) ?? undefined;

    nodes.push({
      id: id || `n${i + 1}`,
      kind: (NODE_KINDS.includes(kind as WorkflowNodeKind) ? kind : "chat") as WorkflowNodeKind,
      label: typeof n.label === "string" ? n.label : id || `n${i + 1}`,
      pos: { x, y },
      ...(config ? { config } : {}),
    });
  }

  const edgesRaw = Array.isArray(doc.edges) ? doc.edges : doc.edges == null ? [] : null;
  if (edgesRaw === null) errors.push('"edges" must be a list');

  const edges: WorkflowEdge[] = [];
  for (const [i, raw] of (edgesRaw ?? []).entries()) {
    const e = asRecord(raw);
    if (!e) {
      errors.push(`edge ${i}: not a mapping`);
      continue;
    }
    const from = typeof e.from === "string" ? e.from : "";
    const to = typeof e.to === "string" ? e.to : "";
    if (!from || !to) errors.push(`edge ${i}: requires "from" and "to"`);
    if (from && !seenNodeIds.has(from)) errors.push(`edge ${i}: unknown "from" node "${from}"`);
    if (to && !seenNodeIds.has(to)) errors.push(`edge ${i}: unknown "to" node "${to}"`);
    edges.push({
      id: typeof e.id === "string" && e.id ? e.id : `e${i + 1}`,
      from,
      to,
      ...(typeof e.label === "string" && e.label ? { label: e.label } : {}),
      ...(typeof e.condition === "string" && e.condition.trim() ? { condition: e.condition } : {}),
    });
  }

  const strList = (v: unknown): string[] =>
    Array.isArray(v) ? v.filter((s): s is string => typeof s === "string") : [];

  if (errors.length > 0) return { spec: null, errors };
  return {
    spec: {
      version: 1,
      nodes,
      edges,
      inputs: strList(doc.inputs),
      outputs: strList(doc.outputs),
    },
    errors: [],
  };
}

export function yamlToSpec(text: string): ParseResult {
  let data: unknown;
  try {
    data = parseYaml(text);
  } catch (e) {
    return { spec: null, errors: [e instanceof Error ? e.message : "YAML parse error"] };
  }
  return coerceSpec(data);
}

export function tomlToSpec(text: string): ParseResult {
  let data: unknown;
  try {
    data = parseToml(text);
  } catch (e) {
    return { spec: null, errors: [e instanceof Error ? e.message : "TOML parse error"] };
  }
  return coerceSpec(data);
}

/** Parse by filename extension: .toml → TOML, everything else → YAML. */
export function textToSpec(text: string, filename: string): ParseResult {
  return /\.toml$/i.test(filename) ? tomlToSpec(text) : yamlToSpec(text);
}
