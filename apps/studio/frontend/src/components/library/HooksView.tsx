import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslations } from "use-intl";
import {
  deleteHookDef,
  getHookLibrary,
  getOperatorHooks,
  putHookDef,
  putOperatorHooks,
} from "@/lib/api";
import type { HookAttachment, HookDef } from "@/lib/api";
import Button from "@/components/ui/Button";
import HookAssemblyEditor from "./HookAssemblyEditor";

/**
 * The Library's Hooks surface: the shared hook library (named, reusable hook
 * definitions) plus the Operator's own assembly. Agents assemble the same
 * library hooks from their detail pages; the Operator has no agent page, so
 * its assembly lives here.
 */
export default function HooksView() {
  const t = useTranslations("library.hooks");

  // ── Library state ──────────────────────────────────────────────────────────
  const [hooks, setHooks] = useState<Record<string, HookDef>>({});
  const [libraryError, setLibraryError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  const reloadLibrary = useCallback(() => {
    getHookLibrary()
      .then((lib) => {
        setHooks(lib.hooks ?? {});
        setLibraryError(lib.error ?? null);
      })
      .catch((err) => setLibraryError(err instanceof Error ? err.message : String(err)))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    reloadLibrary();
  }, [reloadLibrary]);

  const hookNames = useMemo(() => Object.keys(hooks).sort(), [hooks]);

  // ── Operator assembly state ────────────────────────────────────────────────
  const [enabled, setEnabled] = useState(true);
  const [attachments, setAttachments] = useState<HookAttachment[]>([]);
  const [assemblyError, setAssemblyError] = useState<string | null>(null);
  const [assemblySaved, setAssemblySaved] = useState(false);
  const [assemblySaving, setAssemblySaving] = useState(false);
  // The assembly editor stays gated until this initial GET lands: the library
  // request resolving first used to unlock editing, and the slower assembly
  // response then overwrote whatever the user had already changed or saved.
  const [assemblyLoaded, setAssemblyLoaded] = useState(false);

  useEffect(() => {
    getOperatorHooks()
      .then((config) => {
        setEnabled(config.enabled ?? true);
        setAttachments(config.attachments ?? []);
        setAssemblyError(config.error ?? null);
      })
      .catch((err) => setAssemblyError(err instanceof Error ? err.message : String(err)))
      .finally(() => setAssemblyLoaded(true));
  }, []);

  const saveAssembly = useCallback(() => {
    setAssemblySaving(true);
    setAssemblyError(null);
    setAssemblySaved(false);
    putOperatorHooks({ enabled, attachments })
      .then((config) => {
        setAttachments(config.attachments ?? []);
        setAssemblySaved(true);
      })
      .catch((err) => setAssemblyError(err instanceof Error ? err.message : String(err)))
      .finally(() => setAssemblySaving(false));
  }, [enabled, attachments]);

  if (loading) {
    return (
      <div className="p-6">
        <div className="skeleton h-6 w-40 rounded" />
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col gap-6 overflow-y-auto p-6">
      <div>
        <h2 className="text-page-title font-semibold text-content-primary">{t("title")}</h2>
        <p className="mt-1 max-w-2xl text-body text-content-muted">{t("description")}</p>
      </div>

      {libraryError && (
        <div
          role="alert"
          className="rounded border border-status-error bg-status-error-bg px-3 py-2 text-body text-status-error"
        >
          {libraryError}
        </div>
      )}

      {/* ── Hook library ── */}
      <section className="flex flex-col gap-3">
        <div className="flex items-center justify-between">
          <h3 className="text-[length:var(--t-base)] font-semibold text-content-primary">
            {t("libraryTitle")}
          </h3>
          <Button
            size="sm"
            variant="primary"
            onClick={() => {
              setCreating(true);
              setSelected(null);
            }}
          >
            + {t("newHook")}
          </Button>
        </div>
        {hookNames.length === 0 && !creating ? (
          <p className="text-[length:var(--t-sm)] text-content-muted">{t("emptyLibrary")}</p>
        ) : (
          <div className="overflow-hidden rounded border border-edge">
            {hookNames.map((name, i) => (
              <button
                key={name}
                type="button"
                onClick={() => {
                  setSelected(selected === name ? null : name);
                  setCreating(false);
                }}
                aria-expanded={selected === name}
                className="flex w-full items-center gap-3 bg-surface-raised px-3 py-2 text-left transition-colors duration-100 hover:bg-surface-overlay"
                style={{ borderTop: i === 0 ? undefined : "1px solid var(--edge-hairline)" }}
              >
                <span className="font-data text-[length:var(--t-sm)] font-medium text-content-primary">
                  {name}
                </span>
                <span className="min-w-0 flex-1 truncate text-[length:var(--t-xs)] text-content-muted">
                  {hooks[name]?.description}
                </span>
                <span className="shrink-0 font-data text-[length:var(--t-xs)] text-content-muted">
                  {selected === name ? "▾" : "▸"}
                </span>
              </button>
            ))}
          </div>
        )}
        {(creating || selected) && (
          <HookDefForm
            key={creating ? "__new__" : selected}
            name={creating ? null : selected}
            initial={creating ? null : (hooks[selected ?? ""] ?? null)}
            onSaved={(name) => {
              setCreating(false);
              setSelected(name);
              reloadLibrary();
            }}
            onDeleted={() => {
              setSelected(null);
              reloadLibrary();
            }}
            onCancel={() => {
              setCreating(false);
              setSelected(null);
            }}
          />
        )}
      </section>

      {/* ── Operator assembly ── */}
      <section className="flex flex-col gap-3 rounded border border-edge bg-surface-raised p-4">
        <div>
          <h3 className="text-[length:var(--t-base)] font-semibold text-content-primary">
            {t("operatorTitle")}
          </h3>
          <p className="mt-0.5 max-w-2xl text-[length:var(--t-sm)] text-content-muted">
            {t("operatorDescription")}
          </p>
        </div>
        <label className="flex w-fit cursor-pointer items-center gap-2 text-body text-content-secondary">
          <input
            type="checkbox"
            checked={enabled}
            disabled={!assemblyLoaded}
            onChange={(e) => {
              setEnabled(e.target.checked);
              setAssemblySaved(false);
            }}
          />
          {t("enabled")}
        </label>
        <HookAssemblyEditor
          attachments={attachments}
          onChange={(next) => {
            setAttachments(next);
            setAssemblySaved(false);
          }}
          hookNames={hookNames}
          disabled={!assemblyLoaded}
        />
        {assemblyError && (
          <div
            role="alert"
            className="rounded border border-status-error bg-status-error-bg px-3 py-2 text-body text-status-error"
          >
            {assemblyError}
          </div>
        )}
        <div className="flex items-center gap-3">
          <Button
            variant="primary"
            onClick={saveAssembly}
            disabled={assemblySaving || !assemblyLoaded}
          >
            {assemblySaving ? t("saving") : t("save")}
          </Button>
          {assemblySaved && (
            <span className="text-[length:var(--t-sm)] text-content-secondary">
              {t("savedNotice")}
            </span>
          )}
        </div>
      </section>
    </div>
  );
}

interface HookDefFormProps {
  /** null = creating a new hook (name editable). */
  name: string | null;
  initial: HookDef | null;
  onSaved: (name: string) => void;
  onDeleted: () => void;
  onCancel: () => void;
}

function HookDefForm({ name, initial, onSaved, onDeleted, onCancel }: HookDefFormProps) {
  const t = useTranslations("library.hooks");
  const [draftName, setDraftName] = useState(name ?? "");
  const [description, setDescription] = useState(initial?.description ?? "");
  const [command, setCommand] = useState(initial?.command ?? "");
  const [timeout_, setTimeout_] = useState(initial?.timeout != null ? String(initial.timeout) : "");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const handleSave = () => {
    const target = (name ?? draftName).trim();
    if (!target) {
      setError(t("nameRequired"));
      return;
    }
    const spec: HookDef = { description, command };
    if (timeout_.trim()) {
      const parsed = Number(timeout_);
      if (Number.isNaN(parsed)) {
        setError(t("timeoutInvalid"));
        return;
      }
      spec.timeout = parsed;
    }
    setSaving(true);
    setError(null);
    putHookDef(target, spec)
      .then(() => onSaved(target))
      .catch((err) => setError(err instanceof Error ? err.message : String(err)))
      .finally(() => setSaving(false));
  };

  const handleDelete = () => {
    if (!name) return;
    setSaving(true);
    deleteHookDef(name)
      .then(onDeleted)
      .catch((err) => setError(err instanceof Error ? err.message : String(err)))
      .finally(() => setSaving(false));
  };

  return (
    <div className="flex flex-col gap-3 rounded border border-edge bg-surface-raised p-4">
      <label className="flex flex-col gap-1 text-[length:var(--t-sm)] text-content-secondary">
        {t("fieldName")}
        <input
          type="text"
          value={name ?? draftName}
          onChange={(e) => setDraftName(e.target.value)}
          disabled={name !== null}
          className="rounded border border-edge bg-surface-overlay px-2 py-1 font-data text-content-primary disabled:opacity-60"
        />
      </label>
      <label className="flex flex-col gap-1 text-[length:var(--t-sm)] text-content-secondary">
        {t("fieldDescription")}
        <input
          type="text"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          className="rounded border border-edge bg-surface-overlay px-2 py-1 text-content-primary"
        />
      </label>
      <label className="flex flex-col gap-1 text-[length:var(--t-sm)] text-content-secondary">
        {t("fieldCommand")}
        <textarea
          value={command}
          onChange={(e) => setCommand(e.target.value)}
          spellCheck={false}
          rows={2}
          className="resize-y rounded border border-edge bg-surface-overlay px-2 py-1 font-data text-[length:var(--t-sm)] text-content-primary"
        />
      </label>
      <label className="flex w-40 flex-col gap-1 text-[length:var(--t-sm)] text-content-secondary">
        {t("fieldTimeout")}
        <input
          type="text"
          value={timeout_}
          onChange={(e) => setTimeout_(e.target.value)}
          className="rounded border border-edge bg-surface-overlay px-2 py-1 font-data text-content-primary"
        />
      </label>
      {error && (
        <div
          role="alert"
          className="rounded border border-status-error bg-status-error-bg px-3 py-2 text-body text-status-error"
        >
          {error}
        </div>
      )}
      <div className="flex items-center gap-2">
        <Button variant="primary" size="sm" onClick={handleSave} disabled={saving}>
          {saving ? t("saving") : t("save")}
        </Button>
        <Button variant="secondary" size="sm" onClick={onCancel} disabled={saving}>
          {t("cancel")}
        </Button>
        {name !== null && (
          <Button variant="danger" size="sm" onClick={handleDelete} disabled={saving}>
            {t("delete")}
          </Button>
        )}
      </div>
    </div>
  );
}
