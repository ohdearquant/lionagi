/**
 * NodeInspector — the stage editor, docked as a full-height column on the
 * canvas's right edge. Docking (not floating) is the design: it can never
 * overlap the graph, and it has the room a real editor needs.
 *
 * Three honest layers, top to bottom:
 *   EDITABLE — the knobs the launch pipeline actually binds: the casts role
 *     (picked from the live catalog) and the model route, plus engine-wide
 *     bounds. The role IS the capability bundle — picking one swaps the
 *     stage's skills, modes, and structured output contract.
 *   ROLE BUNDLE — what the selected role resolves to right now, from
 *     /api/casts: description, cognitive modes, emission contract.
 *   CONTEXT — what flows in and out of this stage: every inbound hand-off
 *     and spawn trigger with its exact condition, every emission with where
 *     it lands. Code identities (roles, signals, conditions) stay mono and
 *     untranslated.
 */
import { useEffect, useState } from "react";
import { useTranslations } from "use-intl";
import type { FlowModel } from "@/lib/designer/flow";
import type { EngineTopology, TopologyStage, TopologyEdge } from "@/lib/designer/topology";
import { getCasts } from "@/lib/api";
import type { CastRole, CastsCatalog } from "@/lib/api";
import {
  IconShield,
  IconAgent,
  IconTeam,
  IconTool,
  IconSynth,
  IconInput,
} from "@/components/ui/icons";
import InspectorPanel from "@/components/ui/InspectorPanel";
import PropertyRow from "@/components/ui/PropertyRow";
import { useDesignerDraft } from "./DesignerDraftContext";
import KnobChip from "./KnobChip";

let castsCache: Promise<CastsCatalog> | null = null;
function loadCasts(): Promise<CastsCatalog> {
  castsCache ??= getCasts().catch(() => {
    castsCache = null;
    return { roles: [], modes: [] };
  });
  return castsCache;
}

