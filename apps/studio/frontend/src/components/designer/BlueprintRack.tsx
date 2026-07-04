/**
 * BlueprintRack — left rack of the launch console. Two sections:
 * the five engine-kind blueprints (always available starting points) and
 * the user's saved engine definitions. Clicking either loads it onto the
 * canvas. Visibility is owned by DesignerCanvas — re-clicking the designer
 * icon in the rail toggles it; opening a node inspector hides it.
 */
import { useEffect, useState } from "react";
import { useTranslations } from "use-intl";
import { ENGINE_KINDS, ENGINE_TOPOLOGIES, type EngineKind } from "@/lib/designer/topology";
import { deleteEngineDef, listEngineDefs } from "@/lib/api";
import type { EngineDef } from "@/lib/api";
import SectionLabel from "@/components/ui/SectionLabel";
import { IconClose } from "@/components/ui/icons";
import { useToast } from "@/components/ui/Toast";

export interface BlueprintRackProps {
  activeKind: EngineKind;
  /** Id of the loaded saved def, if any. */
  activeDefId: string | null;
  onPickKind: (kind: EngineKind) => void;
  onPickDef: (def: EngineDef) => void;
  /** Called after a successful delete so the canvas can drop a stale id. */
  onDeleted: (defId: string) => void;
  /** Bumped after every save so the rack refreshes its saved list. */
  refreshToken: number;
}

const SHAPE_GLYPH: Record<string, string> = {
  tree: "⊶",
  fanout: "⋔",
  chain: "→",
  cascade: "⇶",
  dag: "⊞",
};

export default function BlueprintRack({
  activeKind,
  activeDefId,
  onPickKind,
  onPickDef,
  onDeleted,
  refreshToken,
}: BlueprintRackProps) {
  const t = useTranslations("designer.rack");
  const { toast } = useToast();
  const [defs, setDefs] = useState<EngineDef[]>([]);
  const [loading, setLoading] = useState(true);
  /** Two-step delete: first click arms this row, second click executes. */
  const [armedId, setArmedId] = useState<string | null>(null);

  const handleDelete = async (def: EngineDef) => {
    if (armedId !== def.id) {
      setArmedId(def.id);
      return;
    }
    setArmedId(null);
    try {
      await deleteEngineDef(def.id);
      setDefs((list) => list.filter((d) => d.id !== def.id));
      onDeleted(def.id);
    } catch (err) {
      toast(String(err), "error");
    }
  };

  useEffect(() => {
    let alive = true;
    listEngineDefs()
      .then((list) => {
        if (!alive) return;
        setDefs(list);
        setLoading(false);
      })
      .catch(() => {
        if (!alive) return;
        setDefs([]);
        setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [refreshToken]);

  return (
    <div className="flex w-[264px] shrink-0 flex-col overflow-y-auto border-r border-edge bg-surface-raised">
      <div className="flex items-center px-3 pb-0 pt-2">
        <span className="font-ui text-[length:var(--t-xs)] font-semibold uppercase tracking-[0.08em] text-content-secondary">
          {t("title")}
        </span>
      </div>

      <div className="px-3 pb-1.5 pt-2.5">
        <SectionLabel>{t("blueprints")}</SectionLabel>
        <div className="mt-0.5 font-ui text-[length:var(--t-xs)] text-content-muted">
          {t("blueprintsHint")}
        </div>
      </div>
      <div className="flex flex-col gap-1 px-2">
        {ENGINE_KINDS.map((k) => {
          const topo = ENGINE_TOPOLOGIES[k];
          const active = k === activeKind && !activeDefId;
          return (
            <button
              key={k}
              type="button"
              onClick={() => onPickKind(k)}
              className={`flex flex-col gap-0.5 rounded-[5px] border px-2.5 py-[7px] text-left transition-[border-color,background] duration-100 ${
                active
                  ? "border-accent bg-surface-overlay"
                  : "border-edge bg-transparent hover:bg-surface-overlay"
              }`}
            >
              <span className="flex items-center gap-1.5">
                <span className="flex-1 font-data text-[length:var(--t-sm)] font-semibold text-content-primary">
                  {k}
                </span>
                <span
                  aria-hidden="true"
                  className="font-data text-[length:var(--t-sm)] text-content-muted"
                >
                  {SHAPE_GLYPH[topo.shape]}
                </span>
              </span>
              <span className="font-data text-[length:var(--t-xs)] text-content-muted">
                {topo.shape} · {topo.stages.length} {t("stages")}
              </span>
            </button>
          );
        })}
      </div>

      <div className="px-3 pb-1.5 pt-2.5">
        <SectionLabel>{t("saved")}</SectionLabel>
        <div className="mt-0.5 font-ui text-[length:var(--t-xs)] text-content-muted">
          {t("savedHint")}
        </div>
      </div>
      <div className="flex flex-col gap-0.5 px-2 pb-3">
        {loading ? (
          <span className="px-1 py-0.5 text-[length:var(--t-xs)] text-content-muted">
            {t("loading")}
          </span>
        ) : defs.length === 0 ? (
          <span className="px-1 py-0.5 text-[length:var(--t-xs)] leading-relaxed text-content-muted">
            {t("emptySaved")}
          </span>
        ) : (
          defs.map((d) => {
            const active = d.id === activeDefId;
            const armed = armedId === d.id;
            return (
              <div
                key={d.id}
                onPointerLeave={() => setArmedId((id) => (id === d.id ? null : id))}
                className={`group flex items-center rounded transition-[background,border-color] duration-100 ${
                  active
                    ? "border border-accent bg-surface-overlay"
                    : "border border-transparent bg-transparent hover:bg-surface-overlay"
                }`}
              >
                <button
                  type="button"
                  onClick={() => onPickDef(d)}
                  className="flex min-w-0 flex-1 items-center gap-1.5 py-[5px] pl-2 text-left"
                >
                  <span className="flex-1 truncate font-data text-[length:var(--t-sm)] text-content-primary">
                    {d.name}
                  </span>
                  <span className="shrink-0 font-data text-[length:var(--t-xs)] text-content-muted">
                    {d.kind}
                  </span>
                </button>
                <button
                  type="button"
                  aria-label={t("delete", { name: d.name })}
                  title={armed ? undefined : t("delete", { name: d.name })}
                  onClick={() => void handleDelete(d)}
                  className={`mx-1 flex h-5 shrink-0 items-center justify-center rounded transition-[background,color,opacity] duration-100 ${
                    armed
                      ? "bg-status-error-bg px-1.5 font-ui text-[length:var(--t-xs)] font-semibold text-status-error"
                      : "w-5 text-content-muted opacity-0 hover:bg-surface-base hover:text-content-primary focus-visible:opacity-100 group-hover:opacity-100"
                  }`}
                >
                  {armed ? t("deleteConfirm") : <IconClose size={9} />}
                </button>
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}
