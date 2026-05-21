"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import Button from "@/components/Button";
import Badge from "@/components/Badge";
import {
  listAgents,
  getDefinition,
  saveDefinition,
  rollbackDefinition,
  getDefinitionVersion,
} from "@/lib/api";
import type {
  DefinitionDetail,
  DefinitionVersion,
} from "@/lib/api";
import type { AgentProfile, AgentProfileSummary } from "@/lib/types";

function messageFromError(error: unknown): string {
  return error instanceof Error ? error.message : "Unknown error";
}

function relativeTime(ts: number): string {
  const diffMs = Date.now() - ts * 1000;
  const s = Math.floor(diffMs / 1000);
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.floor(h / 24);
  return `${d}d ago`;
}

// ─── Left pane: agent list ────────────────────────────────────────────────────

interface AgentListProps {
  agents: AgentProfileSummary[];
  loading: boolean;
  selectedName: string | null;
  onSelect: (name: string) => void;
  searchQuery: string;
  onSearchChange: (q: string) => void;
}

function AgentList({
  agents,
  loading,
  selectedName,
  onSelect,
  searchQuery,
  onSearchChange,
}: AgentListProps) {
  const filtered = agents.filter((a) => {
    if (!searchQuery.trim()) return true;
    const q = searchQuery.toLowerCase();
    return (
      a.name.toLowerCase().includes(q) ||
      (a.description ?? "").toLowerCase().includes(q) ||
      a.provider.toLowerCase().includes(q) ||
      a.model.toLowerCase().includes(q)
    );
  });

  return (
    <div className="flex h-full flex-col border-r border-edge">
      {/* List header */}
      <div className="flex items-center justify-between border-b border-edge px-3 py-2">
        <span className="text-meta font-medium text-content-secondary uppercase tracking-[0.06em]">
          Agents
        </span>
        <Link href="/agents/new">
          <Button variant="primary" size="sm" leading="+">
            New
          </Button>
        </Link>
      </div>

      {/* Search */}
      <div className="px-2 py-2 border-b border-edge">
        <input
          type="text"
          value={searchQuery}
          onChange={(e) => onSearchChange(e.target.value)}
          placeholder="Filter agents..."
          className="w-full rounded border border-edge bg-surface-input px-2 py-1 text-meta text-content-primary placeholder-content-muted focus:border-interactive-primary focus:outline-none"
        />
      </div>

      {/* Agent items */}
      <div className="flex-1 overflow-y-auto">
        {loading ? (
          <div className="px-3 py-4 text-meta text-content-muted">Loading...</div>
        ) : filtered.length === 0 ? (
          <div className="px-3 py-4 text-meta text-content-muted">
            {searchQuery ? "No agents match filter." : "No agents found."}
          </div>
        ) : (
          filtered.map((agent) => {
            const isSelected = agent.name === selectedName;
            return (
              <button
                key={agent.name}
                type="button"
                onClick={() => onSelect(agent.name)}
                className={[
                  "w-full border-b border-edge px-3 py-2.5 text-left transition-colors hover:bg-surface-overlay",
                  isSelected ? "bg-surface-overlay border-l-2 border-l-interactive-primary" : "",
                ]
                  .filter(Boolean)
                  .join(" ")}
              >
                <div className="flex min-w-0 items-center justify-between gap-2">
                  <span className="truncate font-medium text-body text-content-primary">
                    {agent.name}
                  </span>
                  <span className="shrink-0 font-mono text-meta text-content-muted">
                    {agent.provider}
                  </span>
                </div>
                <div className="mt-0.5 flex min-w-0 items-center gap-2">
                  <span className="truncate font-mono text-meta text-content-muted">
                    {agent.model}
                  </span>
                </div>
                {agent.description ? (
                  <p className="mt-1 truncate text-meta text-content-muted">
                    {agent.description}
                  </p>
                ) : null}
              </button>
            );
          })
        )}
      </div>
    </div>
  );
}

// ─── Right pane: agent detail ─────────────────────────────────────────────────

interface AgentDetailProps {
  agentName: string | null;
  agentProfile: AgentProfile | null;
}

