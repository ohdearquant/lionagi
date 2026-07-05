import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslations } from "use-intl";
import { IconArrowLeft } from "@/components/ui/icons";
import { getDefinition, saveDefinition, rollbackDefinition, getDefinitionVersion } from "@/lib/api";
import type { DefinitionDetail, DefinitionVersion } from "@/lib/api";
import type { AgentProfileSummary } from "@/lib/types";
import SectionLabel from "@/components/ui/SectionLabel";
import Button from "@/components/ui/Button";

interface ParsedFm {
  model?: string;
  effort?: string;
  permission_mode?: string;
  yolo?: boolean;
  [key: string]: unknown;
}

function parseFm(raw: string): { fm: ParsedFm; body: string } {
  const m = /^---\r?\n([\s\S]*?)\r?\n---\r?\n?([\s\S]*)$/.exec(raw.trimStart());
  if (!m) return { fm: {}, body: raw };
  const fm: ParsedFm = {};
  for (const line of m[1].split("\n")) {
    const colon = line.indexOf(":");
    if (colon === -1) continue;
    const key = line.slice(0, colon).trim();
    const val = line.slice(colon + 1).trim();
    if (!key) continue;
    if (val === "true") fm[key] = true;
    else if (val === "false") fm[key] = false;
    else if (val === "" || val === "null" || val === "~") fm[key] = undefined;
    else fm[key] = val.replace(/^["']|["']$/g, "");
  }
  return { fm, body: m[2] ?? "" };
}

function serializeFm(fm: ParsedFm): string {
  const lines: string[] = [];
  for (const [k, v] of Object.entries(fm)) {
    if (v === undefined || v === null) continue;
    if (typeof v === "boolean") lines.push(`${k}: ${v}`);
    else lines.push(`${k}: ${String(v)}`);
  }
  return `---\n${lines.join("\n")}\n---\n`;
}

const EFFORT_OPTS = ["", "low", "medium", "high", "xhigh", "max"];
const PERM_OPTS = ["", "default", "acceptEdits", "bypassPermissions"];

interface Props {
  agent: AgentProfileSummary;
  /** Rendered in collapsed (narrow) mode — show a back affordance. */
  onBack?: () => void;
}

export function AgentDetail({ agent, onBack }: Props) {
  const t = useTranslations("library.drawer");
  const [def, setDef] = useState<DefinitionDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [editing, setEditing] = useState(false);
  const [fm, setFm] = useState<ParsedFm>({});
  const [body, setBody] = useState("");
  const [commitMsg, setCommitMsg] = useState("");
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [savedOk, setSavedOk] = useState(false);

  const [previewVer, setPreviewVer] = useState<DefinitionDetail | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    let alive = true;
    /* eslint-disable react-hooks/set-state-in-effect -- synchronous resets clear stale state before the async fetch resolves */
    setLoading(true);
    setError(null);
    setDef(null);
    setEditing(false);
    setPreviewVer(null);
    setSaveError(null);
    setSavedOk(false);
    /* eslint-enable react-hooks/set-state-in-effect */

    getDefinition("agent", agent.name)
      .then((d) => {
        if (!alive) return;
        setDef(d);
        const { fm: f, body: b } = parseFm(d.content);
        setFm(f);
        setBody(b);
      })
      .catch((e) => {
        if (alive) setError(e instanceof Error ? e.message : "Failed to load");
      })
      .finally(() => {
        if (alive) setLoading(false);
      });

    return () => {
      alive = false;
    };
  }, [agent.name]);

  const startEdit = useCallback(() => {
    if (!def) return;
    const { fm: f, body: b } = parseFm(def.content);
    setFm(f);
    setBody(b);
    setEditing(true);
    setSaveError(null);
    setSavedOk(false);
    setPreviewVer(null);
    setTimeout(() => textareaRef.current?.focus(), 0);
  }, [def]);

  const cancelEdit = useCallback(() => {
    setEditing(false);
    setSaveError(null);
    if (def) {
      const { fm: f, body: b } = parseFm(def.content);
      setFm(f);
      setBody(b);
    }
    setCommitMsg("");
  }, [def]);

  const handleSave = useCallback(async () => {
    if (!def || saving) return;
    setSaving(true);
    setSaveError(null);
    const content = serializeFm(fm) + body;
    try {
      await saveDefinition("agent", agent.name, content, commitMsg || undefined);
      const updated = await getDefinition("agent", agent.name);
      setDef(updated);
      const { fm: f, body: b } = parseFm(updated.content);
      setFm(f);
      setBody(b);
      setEditing(false);
      setCommitMsg("");
      setSavedOk(true);
    } catch (e) {
      setSaveError(e instanceof Error ? e.message : "Save failed");
    } finally {
      setSaving(false);
    }
  }, [def, saving, fm, body, commitMsg, agent.name]);

  const handleViewVersion = useCallback(
    async (v: DefinitionVersion) => {
      try {
        const d = await getDefinitionVersion("agent", agent.name, v.version);
        setPreviewVer(d);
        setEditing(false);
      } catch {
        /* silent */
      }
    },
    [agent.name],
  );

  const handleRestoreVersion = useCallback(
    async (version: number) => {
      try {
        await rollbackDefinition("agent", agent.name, version);
        const updated = await getDefinition("agent", agent.name);
        setDef(updated);
        const { fm: f, body: b } = parseFm(updated.content);
        setFm(f);
        setBody(b);
        setPreviewVer(null);
      } catch {
        /* silent */
      }
    },
    [agent.name],
  );

  function setFmField(key: string, value: unknown) {
    setFm((prev) => ({ ...prev, [key]: value }));
  }

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

  const displayContent = previewVer
    ? previewVer.content
    : editing
      ? serializeFm(fm) + body
      : def.content;
  const { fm: dispFm, body: dispBody } = parseFm(displayContent);

  return (
    <div className="flex h-full flex-col overflow-hidden">
      {/* Back affordance — visible only in collapsed (narrow) mode */}
      {onBack && (
        <button
          type="button"
          onClick={onBack}
          className="flex shrink-0 items-center gap-1.5 border-b border-edge px-4 py-2 text-[length:var(--t-xs)] text-content-muted"
        >
          <IconArrowLeft size={11} strokeWidth={2} /> {t("back")}
        </button>
      )}

      {/* Header */}
      <div className="flex shrink-0 items-center gap-3 border-b border-edge px-4 py-3">
        <span className="truncate font-data font-medium text-[length:var(--t-lg)] text-content-primary">
          {agent.name}
        </span>
        {agent.provider && (
          <span className="shrink-0 rounded border border-edge bg-surface-overlay px-1.5 py-0.5 text-[length:var(--t-xs)] uppercase tracking-[0.08em] text-content-muted">
            {agent.provider}
          </span>
        )}
        {agent.model && (
          <span className="shrink-0 font-data text-[length:var(--t-xs)] text-content-muted">
            {agent.model}
          </span>
        )}

        <div className="ml-auto flex items-center gap-2">
          {previewVer ? (
            <>
              <span className="text-[length:var(--t-xs)] text-content-muted">
                v{previewVer.version}
              </span>
              <Button
                size="sm"
                variant="primary"
                onClick={() => void handleRestoreVersion(previewVer.version)}
              >
                {t("restore")}
              </Button>
              <Button size="sm" variant="secondary" onClick={() => setPreviewVer(null)}>
                {t("back")}
              </Button>
            </>
          ) : editing ? (
            <>
              <input
                type="text"
                value={commitMsg}
                onChange={(e) => setCommitMsg(e.target.value)}
                placeholder={t("commitPlaceholder")}
                className="w-36 rounded border border-edge bg-surface-overlay px-2 py-1 font-ui text-[length:var(--t-xs)] text-content-primary"
              />
              <Button
                size="sm"
                variant="primary"
                onClick={() => void handleSave()}
                disabled={saving}
              >
                {saving ? t("saving") : t("save")}
              </Button>
              <Button size="sm" variant="secondary" onClick={cancelEdit}>
                {t("cancel")}
              </Button>
            </>
          ) : (
            <>
              {savedOk && (
                <span className="text-[length:var(--t-xs)] text-status-success">
                  {t("saveDone")}
                </span>
              )}
              <span className="font-data text-[length:var(--t-xs)] text-content-muted">
                v{def.version}
              </span>
              <Button size="sm" variant="secondary" onClick={startEdit}>
                {t("edit")}
              </Button>
            </>
          )}
        </div>
      </div>

      {saveError && (
        <div className="shrink-0 border-b border-edge px-4 py-2 text-[length:var(--t-xs)] text-status-failure">
          {saveError}
        </div>
      )}

      {/* Metadata strip */}
      <div className="flex shrink-0 flex-wrap items-center gap-x-5 gap-y-2 border-b border-edge px-4 py-2.5">
        {editing ? (
          <>
            <label className="flex flex-col gap-1">
              <SectionLabel>{t("fieldModel")}</SectionLabel>
              <input
                type="text"
                value={typeof fm.model === "string" ? fm.model : ""}
                onChange={(e) => setFmField("model", e.target.value || undefined)}
                placeholder={t("modelPlaceholder")}
                className="w-44 rounded border border-edge bg-surface-overlay px-2 py-1 font-data text-[length:var(--t-xs)] text-content-primary"
              />
            </label>
            <label className="flex flex-col gap-1">
              <SectionLabel>{t("fieldEffort")}</SectionLabel>
              <select
                value={typeof fm.effort === "string" ? fm.effort : ""}
                onChange={(e) => setFmField("effort", e.target.value || undefined)}
                className="rounded border border-edge bg-surface-overlay px-2 py-1 text-[length:var(--t-xs)] text-content-primary"
              >
                {EFFORT_OPTS.map((o) => (
                  <option key={o} value={o}>
                    {o || "—"}
                  </option>
                ))}
              </select>
            </label>
            <label className="flex flex-col gap-1">
              <SectionLabel>{t("fieldPermission")}</SectionLabel>
              <select
                value={typeof fm.permission_mode === "string" ? fm.permission_mode : ""}
                onChange={(e) => setFmField("permission_mode", e.target.value || undefined)}
                className="rounded border border-edge bg-surface-overlay px-2 py-1 text-[length:var(--t-xs)] text-content-primary"
              >
                {PERM_OPTS.map((o) => (
                  <option key={o} value={o}>
                    {o || "—"}
                  </option>
                ))}
              </select>
            </label>
            <label className="flex cursor-pointer select-none items-center gap-1.5">
              <input
                type="checkbox"
                checked={fm.yolo === true}
                onChange={(e) => setFmField("yolo", e.target.checked || undefined)}
                className="h-3.5 w-3.5 rounded"
                style={{ accentColor: "var(--accent)" }}
              />
              <SectionLabel>{t("fieldYolo")}</SectionLabel>
            </label>
          </>
        ) : (
          <>
            {dispFm.model && (
              <div className="flex items-center gap-1.5 text-[length:var(--t-xs)]">
                <span className="text-content-muted">{t("fieldModel")}</span>
                <span className="font-data text-content-primary">{String(dispFm.model)}</span>
              </div>
            )}
            {dispFm.effort && (
              <div className="flex items-center gap-1.5 text-[length:var(--t-xs)]">
                <span className="text-content-muted">{t("fieldEffort")}</span>
                <span className="font-data text-content-primary">{String(dispFm.effort)}</span>
              </div>
            )}
            {dispFm.permission_mode && (
              <div className="flex items-center gap-1.5 text-[length:var(--t-xs)]">
                <span className="text-content-muted">{t("fieldPermission")}</span>
                <span className="font-data text-content-primary">
                  {String(dispFm.permission_mode)}
                </span>
              </div>
            )}
            {dispFm.yolo === true && (
              <div className="flex items-center gap-1.5 text-[length:var(--t-xs)]">
                <span className="text-accent">yolo</span>
              </div>
            )}
          </>
        )}
      </div>

      {/* System prompt — dominant element */}
      <div className="flex min-h-0 flex-1 flex-col">
        <div className="flex shrink-0 items-center justify-between border-b border-edge px-4 py-2">
          <SectionLabel>{t("systemPrompt")}</SectionLabel>
          {def.versions.length > 0 && (
            <span className="text-[length:var(--t-xs)] text-content-muted">
              {t("versionCount", { count: def.versions.length })}
            </span>
          )}
        </div>

        {editing ? (
          <textarea
            ref={textareaRef}
            value={body}
            onChange={(e) => setBody(e.target.value)}
            spellCheck={false}
            className="flex-1 resize-none bg-surface-base p-4 font-data text-[length:var(--t-base)] leading-relaxed text-content-primary focus:outline-none"
            style={{ minHeight: "60vh" }}
            placeholder={t("systemPromptPlaceholder")}
          />
        ) : (
          <pre className="flex-1 overflow-auto whitespace-pre-wrap break-words bg-surface-base p-4 font-data text-[length:var(--t-sm)] leading-relaxed text-content-secondary">
            {dispBody.trim() || <span className="italic text-content-muted">{t("noContent")}</span>}
          </pre>
        )}
      </div>

      {/* Version history strip */}
      {!editing && def.versions.length > 0 && (
        <div className="shrink-0 overflow-x-auto border-t border-edge">
          <div className="flex gap-0" style={{ minWidth: "max-content" }}>
            {[...def.versions]
              .sort((a, b) => b.version - a.version)
              .slice(0, 8)
              .map((v) => {
                const isCurrent = v.version === def.version;
                const isPreviewing = previewVer?.version === v.version;
                return (
                  <button
                    key={v.id}
                    type="button"
                    onClick={() => void handleViewVersion(v)}
                    className="flex flex-col gap-0.5 border-r border-edge px-3 py-2 text-left text-[length:var(--t-xs)]"
                    style={{
                      background: isPreviewing ? "var(--surface-overlay)" : "transparent",
                      color: isCurrent ? "var(--accent)" : "var(--content-muted)",
                    }}
                  >
                    <span className="font-data font-medium">
                      v{v.version}
                      {isCurrent ? " ●" : ""}
                    </span>
                    {v.message && (
                      <span className="max-w-[80px] truncate" title={v.message}>
                        {v.message}
                      </span>
                    )}
                  </button>
                );
              })}
          </div>
        </div>
      )}
    </div>
  );
}