function kindGlyph(kind: TopologyStage["kind"], size = 13) {
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

function Section({
  title,
  note,
  children,
}: {
  title: string;
  note?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex flex-col gap-1.5 border-b border-edge px-3.5 pb-3.5 pt-3 last:border-b-0">
      <div className="font-ui text-[length:var(--t-xs)] font-semibold uppercase tracking-[0.11em] text-content-muted">
        {title}
      </div>
      {note && (
        <div className="-mt-1 font-ui text-[length:var(--t-xs)] text-content-muted">{note}</div>
      )}
      {children}
    </div>
  );
}

function SignalDot({ color }: { color: string }) {
  return (
    <span
      aria-hidden="true"
      className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full"
      style={{ background: color }}
    />
  );
}

export interface NodeInspectorProps {
  stage: TopologyStage;
  topo: EngineTopology;
  model: FlowModel;
  onFocusSignal?: (signal: string) => void;
  onClose: () => void;
}

export default function NodeInspector({
  stage,
  topo,
  model,
  onFocusSignal,
  onClose,
}: NodeInspectorProps) {
  const t = useTranslations("designer.panel");
  const tEdge = useTranslations("designer.edge");
  const draftCtx = useDesignerDraft();
  const [roles, setRoles] = useState<CastRole[]>([]);
  useEffect(() => {
    let live = true;
    loadCasts().then((c) => {
      if (live) setRoles(c.roles);
    });
    return () => {
      live = false;
    };
  }, []);

  const laneColorOf = (signal: string) => model.signalColor[signal] ?? "var(--edge-strong)";
  const labelOf = (id: string) =>
    topo.stages.find((s) => s.id === id)?.label ?? topo.groups?.[id]?.label ?? id;

  const spawnRule = model.spawnRules[stage.id];
  const group = stage.group
    ? model.nodes.find((n) => n.kind === "group" && n.id === stage.group)
    : undefined;
  const selfIds = new Set([stage.id, ...(stage.group ? [stage.group] : [])]);
  const inbound = topo.edges.filter((e) => selfIds.has(e.to));
  const outboundSeq = topo.edges.filter((e) => selfIds.has(e.from) && e.kind === "seq");
  const consumersOf = (signal: string) => topo.edges.filter((e) => e.on === signal);

  const depthBound =
    topo.maxDepth.applies && inbound.some((e) => (e.condition ?? "").includes("max_depth"));
  const roleEditable = stage.role != null;
  const modelEditable = stage.modelStage != null;
  const stageOverride = draftCtx?.draft.stages[stage.id];
  const engineWide = stage.kind === "team" || stage.kind === "tool" || depthBound;
  const hasConfig = Boolean(draftCtx && (roleEditable || modelEditable || engineWide));

  const effectiveRole = stageOverride?.role?.trim() || stage.role || "";
  const bundle = roles.find((r) => r.name === effectiveRole);

  const edgeKindLabel = (kind: TopologyEdge["kind"]) =>
    kind === "seq"
      ? tEdge("kindPipeline")
      : kind === "loop"
        ? tEdge("kindLoop")
        : tEdge("kindReaction");

  return (
    <InspectorPanel
      className="w-[372px] shrink-0"
      title={
        <span className="flex min-w-0 items-center gap-2">
          <span className="shrink-0 text-content-muted">{kindGlyph(stage.kind, 14)}</span>
          <span className="min-w-0 flex-1 truncate">{stage.label}</span>
        </span>
      }
      trailing={
        <>
          {group && (
            <span className="truncate font-ui text-[length:var(--t-xs)] text-content-muted">
              {t("viaGroup", { group: group.label ?? group.id })}
            </span>
          )}
          <span className="shrink-0 font-data text-[length:var(--t-xs)] uppercase tracking-wider text-content-muted">
            {stage.kind}
          </span>
        </>
      }
      closeLabel={t("close")}
      onClose={onClose}
      footer={
        <div
          className="truncate px-3.5 py-1.5 font-data text-[length:var(--t-xs)] text-content-muted"
          title={topo.sourceRef}
        >
          {topo.sourceRef}
        </div>
      }
    >
      {/* What you can edit — exactly what the launch pipeline binds */}
      {hasConfig && draftCtx && (
        <Section title={t("config")} note={t("configNote")}>
          {roleEditable && (
            <label className="flex flex-col gap-1">
              <span className="font-ui text-[length:var(--t-xs)] text-content-muted">
                {t("role")} · {t("thisOperator")}
              </span>
              <select
                value={stageOverride?.role ?? ""}
                onChange={(e) => draftCtx.patchStage(stage.id, { role: e.target.value })}
                className="h-7 w-full rounded border border-edge bg-surface-overlay px-2 font-data text-[length:var(--t-sm)] text-content-primary"
              >
                <option value="">{t("roleDefault", { role: stage.role! })}</option>
                {roles
                  .filter((r) => r.name !== stage.role)
                  .map((r) => (
                    <option key={r.name} value={r.name}>
                      {r.name}
                    </option>
                  ))}
              </select>
            </label>
          )}
          {modelEditable && (
            <div className="flex flex-col gap-1">
              <span className="font-ui text-[length:var(--t-xs)] text-content-muted">
                {t("modelStage")} · {t("thisOperator")}
              </span>
              <KnobChip
                label="model"
                value={stageOverride?.model ?? ""}
                placeholder={
                  draftCtx.draft.model.trim() || topo.defaults.model || "provider default"
                }
                width={220}
                onCommit={(v) => draftCtx.patchStage(stage.id, { model: v })}
              />
            </div>
          )}
          {engineWide && (
            <div className="flex flex-col items-start gap-1.5 pt-1">
              <div className="font-ui text-[length:var(--t-xs)] text-content-muted">
                {t("engineWide")}
              </div>
              {stage.kind === "team" && (
                <KnobChip
                  label="max_agents"
                  value={draftCtx.draft.max_agents}
                  placeholder={topo.defaults.max_agents ?? "50"}
                  numeric
                  onCommit={(v) => draftCtx.patchDraft({ max_agents: v })}
                />
              )}
              {stage.kind === "tool" && topo.testCmd.applies && (
                <KnobChip
                  label="test_cmd"
                  value={draftCtx.draft.test_cmd}
                  placeholder="pytest tests/"
                  attention={topo.testCmd.required}
                  width={220}
                  onCommit={(v) => draftCtx.patchDraft({ test_cmd: v })}
                />
              )}
              {depthBound && (
                <KnobChip
                  label="max_depth"
                  value={draftCtx.draft.max_depth}
                  placeholder={topo.defaults.max_depth ?? "3"}
                  numeric
                  onCommit={(v) => draftCtx.patchDraft({ max_depth: v })}
                />
              )}
            </div>
          )}
        </Section>
      )}

      {/* What the chosen role resolves to, live from the casts catalog */}
      {bundle && (
        <Section title={t("bundle")} note={t("bundleNote")}>
          {bundle.description && (
            <div className="line-clamp-3 font-ui text-[length:var(--t-xs)] leading-relaxed text-content-secondary">
              {bundle.description}
            </div>
          )}
          {(bundle.config?.default_modes?.length ?? 0) > 0 && (
            <PropertyRow label={t("mode")}>
              <span className="flex min-w-0 flex-wrap gap-1">
                {bundle.config!.default_modes!.map((m) => (
                  <span
                    key={m}
                    className="rounded bg-surface-overlay px-1.5 py-px font-data text-[length:var(--t-xs)] text-content-secondary"
                  >
                    {m}
                  </span>
                ))}
              </span>
            </PropertyRow>
          )}
          {bundle.emits.length > 0 && (
            <div className="flex flex-col gap-1">
              <span className="font-ui text-[length:var(--t-xs)] text-content-muted">
                {t("structuredOutput")}
              </span>
              {bundle.emits.map((e) => (
                <div key={e.key} className="flex items-baseline gap-2 pl-2">
                  <span className="font-data text-[length:var(--t-sm)] text-content-primary">
                    {e.model}
                  </span>
                  <span className="font-data text-[length:var(--t-xs)] text-content-muted">
                    {e.key}
                  </span>
                </div>
              ))}
            </div>
          )}
        </Section>
      )}

      {/* Context — what flows in, with exact triggers */}
      <Section title={t("context")}>
        <div className="font-ui text-[length:var(--t-xs)] text-content-muted">{t("receives")}</div>
        {stage.kind === "input" ? (
          <div className="font-ui text-[length:var(--t-xs)] text-content-secondary">
            {t("entryNote")}
          </div>
        ) : inbound.length === 0 ? (
          <div className="font-data text-[length:var(--t-xs)] text-content-muted">—</div>
        ) : (
          <div className="flex flex-col gap-1.5">
            {inbound.map((e, i) => (
              <div key={`in-${i}`} className="flex gap-2">
                {e.on ? (
                  <SignalDot color={laneColorOf(e.on)} />
                ) : (
                  <SignalDot color="var(--edge-strong)" />
                )}
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-baseline gap-x-1.5">
                    {e.on ? (
                      <button
                        type="button"
                        onClick={() => onFocusSignal?.(e.on!)}
                        className="font-data text-[length:var(--t-sm)] text-content-primary hover:underline"
                      >
                        {e.on}
                      </button>
                    ) : (
                      <span className="font-data text-[length:var(--t-sm)] text-content-primary">
                        {labelOf(e.from)}
                      </span>
                    )}
                    <span className="font-ui text-[length:var(--t-xs)] text-content-muted">
                      {e.on
                        ? `${edgeKindLabel(e.kind)} · ${t("ctxFrom", { name: labelOf(e.from) })}`
                        : edgeKindLabel(e.kind)}
                    </span>
                    {e.judgeGated && (
                      <span className="text-accent" title="judge-gated">
                        <IconShield size={10} />
                      </span>
                    )}
                  </div>
                  {e.condition && (
                    <div className="font-data text-[length:var(--t-xs)] text-content-secondary">
                      {e.condition}
                    </div>
                  )}
                  {e.bound && (
                    <div className="font-data text-[length:var(--t-xs)] text-content-muted">
                      {e.bound}
                    </div>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}

        <div className="pt-1.5 font-ui text-[length:var(--t-xs)] text-content-muted">
          {t("produces")}
        </div>
        {stage.emits.length === 0 && outboundSeq.length === 0 ? (
          <div className="font-data text-[length:var(--t-xs)] text-content-muted">—</div>
        ) : (
          <div className="flex flex-col gap-1.5">
            {stage.emits.map((sig) => {
              const consumers = consumersOf(sig);
              return (
                <div key={sig} className="flex gap-2">
                  <SignalDot color={laneColorOf(sig)} />
                  <div className="min-w-0 flex-1">
                    <button
                      type="button"
                      onClick={() => onFocusSignal?.(sig)}
                      className="font-data text-[length:var(--t-sm)] text-content-primary hover:underline"
                    >
                      {sig}
                    </button>
                    <div className="font-ui text-[length:var(--t-xs)] text-content-muted">
                      {consumers.length > 0
                        ? consumers
                            .map((c) => `${edgeKindLabel(c.kind)} → ${labelOf(c.to)}`)
                            .join(" · ")
                        : t("toStore")}
                    </div>
                  </div>
                </div>
              );
            })}
            {outboundSeq.map((e, i) => (
              <div key={`out-${i}`} className="flex gap-2">
                <SignalDot color="var(--edge-strong)" />
                <span className="font-ui text-[length:var(--t-xs)] text-content-secondary">
                  {edgeKindLabel(e.kind)} →{" "}
                  <span className="font-data text-[length:var(--t-sm)] text-content-primary">
                    {labelOf(e.to)}
                  </span>
                </span>
              </div>
            ))}
          </div>
        )}
      </Section>

      {/* How the engine runs it — read-only, fixed in engine code */}
      <Section title={t("execution")} note={t("executionNote")}>
        <div className="flex flex-col gap-1">
          {stage.role && (
            <PropertyRow label={t("role")}>
              {stageOverride?.role?.trim() ? (
                <>
                  {stageOverride.role.trim()}{" "}
                  <span className="text-content-muted">· {stage.role}</span>
                </>
              ) : (
                stage.role
              )}
            </PropertyRow>
          )}
          {stage.modelStage && (
            <PropertyRow label={t("modelStage")}>{stage.modelStage}</PropertyRow>
          )}
          {stage.mode && <PropertyRow label={t("mode")}>{stage.mode}</PropertyRow>}
          <PropertyRow label={t("spawns")}>{spawnRule ?? "—"}</PropertyRow>
          {group && (
            <PropertyRow label={t("partOf")}>
              {`${group.label ?? group.id}${group.spawnRule ? ` · ${group.spawnRule}` : ""}`}
            </PropertyRow>
          )}
        </div>
        {stage.exempt && (
          <div className="font-data text-[length:var(--t-xs)] text-content-muted">
            {t("exempt")}
          </div>
        )}
        {stage.note && (
          <div className="font-ui text-[length:var(--t-xs)] text-content-secondary">
            {stage.note}
          </div>
        )}
      </Section>
    </InspectorPanel>
  );
}
