/**
 * Engine definition draft — the editable state behind the designer canvas.
 * Pure logic: draft shape, defaults, validation, and the POST body builder.
 * Only the honest knobs the launch pipeline accepts appear here: model,
 * max_depth, max_agents, options.test_cmd, options.export_dir, and the
 * per-stage role/model overrides in `stages`.
 */
import type { EngineKind, EngineTopology } from "./topology";
import type { CreateEngineDefRequest, EngineDef, StageOverride } from "@/lib/api";

export interface EngineDefDraft {
  name: string;
  kind: EngineKind;
  model: string;
  max_agents: string; // string for input; converted to number on save
  max_depth: string;
  test_cmd: string;
  export_dir: string;
  description: string;
  /** stage id → role/model override; empty strings mean "engine default". */
  stages: Record<string, StageOverride>;
}

export function defaultDraft(kind: EngineKind, existing?: EngineDef | null): EngineDefDraft {
  const stages: Record<string, StageOverride> = {};
  for (const [id, ov] of Object.entries(existing?.stages ?? {})) {
    stages[id] = { ...ov };
  }
  return {
    name: existing?.name ?? "",
    kind,
    model: existing?.model ?? "",
    max_agents: existing?.max_agents != null ? String(existing.max_agents) : "",
    max_depth: existing?.max_depth != null ? String(existing.max_depth) : "",
    test_cmd: existing?.options?.test_cmd ?? "",
    export_dir: existing?.options?.export_dir ?? "",
    description: existing?.description ?? "",
    stages,
  };
}

/** Strip blank values; what remains is exactly what the launch pipeline binds. */
export function cleanStages(stages: Record<string, StageOverride>): Record<string, StageOverride> {
  const out: Record<string, StageOverride> = {};
  for (const [id, ov] of Object.entries(stages)) {
    const entry: StageOverride = {};
    if (ov.role?.trim()) entry.role = ov.role.trim();
    if (ov.model?.trim()) entry.model = ov.model.trim();
    if (Object.keys(entry).length > 0) out[id] = entry;
  }
  return out;
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
  // Always sent (even empty) so clearing the last override clears it on update.
  body.stages = cleanStages(draft.stages);
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
