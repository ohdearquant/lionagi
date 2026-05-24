"use client";

import dynamic from "next/dynamic";
import { useEffect, useState } from "react";
import Badge from "@/components/Badge";
import { listPlugins, getPlugin, getPluginSkill } from "@/lib/api";
import type { PluginSummary, PluginDetail, PluginSkillDetail } from "@/lib/api";

const Markdown = dynamic(() => import("@/components/Markdown"), { ssr: false });

// ─── Types ────────────────────────────────────────────────────────────────────

type PluginTab = "skills" | "agents" | "hooks" | "mcp" | "readme";

// ─── Plugin list (left pane) ──────────────────────────────────────────────────

interface PluginListProps {
  plugins: PluginSummary[];
  loading: boolean;
  selected: string | null;
  onSelect: (name: string) => void;
  filter: string;
  onFilterChange: (v: string) => void;
}

function PluginList({
  plugins,
  loading,
  selected,
  onSelect,
  filter,
  onFilterChange,
}: PluginListProps) {
  const filtered = filter
    ? plugins.filter(
        (p) =>
          p.name.toLowerCase().includes(filter.toLowerCase()) ||
          p.description.toLowerCase().includes(filter.toLowerCase()),
      )
    : plugins;

  return (
    <aside className="flex h-full flex-col border-r border-edge bg-surface-raised">
      <div className="border-b border-edge px-3 py-2.5">
        <h2 className="text-label font-semibold text-content-primary">Plugins</h2>
        <p className="text-meta text-content-muted">
          {plugins.length} plugin{plugins.length !== 1 ? "s" : ""}
        </p>
      </div>

      <div className="border-b border-edge px-3 py-2">
        <input
          type="text"
          value={filter}
          onChange={(e) => onFilterChange(e.target.value)}
          placeholder="Filter plugins..."
          className="w-full rounded border border-edge bg-surface-input px-2 py-1 text-body text-content-primary placeholder-content-muted focus:border-interactive-primary focus:outline-none"
        />
      </div>

      <div className="flex-1 overflow-y-auto">
        {loading ? (
          <div className="px-3 py-4 text-body text-content-muted">Loading...</div>
        ) : filtered.length === 0 ? (
          <div className="px-3 py-4 text-body text-content-muted">
            {filter ? "No matching plugins" : "No plugins found"}
          </div>
        ) : (
          filtered.map((p) => (
            <button
              key={`${p.source}:${p.name}`}
              type="button"
              onClick={() => onSelect(p.name)}
              className={[
                "flex w-full flex-col gap-1 border-b border-edge/50 px-3 py-2.5 text-left transition-colors",
                selected === p.name
                  ? "bg-interactive-primary/10 border-l-2 border-l-interactive-primary"
                  : "hover:bg-surface-input/50",
              ].join(" ")}
            >
              <span className="text-body font-medium text-content-primary">{p.name}</span>
              {p.description && (
                <span className="line-clamp-2 text-meta text-content-muted">{p.description}</span>
              )}
              <div className="flex flex-wrap items-center gap-1 pt-0.5">
                {p.skill_count > 0 && (
                  <Badge tone="default">
                    {p.skill_count} skill{p.skill_count !== 1 ? "s" : ""}
                  </Badge>
                )}
                {p.agent_count > 0 && (
                  <Badge tone="default">
                    {p.agent_count} agent{p.agent_count !== 1 ? "s" : ""}
                  </Badge>
                )}
                <Badge tone={p.source === "marketplace" ? "ok" : "default"}>{p.source}</Badge>
              </div>
            </button>
          ))
        )}
      </div>
    </aside>
  );
}

// ─── Skill sub-pane (inside Skills tab) ──────────────────────────────────────

interface SkillSubPaneProps {
  pluginName: string;
  skillNames: Array<{ name: string; description: string }>;
}

