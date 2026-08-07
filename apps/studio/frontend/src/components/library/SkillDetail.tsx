/**
 * Detail pane for a skill — either a standalone, user-authored skill (full
 * view/edit/save/version-history, via the definitions API) or a skill
 * bundled inside a plugin (read-only, via the plugin-nested endpoint; that
 * content belongs to the plugin, not the user's local skill directory).
 */

import { lazy, Suspense, useCallback, useEffect, useRef, useState } from "react";
import { useTranslations } from "use-intl";
import {
  getDefinition,
  getDefinitionVersion,
  getPluginSkill,
  getSkill,
  rollbackDefinition,
  saveDefinition,
  validateSkill,
} from "@/lib/api";
import type {
  DefinitionDetail,
  DefinitionVersion,
  DefinitionVersionDetail,
  SkillDetail as SkillSummaryDetail,
} from "@/lib/api";
import { formatInvocationAge, useInvocationStats } from "@/lib/useInvocationStats";
import DrawerBackButton from "@/components/ui/DrawerBackButton";
import DrawerHeader from "@/components/ui/DrawerHeader";
import SectionLabel from "@/components/ui/SectionLabel";
import Button from "@/components/ui/Button";
import StatusPill from "@/components/ui/StatusPill";
import { Link } from "@tanstack/react-router";

const Markdown = lazy(() => import("@/components/ui/Markdown"));

interface SkillDetailProps {
  name: string;
  /** Set when this skill is bundled inside a plugin — read-only, fetched
   * from the plugin's nested endpoint instead of the definitions editor. */
  pluginName?: string;
  onBack?: () => void;
}

