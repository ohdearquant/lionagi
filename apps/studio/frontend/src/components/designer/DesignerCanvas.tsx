/**
 * DesignerCanvas — the launch console. The canvas renders the SPEC layer of an
 * engine as a causal flow graph: operator templates ordered by causal depth,
 * signal hand-offs as colored edges. Selecting a card opens the stage editor
 * docked on the right — the canvas narrows but the camera holds, so nothing
 * ever overlaps the graph. Blueprint rack left, prompt + Save + Launch in the
 * bottom dock, secondary knobs in Advanced.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslations } from "use-intl";

import {
  ENGINE_KINDS,
  ENGINE_TOPOLOGIES,
  type EngineKind,
  type EngineTopology,
} from "@/lib/designer/topology";
import { deriveFlow } from "@/lib/designer/flow";
import type { FlowNode as FlowNodeT } from "@/lib/designer/flow";
import { getEngineDef } from "@/lib/api";
import type { EngineDef } from "@/lib/api";
import { defaultDraft } from "@/lib/designer/draft";
import type { EngineDefDraft } from "@/lib/designer/draft";

import { DesignerDraftProvider } from "./DesignerDraftContext";
import type { DesignerDraftValue } from "./DesignerDraftContext";
import BlueprintRack from "./BlueprintRack";
import LaunchDock from "./LaunchDock";
import AdvancedDrawer from "./AdvancedDrawer";
import CanvasToolbar from "./CanvasToolbar";
import FlowCanvas from "./FlowCanvas";
import OperatorCard from "./OperatorCard";
import NodeInspector from "./NodeInspector";

export interface DesignerCanvasProps {
  /** If set, load this def on mount for editing */
  editDefId?: string | null;
  /** Preselect a topology kind (deep link from launch surfaces) */
  initialKind?: string | null;
}

