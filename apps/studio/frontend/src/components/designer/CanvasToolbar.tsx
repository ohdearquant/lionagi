/**
 * CanvasToolbar — floating editing dock at the bottom of the designer canvas.
 * Dispatches topology mutations through DesignerDraftContext.patchTopo.
 * Popovers open upward.
 *
 * Tool slots:
 *   Add Operator — dropdown of roles, inserts a new stage + a seq edge from
 *                  the current tail stage.
 *   Add Signal   — inline popover to name + color a new signal, then assigns
 *                  it to one emitter stage.
 *   Delete       — active only when a node or edge is selected.
 *   Zoom group   — fit / zoom-in / zoom-out (callbacks from FlowCanvas).
 */
import { useCallback, useRef, useState } from "react";
import { useTranslations } from "use-intl";
import type { EngineTopology, TopologyStage } from "@/lib/designer/topology";
import { SIGNAL_PALETTE } from "@/lib/designer/flow";
import {
  IconAgent,
  IconClose,
  IconFitView,
  IconMinus,
  IconPlus,
  IconSignal,
  IconTrash,
} from "@/components/ui/icons";
import { useDesignerDraft } from "./DesignerDraftContext";

const ROLES = [
  "researcher",
  "analyst",
  "critic",
  "implementer",
  "tester",
  "reviewer",
  "synthesizer",
] as const;

function nextId(prefix: string, topo: EngineTopology): string {
  let n = topo.stages.length + 1;
  while (topo.stages.some((s) => s.id === `${prefix}_${n}`)) n++;
  return `${prefix}_${n}`;
}

export interface CanvasToolbarProps {
  selectedId: string | null;
  onZoomIn: () => void;
  onZoomOut: () => void;
  onFit: () => void;
  onDeselect: () => void;
}