export function SkillDetail({ name, pluginName, onBack }: SkillDetailProps) {
  const t = useTranslations("library.drawer");
  const readOnly = pluginName != null;

  const [summary, setSummary] = useState<SkillSummaryDetail | null>(null);
  const [def, setDef] = useState<DefinitionDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [editing, setEditing] = useState(false);
  const [content, setContent] = useState("");
  const [commitMsg, setCommitMsg] = useState("");
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [validationErrors, setValidationErrors] = useState<string[] | null>(null);
  const [savedOk, setSavedOk] = useState(false);

  const [previewVer, setPreviewVer] = useState<DefinitionVersionDetail | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const { stats, loading: statsLoading } = useInvocationStats("skill", name);

  useEffect(() => {
    let alive = true;
    /* eslint-disable react-hooks/set-state-in-effect -- synchronous resets clear stale state before the async fetch resolves */
    setLoading(true);
    setError(null);
    setSummary(null);
    setDef(null);
    setEditing(false);
    setPreviewVer(null);
    setSaveError(null);
    setValidationErrors(null);
    setSavedOk(false);
    /* eslint-enable react-hooks/set-state-in-effect */

    const fetchSummary = readOnly ? getPluginSkill(pluginName, name) : getSkill(name);
    const fetchDef = readOnly ? Promise.resolve(null) : getDefinition("skill", name);

    Promise.all([fetchSummary, fetchDef])
      .then(([s, d]) => {
        if (!alive) return;
        setSummary(s);
        setDef(d);
        if (d) setContent(d.content);
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
  }, [name, pluginName, readOnly]);

  const startEdit = useCallback(() => {
    if (!def) return;
    setContent(def.content);
    setEditing(true);
    setSaveError(null);
    setValidationErrors(null);
    setSavedOk(false);
    setPreviewVer(null);
    setTimeout(() => textareaRef.current?.focus(), 0);
  }, [def]);

  const cancelEdit = useCallback(() => {
    setEditing(false);
    setSaveError(null);
    setValidationErrors(null);
    if (def) setContent(def.content);
    setCommitMsg("");
  }, [def]);

  const handleSave = useCallback(async () => {
    if (!def || saving) return;
    setSaving(true);
    setSaveError(null);
    setValidationErrors(null);
    try {
      const validation = await validateSkill(name, content);
      if (!validation.ok) {
        setValidationErrors(validation.errors ?? [t("skillValidationFailed")]);
        return;
      }
      await saveDefinition("skill", name, content, commitMsg || undefined);
      const [updatedSummary, updatedDef] = await Promise.all([
        getSkill(name),
        getDefinition("skill", name),
      ]);
      setSummary(updatedSummary);
      setDef(updatedDef);
      setContent(updatedDef.content);
      setEditing(false);
      setCommitMsg("");
      setSavedOk(true);
    } catch (e) {
      setSaveError(e instanceof Error ? e.message : "Save failed");
    } finally {
      setSaving(false);
    }
  }, [def, saving, name, content, commitMsg, t]);

  const handleViewVersion = useCallback(
    async (v: DefinitionVersion) => {
      try {
        const d = await getDefinitionVersion("skill", name, v.version);
        setPreviewVer(d);
        setEditing(false);
      } catch {
        /* silent */
      }
    },
    [name],
  );

  const handleRestoreVersion = useCallback(
    async (version: number) => {
      try {
        await rollbackDefinition("skill", name, version);
        const [updatedSummary, updatedDef] = await Promise.all([
          getSkill(name),
          getDefinition("skill", name),
        ]);
        setSummary(updatedSummary);
        setDef(updatedDef);
        setContent(updatedDef.content);
        setPreviewVer(null);
      } catch {
        /* silent */
      }
    },
    [name],
  );

  if (loading) {
    return (
      <div className="flex h-full items-center justify-center text-meta text-content-muted">
        {t("loading")}
      </div>
    );
  }

  if (error || !summary) {
    return <div className="p-4 text-meta text-status-failure">{error ?? t("notFound")}</div>;
  }

  const displayBody = previewVer ? previewVer.content : summary.content;

  return (
    <div className="flex h-full flex-col overflow-hidden">
      {onBack && <DrawerBackButton onClick={onBack}>{t("back")}</DrawerBackButton>}

      <DrawerHeader
        name={summary.name}
        badge="skill"
        trailing={
          readOnly ? (
            <span
              title={t("pluginOwnedSkillHint")}
              className="rounded border border-edge bg-surface-overlay px-1.5 py-0.5 text-[length:var(--t-xs)] uppercase tracking-[0.08em] text-content-muted"
            >
              {t("readOnly")}
            </span>
          ) : previewVer ? (
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
              {def && def.version != null && (
                <span className="font-data text-[length:var(--t-xs)] text-content-muted">
                  v{def.version}
                </span>
              )}
              <Button size="sm" variant="secondary" onClick={startEdit} disabled={!def}>
                {t("edit")}
              </Button>
            </>
          )
        }
      />

      {summary.path && (
        <div className="shrink-0 border-b border-edge px-4 py-1.5 font-data text-[length:var(--t-xs)] text-content-muted">
          {summary.path}
        </div>
      )}

      {saveError && (
        <div className="shrink-0 border-b border-edge px-4 py-2 text-[length:var(--t-xs)] text-status-failure">
          {saveError}
        </div>
      )}

      {validationErrors && validationErrors.length > 0 && (
        <div className="shrink-0 border-b border-edge px-4 py-2 text-[length:var(--t-xs)] text-status-failure">
          <div>{t("skillValidationFailed")}</div>
          <ul className="ml-4 list-disc">
            {validationErrors.map((err) => (
              <li key={err}>{err}</li>
            ))}
          </ul>
        </div>
      )}

      <div className="flex-1 overflow-auto">
        <div className="flex flex-col gap-4 p-4">
          {summary.description && (
            <p className="text-[length:var(--t-sm)] text-content-secondary">
              {summary.description}
            </p>
          )}

          {summary.allowed_tools.length > 0 && (
            <div>
              <SectionLabel className="mb-1.5">{t("skillTools")}</SectionLabel>
              <div className="flex flex-wrap gap-1.5">
                {summary.allowed_tools.map((tool) => (
                  <span
                    key={tool}
                    className="rounded border border-edge bg-surface-overlay px-1.5 py-0.5 font-data text-[length:var(--t-xs)] text-content-secondary"
                  >
                    {tool}
                  </span>
                ))}
              </div>
            </div>
          )}

          {/* Stats strip */}
          <div className="grid grid-cols-3 gap-px overflow-hidden rounded border border-edge bg-edge">
            {[
              { label: t("invocations"), value: statsLoading ? "—" : String(stats?.total ?? 0) },
              {
                label: t("successRate"),
                value: statsLoading
                  ? "—"
                  : stats?.successRate != null
                    ? `${stats.successRate}%`
                    : "—",
              },
              {
                label: t("lastUsed"),
                value: statsLoading
                  ? "—"
                  : stats?.lastUsedSec != null
                    ? formatInvocationAge(stats.lastUsedSec)
                    : t("never"),
              },
            ].map(({ label, value }) => (
              <div key={label} className="flex flex-col gap-0.5 bg-surface-raised px-3 py-2">
                <SectionLabel>{label}</SectionLabel>
                <span className="font-data tabular-nums text-[length:var(--t-base)] text-content-primary">
                  {value}
                </span>
              </div>
            ))}
          </div>

          {/* Recent invocations */}
          <div>
            <SectionLabel className="mb-1.5">{t("recentInvocations")}</SectionLabel>
            {statsLoading ? (
              <p className="text-[length:var(--t-sm)] text-content-muted">{t("loading")}</p>
            ) : !stats || stats.recent.length === 0 ? (
              <p className="text-[length:var(--t-sm)] text-content-muted">{t("noInvocations")}</p>
            ) : (
              <div className="flex flex-col rounded border border-edge" style={{ borderRadius: 4 }}>
                {stats.recent.map((inv, i) => (
                  <Link
                    key={inv.id}
                    to="/fleet"
                    className="flex items-center gap-2 px-2.5 py-2 font-data text-[length:var(--t-sm)] text-content-primary hover:underline"
                    style={{
                      borderTop: i > 0 ? "1px solid var(--edge-hairline)" : undefined,
                    }}
                  >
                    <StatusPill value={inv.status} kind="lifecycle" taxonomy="session" />
                    <span className="min-w-0 flex-1 truncate">
                      {inv.status === "failed" && inv.status_reason_summary
                        ? inv.status_reason_summary
                        : inv.skill}
                    </span>
                    <span className="shrink-0 tabular-nums text-[length:var(--t-xs)] text-content-muted">
                      {formatInvocationAge(inv.ended_at ?? inv.started_at)}
                    </span>
                  </Link>
                ))}
              </div>
            )}
          </div>

          {/* Instructions */}
          <div>
            <SectionLabel className="mb-1.5">{t("skillInstructions")}</SectionLabel>
            {editing ? (
              <textarea
                ref={textareaRef}
                value={content}
                onChange={(e) => setContent(e.target.value)}
                spellCheck={false}
                className="w-full resize-none rounded border border-edge bg-surface-base p-3 font-data text-[length:var(--t-sm)] leading-relaxed text-content-primary focus:outline-none"
                style={{ minHeight: "50vh" }}
              />
            ) : displayBody.trim() ? (
              <div className="rounded border border-edge bg-surface-base px-4 py-3">
                <Suspense
                  fallback={
                    <pre className="whitespace-pre-wrap break-words font-data text-[length:var(--t-sm)] leading-relaxed text-content-secondary">
                      {displayBody.trim()}
                    </pre>
                  }
                >
                  <Markdown className="max-w-4xl text-[length:var(--t-sm)]">
                    {displayBody.trim()}
                  </Markdown>
                </Suspense>
              </div>
            ) : (
              <span className="italic text-content-muted">{t("noContent")}</span>
            )}
          </div>
        </div>
      </div>

      {/* Version history strip — omitted (not crashed) when the history
          store is unreadable; def.content above still renders either way. */}
      {!readOnly && !editing && def && def.versions && def.versions.length > 0 && (
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
