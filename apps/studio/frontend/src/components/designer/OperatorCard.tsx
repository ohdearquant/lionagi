/**
 * OperatorCard — one node on the blueprint canvas, port-based:
 * input ports (what it observes) anchor on the LEFT border, output ports
 * (what it emits — including `handoff`, the final response hand-off) anchor
 * on the RIGHT border. Edges land exactly on these rows, so causality reads
 * left→right at the card itself.
 *
 * Three faces share the frame:
 *   entry  — compact identity + output ports;
 *   op     — identity + executor binding + ports;
 *   group  — composite spawn unit: spawn rule + member rows (sequential
 *            order) + boundary ports. Members select individually.
 */
import { memo } from "react";
import type { FlowNode, PortSpec } from "@/lib/designer/flow";
import { HANDOFF, MEMBER_H, QUIESCENCE } from "@/lib/designer/flow";
import type { TopologyStage } from "@/lib/designer/topology";
import { resolveStageModel } from "@/lib/designer/topology";
import { IconAgent, IconTeam, IconTool, IconSynth, IconInput } from "@/components/ui/icons";

const SELECTED_RING =
  "0 0 0 1px var(--accent), 0 0 0 4px color-mix(in srgb, var(--accent) 18%, transparent)";

function kindGlyph(kind: TopologyStage["kind"], size = 12) {
  switch (kind) {
    case "agent":
      return <IconAgent size={size} />;
    case "team":
      return <IconTeam size={size} />;
    case "tool":
      return <IconTool size={size} />;
    case "synth":
      return <IconSynth size={size} />;
    case "input":
      return <IconInput size={size} />;
  }
}

/** One port row — dot rides the card border, name sits inside. */
function PortRow({ port, quiescenceLabel }: { port: PortSpec; quiescenceLabel: string }) {
  const isIn = port.side === "in";
  const label = port.name === QUIESCENCE ? quiescenceLabel : port.name;
  return (
    <div
      className={`absolute flex items-center gap-1.5 ${isIn ? "left-0 pl-2" : "right-0 justify-end pr-2"}`}
      style={{ top: port.relY - 9, height: 18, maxWidth: "88%" }}
      title={port.name}
    >
      {/* Dot centered on the border */}
      <span
        aria-hidden="true"
        className={`absolute top-1/2 h-2 w-2 -translate-y-1/2 rounded-full ${
          port.system ? "border bg-surface-raised" : ""
        }`}
        style={{
          [isIn ? "left" : "right"]: -4.5,
          background: port.system ? undefined : port.color,
          borderColor: port.system ? port.color : undefined,
        }}
      />
      <span
        className={`truncate font-data text-[length:var(--t-xs)] ${
          port.system ? "italic text-content-muted" : "text-content-secondary"
        } ${isIn ? "pl-2" : "pr-2"}`}
      >
        {port.name === HANDOFF ? "handoff" : label}
      </span>
    </div>
  );
}

export interface OperatorCardProps {
  node: FlowNode;
  x: number;
  y: number;
  defModel?: string | null;
  /** Per-stage draft overrides — shown in place of the engine defaults. */
  overrides?: Record<string, { role?: string; model?: string } | undefined>;
  selectedId: string | null;
  onSelect: (id: string) => void;
  /** Localized label for the quiescence system port. */
  quiescenceLabel: string;
}

