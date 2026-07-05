/**
 * EdgeInspector — what one hand-off means. Opens when an edge (or its label
 * chip) is clicked: signal identity, route, the FULL trigger condition, what
 * bounds re-entry, and the judge gate. The chip on the canvas shows a
 * fragment; this is the whole sentence.
 */
import { useTranslations } from "use-intl";
import type { FlowEdge, FlowModel } from "@/lib/designer/flow";
import { IconShield, IconClose, IconTrash } from "@/components/ui/icons";
import IconButton from "@/components/ui/IconButton";
import InspectorMetaRow from "@/components/ui/InspectorMetaRow";

type SemanticKind = "reaction" | "loop" | "pipeline" | "quiescence";

function semanticKind(edge: FlowEdge): SemanticKind {
  if (edge.kind === "quiescence") return "quiescence";
  if (edge.kind === "loop" || edge.kind === "self") return "loop";
  return edge.signal ? "reaction" : "pipeline";
}

const KIND_KEY: Record<SemanticKind, string> = {
  reaction: "kindReaction",
  loop: "kindLoop",
  pipeline: "kindPipeline",
  quiescence: "kindQuiescence",
};

const DESC_KEY: Record<SemanticKind, string> = {
  reaction: "descReaction",
  loop: "descLoop",
  pipeline: "descPipeline",
  quiescence: "descQuiescence",
};

export interface EdgeInspectorProps {
  edge: FlowEdge;
  model: FlowModel;
  onTrace?: (signal: string) => void;
  onClose: () => void;
  onDelete?: () => void;
}

export default function EdgeInspector({
  edge,
  model,
  onTrace,
  onClose,
  onDelete,
}: EdgeInspectorProps) {
  const t = useTranslations("designer.edge");
  const sem = semanticKind(edge);

  const labelOf = (id: string) => {
    const n = model.nodes.find((nn) => nn.id === id);
    if (!n) return id;
    return n.kind === "group" ? (n.label ?? n.id) : n.stages[0].label;
  };

  const title = sem === "quiescence" ? t("kindQuiescence") : (edge.signal ?? t("kindPipeline"));

  return (
    <div className="flex max-h-full w-[264px] flex-col overflow-hidden rounded-md border border-edge-strong bg-surface-raised shadow-lg">
      <div className="flex h-8 shrink-0 items-center gap-1.5 border-b border-edge px-2.5">
        <span
          aria-hidden="true"
          className="h-2 w-2 shrink-0 rounded-full"
          style={{ background: edge.color }}
        />
        <span className="min-w-0 flex-1 truncate font-data text-[length:var(--t-sm)] font-semibold text-content-primary">
          {sem === "loop" ? `↺ ${title}` : title}
        </span>
        <span className="shrink-0 font-ui text-[length:var(--t-xs)] uppercase tracking-wider text-content-muted">
          {t(KIND_KEY[sem])}
        </span>
        {onDelete && (
          <IconButton
            aria-label="Delete edge"
            title="Delete edge"
            onClick={onDelete}
            className="text-status-danger hover:text-status-danger"
          >
            <IconTrash size={11} />
          </IconButton>
        )}
        <IconButton aria-label={t("close")} onClick={onClose}>
          <IconClose size={11} />
        </IconButton>
      </div>

      <div className="flex min-h-0 flex-1 flex-col gap-2.5 overflow-y-auto px-3 py-2.5">
        {/* Route — a self-loop names its unit once instead of X → X */}
        <div className="flex items-center gap-1.5 overflow-hidden whitespace-nowrap font-data text-[length:var(--t-sm)]">
          {edge.from === edge.to ? (
            <>
              <span aria-hidden="true" className="shrink-0 text-content-muted">
                ↺
              </span>
              <span className="truncate text-content-primary">{labelOf(edge.from)}</span>
            </>
          ) : (
            <>
              <span className="truncate text-content-primary">{labelOf(edge.from)}</span>
              <span aria-hidden="true" className="shrink-0 text-content-muted">
                →
              </span>
              <span className="truncate text-content-primary">{labelOf(edge.to)}</span>
            </>
          )}
        </div>

        {/* What this hand-off means at run time */}
        <div className="font-ui text-[length:var(--t-xs)] leading-relaxed text-content-secondary">
          {t(DESC_KEY[sem])}
        </div>

        {edge.condition && <InspectorMetaRow label={t("when")}>{edge.condition}</InspectorMetaRow>}
        {edge.bound && <InspectorMetaRow label={t("bound")}>{edge.bound}</InspectorMetaRow>}

        {edge.judgeGated && (
          <div className="flex items-start gap-1.5">
            <span className="mt-px shrink-0 text-accent">
              <IconShield size={11} />
            </span>
            <span className="font-ui text-[length:var(--t-xs)] leading-relaxed text-content-secondary">
              {t("judgeDesc")}
            </span>
          </div>
        )}

        {edge.signal && onTrace && (
          <button
            type="button"
            onClick={() => onTrace(edge.signal!)}
            className="self-start rounded border border-edge bg-surface-overlay px-2 py-0.5 font-ui text-[length:var(--t-xs)] text-content-secondary hover:border-edge-strong hover:text-content-primary"
          >
            {t("trace")}
          </button>
        )}
      </div>
    </div>
  );
}
