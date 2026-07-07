import { useTranslations } from "use-intl";
import type { WorkflowNode, WorkflowEdge, EngineDef } from "@/lib/api";
import { useWorkflowDraft } from "./WorkflowDraftContext";
import SectionLabel from "@/components/ui/SectionLabel";

const MAX_CONDITION_LEN = 1000;

/**
 * Best-effort, non-authoritative check for an edge condition that is
 * definitely invalid. The backend compiles conditions against a Python AST
 * allow-list and is the source of truth (a bad expression comes back as a 422
 * carrying the edge id). We only flag the one thing the client can be certain
 * of without a tokenizer: the hard length cap. Pattern-matching for calls /
 * lambda / f-strings / dunders was removed because every such token also
 * appears legitimately inside string literals (`status == "off"`,
 * `data["f"]`) or as an operator keyword before a group (`not (x)`,
 * `y in ("a", "b")`), producing false "likely invalid" warnings on valid input.
 */
function looksInvalidCondition(expr: string): boolean {
  return expr.trim().length > MAX_CONDITION_LEN;
}

interface Props {
  nodeId: string | null;
  engineDefs: EngineDef[];
}

export default function WorkflowNodeInspector({ nodeId, engineDefs }: Props) {
  const t = useTranslations("workflow");
  const { state, patchNode, patchEdge, removeNode } = useWorkflowDraft();
  const [armedId, setArmedId] = useState<string | null>(null);

  if (!nodeId) {
    return (
      <div className="flex h-full items-center justify-center p-4 text-[length:var(--t-xs)] text-content-muted">
        {t("inspectorEmpty")}
      </div>
    );
  }

  const node = state.spec.nodes.find((n) => n.id === nodeId);
  if (!node) return null;

  const handleDelete = () => {
    if (armedId !== nodeId) {
      setArmedId(nodeId);
      return;
    }
    setArmedId(null);
    removeNode(nodeId);
  };

  return (
    <div className="flex h-full flex-col overflow-hidden">
      <div className="flex shrink-0 items-center justify-between border-b border-edge px-3 py-2">
        <span className="font-ui text-[length:var(--t-xs)] font-semibold uppercase tracking-[0.09em] text-content-muted">
          {t(`kinds.${node.kind}`)}
        </span>
        <button
          type="button"
          aria-label={armedId === nodeId ? t("deleteConfirm") : t("deleteNode")}
          onClick={handleDelete}
          onPointerLeave={() => setArmedId((id) => (id === nodeId ? null : id))}
          className="rounded px-2 py-0.5 text-[length:var(--t-xs)] transition-colors"
          style={{
            color: armedId === nodeId ? "var(--status-failure)" : "var(--content-muted)",
            background: armedId === nodeId ? "var(--status-failure)18" : "transparent",
          }}
        >
          {armedId === nodeId ? t("deleteConfirm") : "×"}
        </button>
      </div>

      <div className="flex flex-1 flex-col gap-3 overflow-auto p-3">
        <label className="flex flex-col gap-1">
          <SectionLabel>{t("nodeLabel")}</SectionLabel>
          <input
            type="text"
            value={node.label}
            onChange={(e) => patchNode(nodeId, { label: e.target.value })}
            className="rounded border border-edge bg-surface-overlay px-2 py-1 text-[length:var(--t-sm)] text-content-primary focus:outline-none focus:ring-1 focus:ring-accent"
          />
        </label>

        {node.kind === "engine" && (
          <EngineInspector node={node} engineDefs={engineDefs} patchNode={patchNode} t={t} />
        )}

        {node.kind === "chat" && <ChatInspector node={node} patchNode={patchNode} t={t} />}

        <OutgoingEdgesInspector
          edges={state.spec.edges.filter((e) => e.from === nodeId)}
          sourceIsInput={node.kind === "input"}
          patchEdge={patchEdge}
          t={t}
        />
      </div>
    </div>
  );
}

// Need useState in the component file
import { useState } from "react";

