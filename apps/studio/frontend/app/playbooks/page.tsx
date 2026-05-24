"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import Button from "@/components/Button";
import Badge from "@/components/Badge";
import {
  listWorkers,
  getDefinition,
  getDefinitionVersion,
  saveDefinition,
  rollbackDefinition,
} from "@/lib/api";
import type { DefinitionDetail, DefinitionVersion } from "@/lib/api";
import { notImplemented } from "@/lib/copy";
import type { WorkerSummary } from "@/lib/types";

type PlaybookItem = WorkerSummary;

function formatTime(epochSeconds: number): string {
  const d = new Date(epochSeconds * 1000);
  return d.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

// ─── Left pane: playbook list ─────────────────────────────────────────────────

function PlaybookList({
  items,
  selected,
  onSelect,
  loading,
  error,
}: {
  items: PlaybookItem[];
  selected: string | null;
  onSelect: (name: string) => void;
  loading: boolean;
  error: string | null;
}) {
  return (
    <aside className="flex w-[280px] shrink-0 flex-col border-r border-edge bg-surface-nav">
      {/* Header */}
      <div className="flex items-center justify-between border-b border-edge px-3 py-2.5">
        <span className="text-label font-semibold text-content-primary">Playbooks</span>
        <Button variant="primary" size="sm" leading="+" disabled title="Coming soon">
          New
        </Button>
      </div>

      {/* Error */}
      {error ? <div className="px-3 py-2 text-meta text-status-error">{error}</div> : null}

      {/* List */}
      <nav className="flex-1 overflow-y-auto">
        {loading && items.length === 0 ? (
          <p className="px-3 py-4 text-meta text-content-muted">Loading…</p>
        ) : items.length === 0 ? (
          <p className="px-3 py-4 text-meta text-content-muted">No playbooks found.</p>
        ) : (
          items.map((item) => {
            const isSelected = item.name === selected;
            return (
              <button
                key={item.name}
                type="button"
                onClick={() => onSelect(item.name)}
                className={[
                  "flex w-full flex-col gap-0.5 border-b border-edge px-3 py-2.5 text-left transition-colors",
                  isSelected
                    ? "bg-surface-overlay text-content-primary"
                    : "hover:bg-surface-raised text-content-secondary hover:text-content-primary",
                ]
                  .filter(Boolean)
                  .join(" ")}
              >
                <span
                  className={[
                    "truncate font-mono text-body font-medium",
                    isSelected ? "text-content-primary" : "text-content-primary",
                  ].join(" ")}
                >
                  {item.name}
                </span>
                {item.description ? (
                  <span
                    className="text-meta text-content-muted"
                    style={{
                      display: "-webkit-box",
                      WebkitBoxOrient: "vertical",
                      WebkitLineClamp: 2,
                      overflow: "hidden",
                    }}
                  >
                    {item.description}
                  </span>
                ) : null}
              </button>
            );
          })
        )}
      </nav>
    </aside>
  );
}

// ─── Version history sidebar ──────────────────────────────────────────────────

function VersionHistory({
  versions,
  currentVersion,
  viewingVersion,
  onView,
  onRestore,
  restoring,
}: {
  versions: DefinitionVersion[];
  currentVersion: number;
  viewingVersion: number | null;
  onView: (v: number) => void;
  onRestore: (v: number) => void;
  restoring: boolean;
}) {
  return (
    <div className="flex w-[200px] shrink-0 flex-col border-l border-edge bg-surface-nav">
      <div className="border-b border-edge px-3 py-2.5">
        <span className="text-meta font-semibold uppercase tracking-[0.06em] text-content-muted">
          Versions
        </span>
      </div>
      <div className="flex-1 overflow-y-auto">
        {versions.length === 0 ? (
          <p className="px-3 py-3 text-meta text-content-muted">No history</p>
        ) : (
          versions.map((v) => {
            const isCurrent = v.version === currentVersion;
            const isViewing = viewingVersion === v.version;
            return (
              <div
                key={v.id}
                className={[
                  "flex flex-col gap-0.5 border-b border-edge px-3 py-2",
                  isViewing ? "bg-surface-overlay" : "",
                ].join(" ")}
              >
                <div className="flex items-center gap-1.5">
                  <span className="font-mono text-meta text-content-secondary">v{v.version}</span>
                  {isCurrent ? <Badge tone="ok">current</Badge> : null}
                </div>
                <span className="text-meta text-content-muted">{formatTime(v.created_at)}</span>
                {v.message ? (
                  <span
                    className="text-meta text-content-muted"
                    style={{
                      display: "-webkit-box",
                      WebkitBoxOrient: "vertical",
                      WebkitLineClamp: 2,
                      overflow: "hidden",
                    }}
                  >
                    {v.message}
                  </span>
                ) : null}
                <div className="mt-1 flex gap-1">
                  <button
                    type="button"
                    onClick={() => onView(v.version)}
                    className="text-meta text-content-muted underline hover:text-content-primary"
                  >
                    {isViewing ? "viewing" : "view"}
                  </button>
                  {!isCurrent ? (
                    <>
                      <span className="text-meta text-content-muted">·</span>
                      <button
                        type="button"
                        onClick={() => onRestore(v.version)}
                        disabled={restoring}
                        className="text-meta text-status-error underline hover:opacity-80 disabled:opacity-50"
                      >
                        restore
                      </button>
                    </>
                  ) : null}
                </div>
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}

// ─── Right pane: playbook detail ──────────────────────────────────────────────

function PlaybookDetail({ name }: { name: string }) {
  const [detail, setDetail] = useState<DefinitionDetail | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  // Editor state
  const [editing, setEditing] = useState(false);
  const [editorContent, setEditorContent] = useState("");
  const [commitMessage, setCommitMessage] = useState("");
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saveSuccess, setSaveSuccess] = useState(false);

  // Version viewing
  const [viewingVersion, setViewingVersion] = useState<number | null>(null);
  const [viewContent, setViewContent] = useState<string | null>(null);
  const [viewLoading, setViewLoading] = useState(false);
  const [restoring, setRestoring] = useState(false);

  // Run state
  const [runError, setRunError] = useState<string | null>(null);

  // Track active fetch so stale loads from previous selection don't apply
  const loadToken = useRef(0);

  const loadDetail = useCallback((playbookName: string) => {
    const token = ++loadToken.current;
    // Defer resets into a Promise so setState is never called synchronously
    // inside the effect body — avoids react-hooks/set-state-in-effect
    void Promise.resolve()
      .then(() => {
        setDetail(null);
        setLoadError(null);
        setEditing(false);
        setEditorContent("");
        setCommitMessage("");
        setSaveError(null);
        setSaveSuccess(false);
        setViewingVersion(null);
        setViewContent(null);
        setRunError(null);
        return getDefinition("playbook", playbookName);
      })
      .then((d) => {
        if (token !== loadToken.current) return;
        setDetail(d);
        setEditorContent(d.content);
      })
      .catch((err) => {
        if (token !== loadToken.current) return;
        setLoadError(err instanceof Error ? err.message : "Failed to load");
      });
  }, []);

  useEffect(() => {
    loadDetail(name);
  }, [name, loadDetail]);

  const handleSave = useCallback(async () => {
    if (!detail || saving) return;
    setSaving(true);
    setSaveError(null);
    setSaveSuccess(false);
    try {
      await saveDefinition("playbook", name, editorContent, commitMessage.trim() || undefined);
      setSaveSuccess(true);
      setCommitMessage("");
      setEditing(false);
      loadDetail(name);
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : "Save failed");
    } finally {
      setSaving(false);
    }
  }, [detail, saving, name, editorContent, commitMessage, loadDetail]);

  const handleViewVersion = useCallback(
    async (version: number) => {
      if (!detail) return;
      if (version === detail.version) {
        // Viewing current — reset to live content
        setViewingVersion(null);
        setViewContent(null);
        return;
      }
      setViewingVersion(version);
      setViewLoading(true);
      setViewContent(null);
      try {
        const res = await getDefinitionVersion("playbook", name, version);
        setViewContent(res.content);
      } catch {
        setViewContent("(failed to load version content)");
      } finally {
        setViewLoading(false);
      }
    },
    [detail, name],
  );

  const handleRestore = useCallback(
    async (version: number) => {
      if (!detail || restoring) return;
      setRestoring(true);
      setSaveError(null);
      try {
        await rollbackDefinition("playbook", name, version);
        loadDetail(name);
      } catch (err) {
        setSaveError(err instanceof Error ? err.message : "Restore failed");
      } finally {
        setRestoring(false);
      }
    },
    [detail, restoring, name, loadDetail],
  );

  const handleRun = useCallback(async () => {
    setRunError("Not yet available");
  }, []);

  // Displayed content: versioned view, editing buffer, or live content
  const displayedContent =
    viewingVersion !== null && viewContent !== null
      ? viewContent
      : editing
        ? editorContent
        : (detail?.content ?? "");

  if (loadError) {
    return (
      <div className="flex flex-1 items-start p-4">
        <div className="rounded border border-status-error/30 bg-status-error-bg px-3 py-2 text-body text-status-error">
          {loadError}
        </div>
      </div>
    );
  }

  if (!detail) {
    return (
      <div className="flex flex-1 items-center justify-center">
        <p className="text-body text-content-muted">Loading…</p>
      </div>
    );
  }

  const isViewingOld = viewingVersion !== null && viewingVersion !== detail.version;

  return (
    <div className="flex flex-1 min-h-0 flex-col">
      {/* Detail toolbar */}
      <div className="flex items-center gap-2 border-b border-edge bg-surface-nav px-3 py-2">
        <span className="font-mono text-label font-semibold text-content-primary">
          {detail.name}
        </span>
        <span className="text-meta text-content-muted">
          v{isViewingOld ? viewingVersion : detail.version}
        </span>
        {isViewingOld ? <Badge tone="pending">viewing old version</Badge> : null}

        <div className="ml-auto flex items-center gap-2">
          {runError ? <span className="text-meta text-status-error">{runError}</span> : null}
          {saveError ? <span className="text-meta text-status-error">{saveError}</span> : null}
          {saveSuccess ? <span className="text-meta text-status-success">Saved</span> : null}

          {editing ? (
            <>
              <input
                type="text"
                value={commitMessage}
                onChange={(e) => setCommitMessage(e.target.value)}
                placeholder="Commit message (optional)"
                className="w-52 rounded-md border border-edge bg-surface-input px-2 py-1 text-meta text-content-primary placeholder-content-muted focus:border-interactive-primary focus:outline-none"
              />
              <Button
                variant="ghost"
                size="sm"
                onClick={() => {
                  setEditing(false);
                  setEditorContent(detail.content);
                  setSaveError(null);
                }}
              >
                Cancel
              </Button>
              <Button variant="primary" size="sm" onClick={handleSave} disabled={saving}>
                {saving ? "Saving…" : "Save"}
              </Button>
            </>
          ) : (
            <>
              {isViewingOld ? (
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => {
                    setViewingVersion(null);
                    setViewContent(null);
                  }}
                >
                  Back to current
                </Button>
              ) : null}
              <Button
                variant="secondary"
                size="sm"
                leading="✎"
                onClick={() => {
                  setEditing(true);
                  setEditorContent(detail.content);
                  setSaveSuccess(false);
                }}
                disabled={isViewingOld}
              >
                Edit
              </Button>
              <Button
                variant="primary"
                size="sm"
                leading="▶"
                onClick={handleRun}
                disabled
                title={notImplemented.runPlaybook}
              >
                Run
              </Button>
            </>
          )}
        </div>
      </div>

      {/* Main content + version sidebar */}
      <div className="flex flex-1 min-h-0">
        {/* Editor / viewer */}
        <div className="flex flex-1 min-h-0 flex-col p-0">
          {viewLoading ? (
            <div className="flex flex-1 items-center justify-center">
              <p className="text-body text-content-muted">Loading version…</p>
            </div>
          ) : editing ? (
            <textarea
              value={editorContent}
              onChange={(e) => setEditorContent(e.target.value)}
              spellCheck={false}
              className="flex-1 resize-none bg-surface-base px-4 py-3 font-mono text-meta text-content-primary focus:outline-none"
              style={{ minHeight: 0 }}
            />
          ) : (
            <pre className="flex-1 overflow-auto bg-surface-base px-4 py-3 font-mono text-meta text-content-secondary">
              {displayedContent || <span className="text-content-muted">(empty)</span>}
            </pre>
          )}
        </div>

        {/* Version history sidebar */}
        {detail.versions.length > 0 ? (
          <VersionHistory
            versions={detail.versions}
            currentVersion={detail.version}
            viewingVersion={viewingVersion}
            onView={handleViewVersion}
            onRestore={handleRestore}
            restoring={restoring}
          />
        ) : null}
      </div>
    </div>
  );
}

// ─── Empty state when no playbook is selected ─────────────────────────────────

function EmptyDetail() {
  return (
    <div className="flex flex-1 items-center justify-center bg-surface-base">
      <div className="flex max-w-sm flex-col items-center gap-3 text-center">
        <span className="text-3xl text-content-muted" aria-hidden>
          ◇
        </span>
        <p className="text-body text-content-secondary">
          Select a playbook from the list to view its content and version history.
        </p>
        <Button variant="primary" size="md" leading="+" disabled title="Coming soon">
          New Playbook
        </Button>
      </div>
    </div>
  );
}

// ─── Page root ────────────────────────────────────────────────────────────────

export default function PlaybooksPage() {
  const [items, setItems] = useState<PlaybookItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [listError, setListError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    listWorkers()
      .then((res) => {
        if (!active) return;
        setItems(res.workers);
        setListError(null);
      })
      .catch((err) => {
        if (!active) return;
        setListError(err instanceof Error ? err.message : "Failed to load");
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, []);

  return (
    <div className="flex h-[calc(100vh-56px)]">
      <PlaybookList
        items={items}
        selected={selected}
        onSelect={setSelected}
        loading={loading}
        error={listError}
      />

      {selected ? <PlaybookDetail name={selected} /> : <EmptyDetail />}
    </div>
  );
}