function OperatorCard({
  node,
  x,
  y,
  defModel,
  overrides,
  selectedId,
  onSelect,
  quiescenceLabel,
}: OperatorCardProps) {
  const isGroup = node.kind === "group";
  const stage = node.stages[0];
  const isEntry = !isGroup && stage.kind === "input";
  const selected = isGroup ? node.stages.some((s) => s.id === selectedId) : selectedId === stage.id;

  const ports = (
    <>
      {node.inPorts.map((p) => (
        <PortRow key={`in-${p.name}`} port={p} quiescenceLabel={quiescenceLabel} />
      ))}
      {node.outPorts.map((p) => (
        <PortRow key={`out-${p.name}`} port={p} quiescenceLabel={quiescenceLabel} />
      ))}
    </>
  );

  const header = (label: string, kind: TopologyStage["kind"], exempt?: boolean) => (
    <div className="flex items-center gap-1.5 px-2.5" style={{ height: 26 }}>
      <span className="shrink-0 text-content-muted">{kindGlyph(kind)}</span>
      <span className="min-w-0 flex-1 truncate font-data text-[length:var(--t-sm)] font-semibold text-content-primary">
        {label}
      </span>
      {exempt && (
        <span
          className="shrink-0 rounded bg-surface-overlay px-1 py-px font-data text-[10px] text-content-muted"
          title="Runs past budget exhaustion"
        >
          exempt
        </span>
      )}
      <span className="shrink-0 font-data text-[10px] uppercase tracking-[0.08em] text-content-muted">
        {node.typeLabel}
      </span>
    </div>
  );

  if (isGroup) {
    return (
      <div
        className="absolute rounded-md border border-edge-hairline bg-surface-raised transition-shadow duration-100"
        style={{
          left: x,
          top: y,
          width: node.w,
          height: node.h,
          boxShadow: selected ? SELECTED_RING : "none",
        }}
      >
        {header(node.label ?? node.id, "team")}
        {node.spawnRule && (
          <div
            className="flex items-center overflow-hidden whitespace-nowrap px-2.5"
            style={{ height: 16 }}
          >
            <span className="truncate font-data text-[10px] text-content-muted">
              {node.spawnRule}
            </span>
          </div>
        )}
        {/* Members — the sequential unit, each row selects its stage */}
        {(node.members ?? []).map((m, i) => {
          const ov = overrides?.[m.stage.id];
          const memberSelected = selectedId === m.stage.id;
          return (
            <button
              key={m.stage.id}
              type="button"
              data-stage-id={m.stage.id}
              onClick={(e) => {
                e.stopPropagation();
                onSelect(m.stage.id);
              }}
              className={`absolute left-1.5 right-1.5 flex items-center gap-1.5 rounded border px-1.5 text-left transition-colors duration-100 ${
                memberSelected
                  ? "border-accent bg-surface-overlay"
                  : "border-transparent hover:bg-surface-overlay"
              }`}
              style={{ top: m.relY + 1, height: MEMBER_H - 2 }}
            >
              <span className="w-3 shrink-0 text-center font-data text-[10px] text-content-muted">
                {i + 1}
              </span>
              <span className="min-w-0 flex-1 truncate font-data text-[length:var(--t-xs)] text-content-primary">
                {m.stage.label}
              </span>
              <span
                className={`shrink-0 truncate font-data text-[10px] ${
                  ov?.role?.trim() ? "text-accent" : "text-content-muted"
                }`}
              >
                {ov?.role?.trim() || m.stage.role}
              </span>
            </button>
          );
        })}
        {ports}
      </div>
    );
  }

  const roleOverridden = Boolean(overrides?.[stage.id]?.role?.trim());
  const modelOverridden = Boolean(overrides?.[stage.id]?.model?.trim());
  const role = roleOverridden ? overrides![stage.id]!.role!.trim() : stage.role;
  const model = modelOverridden ? overrides![stage.id]!.model!.trim() : resolveStageModel(defModel);
  const showBinding = !isEntry && (stage.role || stage.modelStage != null);

  return (
    <button
      type="button"
      data-stage-id={stage.id}
      onClick={(e) => {
        e.stopPropagation();
        onSelect(stage.id);
      }}
      title={stage.note || undefined}
      className="absolute flex flex-col items-stretch justify-start rounded-md border border-edge-hairline bg-surface-raised text-left transition-shadow duration-100"
      style={{
        left: x,
        top: y,
        width: node.w,
        height: node.h,
        boxShadow: selected ? SELECTED_RING : "none",
      }}
    >
      {header(stage.label, stage.kind, stage.exempt)}
      {showBinding && (
        <div
          className="flex items-center gap-1.5 overflow-hidden whitespace-nowrap px-2.5"
          style={{ height: 20 }}
        >
          {role && (
            <span
              className={`shrink-0 rounded border px-1 py-px font-data text-[10px] ${
                roleOverridden
                  ? "border-accent text-content-primary"
                  : "border-edge text-content-secondary"
              }`}
            >
              {role}
            </span>
          )}
          <span
            className={`truncate font-data text-[10px] ${
              modelOverridden ? "font-semibold text-content-secondary" : "text-content-muted"
            }`}
          >
            {model}
          </span>
        </div>
      )}
      {ports}
    </button>
  );
}

export default memo(OperatorCard);
