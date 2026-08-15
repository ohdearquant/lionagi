/**
 * Engine definition draft — the editable state behind the designer canvas.
 * Pure logic: draft shape, defaults, validation, and the POST body builder.
 * Only the honest knobs the launch pipeline accepts appear here: model,
 * max_depth, max_agents, options.test_cmd, and options.export_dir.
 */
import type { EngineKind, EngineTopology } from "./topology";
import type { CreateEngineDefRequest, EngineDef } from "@/lib/api";

export interface EngineDefDraft {
  name: string;
  kind: EngineKind;
  model: string;
  max_agents: string; // string for input; converted to number on save
  max_depth: string;
  test_cmd: string;
  export_dir: string;
  description: string;
}

export function defaultDraft(kind: EngineKind, existing?: EngineDef | null): EngineDefDraft {
  return {
    name: existing?.name ?? "",
    kind,
    model: existing?.model ?? "",
    max_agents: existing?.max_agents != null ? String(existing.max_agents) : "",
    max_depth: existing?.max_depth != null ? String(existing.max_depth) : "",
    test_cmd: existing?.options?.test_cmd ?? "",
    export_dir: existing?.options?.export_dir ?? "",
    description: existing?.description ?? "",
  };
}

export function buildDefBody(draft: EngineDefDraft): CreateEngineDefRequest {
  const body: CreateEngineDefRequest = {
    name: draft.name,
    kind: draft.kind,
  };
  if (draft.model.trim()) body.model = draft.model.trim();
  if (draft.description.trim()) body.description = draft.description.trim();
  const maxAgents = parseInt(draft.max_agents, 10);
  if (!isNaN(maxAgents) && maxAgents >= 1 && maxAgents <= 100) body.max_agents = maxAgents;
  const maxDepth = parseInt(draft.max_depth, 10);
  if (!isNaN(maxDepth) && maxDepth >= 1 && maxDepth <= 100) body.max_depth = maxDepth;
  const options: Record<string, string> = {};
  if (draft.test_cmd.trim()) options.test_cmd = draft.test_cmd.trim();
  if (draft.export_dir.trim()) options.export_dir = draft.export_dir.trim();
  if (Object.keys(options).length > 0) body.options = options;
  return body;
}

export function validateDraft(draft: EngineDefDraft, topo: EngineTopology): Record<string, string> {
  const errors: Record<string, string> = {};
  if (!draft.name.trim()) errors.name = "name";
  if (topo.testCmd.applies && topo.testCmd.required && !draft.test_cmd.trim()) {
    errors.test_cmd = "test_cmd";
  }
  const maxAgents = draft.max_agents.trim();
  if (maxAgents) {
    const n = parseInt(maxAgents, 10);
    if (isNaN(n) || n < 1 || n > 100) errors.max_agents = "range";
  }
  const maxDepth = draft.max_depth.trim();
  if (maxDepth && topo.maxDepth.applies) {
    const n = parseInt(maxDepth, 10);
    if (isNaN(n) || n < 1 || n > 100) errors.max_depth = "range";
  }
  return errors;
}
