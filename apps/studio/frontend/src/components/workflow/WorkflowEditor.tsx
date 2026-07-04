import { useCallback, useMemo, useState } from "react";
import { useTranslations } from "use-intl";
import FlowCanvas from "@/components/designer/FlowCanvas";
import type { FlowNode } from "@/lib/designer/flow";
import type { WorkflowNodeKind, EngineDef } from "@/lib/api";
import { specToFlowModel } from "@/lib/workflow/flow";
import { validateSpec } from "@/lib/workflow/validation";
import { WorkflowDraftProvider, useWorkflowDraft } from "./WorkflowDraftContext";
import WorkflowNodeCard from "./WorkflowNodeCard";
import WorkflowNodePalette from "./WorkflowNodePalette";
import WorkflowNodeInspector from "./WorkflowNodeInspector";
import type { WorkflowSpec } from "@/lib/api";

interface WorkflowEditorProps {
  initialSpec: WorkflowSpec;
  engineDefs: EngineDef[];
  onSave: (spec: WorkflowSpec) => Promise<void>;
  saving?: boolean;
}

// Palette is 60px wide + 12px left inset; add 16px breathing room.
const PALETTE_W = 88;
// No right pad needed — inspector is a sibling flex column, not an overlay.
const WORKFLOW_FIT_PAD = { top: 24, right: 24, bottom: 24, left: PALETTE_W };

export default function WorkflowEditor(props: WorkflowEditorProps) {
  return (
    <WorkflowDraftProvider initialSpec={props.initialSpec}>
      <WorkflowEditorInner {...props} />
    </WorkflowDraftProvider>
  );
}

function WorkflowEditorInner({
  engineDefs,
  onSave,
  saving,
}: Omit<WorkflowEditorProps, "initialSpec">) {
  const t = useTranslations("workflow");
  const { state, moveNode, reset } = useWorkflowDraft();
  const { spec, dirty } = state;

  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [savedOk, setSavedOk] = useState(false);

  const knownEngineDefIds = useMemo(() => new Set(engineDefs.map((d) => d.id)), [engineDefs]);

  const validationErrors = useMemo(
    () => validateSpec(spec, knownEngineDefIds),
    [spec, knownEngineDefIds],
  );

  const model = useMemo(() => specToFlowModel(spec), [spec]);
  // Change fitKey whenever spec node count changes so fit re-runs on add/remove.
  const fitKey = `workflow-${spec.nodes.length}`;

  const renderNode = useCallback(
    ({ node, x, y }: { node: FlowNode; x: number; y: number }) => {
      const specNode = spec.nodes.find((n) => n.id === node.id);
      return (
        <WorkflowNodeCard
          node={node}
          x={x}
          y={y}
          selected={selectedId === node.id}
          label={specNode?.label ?? node.id}
          kind={(specNode?.kind ?? "input") as WorkflowNodeKind}
        />
      );
    },
    [spec.nodes, selectedId],
  );

  const handleSave = async () => {
    if (validationErrors.length > 0) return;
    setSaveError(null);
    setSavedOk(false);
    try {
      await onSave(spec);
      setSavedOk(true);
      reset(spec);
    } catch (e) {
      setSaveError(e instanceof Error ? e.message : t("saveError"));
    }
  };

  return (
    <div className="flex h-full flex-col overflow-hidden">
      {/* Toolbar */}
      <div className="flex shrink-0 items-center gap-2 border-b border-edge px-3 py-2">
        <span className="font-ui text-[length:var(--t-xs)] font-semibold uppercase tracking-[0.09em] text-content-muted">
          {t("editorTitle")}
        </span>
        <div className="flex-1" />
        {saveError && (
          <span className="text-[length:var(--t-xs)] text-status-failure">{saveError}</span>
        )}
        {savedOk && !dirty && (
          <span className="text-[length:var(--t-xs)] text-status-success">{t("saveOk")}</span>
        )}
        {validationErrors.length > 0 && (
          <span className="text-[length:var(--t-xs)] text-status-failure">
            {t("validationIssues", { count: validationErrors.length })}
          </span>
        )}
        <button
          type="button"
          onClick={() => void handleSave()}
          disabled={saving || validationErrors.length > 0}
          className="rounded px-3 py-1 text-[length:var(--t-xs)] font-medium transition-colors"
          style={{
            background: dirty ? "var(--accent)" : "var(--surface-overlay)",
            color: dirty ? "var(--surface-base)" : "var(--content-muted)",
            border: "1px solid var(--edge)",
            opacity: saving || validationErrors.length > 0 ? 0.5 : 1,
            cursor: saving || validationErrors.length > 0 ? "not-allowed" : "pointer",
          }}
        >
          {saving ? t("saving") : t("save")}
        </button>
      </div>

      {/* Canvas + inspector split */}
      <div className="flex min-h-0 flex-1">
        {/* Canvas region */}
        <div className="relative flex-1 overflow-hidden">
          <WorkflowNodePalette />
          <FlowCanvas
            model={model}
            fitKey={fitKey}
            selectedId={selectedId}
            onSelect={setSelectedId}
            focusSignal={null}
            onFocusSignal={() => {}}
            renderNode={renderNode}
            hideSignalIndex
            onNodeMoved={moveNode}
            fitPad={WORKFLOW_FIT_PAD}
          />
        </div>

        {/* Inspector panel */}
        <div
          className="flex w-[220px] shrink-0 flex-col border-l border-edge"
          style={{ transition: "width 150ms" }}
        >
          <WorkflowNodeInspector nodeId={selectedId} engineDefs={engineDefs} />
        </div>
      </div>
    </div>
  );
}