function AgentDetail({ agentName, agentProfile }: AgentDetailProps) {
  const [def, setDef] = useState<DefinitionDetail | null>(null);
  const [defError, setDefError] = useState<string | null>(null);
  const [defLoading, setDefLoading] = useState(false);

  const [editing, setEditing] = useState(false);
  const [editContent, setEditContent] = useState("");
  const [commitMessage, setCommitMessage] = useState("");
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  const [previewVersion, setPreviewVersion] = useState<DefinitionDetail | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [rollingBack, setRollingBack] = useState(false);

  // Load definition whenever the selected agent changes
  useEffect(() => {
    if (!agentName) {
      setDef(null);
      setDefError(null);
      setPreviewVersion(null);
      setEditing(false);
      return;
    }

    let active = true;
    setDefLoading(true);
    setDefError(null);
    setDef(null);
    setPreviewVersion(null);
    setEditing(false);

    getDefinition("agent", agentName)
      .then((data) => {
        if (active) {
          setDef(data);
          setEditContent(data.content);
        }
      })
      .catch((err) => {
        if (active) setDefError(messageFromError(err));
      })
      .finally(() => {
        if (active) setDefLoading(false);
      });

    return () => {
      active = false;
    };
  }, [agentName]);

  const handleEdit = useCallback(() => {
    if (def) setEditContent(def.content);
    setEditing(true);
    setSaveError(null);
    setPreviewVersion(null);
  }, [def]);

  const handleCancelEdit = useCallback(() => {
    setEditing(false);
    setSaveError(null);
    if (def) setEditContent(def.content);
    setCommitMessage("");
  }, [def]);

  const handleSave = useCallback(async () => {
    if (!agentName || !editing) return;
    setSaving(true);
    setSaveError(null);
    try {
      await saveDefinition("agent", agentName, editContent, commitMessage || undefined);
      // Reload the definition to get updated version + history
      const updated = await getDefinition("agent", agentName);
      setDef(updated);
      setEditContent(updated.content);
      setEditing(false);
      setCommitMessage("");
    } catch (err) {
      setSaveError(messageFromError(err));
    } finally {
      setSaving(false);
    }
  }, [agentName, editing, editContent, commitMessage]);

  const handleViewVersion = useCallback(
    async (v: DefinitionVersion) => {
      if (!agentName) return;
      setPreviewLoading(true);
      try {
        const data = await getDefinitionVersion("agent", agentName, v.version);
        setPreviewVersion(data);
        setEditing(false);
      } catch {
        /* silently ignore preview errors */
      } finally {
        setPreviewLoading(false);
      }
    },
    [agentName],
  );

  const handleRestoreVersion = useCallback(
    async (version: number) => {
      if (!agentName) return;
      setRollingBack(true);
      try {
        await rollbackDefinition("agent", agentName, version);
        const updated = await getDefinition("agent", agentName);
        setDef(updated);
        setEditContent(updated.content);
        setPreviewVersion(null);
      } catch {
        /* silently ignore rollback errors */
      } finally {
        setRollingBack(false);
      }
    },
    [agentName],
  );

  if (!agentName) {
    return (
      <div className="flex flex-1 items-center justify-center text-body text-content-muted">
        Select an agent to view details.
      </div>
    );
  }

  if (defLoading) {
    return (
      <div className="flex flex-1 items-center justify-center text-body text-content-muted">
        Loading...
      </div>
    );
  }

  if (defError) {
    return (
      <div className="flex flex-1 flex-col gap-3 p-4">
        <div className="rounded border border-status-error/30 bg-status-error-bg px-3 py-2 text-body text-status-error">
          {defError}
        </div>
        {/* Fall back to parsed profile when definitions API is unavailable */}
        {agentProfile ? <AgentProfileFallback agent={agentProfile} /> : null}
      </div>
    );
  }

  if (!def) return null;

  const displayContent = previewVersion ? previewVersion.content : (editing ? editContent : def.content);

  return (
    <div className="flex h-full flex-col overflow-hidden">
      {/* Top bar */}
      <div className="flex shrink-0 items-center gap-3 border-b border-edge px-4 py-2">
        <h2 className="font-mono text-label font-semibold text-content-primary">
          {agentName}
        </h2>
        {agentProfile?.provider ? (
          <Badge tone="default">{agentProfile.provider}</Badge>
        ) : null}
        {agentProfile?.model ? (
          <span className="font-mono text-meta text-content-muted">{agentProfile.model}</span>
        ) : null}

        <div className="ml-auto flex items-center gap-2">
          {previewVersion ? (
            <>
              <span className="text-meta text-content-muted">
                Viewing v{previewVersion.version}
              </span>
              <Button
                variant="primary"
                size="sm"
                onClick={() => handleRestoreVersion(previewVersion.version)}
                disabled={rollingBack}
              >
                {rollingBack ? "Restoring..." : "Restore this version"}
              </Button>
              <Button
                variant="secondary"
                size="sm"
                onClick={() => setPreviewVersion(null)}
              >
                Back to current
              </Button>
            </>
          ) : editing ? (
            <>
              <input
                type="text"
                value={commitMessage}
                onChange={(e) => setCommitMessage(e.target.value)}
                placeholder="Commit message (optional)"
                className="w-48 rounded border border-edge bg-surface-input px-2 py-1 text-meta text-content-primary placeholder-content-muted focus:border-interactive-primary focus:outline-none"
              />
              <Button
                variant="primary"
                size="sm"
                onClick={handleSave}
                disabled={saving}
              >
                {saving ? "Saving..." : "Save"}
              </Button>
              <Button variant="secondary" size="sm" onClick={handleCancelEdit}>
                Cancel
              </Button>
            </>
          ) : (
            <>
              <span className="text-meta text-content-muted">v{def.version}</span>
              <Button variant="secondary" size="sm" leading="✎" onClick={handleEdit}>
                Edit
              </Button>
              <Link href={`/agents/${encodeURIComponent(agentName)}/edit`}>
                <Button variant="ghost" size="sm">
                  Full editor
                </Button>
              </Link>
            </>
          )}
        </div>
      </div>

      {saveError ? (
        <div className="shrink-0 border-b border-status-error/30 bg-status-error-bg px-4 py-2 text-meta text-status-error">
          {saveError}
        </div>
      ) : null}

      {/* Two-column body: content + version history */}
      <div className="flex min-h-0 flex-1">
        {/* Content area */}
        <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
          {previewLoading ? (
            <div className="flex flex-1 items-center justify-center text-meta text-content-muted">
              Loading version...
            </div>
          ) : editing ? (
            <textarea
              value={editContent}
              onChange={(e) => setEditContent(e.target.value)}
              spellCheck={false}
              className="flex-1 resize-none bg-surface-base p-4 font-mono text-meta text-content-primary focus:outline-none"
            />
          ) : (
            <pre className="flex-1 overflow-auto whitespace-pre-wrap break-words p-4 font-mono text-meta text-content-secondary">
              {displayContent}
            </pre>
          )}
        </div>

        {/* Version history sidebar */}
        <VersionHistory
          versions={def.versions}
          currentVersion={def.version}
          previewingVersion={previewVersion?.version ?? null}
          onViewVersion={handleViewVersion}
          onRestoreVersion={handleRestoreVersion}
          rollingBack={rollingBack}
        />
      </div>
    </div>
  );
}