export default function DesignerCanvas({ editDefId, initialKind }: DesignerCanvasProps) {
  const t = useTranslations("designer");

  const startKind: EngineKind = ENGINE_KINDS.includes(initialKind as EngineKind)
    ? (initialKind as EngineKind)
    : "research";
  const [kind, setKind] = useState<EngineKind>(startKind);
  const [draft, setDraft] = useState<EngineDefDraft>(defaultDraft(startKind));
  const [localTopo, setLocalTopo] = useState<EngineTopology>(() => ENGINE_TOPOLOGIES[startKind]);
  const [existingId, setExistingId] = useState<string | null>(null);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [selectedStageId, setSelectedStageId] = useState<string | null>(null);
  const [focusSignal, setFocusSignal] = useState<string | null>(null);
  const [rackRefresh, setRackRefresh] = useState(0);
  // Rack visibility — toggled by re-clicking the designer icon in the rail.
  const [rackVisible, setRackVisible] = useState(true);
  // Refs to FlowCanvas imperative zoom callbacks; populated via callback props
  const zoomInRef = useRef<(() => void) | null>(null);
  const zoomOutRef = useRef<(() => void) | null>(null);
  const fitRef = useRef<(() => void) | null>(null);

  useEffect(() => {
    const onTogglePane = () => setRackVisible((v) => !v);
    window.addEventListener("studio:toggle-pane", onTogglePane);
    return () => window.removeEventListener("studio:toggle-pane", onTogglePane);
  }, []);

  // Load existing def when editDefId provided
  useEffect(() => {
    if (!editDefId) return;
    getEngineDef(editDefId)
      .then((def) => {
        const k = def.kind as EngineKind;
        setKind(k);
        setDraft(defaultDraft(k, def));
        setExistingId(def.id);
      })
      .catch(() => {
        // If not found, proceed with empty draft
      });
  }, [editDefId]);

  const patchDraft = useCallback((patch: Partial<EngineDefDraft>) => {
    setDraft((d) => ({ ...d, ...patch }));
  }, []);

  const patchStage = useCallback((stageId: string, patch: { role?: string; model?: string }) => {
    setDraft((d) => ({
      ...d,
      stages: { ...d.stages, [stageId]: { ...d.stages[stageId], ...patch } },
    }));
  }, []);

  const handlePickKind = useCallback((k: EngineKind) => {
    setKind(k);
    setLocalTopo(ENGINE_TOPOLOGIES[k]);
    setExistingId(null);
    setDraft(defaultDraft(k));
    setSelectedStageId(null);
    setFocusSignal(null);
  }, []);

  const handlePickDef = useCallback((def: EngineDef) => {
    const k = ENGINE_KINDS.includes(def.kind as EngineKind) ? (def.kind as EngineKind) : "research";
    setKind(k);
    setLocalTopo(ENGINE_TOPOLOGIES[k]);
    setDraft(defaultDraft(k, def));
    setExistingId(def.id);
    setSelectedStageId(null);
    setFocusSignal(null);
  }, []);

  const handleSaved = useCallback((id: string, name: string) => {
    setExistingId(id);
    setDraft((d) => ({ ...d, name }));
    setRackRefresh((n) => n + 1);
  }, []);

  // Deleting the loaded def keeps the draft on canvas but unbinds it — the
  // header flips back to "draft" and the next save creates a new definition.
  const handleDefDeleted = useCallback((defId: string) => {
    setExistingId((cur) => (cur === defId ? null : cur));
  }, []);

  const patchTopo = useCallback((updater: (t: EngineTopology) => EngineTopology) => {
    setLocalTopo((t) => updater(t));
  }, []);

  const flowModel = useMemo(() => deriveFlow(localTopo), [localTopo]);
  const draftCtx: DesignerDraftValue = useMemo(
    () => ({ draft, patchDraft, patchStage, topo: localTopo, patchTopo }),
    [draft, patchDraft, patchStage, localTopo, patchTopo],
  );

  // Inspector and drawer are both inspect modes — one at a time.
  const handleSelect = useCallback((id: string | null) => {
    setSelectedStageId(id);
    if (id != null) setAdvancedOpen(false);
  }, []);
  const handleToggleAdvanced = useCallback(() => {
    setAdvancedOpen((v) => {
      if (!v) setSelectedStageId(null);
      return !v;
    });
  }, []);

  const quiescenceLabel = t("flow.quiescence");
  const renderNode = useCallback(
    ({ node, x, y }: { node: FlowNodeT; x: number; y: number }) => (
      <OperatorCard
        node={node}
        x={x}
        y={y}
        defModel={draft.model}
        overrides={draft.stages}
        selectedId={selectedStageId}
        onSelect={handleSelect}
        quiescenceLabel={quiescenceLabel}
      />
    ),
    [draft.model, draft.stages, selectedStageId, handleSelect, quiescenceLabel],
  );

  const selectedStage = selectedStageId
    ? (localTopo.stages.find((s) => s.id === selectedStageId) ?? null)
    : null;

  return (
    <DesignerDraftProvider value={draftCtx}>
      <div className="flex h-full flex-col bg-surface-base">
        <div className="flex flex-1 overflow-hidden">
          {/* Rack hides while a node is being inspected — the inspector and
              Leo need the room; the rail icon brings it back. */}
          {rackVisible && !selectedStage && (
            <BlueprintRack
              activeKind={kind}
              activeDefId={existingId}
              onPickKind={handlePickKind}
              onPickDef={handlePickDef}
              onDeleted={handleDefDeleted}
              refreshToken={rackRefresh}
            />
          )}

          {/* Canvas */}
          <div className="relative min-w-0 flex-1">
            {/* Identity strip — compact, top-left. Engine knobs live in Advanced. */}
            <div className="absolute left-3 top-2.5 z-10 flex items-center gap-2 rounded-md border border-edge bg-surface-raised/95 px-2.5 py-1.5 backdrop-blur-sm">
              <span className="font-data text-[length:var(--t-sm)] font-semibold text-content-primary">
                {kind}
              </span>
              {draft.name.trim() && (
                <span className="max-w-[220px] truncate font-data text-[length:var(--t-sm)] text-content-secondary">
                  {draft.name}
                </span>
              )}
              {!existingId && (
                <span className="font-ui text-[length:var(--t-xs)] text-content-muted">
                  {t("header.template")}
                </span>
              )}
              {existingId ? (
                <span
                  className="whitespace-nowrap rounded px-1.5 py-px font-data text-[length:var(--t-xs)] text-status-success"
                  style={{ background: "rgba(79,180,119,0.10)" }}
                >
                  {t("header.saved")}
                </span>
              ) : (
                <span
                  className="whitespace-nowrap rounded bg-surface-overlay px-1.5 py-px font-data text-[length:var(--t-xs)] text-content-muted"
                  title={t("header.draftHint")}
                >
                  {t("header.draft")}
                </span>
              )}
            </div>

            {/* Editing tools — floating dock at the bottom of the canvas */}
            <div className="absolute bottom-3 left-1/2 z-10 -translate-x-1/2">
              <CanvasToolbar
                selectedId={selectedStageId}
                onZoomIn={() => zoomInRef.current?.()}
                onZoomOut={() => zoomOutRef.current?.()}
                onFit={() => fitRef.current?.()}
                onDeselect={() => handleSelect(null)}
              />
            </div>

            <FlowCanvas
              model={flowModel}
              fitKey={kind}
              selectedId={selectedStageId}
              onSelect={handleSelect}
              focusSignal={focusSignal}
              onFocusSignal={setFocusSignal}
              renderNode={renderNode}
              onRegisterControls={(controls) => {
                zoomInRef.current = controls.zoomIn;
                zoomOutRef.current = controls.zoomOut;
                fitRef.current = controls.fit;
              }}
            />
          </div>

          {selectedStage && (
            <NodeInspector
              stage={selectedStage}
              topo={localTopo}
              model={flowModel}
              onFocusSignal={setFocusSignal}
              onClose={() => setSelectedStageId(null)}
            />
          )}

          {advancedOpen && !selectedStageId && (
            <AdvancedDrawer draft={draft} patchDraft={patchDraft} />
          )}
        </div>

        <LaunchDock
          draft={draft}
          patchDraft={patchDraft}
          existingId={existingId}
          onSaved={handleSaved}
          advancedOpen={advancedOpen}
          onToggleAdvanced={handleToggleAdvanced}
        />
      </div>
    </DesignerDraftProvider>
  );
}
