import { useCallback, useEffect, useState } from "react";
import { useTranslations } from "use-intl";
import {
  getWorkflowDef,
  createWorkflowDef,
  updateWorkflowDef,
  deleteWorkflowDef,
  listEngineDefs,
} from "@/lib/api";
import type { WorkflowDef, WorkflowSpec, EngineDef } from "@/lib/api";
import { emptySpec } from "@/lib/workflow/validation";
import WorkflowEditor from "@/components/workflow/WorkflowEditor";
import DrawerBackButton from "@/components/ui/DrawerBackButton";
import DrawerHeader from "@/components/ui/DrawerHeader";
import SectionLabel from "@/components/ui/SectionLabel";
import Button from "@/components/ui/Button";

interface WorkflowDetailProps {
  id: string;
  onBack?: () => void;
}

export function WorkflowDetail({ id, onBack }: WorkflowDetailProps) {
  const t = useTranslations("workflow");
  const [def, setDef] = useState<WorkflowDef | null>(null);
  const [engineDefs, setEngineDefs] = useState<EngineDef[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [armedDelete, setArmedDelete] = useState(false);

  useEffect(() => {
    let alive = true;
    /* eslint-disable react-hooks/set-state-in-effect */
    setLoading(true);
    setError(null);
    setDef(null);
    setArmedDelete(false);
    /* eslint-enable react-hooks/set-state-in-effect */

    Promise.all([getWorkflowDef(id), listEngineDefs()])
      .then(([d, eds]) => {
        if (!alive) return;
        setDef(d);
        setEngineDefs(eds);
      })
      .catch((e) => {
        if (alive) setError(e instanceof Error ? e.message : t("loadError"));
      })
      .finally(() => {
        if (alive) setLoading(false);
      });

    return () => {
      alive = false;
    };
  }, [id, t]);

  const handleSave = useCallback(
    async (spec: WorkflowSpec) => {
      if (!def) return;
      setSaving(true);
      try {
        await updateWorkflowDef(def.id, { spec_json: spec });
        const updated = await getWorkflowDef(def.id);
        setDef(updated);
      } finally {
        setSaving(false);
      }
    },
    [def],
  );

  const handleDelete = useCallback(async () => {
    if (!def) return;
    if (!armedDelete) {
      setArmedDelete(true);
      return;
    }
    try {
      await deleteWorkflowDef(def.id);
      onBack?.();
    } catch {
      setArmedDelete(false);
    }
  }, [def, armedDelete, onBack]);

  if (loading) {
    return (
      <div className="flex h-full items-center justify-center text-meta text-content-muted">
        {t("loading")}
      </div>
    );
  }
  if (error || !def) {
    return <div className="p-4 text-meta text-status-failure">{error ?? t("notFound")}</div>;
  }

  const spec: WorkflowSpec = def.spec_json ?? emptySpec();

  return (
    <div className="flex h-full flex-col overflow-hidden">
      {onBack && <DrawerBackButton onClick={onBack}>{t("back")}</DrawerBackButton>}

      <DrawerHeader
        name={def.name}
        trailing={
          <button
            type="button"
            onClick={() => void handleDelete()}
            onPointerLeave={() => setArmedDelete(false)}
            className="rounded px-2 py-0.5 text-[length:var(--t-xs)] transition-colors"
            style={{
              color: armedDelete ? "var(--status-failure)" : "var(--content-muted)",
              background: armedDelete ? "var(--status-failure)18" : "transparent",
            }}
          >
            {armedDelete ? t("deleteConfirm") : t("delete")}
          </button>
        }
      />

      <div className="min-h-0 flex-1">
        <WorkflowEditor
          initialSpec={spec}
          engineDefs={engineDefs}
          onSave={handleSave}
          saving={saving}
        />
      </div>
    </div>
  );
}

// ── Create panel ──────────────────────────────────────────────────────────────

interface CreateWorkflowProps {
  onCreated: (name: string) => void;
  onCancel: () => void;
}

export function CreateWorkflowPanel({ onCreated, onCancel }: CreateWorkflowProps) {
  const t = useTranslations("workflow");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);

  const handleCreate = useCallback(async () => {
    const trimmed = name.trim();
    if (!trimmed || creating) return;
    setCreating(true);
    setCreateError(null);
    try {
      await createWorkflowDef({
        name: trimmed,
        description: description.trim() || undefined,
        spec_json: emptySpec(),
      });
      onCreated(trimmed);
    } catch (e) {
      setCreateError(e instanceof Error ? e.message : t("createError"));
    } finally {
      setCreating(false);
    }
  }, [name, description, creating, onCreated, t]);

  return (
    <div className="flex h-full flex-col overflow-hidden">
      <div className="flex shrink-0 items-center justify-between border-b border-edge px-4 py-3">
        <span className="font-medium text-[length:var(--t-md)] text-content-primary">
          {t("createTitle")}
        </span>
        <button
          type="button"
          onClick={onCancel}
          className="text-[length:var(--t-xs)] text-content-muted"
        >
          {t("cancel")}
        </button>
      </div>

      <div className="flex flex-1 flex-col gap-4 overflow-auto p-4">
        <label className="flex flex-col gap-1.5">
          <SectionLabel>{t("createName")}</SectionLabel>
          <input
            type="text"
            value={name}
            onChange={(e) => {
              if (e.nativeEvent instanceof InputEvent && e.nativeEvent.isComposing) return;
              setName(e.target.value);
            }}
            onKeyDown={(e) => {
              if (e.nativeEvent.isComposing) return;
              if (e.key === "Enter") void handleCreate();
            }}
            placeholder={t("createNamePlaceholder")}
            className="rounded border border-edge bg-surface-overlay px-3 py-2 font-data text-[length:var(--t-base)] text-content-primary focus:outline-none focus:ring-1 focus:ring-accent"
          />
        </label>

        <label className="flex flex-col gap-1.5">
          <SectionLabel>{t("createDescription")}</SectionLabel>
          <input
            type="text"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder={t("createDescriptionPlaceholder")}
            className="rounded border border-edge bg-surface-overlay px-3 py-2 font-data text-[length:var(--t-base)] text-content-primary focus:outline-none focus:ring-1 focus:ring-accent"
          />
        </label>

        {createError && (
          <div className="text-[length:var(--t-xs)] text-status-failure">{createError}</div>
        )}
      </div>

      <div className="flex shrink-0 items-center gap-2 border-t border-edge px-4 py-3">
        <div className="flex-1" />
        <Button
          size="sm"
          variant="primary"
          onClick={() => void handleCreate()}
          disabled={!name.trim() || creating}
        >
          {creating ? t("creating") : t("create")}
        </Button>
      </div>
    </div>
  );
}
