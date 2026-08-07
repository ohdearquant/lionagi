/**
 * Detail pane for a plugin — a bundle of skills + agents, not a single item.
 * Renders composition (counts, hooks/mcp presence), the bundled skill and
 * agent lists (each opening a nested read-only detail view in-place), and
 * usage stats scoped to this plugin specifically (server-side filter, not a
 * best-effort scan of the most recent invocations).
 */

import { useEffect, useState } from "react";
import { useTranslations } from "use-intl";
import { getPlugin, getPluginAgent } from "@/lib/api";
import type { PluginAgentDetail, PluginDetail as PluginDetailData } from "@/lib/api";
import { formatInvocationAge, useInvocationStats } from "@/lib/useInvocationStats";
import { SkillDetail } from "@/components/library/SkillDetail";
import DrawerBackButton from "@/components/ui/DrawerBackButton";
import DrawerHeader from "@/components/ui/DrawerHeader";
import SectionLabel from "@/components/ui/SectionLabel";
import StatusPill from "@/components/ui/StatusPill";
import { Link } from "@tanstack/react-router";

interface PluginDetailProps {
  name: string;
  onBack?: () => void;
}

type NestedSelection = { kind: "skill" | "agent"; name: string } | null;

export function PluginDetail({ name, onBack }: PluginDetailProps) {
  const t = useTranslations("library.drawer");
  const [plugin, setPlugin] = useState<PluginDetailData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [nested, setNested] = useState<NestedSelection>(null);

  const { stats, loading: statsLoading } = useInvocationStats("plugin", name);

  useEffect(() => {
    let alive = true;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- synchronous reset clears stale state before the async fetch resolves
    setLoading(true);
    setError(null);
    setPlugin(null);
    setNested(null);

    getPlugin(name)
      .then((p) => {
        if (alive) setPlugin(p);
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
  }, [name]);

  if (nested?.kind === "skill") {
    return <SkillDetail name={nested.name} pluginName={name} onBack={() => setNested(null)} />;
  }
  if (nested?.kind === "agent") {
    return (
      <PluginAgentDetailView
        pluginName={name}
        agentName={nested.name}
        onBack={() => setNested(null)}
      />
    );
  }

  if (loading) {
    return (
      <div className="flex h-full items-center justify-center text-meta text-content-muted">
        {t("loading")}
      </div>
    );
  }

  if (error || !plugin) {
    return <div className="p-4 text-meta text-status-failure">{error ?? t("notFound")}</div>;
  }

  const mcpServerNames = plugin.mcp ? Object.keys(plugin.mcp) : [];

  return (
    <div className="flex h-full flex-col overflow-hidden">
      {onBack && <DrawerBackButton onClick={onBack}>{t("back")}</DrawerBackButton>}

      <DrawerHeader
        name={plugin.name}
        badge="plugin"
        trailing={
          <span className="font-data text-[length:var(--t-xs)] text-content-muted">
            v{plugin.version}
          </span>
        }
      />

      <div className="shrink-0 border-b border-edge px-4 py-1.5 font-data text-[length:var(--t-xs)] text-content-muted">
        {plugin.source}
        {plugin.path ? ` · ${plugin.path}` : ""}
      </div>

      <div className="flex-1 overflow-auto">
        <div className="flex flex-col gap-4 p-4">
          {plugin.description && (
            <p className="text-[length:var(--t-sm)] text-content-secondary">{plugin.description}</p>
          )}

          {/* Composition strip */}
          <div className="flex flex-wrap gap-x-5 gap-y-2 text-[length:var(--t-xs)]">
            <div className="flex items-center gap-1.5">
              <span className="text-content-muted">{t("skillCount")}</span>
              <span className="font-data text-content-primary">{plugin.skill_count}</span>
            </div>
            <div className="flex items-center gap-1.5">
              <span className="text-content-muted">{t("agentCount")}</span>
              <span className="font-data text-content-primary">{plugin.agent_count}</span>
            </div>
            <div className="flex items-center gap-1.5">
              <span className="text-content-muted">{t("pluginHooks")}</span>
              <span className="font-data text-content-primary">
                {plugin.has_hooks ? t("yes") : t("no")}
              </span>
            </div>
            <div className="flex items-center gap-1.5">
              <span className="text-content-muted">{t("pluginMcp")}</span>
              <span className="font-data text-content-primary">
                {plugin.has_mcp ? t("yes") : t("no")}
              </span>
            </div>
          </div>

          {plugin.skills.length > 0 && (
            <div>
              <SectionLabel className="mb-1.5">{t("skillCount")}</SectionLabel>
              <div className="flex flex-col rounded border border-edge" style={{ borderRadius: 4 }}>
                {plugin.skills.map((s, i) => (
                  <button
                    key={s.name}
                    type="button"
                    onClick={() => setNested({ kind: "skill", name: s.name })}
                    className="flex items-center gap-2 px-2.5 py-2 text-left font-data text-[length:var(--t-sm)] text-content-primary hover:bg-surface-overlay"
                    style={{ borderTop: i > 0 ? "1px solid var(--edge-hairline)" : undefined }}
                  >
                    <span className="min-w-0 shrink-0 font-medium">{s.name}</span>
                    {s.description && (
                      <span className="min-w-0 flex-1 truncate text-content-muted">
                        {s.description}
                      </span>
                    )}
                  </button>
                ))}
              </div>
            </div>
          )}

          {plugin.agents.length > 0 && (
            <div>
              <SectionLabel className="mb-1.5">{t("agentCount")}</SectionLabel>
              <div className="flex flex-col rounded border border-edge" style={{ borderRadius: 4 }}>
                {plugin.agents.map((a, i) => (
                  <button
                    key={a.name}
                    type="button"
                    onClick={() => setNested({ kind: "agent", name: a.name })}
                    className="flex items-center gap-2 px-2.5 py-2 text-left font-data text-[length:var(--t-sm)] text-content-primary hover:bg-surface-overlay"
                    style={{ borderTop: i > 0 ? "1px solid var(--edge-hairline)" : undefined }}
                  >
                    <span className="min-w-0 shrink-0 font-medium">{a.name}</span>
                    {a.description && (
                      <span className="min-w-0 flex-1 truncate text-content-muted">
                        {a.description}
                      </span>
                    )}
                  </button>
                ))}
              </div>
            </div>
          )}

          {mcpServerNames.length > 0 && (
            <div>
              <SectionLabel className="mb-1.5">{t("pluginMcpServers")}</SectionLabel>
              <div className="flex flex-wrap gap-1.5">
                {mcpServerNames.map((n) => (
                  <span
                    key={n}
                    className="rounded border border-edge bg-surface-overlay px-1.5 py-0.5 font-data text-[length:var(--t-xs)] text-content-secondary"
                  >
                    {n}
                  </span>
                ))}
              </div>
            </div>
          )}

          {plugin.hooks != null && (
            <details className="rounded border border-edge">
              <summary className="cursor-pointer px-3 py-2 text-[length:var(--t-xs)] font-medium uppercase tracking-[0.08em] text-content-muted">
                {t("pluginHooksSection")}
              </summary>
              <pre className="overflow-auto border-t border-edge bg-surface-base px-3 py-2 font-data text-[length:var(--t-xs)] text-content-secondary">
                {JSON.stringify(plugin.hooks, null, 2)}
              </pre>
            </details>
          )}

          {plugin.readme && (
            <details className="rounded border border-edge">
              <summary className="cursor-pointer px-3 py-2 text-[length:var(--t-xs)] font-medium uppercase tracking-[0.08em] text-content-muted">
                {t("pluginReadmeSection")}
              </summary>
              <div className="whitespace-pre-wrap border-t border-edge bg-surface-base px-3 py-2 font-data text-[length:var(--t-sm)] leading-relaxed text-content-secondary">
                {plugin.readme}
              </div>
            </details>
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
                    style={{ borderTop: i > 0 ? "1px solid var(--edge-hairline)" : undefined }}
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
        </div>
      </div>
    </div>
  );
}

// ── Nested agent view (read-only) ───────────────────────────────────────────
//
// A lightweight, plugin-scoped view — not the full agent editor (AgentDetail
// reads/writes through the standalone-agent definitions path, which a
// plugin-bundled agent isn't part of).

function PluginAgentDetailView({
  pluginName,
  agentName,
  onBack,
}: {
  pluginName: string;
  agentName: string;
  onBack: () => void;
}) {
  const t = useTranslations("library.drawer");
  const [agent, setAgent] = useState<PluginAgentDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- synchronous reset clears stale state before the async fetch resolves
    setLoading(true);
    setError(null);
    setAgent(null);

    getPluginAgent(pluginName, agentName)
      .then((a) => {
        if (alive) setAgent(a);
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
  }, [pluginName, agentName]);

  return (
    <div className="flex h-full flex-col overflow-hidden">
      <DrawerBackButton onClick={onBack}>{t("back")}</DrawerBackButton>
      {loading ? (
        <div className="flex h-full items-center justify-center text-meta text-content-muted">
          {t("loading")}
        </div>
      ) : error || !agent ? (
        <div className="p-4 text-meta text-status-failure">{error ?? t("notFound")}</div>
      ) : (
        <>
          <DrawerHeader
            name={agent.name}
            badge="agent"
            trailing={
              <span className="rounded border border-edge bg-surface-overlay px-1.5 py-0.5 text-[length:var(--t-xs)] uppercase tracking-[0.08em] text-content-muted">
                {t("readOnly")}
              </span>
            }
          />
          <div className="flex-1 overflow-auto p-4">
            {agent.description && (
              <p className="mb-3 text-[length:var(--t-sm)] text-content-secondary">
                {agent.description}
              </p>
            )}
            {agent.content.trim() ? (
              <pre className="whitespace-pre-wrap break-words font-data text-[length:var(--t-sm)] leading-relaxed text-content-secondary">
                {agent.content.trim()}
              </pre>
            ) : (
              <span className="italic text-content-muted">{t("noContent")}</span>
            )}
          </div>
        </>
      )}
    </div>
  );
}
