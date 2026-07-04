/**
 * AdvancedDrawer — engine-wide settings behind a toggle: the persisted knobs
 * (model route, max_agents, max_depth, test_cmd), description, export_dir,
 * the engine's read-only internal defaults, and the request preview. The
 * canvas stays about topology; every engine-level knob lives here.
 */
import { useTranslations } from "use-intl";
import type { EngineDefDraft } from "@/lib/designer/draft";
import { buildDefBody } from "@/lib/designer/draft";
import { ENGINE_TOPOLOGIES } from "@/lib/designer/topology";
import { IconInfo } from "@/components/ui/icons";
import { FieldLabel, Input, TextArea } from "@/components/ui/Field";

export interface AdvancedDrawerProps {
  draft: EngineDefDraft;
  patchDraft: (patch: Partial<EngineDefDraft>) => void;
}

export default function AdvancedDrawer({ draft, patchDraft }: AdvancedDrawerProps) {
  const t = useTranslations("designer.advanced");
  const tHeader = useTranslations("designer.header");
  const topo = ENGINE_TOPOLOGIES[draft.kind];
  const requestJson = JSON.stringify(buildDefBody(draft), null, 2);

  return (
    <aside className="flex w-80 shrink-0 flex-col gap-3.5 overflow-y-auto border-l border-edge bg-surface-raised px-4 py-3.5">
      <div className="font-ui text-[length:var(--t-xs)] font-semibold uppercase tracking-[0.08em] text-content-secondary">
        {t("title")}
      </div>

      {/* Persisted engine knobs — knob names are code identities, mono */}
      <FieldLabel label="model">
        <Input
          type="text"
          mono
          value={draft.model}
          onChange={(e) => patchDraft({ model: e.target.value })}
          placeholder={tHeader("providerDefault")}
        />
      </FieldLabel>
      <div className="grid grid-cols-2 gap-2.5">
        <FieldLabel label="max_agents">
          <Input
            type="text"
            mono
            inputMode="numeric"
            value={draft.max_agents}
            onChange={(e) => patchDraft({ max_agents: e.target.value })}
            placeholder="50"
          />
        </FieldLabel>
        {topo.maxDepth.applies && (
          <FieldLabel label="max_depth">
            <Input
              type="text"
              mono
              inputMode="numeric"
              value={draft.max_depth}
              onChange={(e) => patchDraft({ max_depth: e.target.value })}
              placeholder={topo.defaults.max_depth ?? "3"}
            />
          </FieldLabel>
        )}
      </div>
      {topo.testCmd.applies && (
        <FieldLabel label="test_cmd">
          <Input
            type="text"
            mono
            value={draft.test_cmd}
            onChange={(e) => patchDraft({ test_cmd: e.target.value })}
            placeholder="pytest tests/"
          />
        </FieldLabel>
      )}

      <FieldLabel label={t("description")}>
        <TextArea
          value={draft.description}
          onChange={(e) => patchDraft({ description: e.target.value })}
          placeholder={t("descriptionPlaceholder")}
          rows={2}
        />
      </FieldLabel>

      <FieldLabel label={t("exportDir")}>
        <Input
          type="text"
          mono
          value={draft.export_dir}
          onChange={(e) => patchDraft({ export_dir: e.target.value })}
          placeholder="./output"
        />
      </FieldLabel>

      {/* Engine internals — truthful, read-only */}
      <div>
        <div className="mb-1.5 text-[length:var(--t-xs)] uppercase tracking-[0.06em] text-content-muted">
          {t("engineDefaults")}
        </div>
        <div className="flex flex-col gap-1 rounded border border-edge px-2.5 py-2">
          {Object.entries(topo.defaults).map(([k, v]) => (
            <div key={k} className="grid gap-1.5" style={{ gridTemplateColumns: "100px 1fr" }}>
              <span className="font-data text-[length:var(--t-xs)] text-content-muted">{k}</span>
              <span className="break-words font-data text-[length:var(--t-xs)] text-content-secondary">
                {v}
              </span>
            </div>
          ))}
          <div className="mt-1 font-ui text-[length:var(--t-xs)] text-content-muted">
            <IconInfo
              size={9}
              style={{ display: "inline", verticalAlign: "middle", marginRight: 3 }}
            />
            {t("sourceRef")} {topo.sourceRef}
          </div>
        </div>
      </div>

      <div>
        <div className="mb-1.5 text-[length:var(--t-xs)] uppercase tracking-[0.06em] text-content-muted">
          {t("requestPreview")}
        </div>
        <pre className="m-0 max-h-[200px] overflow-auto rounded border border-edge bg-surface-overlay px-2.5 py-2 font-data text-[length:var(--t-xs)] text-content-secondary">
          {requestJson}
        </pre>
      </div>
    </aside>
  );
}