function EngineInspector({
  node,
  engineDefs,
  patchNode,
  t,
}: {
  node: WorkflowNode;
  engineDefs: EngineDef[];
  patchNode: (id: string, patch: Partial<Omit<WorkflowNode, "id">>) => void;
  t: ReturnType<typeof useTranslations>;
}) {
  const config = (node.config ?? {}) as { engine_def_id?: string; model?: string };
  return (
    <>
      <label className="flex flex-col gap-1">
        <SectionLabel>{t("engineDef")}</SectionLabel>
        <select
          value={config.engine_def_id ?? ""}
          onChange={(e) =>
            patchNode(node.id, { config: { ...config, engine_def_id: e.target.value } })
          }
          className="rounded border border-edge bg-surface-overlay px-2 py-1 text-[length:var(--t-sm)] text-content-primary focus:outline-none focus:ring-1 focus:ring-accent"
        >
          <option value="">{t("engineDefPlaceholder")}</option>
          {engineDefs.map((d) => (
            <option key={d.id} value={d.id}>
              {d.name}
            </option>
          ))}
        </select>
      </label>
      <label className="flex flex-col gap-1">
        <SectionLabel>{t("engineModel")}</SectionLabel>
        <input
          type="text"
          value={config.model ?? ""}
          onChange={(e) =>
            patchNode(node.id, { config: { ...config, model: e.target.value || undefined } })
          }
          placeholder={t("engineModelPlaceholder")}
          className="rounded border border-edge bg-surface-overlay px-2 py-1 text-[length:var(--t-sm)] text-content-primary focus:outline-none focus:ring-1 focus:ring-accent"
        />
      </label>
    </>
  );
}

function OutgoingEdgesInspector({
  edges,
  sourceIsInput,
  patchEdge,
  t,
}: {
  edges: WorkflowEdge[];
  sourceIsInput: boolean;
  patchEdge: (edgeId: string, patch: Partial<Omit<WorkflowEdge, "id">>) => void;
  t: ReturnType<typeof useTranslations>;
}) {
  return (
    <div className="flex flex-col gap-2">
      <SectionLabel>{t("outgoingEdges")}</SectionLabel>
      {edges.length === 0 ? (
        <span className="text-[length:var(--t-xs)] text-content-muted">{t("noOutgoingEdges")}</span>
      ) : (
        edges.map((edge) => {
          const condition = edge.condition ?? "";
          const invalid = looksInvalidCondition(condition);
          return (
            <div key={edge.id} className="flex flex-col gap-1 rounded border border-edge p-2">
              <span className="text-[length:var(--t-xs)] text-content-muted">
                {t("edgeConditionTarget", { target: edge.to })}
                {edge.label ? ` (${edge.label})` : ""}
              </span>
              {sourceIsInput ? (
                // An edge from an input node carries no runtime gate — the
                // compiler rejects a condition on it. Explain instead of
                // offering an input that always fails on save.
                <span className="text-[length:var(--t-xs)] text-content-muted">
                  {t("edgeConditionInputUnsupported")}
                </span>
              ) : (
                <>
                  <input
                    type="text"
                    value={condition}
                    onChange={(e) => {
                      if (e.nativeEvent instanceof InputEvent && e.nativeEvent.isComposing) return;
                      patchEdge(edge.id, { condition: e.target.value });
                    }}
                    onKeyDown={(e) => {
                      if (e.nativeEvent.isComposing) return;
                    }}
                    placeholder={t("edgeConditionPlaceholder")}
                    className="rounded border border-edge bg-surface-overlay px-2 py-1 font-data text-[length:var(--t-sm)] text-content-primary focus:outline-none focus:ring-1 focus:ring-accent"
                  />
                  <span
                    className="text-[length:var(--t-xs)]"
                    style={{ color: invalid ? "var(--status-warning)" : "var(--content-muted)" }}
                  >
                    {invalid ? t("edgeConditionWarning") : t("edgeConditionHint")}
                  </span>
                </>
              )}
            </div>
          );
        })
      )}
    </div>
  );
}

function ChatInspector({
  node,
  patchNode,
  t,
}: {
  node: WorkflowNode;
  patchNode: (id: string, patch: Partial<Omit<WorkflowNode, "id">>) => void;
  t: ReturnType<typeof useTranslations>;
}) {
  const config = (node.config ?? {}) as { prompt?: string };
  return (
    <label className="flex flex-col gap-1">
      <SectionLabel>{t("chatPrompt")}</SectionLabel>
      <textarea
        value={config.prompt ?? ""}
        onChange={(e) => patchNode(node.id, { config: { ...config, prompt: e.target.value } })}
        rows={4}
        placeholder={t("chatPromptPlaceholder")}
        className="resize-none rounded border border-edge bg-surface-overlay p-2 font-data text-[length:var(--t-sm)] text-content-primary focus:outline-none focus:ring-1 focus:ring-accent"
      />
    </label>
  );
}