export default function CanvasToolbar({
  selectedId,
  onZoomIn,
  onZoomOut,
  onFit,
  onDeselect,
}: CanvasToolbarProps) {
  const t = useTranslations("designer.toolbar");
  const ctx = useDesignerDraft();
  const [addOpOpen, setAddOpOpen] = useState(false);
  const [addSigOpen, setAddSigOpen] = useState(false);
  const [sigName, setSigName] = useState("");
  const [sigColor, setSigColor] = useState(SIGNAL_PALETTE[0]);
  const [sigEmitter, setSigEmitter] = useState("");
  const [deleteConfirm, setDeleteConfirm] = useState(false);
  const addOpRef = useRef<HTMLDivElement>(null);
  const addSigRef = useRef<HTMLDivElement>(null);

  const handleAddOperator = useCallback(
    (role: string) => {
      if (!ctx) return;
      setAddOpOpen(false);
      ctx.patchTopo((topo) => {
        const id = nextId(role, topo);
        const newStage: TopologyStage = {
          id,
          label: role,
          role,
          kind: "agent",
          modelStage: role,
          emits: [],
        };
        // Attach a seq edge from the last non-input stage (or from the first input).
        const lastStage =
          [...topo.stages].reverse().find((s) => s.kind !== "input") ??
          topo.stages[topo.stages.length - 1];
        const newEdge = lastStage
          ? { from: lastStage.id, to: id, kind: "seq" as const }
          : undefined;
        return {
          ...topo,
          stages: [...topo.stages, newStage],
          edges: newEdge ? [...topo.edges, newEdge] : topo.edges,
        };
      });
    },
    [ctx],
  );

  const handleAddSignal = useCallback(() => {
    if (!ctx || !sigName.trim()) return;
    setAddSigOpen(false);
    const name = sigName.trim();
    const emitterId = sigEmitter;
    setSigName("");
    setSigColor(SIGNAL_PALETTE[0]);
    setSigEmitter("");
    // color assignment is handled by deriveFlow via SIGNAL_PALETTE appearance order
    ctx.patchTopo((topo) => ({
      ...topo,
      stages: topo.stages.map((s) =>
        s.id === emitterId && !s.emits.includes(name) ? { ...s, emits: [...s.emits, name] } : s,
      ),
    }));
  }, [ctx, sigName, sigEmitter]);

  const handleDelete = useCallback(() => {
    if (!ctx || !selectedId) return;
    setDeleteConfirm(false);
    ctx.patchTopo((topo) => {
      // Check if selectedId is a stage
      const isStage = topo.stages.some((s) => s.id === selectedId);
      if (isStage) {
        return {
          ...topo,
          stages: topo.stages.filter((s) => s.id !== selectedId),
          edges: topo.edges.filter((e) => e.from !== selectedId && e.to !== selectedId),
        };
      }
      // Otherwise treat as an edge id — edges are identified by "from:to:on" or similar;
      // selectedId from FlowCanvas is a stage node id, so this branch covers future edge deletion
      return topo;
    });
    onDeselect();
  }, [ctx, selectedId, onDeselect]);

  if (!ctx) return null;
  const { topo } = ctx;
  const emitterCandidates = topo.stages.filter((s) => s.kind === "agent" || s.kind === "synth");
  // A stage is deletable if it was added by the user (not in the original static topology).
  // We mark custom stages by checking if their id matches the nextId pattern.
  // For simplicity: any stage with a role the user added via toolbar can be deleted.
  // The static topology stages should remain; we allow deletion of any stage for now.
  const canDelete = Boolean(selectedId && topo.stages.some((s) => s.id === selectedId));

  return (
    <div
      data-flow-stop
      className="relative flex items-center gap-px overflow-visible rounded-md border border-edge bg-surface-raised/95 shadow-card"
      style={{ backdropFilter: "blur(8px)" }}
    >
      {/* Add Operator */}
      <div ref={addOpRef} className="relative">
        <button
          type="button"
          aria-label={t("addOperator")}
          title={t("addOperator")}
          onClick={() => {
            setAddOpOpen((v) => !v);
            setAddSigOpen(false);
            setDeleteConfirm(false);
          }}
          className="flex h-8 items-center gap-1.5 rounded-l-md px-2.5 text-content-secondary hover:bg-surface-overlay hover:text-content-primary"
        >
          <IconAgent size={13} />
          <span className="font-ui text-[length:var(--t-xs)]">{t("addOperator")}</span>
          <span className="font-ui text-[length:var(--t-xs)] text-content-muted">▴</span>
        </button>
        {addOpOpen && (
          <div className="absolute bottom-full left-0 mb-1 w-44 rounded-md border border-edge bg-surface-raised py-1 shadow-card">
            {ROLES.map((role) => (
              <button
                key={role}
                type="button"
                onClick={() => handleAddOperator(role)}
                className="flex w-full items-center gap-2 px-3 py-1.5 text-left hover:bg-surface-overlay"
              >
                <IconAgent size={11} className="text-content-muted" />
                <span className="font-data text-[length:var(--t-xs)] text-content-primary">
                  {role}
                </span>
              </button>
            ))}
          </div>
        )}
      </div>

      <span aria-hidden className="h-5 w-px bg-edge" />

      {/* Add Signal */}
      <div ref={addSigRef} className="relative">
        <button
          type="button"
          aria-label={t("addSignal")}
          title={t("addSignal")}
          onClick={() => {
            setAddSigOpen((v) => !v);
            setAddOpOpen(false);
            setDeleteConfirm(false);
          }}
          className="flex h-8 items-center gap-1.5 px-2.5 text-content-secondary hover:bg-surface-overlay hover:text-content-primary"
        >
          <IconSignal size={13} />
          <span className="font-ui text-[length:var(--t-xs)]">{t("addSignal")}</span>
          <span className="font-ui text-[length:var(--t-xs)] text-content-muted">▴</span>
        </button>
        {addSigOpen && (
          <div className="absolute bottom-full left-0 mb-1 w-60 rounded-md border border-edge bg-surface-raised p-3 shadow-card">
            <div className="flex flex-col gap-2">
              <label className="flex flex-col gap-1">
                <span className="font-ui text-[length:var(--t-xs)] text-content-muted">
                  {t("signalName")}
                </span>
                <input
                  type="text"
                  value={sigName}
                  onChange={(e) => setSigName(e.target.value)}
                  placeholder={t("signalPlaceholder")}
                  className="h-7 rounded border border-edge bg-surface-overlay px-2 font-data text-[length:var(--t-xs)] text-content-primary placeholder:text-content-muted focus:outline-none focus:ring-1 focus:ring-accent"
                />
              </label>
              <label className="flex flex-col gap-1">
                <span className="font-ui text-[length:var(--t-xs)] text-content-muted">
                  {t("color")}
                </span>
                <div className="flex flex-wrap gap-1.5">
                  {SIGNAL_PALETTE.map((c) => (
                    <button
                      key={c}
                      type="button"
                      aria-label={t("colorAria", { color: c })}
                      onClick={() => setSigColor(c)}
                      className="h-5 w-5 rounded-full border-2 transition-transform hover:scale-110"
                      style={{
                        background: c,
                        borderColor: sigColor === c ? "var(--content-primary)" : "transparent",
                      }}
                    />
                  ))}
                </div>
              </label>
              {emitterCandidates.length > 0 && (
                <label className="flex flex-col gap-1">
                  <span className="font-ui text-[length:var(--t-xs)] text-content-muted">
                    {t("emittingOperator")}
                  </span>
                  <select
                    value={sigEmitter}
                    onChange={(e) => setSigEmitter(e.target.value)}
                    className="h-7 rounded border border-edge bg-surface-overlay px-2 font-data text-[length:var(--t-xs)] text-content-primary focus:outline-none focus:ring-1 focus:ring-accent"
                  >
                    <option value="">{t("none")}</option>
                    {emitterCandidates.map((s) => (
                      <option key={s.id} value={s.id}>
                        {s.label}
                      </option>
                    ))}
                  </select>
                </label>
              )}
              <div className="flex gap-2 pt-1">
                <button
                  type="button"
                  onClick={handleAddSignal}
                  disabled={!sigName.trim()}
                  className="flex h-7 flex-1 items-center justify-center rounded bg-accent px-3 font-ui text-[length:var(--t-xs)] text-white disabled:cursor-not-allowed disabled:opacity-40 hover:opacity-90"
                >
                  {t("add")}
                </button>
                <button
                  type="button"
                  onClick={() => setAddSigOpen(false)}
                  className="flex h-7 items-center justify-center rounded border border-edge px-2 text-content-muted hover:text-content-primary"
                >
                  <IconClose size={10} strokeWidth={2} />
                </button>
              </div>
            </div>
          </div>
        )}
      </div>

      <span aria-hidden className="h-5 w-px bg-edge" />

      {/* Delete selected */}
      {canDelete ? (
        <div className="relative">
          {deleteConfirm ? (
            <div className="flex items-center gap-1 px-2">
              <span className="font-ui text-[length:var(--t-xs)] text-content-secondary">
                {t("removeConfirm")}
              </span>
              <button
                type="button"
                onClick={handleDelete}
                className="rounded px-1.5 py-0.5 font-ui text-[length:var(--t-xs)] text-status-danger hover:bg-surface-overlay"
              >
                {t("yes")}
              </button>
              <button
                type="button"
                onClick={() => setDeleteConfirm(false)}
                className="rounded px-1.5 py-0.5 font-ui text-[length:var(--t-xs)] text-content-muted hover:bg-surface-overlay"
              >
                {t("no")}
              </button>
            </div>
          ) : (
            <button
              type="button"
              aria-label={t("deleteSelectedTitle")}
              title={t("deleteSelectedTitle")}
              onClick={() => {
                setDeleteConfirm(true);
                setAddOpOpen(false);
                setAddSigOpen(false);
              }}
              className="flex h-8 items-center gap-1.5 px-2.5 text-status-danger hover:bg-surface-overlay"
            >
              <IconTrash size={13} />
              <span className="font-ui text-[length:var(--t-xs)]">{t("delete")}</span>
            </button>
          )}
        </div>
      ) : (
        <button
          type="button"
          aria-label={t("deleteDisabledTitle")}
          title={t("deleteDisabledTitle")}
          disabled
          className="flex h-8 items-center gap-1.5 px-2.5 text-content-muted opacity-40"
        >
          <IconTrash size={13} />
          <span className="font-ui text-[length:var(--t-xs)]">{t("delete")}</span>
        </button>
      )}

      <span aria-hidden className="h-5 w-px bg-edge" />

      {/* Zoom controls */}
      <button
        type="button"
        aria-label={t("fitView")}
        title={t("fitView")}
        onClick={onFit}
        className="flex h-8 w-8 items-center justify-center text-content-secondary hover:bg-surface-overlay hover:text-content-primary"
      >
        <IconFitView size={13} strokeWidth={1.5} />
      </button>
      <button
        type="button"
        aria-label={t("zoomIn")}
        title={t("zoomIn")}
        onClick={onZoomIn}
        className="flex h-8 w-8 items-center justify-center text-content-secondary hover:bg-surface-overlay hover:text-content-primary"
      >
        <IconPlus size={12} strokeWidth={2} />
      </button>
      <button
        type="button"
        aria-label={t("zoomOut")}
        title={t("zoomOut")}
        onClick={onZoomOut}
        className="flex h-8 w-8 items-center justify-center rounded-r-md text-content-secondary hover:bg-surface-overlay hover:text-content-primary"
      >
        <IconMinus size={12} strokeWidth={2} />
      </button>
    </div>
  );
}
