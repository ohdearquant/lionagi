/**
 * Draft context — lets stage nodes and edges deep inside ReactFlow read and
 * patch the engine draft without prop-threading through the flow renderer.
 * The provider wraps the whole designer surface in DesignerCanvas.
 */
import { createContext, useContext } from "react";
import type { EngineDefDraft } from "@/lib/designer/draft";
import type { EngineTopology } from "@/lib/designer/topology";
import type { StageOverride } from "@/lib/api";

export interface DesignerDraftValue {
  draft: EngineDefDraft;
  patchDraft: (patch: Partial<EngineDefDraft>) => void;
  /** Merge a role/model override for one stage into the draft. */
  patchStage: (stageId: string, patch: StageOverride) => void;
  topo: EngineTopology;
  /** Replace the mutable topology in DesignerCanvas. */
  patchTopo: (updater: (t: EngineTopology) => EngineTopology) => void;
}

const DesignerDraftContext = createContext<DesignerDraftValue | null>(null);

export const DesignerDraftProvider = DesignerDraftContext.Provider;

export function useDesignerDraft(): DesignerDraftValue | null {
  return useContext(DesignerDraftContext);
}