// ─── Version history panel ────────────────────────────────────────────────────

interface VersionHistoryProps {
  versions: DefinitionVersion[];
  currentVersion: number;
  previewingVersion: number | null;
  onViewVersion: (v: DefinitionVersion) => void;
  onRestoreVersion: (version: number) => void;
  rollingBack: boolean;
}

function VersionHistory({
  versions,
  currentVersion,
  previewingVersion,
  onViewVersion,
  onRestoreVersion,
  rollingBack,
}: VersionHistoryProps) {
  const sorted = [...versions].sort((a, b) => b.version - a.version);

  return (
    <div className="flex w-56 shrink-0 flex-col border-l border-edge">
      <div className="border-b border-edge px-3 py-2">
        <span className="text-meta font-medium uppercase tracking-[0.06em] text-content-muted">
          Version History
        </span>
      </div>
      <div className="flex-1 overflow-y-auto">
        {sorted.length === 0 ? (
          <div className="px-3 py-3 text-meta text-content-muted">No versions yet.</div>
        ) : (
          sorted.map((v) => {
            const isCurrent = v.version === currentVersion;
            const isPreviewing = v.version === previewingVersion;
            return (
              <div
                key={v.id}
                className={[
                  "border-b border-edge px-3 py-2 text-meta",
                  isPreviewing ? "bg-surface-overlay" : "",
                ]
                  .filter(Boolean)
                  .join(" ")}
              >
                <div className="flex items-center justify-between gap-1">
                  <span
                    className={[
                      "font-mono font-medium",
                      isCurrent ? "text-content-primary" : "text-content-secondary",
                    ].join(" ")}
                  >
                    v{v.version}
                    {isCurrent ? (
                      <span className="ml-1 text-meta text-content-muted">(current)</span>
                    ) : null}
                  </span>
                  <span className="text-meta text-content-muted">
                    {relativeTime(v.created_at)}
                  </span>
                </div>
                {v.message ? (
                  <p className="mt-0.5 truncate text-meta text-content-muted" title={v.message}>
                    {v.message}
                  </p>
                ) : null}
                {!isCurrent ? (
                  <div className="mt-1.5 flex gap-1.5">
                    <button
                      type="button"
                      onClick={() => onViewVersion(v)}
                      className="rounded border border-edge px-1.5 py-0.5 text-meta text-content-muted hover:border-edge-strong hover:text-content-primary"
                    >
                      View
                    </button>
                    <button
                      type="button"
                      onClick={() => onRestoreVersion(v.version)}
                      disabled={rollingBack}
                      className="rounded border border-edge px-1.5 py-0.5 text-meta text-content-muted hover:border-edge-strong hover:text-content-primary disabled:opacity-50"
                    >
                      Restore
                    </button>
                  </div>
                ) : null}
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}

// ─── Fallback: show parsed profile when definitions API unavailable ────────────

function AgentProfileFallback({ agent }: { agent: AgentProfile }) {
  return (
    <div className="flex flex-col gap-3 overflow-auto p-4">
      <div className="flex flex-wrap items-center gap-x-5 gap-y-1 rounded border border-edge bg-surface-overlay px-4 py-3 text-meta">
        <SummaryChip label="Provider" value={agent.provider || "—"} />
        <SummaryChip label="Model" value={agent.model || "—"} />
        {agent.permission_mode ? (
          <SummaryChip label="Permission" value={agent.permission_mode} />
        ) : null}
        {agent.reasoning_effort ? (
          <SummaryChip label="Effort" value={agent.reasoning_effort} />
        ) : null}
      </div>

      {agent.system_prompt ? (
        <div className="flex flex-col gap-2 rounded border border-edge bg-surface-raised p-3">
          <span className="text-meta font-medium uppercase tracking-[0.06em] text-content-muted">
            System Prompt
          </span>
          <pre className="max-h-60 overflow-auto whitespace-pre-wrap break-words font-mono text-meta text-content-secondary">
            {agent.system_prompt}
          </pre>
        </div>
      ) : null}

      {agent.guidance ? (
        <div className="flex flex-col gap-2 rounded border border-edge bg-surface-raised p-3">
          <span className="text-meta font-medium uppercase tracking-[0.06em] text-content-muted">
            Guidance
          </span>
          <pre className="max-h-60 overflow-auto whitespace-pre-wrap break-words font-mono text-meta text-content-secondary">
            {agent.guidance}
          </pre>
        </div>
      ) : null}
    </div>
  );
}

function SummaryChip({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center gap-1.5">
      <span className="uppercase tracking-[0.06em] text-content-muted">{label}</span>
      <span className="font-mono text-content-primary">{value}</span>
    </div>
  );
}

// ─── Page root ────────────────────────────────────────────────────────────────

export default function AgentsPage() {
  const [agents, setAgents] = useState<AgentProfileSummary[]>([]);
  const [agentsLoading, setAgentsLoading] = useState(true);
  const [selectedName, setSelectedName] = useState<string | null>(null);
  const [searchQuery, setSearchQuery] = useState("");

  // Cached full agent profiles (for provider/model display in detail header)
  const [profileCache, setProfileCache] = useState<Record<string, AgentProfile>>({});

  useEffect(() => {
    let active = true;
    listAgents()
      .then((res) => {
        if (active) {
          setAgents(res.agents);
          // Auto-select first agent
          if (res.agents.length > 0) {
            setSelectedName(res.agents[0].name);
          }
        }
      })
      .catch(() => {
        /* errors shown inline */
      })
      .finally(() => {
        if (active) setAgentsLoading(false);
      });
    return () => {
      active = false;
    };
  }, []);

  // Eager-load profile for selected agent (for badges in detail header)
  useEffect(() => {
    if (!selectedName || profileCache[selectedName]) return;
    // listAgents already returns summaries with provider/model; we have enough.
    // Only fetch full profile lazily if someone needs system_prompt / guidance
    // in the fallback path — triggered from AgentDetail itself when def API fails.
  }, [selectedName, profileCache]);

  const selectedSummary = agents.find((a) => a.name === selectedName) ?? null;

  // Convert AgentProfileSummary to a minimal AgentProfile for the fallback
  const fallbackProfile: AgentProfile | null = selectedSummary
    ? {
        name: selectedSummary.name,
        path: "",
        provider: selectedSummary.provider,
        model: selectedSummary.model,
        system_prompt: null,
        guidance: null,
        description: selectedSummary.description,
      }
    : null;

  return (
    <div
      className="flex"
      style={{ height: "calc(100vh - 44px)" }}
    >
      {/* Left pane: ~280px */}
      <div className="w-[280px] shrink-0">
        <AgentList
          agents={agents}
          loading={agentsLoading}
          selectedName={selectedName}
          onSelect={setSelectedName}
          searchQuery={searchQuery}
          onSearchChange={setSearchQuery}
        />
      </div>

      {/* Right pane: fill remainder */}
      <div className="flex min-w-0 flex-1 bg-surface-base">
        <AgentDetail
          agentName={selectedName}
          agentProfile={fallbackProfile}
        />
      </div>
    </div>
  );
}
