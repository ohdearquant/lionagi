import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "@tanstack/react-router";
import { useTranslations } from "use-intl";
import { listWorkflowDefs, getWorkflowDef, updateWorkflowDef, listEngineDefs } from "@/lib/api";
import type { WorkflowDef, WorkflowSpec, EngineDef } from "@/lib/api";
import { emptySpec } from "@/lib/workflow/validation";
import SectionLabel from "@/components/ui/SectionLabel";
import WorkflowEditor from "./WorkflowEditor";
import { CreateWorkflowPanel } from "@/components/library/WorkflowDetail";

interface WorkflowDesignerProps {
  defId?: string | null;
}

/**
 * The Designer surface: author workflow definitions on a canvas with a
 * synchronized YAML view. Definitions live in the daemon's workflow-def
 * store; YAML is the canonical text form, TOML is accepted and produced at
 * the file import/export boundary.
 */
export default function WorkflowDesigner({ defId }: WorkflowDesignerProps) {
  const t = useTranslations("workflow");
  const navigate = useNavigate();

  const [defs, setDefs] = useState<WorkflowDef[]>([]);
  const [listError, setListError] = useState<string | null>(null);
  const [selected, setSelected] = useState<WorkflowDef | null>(null);
  const [engineDefs, setEngineDefs] = useState<EngineDef[]>([]);
  const [saving, setSaving] = useState(false);
  const [creating, setCreating] = useState(false);
  const [rackVisible, setRackVisible] = useState(true);

  useEffect(() => {
    const onTogglePane = () => setRackVisible((v) => !v);
    window.addEventListener("studio:toggle-pane", onTogglePane);
    return () => window.removeEventListener("studio:toggle-pane", onTogglePane);
  }, []);

  const refreshList = useCallback(async (): Promise<WorkflowDef[]> => {
    try {
      const list = await listWorkflowDefs();
      setDefs(list);
      setListError(null);
      return list;
    } catch (e) {
      setListError(e instanceof Error ? e.message : t("loadError"));
      return [];
    }
  }, [t]);

  useEffect(() => {
    /* eslint-disable react-hooks/set-state-in-effect */
    void refreshList();
    /* eslint-enable react-hooks/set-state-in-effect */
    listEngineDefs()
      .then(setEngineDefs)
      .catch(() => {});
  }, [refreshList]);

  useEffect(() => {
    if (!defId) return;
    let alive = true;
    getWorkflowDef(defId)
      .then((d) => {
        if (alive) setSelected(d);
      })
      .catch(() => {
        if (alive) setSelected(null);
      });
    return () => {
      alive = false;
    };
  }, [defId]);

  // Selection is derived: stale fetches for a previous defId never render.
  const active = defId && selected?.id === defId ? selected : null;

  const selectDef = useCallback(
    (id: string | null) => {
      void navigate({ to: "/designer", search: id ? { id } : {} });
    },
    [navigate],
  );

  const handleSave = useCallback(
    async (spec: WorkflowSpec) => {
      if (!active) return;
      setSaving(true);
      try {
        await updateWorkflowDef(active.id, { spec_json: spec });
        const updated = await getWorkflowDef(active.id);
        setSelected(updated);
        void refreshList();
      } finally {
        setSaving(false);
      }
    },
    [active, refreshList],
  );

  const handleCreated = useCallback(
    (name: string) => {
      setCreating(false);
      void refreshList().then((list) => {
        const created = list.find((d) => d.name === name);
        if (created) selectDef(created.id);
      });
    },
    [refreshList, selectDef],
  );

  return (
    <div className="flex h-full min-h-0 bg-surface-base">
      {rackVisible && (
        <aside className="flex w-[220px] shrink-0 flex-col border-r border-edge">
          <div className="flex shrink-0 items-center gap-2 px-3 py-2.5">
            <SectionLabel>{t("rackTitle")}</SectionLabel>
            <div className="flex-1" />
            <button
              type="button"
              onClick={() => setCreating(true)}
              className="rounded border border-edge px-2 py-0.5 text-[length:var(--t-xs)] text-content-secondary transition-colors hover:text-content-primary"
            >
              {t("rackNew")}
            </button>
          </div>

          {listError && (
            <p className="px-3 py-1 text-[length:var(--t-xs)] text-status-failure">{listError}</p>
          )}

          <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-2">
            {defs.length === 0 && !listError ? (
              <p className="px-1.5 py-2 text-[length:var(--t-xs)] text-content-muted">
                {t("rackEmpty")}
              </p>
            ) : (
              defs.map((d) => (
                <button
                  key={d.id}
                  type="button"
                  onClick={() => selectDef(d.id)}
                  className="mb-1 block w-full rounded px-2 py-1.5 text-left transition-colors"
                  style={{
                    background: d.id === active?.id ? "var(--surface-overlay)" : "transparent",
                  }}
                >
                  <span className="block truncate font-data text-[length:var(--t-sm)] text-content-primary">
                    {d.name}
                  </span>
                  {d.description && (
                    <span className="block truncate text-[length:var(--t-xs)] text-content-muted">
                      {d.description}
                    </span>
                  )}
                </button>
              ))
            )}
          </div>
        </aside>
      )}

      <div className="min-h-0 min-w-0 flex-1">
        {creating ? (
          <CreateWorkflowPanel onCreated={handleCreated} onCancel={() => setCreating(false)} />
        ) : active ? (
          <WorkflowEditor
            key={active.id}
            initialSpec={active.spec_json ?? emptySpec()}
            engineDefs={engineDefs}
            onSave={handleSave}
            saving={saving}
            withText
            exportName={active.name}
          />
        ) : (
          <div className="flex h-full flex-col items-center justify-center gap-3">
            <p className="text-[length:var(--t-sm)] text-content-muted">{t("designerEmpty")}</p>
            <button
              type="button"
              onClick={() => setCreating(true)}
              className="rounded px-3 py-1.5 text-[length:var(--t-sm)] font-medium transition-colors"
              style={{
                background: "var(--accent)",
                color: "var(--surface-base)",
              }}
            >
              {t("designerEmptyCta")}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