function SkillSubPane({ pluginName, skillNames }: SkillSubPaneProps) {
  const [selectedSkill, setSelectedSkill] = useState<string | null>(
    skillNames.length > 0 ? skillNames[0].name : null,
  );
  const [skillDetail, setSkillDetail] = useState<PluginSkillDetail | null>(null);
  const [skillLoading, setSkillLoading] = useState(false);

  // Fetch skill detail when selection changes
  useEffect(() => {
    if (!selectedSkill) return;
    let active = true;
    void Promise.resolve()
      .then(() => {
        setSkillLoading(true);
        return getPluginSkill(pluginName, selectedSkill);
      })
      .then((d) => {
        if (active) setSkillDetail(d);
      })
      .catch(() => {
        if (active) setSkillDetail(null);
      })
      .finally(() => {
        if (active) setSkillLoading(false);
      });
    return () => {
      active = false;
    };
  }, [pluginName, selectedSkill]);

  if (skillNames.length === 0) {
    return (
      <div className="flex flex-1 items-center justify-center">
        <p className="text-body text-content-muted">No skills in this plugin</p>
      </div>
    );
  }

  return (
    <div className="flex min-h-0 flex-1">
      {/* Skill list */}
      <div className="flex w-[200px] shrink-0 flex-col border-r border-edge bg-surface-raised">
        <div className="flex-1 overflow-y-auto">
          {skillNames.map((s) => (
            <button
              key={s.name}
              type="button"
              onClick={() => setSelectedSkill(s.name)}
              className={[
                "flex w-full flex-col gap-0.5 border-b border-edge/50 px-3 py-2 text-left transition-colors",
                selectedSkill === s.name
                  ? "bg-interactive-primary/10 border-l-2 border-l-interactive-primary"
                  : "hover:bg-surface-input/50",
              ].join(" ")}
            >
              <span className="text-body font-medium text-content-primary">{s.name}</span>
              {s.description && (
                <span className="line-clamp-2 text-meta text-content-muted">{s.description}</span>
              )}
            </button>
          ))}
        </div>
      </div>

      {/* Skill detail */}
      <div className="flex min-w-0 flex-1 flex-col overflow-hidden">
        {!selectedSkill ? (
          <div className="flex flex-1 items-center justify-center">
            <p className="text-body text-content-muted">Select a skill to view details</p>
          </div>
        ) : skillLoading ? (
          <div className="flex flex-1 items-center justify-center">
            <p className="text-body text-content-muted">Loading...</p>
          </div>
        ) : !skillDetail ? (
          <div className="flex flex-1 items-center justify-center">
            <p className="text-body text-content-muted">Skill not found</p>
          </div>
        ) : (
          <>
            <div className="flex shrink-0 flex-wrap items-center gap-2 border-b border-edge px-4 py-2.5">
              <h3 className="text-label font-semibold text-content-primary">{skillDetail.name}</h3>
              {skillDetail.allowed_tools.length > 0 && (
                <div className="flex flex-wrap gap-1">
                  {skillDetail.allowed_tools.map((t) => (
                    <Badge key={t} tone="default">
                      {t}
                    </Badge>
                  ))}
                </div>
              )}
              <span
                className="ml-auto truncate font-mono text-meta text-content-muted"
                title={skillDetail.path}
              >
                {skillDetail.path.split("/").slice(-2).join("/")}
              </span>
            </div>
            {skillDetail.description && (
              <div className="shrink-0 border-b border-edge bg-surface-raised px-4 py-2">
                <p className="text-body text-content-secondary">{skillDetail.description}</p>
              </div>
            )}
            <div className="flex-1 overflow-y-auto px-4 py-3">
              <pre className="whitespace-pre-wrap break-words rounded border border-edge bg-surface-base p-4 font-mono text-body text-content-secondary leading-relaxed">
                {skillDetail.content}
              </pre>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

// ─── Agent sub-pane (inside Agents tab) ──────────────────────────────────────

interface AgentSubPaneProps {
  agentRefs: Array<{ name: string; description: string }>;
}

function AgentSubPane({ agentRefs }: AgentSubPaneProps) {
  const [selectedAgent, setSelectedAgent] = useState<string | null>(
    agentRefs.length > 0 ? agentRefs[0].name : null,
  );

  const selected = agentRefs.find((a) => a.name === selectedAgent) ?? null;

  if (agentRefs.length === 0) {
    return (
      <div className="flex flex-1 items-center justify-center">
        <p className="text-body text-content-muted">No agents in this plugin</p>
      </div>
    );
  }

  return (
    <div className="flex min-h-0 flex-1">
      {/* Agent list */}
      <div className="flex w-[200px] shrink-0 flex-col border-r border-edge bg-surface-raised">
        <div className="flex-1 overflow-y-auto">
          {agentRefs.map((a) => (
            <button
              key={a.name}
              type="button"
              onClick={() => setSelectedAgent(a.name)}
              className={[
                "flex w-full flex-col gap-0.5 border-b border-edge/50 px-3 py-2 text-left transition-colors",
                selectedAgent === a.name
                  ? "bg-interactive-primary/10 border-l-2 border-l-interactive-primary"
                  : "hover:bg-surface-input/50",
              ].join(" ")}
            >
              <span className="text-body font-medium text-content-primary">{a.name}</span>
              {a.description && (
                <span className="line-clamp-2 text-meta text-content-muted">{a.description}</span>
              )}
            </button>
          ))}
        </div>
      </div>

      {/* Agent detail */}
      <div className="flex min-w-0 flex-1 flex-col overflow-hidden">
        {!selected ? (
          <div className="flex flex-1 items-center justify-center">
            <p className="text-body text-content-muted">Select an agent to view details</p>
          </div>
        ) : (
          <>
            <div className="flex shrink-0 items-center gap-3 border-b border-edge px-4 py-2.5">
              <h3 className="text-label font-semibold text-content-primary">{selected.name}</h3>
            </div>
            {selected.description && (
              <div className="shrink-0 border-b border-edge bg-surface-raised px-4 py-2">
                <p className="text-body text-content-secondary">{selected.description}</p>
              </div>
            )}
            <div className="flex flex-1 items-center justify-center">
              <p className="text-body text-content-muted">
                Open the Agents page to view and edit this agent.
              </p>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

// ─── Plugin detail (right pane) ───────────────────────────────────────────────

interface PluginDetailPaneProps {
  pluginName: string | null;
}

function PluginDetailPane({ pluginName }: PluginDetailPaneProps) {
  const [detail, setDetail] = useState<PluginDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [activeTab, setActiveTab] = useState<PluginTab>("skills");

  useEffect(() => {
    if (!pluginName) return;
    let active = true;
    void Promise.resolve()
      .then(() => {
        setLoading(true);
        setDetail(null);
        return getPlugin(pluginName);
      })
      .then((d) => {
        if (active) {
          setDetail(d);
          // Default to first available tab
          if (d.skill_count > 0) {
            setActiveTab("skills");
          } else if (d.agent_count > 0) {
            setActiveTab("agents");
          } else if (d.has_hooks) {
            setActiveTab("hooks");
          } else if (d.has_mcp) {
            setActiveTab("mcp");
          } else if (d.readme) {
            setActiveTab("readme");
          } else {
            setActiveTab("skills");
          }
        }
      })
      .catch(() => {
        if (active) setDetail(null);
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [pluginName]);

  if (!pluginName) {
    return (
      <div className="flex flex-1 items-center justify-center">
        <p className="text-body text-content-muted">Select a plugin to view its details</p>
      </div>
    );
  }

  if (loading) {
    return (
      <div className="flex flex-1 items-center justify-center">
        <p className="text-body text-content-muted">Loading...</p>
      </div>
    );
  }

  if (!detail) {
    return (
      <div className="flex flex-1 items-center justify-center">
        <p className="text-body text-content-muted">Plugin not found</p>
      </div>
    );
  }

  // Determine which tabs are visible
  const tabs: Array<{ id: PluginTab; label: string }> = [
    ...(detail.skill_count > 0 ? [{ id: "skills" as PluginTab, label: "Skills" }] : []),
    ...(detail.agent_count > 0 ? [{ id: "agents" as PluginTab, label: "Agents" }] : []),
    ...(detail.has_hooks ? [{ id: "hooks" as PluginTab, label: "Hooks" }] : []),
    ...(detail.has_mcp ? [{ id: "mcp" as PluginTab, label: "MCP" }] : []),
    ...(detail.readme ? [{ id: "readme" as PluginTab, label: "README" }] : []),
  ];

  // If the active tab is no longer visible (plugin changed), fallback to first
  const visibleTab = tabs.find((t) => t.id === activeTab) ? activeTab : (tabs[0]?.id ?? "skills");

  return (
    <div className="flex h-full flex-col overflow-hidden">
      {/* Plugin header */}
      <header className="flex shrink-0 flex-wrap items-center gap-3 border-b border-edge px-4 py-2.5">
        <h1 className="text-label font-semibold text-content-primary">{detail.name}</h1>
        <Badge tone="default">v{detail.version}</Badge>
        <Badge tone={detail.source === "marketplace" ? "ok" : "default"}>{detail.source}</Badge>
        <span
          className="ml-auto truncate font-mono text-meta text-content-muted"
          title={detail.path}
        >
          {detail.path}
        </span>
      </header>

      {detail.description && (
        <div className="shrink-0 border-b border-edge bg-surface-raised px-4 py-2">
          <p className="text-body text-content-secondary">{detail.description}</p>
        </div>
      )}

      {/* Tab bar */}
      {tabs.length > 0 && (
        <div
          role="tablist"
          aria-label="Plugin details"
          className="flex shrink-0 items-center gap-0 border-b border-edge px-4"
        >
          {tabs.map((tab) => (
            <button
              key={tab.id}
              type="button"
              role="tab"
              aria-selected={visibleTab === tab.id}
              aria-controls={`plugin-${detail.name}-panel-${tab.id}`}
              onClick={() => setActiveTab(tab.id)}
              className={[
                "relative px-3 py-2 text-body transition-colors",
                visibleTab === tab.id
                  ? "text-content-primary after:absolute after:inset-x-0 after:bottom-0 after:h-0.5 after:rounded-t after:bg-interactive-primary"
                  : "text-content-muted hover:text-content-secondary",
              ].join(" ")}
            >
              {tab.label}
            </button>
          ))}
        </div>
      )}

      {/* Tab content */}
      <div className="flex min-h-0 flex-1 overflow-hidden">
        {visibleTab === "skills" && (
          <div
            role="tabpanel"
            id={`plugin-${detail.name}-panel-skills`}
            className="flex flex-1 overflow-hidden"
          >
            <SkillSubPane key={detail.name} pluginName={detail.name} skillNames={detail.skills} />
          </div>
        )}
        {visibleTab === "agents" && (
          <div
            role="tabpanel"
            id={`plugin-${detail.name}-panel-agents`}
            className="flex flex-1 overflow-hidden"
          >
            <AgentSubPane key={detail.name} agentRefs={detail.agents} />
          </div>
        )}
        {visibleTab === "hooks" && detail.hooks && (
          <div
            role="tabpanel"
            id={`plugin-${detail.name}-panel-hooks`}
            className="flex-1 overflow-y-auto px-4 py-3"
          >
            <pre className="whitespace-pre-wrap break-words rounded border border-edge bg-surface-base p-4 font-mono text-body text-content-secondary leading-relaxed">
              {JSON.stringify(detail.hooks, null, 2)}
            </pre>
          </div>
        )}
        {visibleTab === "mcp" && detail.mcp && (
          <div
            role="tabpanel"
            id={`plugin-${detail.name}-panel-mcp`}
            className="flex-1 overflow-y-auto px-4 py-3"
          >
            <pre className="whitespace-pre-wrap break-words rounded border border-edge bg-surface-base p-4 font-mono text-body text-content-secondary leading-relaxed">
              {JSON.stringify(detail.mcp, null, 2)}
            </pre>
          </div>
        )}
        {visibleTab === "readme" && detail.readme && (
          <div
            role="tabpanel"
            id={`plugin-${detail.name}-panel-readme`}
            className="flex-1 overflow-y-auto px-4 py-4"
          >
            <Markdown>{detail.readme}</Markdown>
          </div>
        )}
      </div>
    </div>
  );
}

// ─── Page root ────────────────────────────────────────────────────────────────

export default function PluginsPage() {
  const [plugins, setPlugins] = useState<PluginSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState<string | null>(null);
  const [filter, setFilter] = useState("");

  useEffect(() => {
    let active = true;
    listPlugins()
      .then((data) => {
        if (active) {
          setPlugins(data.plugins);
          if (data.plugins.length > 0) {
            setSelected(data.plugins[0].name);
          }
        }
      })
      .catch(() => {
        if (active) setPlugins([]);
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, []);

  return (
    <div className="flex" style={{ height: "calc(100vh - 44px)" }}>
      {/* Left pane: 280px */}
      <div className="w-[280px] shrink-0">
        <PluginList
          plugins={plugins}
          loading={loading}
          selected={selected}
          onSelect={setSelected}
          filter={filter}
          onFilterChange={setFilter}
        />
      </div>

      {/* Right pane: fill remainder */}
      <div className="flex min-w-0 flex-1 bg-surface-base">
        <PluginDetailPane pluginName={selected} />
      </div>
    </div>
  );
}
