import { createFileRoute, Link, useNavigate } from "@tanstack/react-router";
import { useCallback, useEffect, useState, type ReactNode } from "react";
import { useTranslations } from "use-intl";
import { AgentDetail } from "@/components/library/AgentDetail";
import { WorkflowDetail, CreateWorkflowPanel } from "@/components/library/WorkflowDetail";
import { KindBadge } from "@/components/library/KindBadge";
import SplitPane from "@/components/ui/SplitPane";
import TabBar from "@/components/shell/TabBar";
import { IconCheck, IconClose, IconDotFilled } from "@/components/ui/icons";
import SectionLabel from "@/components/ui/SectionLabel";
import Button from "@/components/ui/Button";
import EmptyState from "@/components/ui/EmptyState";
import DrawerBackButton from "@/components/ui/DrawerBackButton";
import DrawerHeader from "@/components/ui/DrawerHeader";
import type { LibraryKind } from "@/components/library/KindBadge";
import {
  listAgents,
  listWorkflowDefs,
  listSkills,
  listPlugins,
  listEngineDefs,
  listInvocations,
} from "@/lib/api";
import type { InvocationSummary } from "@/lib/api";
import type { AgentProfileSummary } from "@/lib/types";
import type { EngineDef } from "@/lib/api";

const LIBRARY_TABS = ["all", "agent", "workflow", "skill", "plugin", "engine"] as const;
type LibraryTab = (typeof LIBRARY_TABS)[number];

export const Route = createFileRoute("/library")({
  validateSearch: (search: Record<string, unknown>): { tab?: LibraryTab; sel?: string } => {
    const tab = search.tab;
    const sel = typeof search.sel === "string" ? search.sel : undefined;
    return {
      ...(LIBRARY_TABS.includes(tab as LibraryTab) ? { tab: tab as LibraryTab } : {}),
      ...(sel ? { sel } : {}),
    };
  },
  component: LibraryPage,
});

interface LibraryItem {
  key: string;
  kind: LibraryKind;
  name: string;
  description?: string;
  meta?: string;
}

function useLibraryData() {
  const [items, setItems] = useState<LibraryItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [allAgents, setAllAgents] = useState<AgentProfileSummary[]>([]);
  const [allEngines, setAllEngines] = useState<EngineDef[]>([]);

  const reload = useCallback(() => {
    let alive = true;
    setLoading(true);
    setError(null);

    Promise.allSettled([
      listAgents(),
      listWorkflowDefs(),
      listSkills(),
      listPlugins(),
      listEngineDefs(),
    ]).then(([agentsRes, workflowsRes, skillsRes, pluginsRes, enginesRes]) => {
      if (!alive) return;

      const out: LibraryItem[] = [];

      if (agentsRes.status === "fulfilled") {
        setAllAgents(agentsRes.value.agents);
        for (const a of agentsRes.value.agents) {
          out.push({
            key: `agent:${a.name}`,
            kind: "agent",
            name: a.name,
            description: a.description ?? undefined,
            meta: a.model ?? undefined,
          });
        }
      }
      if (workflowsRes.status === "fulfilled") {
        for (const w of workflowsRes.value) {
          out.push({
            key: `workflow:${w.id}`,
            kind: "workflow",
            name: w.name,
            description: w.description ?? undefined,
            meta: w.id,
          });
        }
      }
      if (skillsRes.status === "fulfilled") {
        for (const s of skillsRes.value.skills) {
          out.push({
            key: `skill:${s.name}`,
            kind: "skill",
            name: s.name,
            description: s.description ?? undefined,
          });
        }
      }
      if (pluginsRes.status === "fulfilled") {
        // The same plugin can be listed by several sources (marketplace +
        // installed cache); detail lookup is by name, so one row suffices.
        const seenPlugins = new Set<string>();
        for (const p of pluginsRes.value.plugins) {
          if (seenPlugins.has(p.name)) continue;
          seenPlugins.add(p.name);
          out.push({
            key: `plugin:${p.name}`,
            kind: "plugin",
            name: p.name,
            description: p.description ?? undefined,
            meta: `v${p.version}`,
          });
        }
      }
      if (enginesRes.status === "fulfilled") {
        setAllEngines(enginesRes.value);
        for (const e of enginesRes.value) {
          out.push({
            key: `engine:${e.id}`,
            kind: "engine",
            name: e.name,
            description: e.description ?? undefined,
            meta: e.kind,
          });
        }
      }

      setItems(out);
      setLoading(false);
    });

    return () => {
      alive = false;
    };
  }, []);

  useEffect(() => {
    // Return reload()'s cleanup so an unmount mid-flight flips its alive flag
    // and the resolved fetch can't setState on the stale component.
    // eslint-disable-next-line react-hooks/set-state-in-effect -- reload() calls setState inside async callbacks; synchronous reset is needed to clear stale items before the fetch resolves
    return reload();
  }, [reload]);

  return { items, loading, error, reload, allAgents, allEngines };
}

/** Parse a ?sel param into kind + name. */
function parseSel(sel: string | undefined): { kind: LibraryKind; name: string } | null {
  if (!sel) return null;
  const colon = sel.indexOf(":");
  if (colon === -1) return null;
  const kind = sel.slice(0, colon) as LibraryKind;
  const name = sel.slice(colon + 1);
  const valid: LibraryKind[] = ["agent", "workflow", "skill", "plugin", "engine"];
  if (!valid.includes(kind) || !name) return null;
  return { kind, name };
}

function encodeSel(kind: LibraryKind, name: string): string {
  return `${kind}:${name}`;
}

function LibraryPage() {
  const t = useTranslations("library");
  const { items, loading, error, reload, allAgents, allEngines } = useLibraryData();
  const navigate = useNavigate({ from: "/library" });
  const { tab, sel } = Route.useSearch();
  const kindFilter: LibraryKind | "all" = tab ?? "all";

  const [search, setSearch] = useState("");
  const [showCreate, setShowCreate] = useState(false);

  // Collapsed split-pane: show detail when a selection exists or create is open.
  const [detailActive, setDetailActive] = useState(false);

  const KIND_TABS: Array<{ value: LibraryTab; label: string }> = [
    { value: "all", label: t("filterAll") },
    { value: "agent", label: t("filterAgent") },
    { value: "workflow", label: t("filterWorkflow") },
    { value: "skill", label: t("filterSkill") },
    { value: "plugin", label: t("filterPlugin") },
    { value: "engine", label: t("filterEngine") },
  ];

  const filtered = items.filter((item) => {
    if (kindFilter !== "all" && item.kind !== kindFilter) return false;
    if (search.trim()) {
      const q = search.toLowerCase();
      if (
        !item.name.toLowerCase().includes(q) &&
        !(item.description ?? "").toLowerCase().includes(q)
      ) {
        return false;
      }
    }
    return true;
  });

  // Auto-select the first row whenever the tab or loaded items change (and no explicit sel).
  useEffect(() => {
    if (loading) return;

    // Keep any sel that resolves in the current filtered list — this is what
    // makes deep links into the Library work. A sel from another tab won't be
    // in `filtered`, so it falls through to select-first.
    if (sel) {
      const parsed = parseSel(sel);
      if (parsed && filtered.some((i) => i.kind === parsed.kind && i.name === parsed.name)) {
        return;
      }
    }

    // Otherwise select first row.
    const first = filtered[0];
    if (first) {
      void navigate({
        search: (prev) => ({ ...prev, sel: encodeSel(first.kind, first.name) }),
        replace: true,
      });
    } else {
      void navigate({
        search: (prev) => {
          const next = { ...prev };
          delete next.sel;
          return next;
        },
        replace: true,
      });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- intentionally runs only on tab/items change
  }, [tab, loading, items.length]);

  const selectItem = useCallback(
    (item: LibraryItem) => {
      setShowCreate(false);
      setDetailActive(true);
      void navigate({
        search: (prev) => ({ ...prev, sel: encodeSel(item.kind, item.name) }),
        replace: false,
      });
    },
    [navigate],
  );

  const parsed = parseSel(sel);

  // Resolve the agent/engine objects from the selection.
  const selectedAgent =
    parsed?.kind === "agent"
      ? (allAgents.find((a) => a.name === parsed.name) ?? {
          name: parsed.name,
          provider: "",
          model: "",
        })
      : null;

  const selectedEngine =
    parsed?.kind === "engine" ? allEngines.find((e) => e.name === parsed?.name) : null;

  const selectedWorkflowId =
    parsed?.kind === "workflow"
      ? (items.find((i) => i.kind === "workflow" && i.name === parsed.name)?.meta ?? parsed.name)
      : null;

  const isEmpty = !loading && filtered.length === 0;
  const isFiltered = kindFilter !== "all" || search.trim().length > 0;

  const detailPaneActive = detailActive || showCreate || !!parsed;

  // ── Master pane ────────────────────────────────────────────────────────────

  const masterPane = (
    <div className="flex h-full flex-col bg-surface-raised">
      {/* Toolbar */}
      <div className="flex shrink-0 items-center gap-3 border-b border-edge px-3 py-2">
        <input
          type="text"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder={t("searchPlaceholder")}
          className="min-w-0 flex-1 rounded border border-edge bg-surface-overlay px-2 py-1 font-ui text-[length:var(--t-sm)] text-content-primary focus:outline-none"
        />
        <Button
          size="sm"
          variant="primary"
          onClick={() => {
            setShowCreate(true);
            setDetailActive(true);
            void navigate({
              search: (prev) => {
                const next = { ...prev };
                delete next.sel;
                return next;
              },
              replace: false,
            });
          }}
        >
          + {t("newWorkflow")}
        </Button>
      </div>

      {/* Error banner */}
      {error && (
        <div className="shrink-0 border-b border-edge px-3 py-1.5 text-[length:var(--t-xs)] text-status-failure">
          {error}
        </div>
      )}

      {/* Catalog */}
      <div className="flex-1 overflow-y-auto">
        {loading ? (
          <div className="flex items-center justify-center py-12 text-[length:var(--t-sm)] text-content-muted">
            {t("loading")}
          </div>
        ) : isEmpty ? (
          <EmptyState
            glyph="▤"
            title={isFiltered ? t("empty.filtered") : t("empty.all")}
            body={isFiltered ? t("empty.filteredHint") : t("empty.allHint")}
            action={
              !isFiltered ? (
                <Button
                  size="sm"
                  variant="primary"
                  onClick={() => {
                    setShowCreate(true);
                    setDetailActive(true);
                  }}
                >
                  + {t("empty.createWorkflow")}
                </Button>
              ) : undefined
            }
            className="px-6 py-16"
          />
        ) : (
          <table className="w-full text-left" style={{ borderCollapse: "collapse" }}>
            <thead>
              <tr
                className="text-[length:var(--t-xs)] uppercase tracking-[0.08em] text-content-muted border-b border-edge bg-surface-raised"
                style={{ position: "sticky", top: 0, zIndex: 1 }}
              >
                <th className="w-8 px-3 py-2 font-medium" aria-label="Kind" />
                <th className="px-2 py-2 font-medium">{t("table.name")}</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((item) => {
                const isSelected =
                  !showCreate && parsed?.kind === item.kind && parsed.name === item.name;
                return (
                  <tr
                    key={item.key}
                    onClick={() => selectItem(item)}
                    aria-selected={isSelected}
                    className="cursor-pointer border-b border-edge"
                    style={{
                      background: isSelected ? "var(--surface-overlay)" : "transparent",
                    }}
                    onMouseEnter={(e) => {
                      if (!isSelected)
                        (e.currentTarget as HTMLTableRowElement).style.background =
                          "color-mix(in srgb, var(--surface-overlay) 60%, transparent)";
                    }}
                    onMouseLeave={(e) => {
                      if (!isSelected)
                        (e.currentTarget as HTMLTableRowElement).style.background = "transparent";
                    }}
                  >
                    <td className="px-3 py-2.5">
                      <KindBadge kind={item.kind} />
                    </td>
                    <td className="px-2 py-2.5">
                      <div className="font-data text-[length:var(--t-base)] font-medium leading-snug text-content-primary">
                        {item.name}
                      </div>
                      {item.meta && (
                        <div className="font-data text-[length:var(--t-xs)] leading-snug text-content-muted">
                          {item.meta}
                        </div>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );

  // ── Detail pane ────────────────────────────────────────────────────────────

  function handleBack() {
    setDetailActive(false);
    setShowCreate(false);
  }

  let detailPane: ReactNode;

  if (showCreate) {
    detailPane = (
      <div className="flex h-full flex-col overflow-hidden">
        <CreateWorkflowPanel
          onCreated={(name) => {
            setShowCreate(false);
            void reload();
            void navigate({
              search: (prev) => ({ ...prev, sel: encodeSel("workflow", name) }),
              replace: false,
            });
          }}
          onCancel={() => {
            setShowCreate(false);
            setDetailActive(false);
          }}
        />
      </div>
    );
  } else if (parsed?.kind === "agent" && selectedAgent) {
    detailPane = <AgentDetail agent={selectedAgent} onBack={handleBack} />;
  } else if (parsed?.kind === "workflow" && selectedWorkflowId) {
    detailPane = <WorkflowDetail id={selectedWorkflowId} onBack={handleBack} />;
  } else if (parsed?.kind === "skill" || parsed?.kind === "plugin") {
    const item = filtered.find((i) => i.kind === parsed.kind && i.name === parsed.name);
    detailPane = (
      <SimpleDetail
        kind={parsed.kind}
        name={parsed.name}
        description={item?.description}
        onBack={handleBack}
      />
    );
  } else if (parsed?.kind === "engine") {
    detailPane = (
      <EngineDetail name={parsed.name} def={selectedEngine ?? null} onBack={handleBack} />
    );
  } else {
    detailPane = (
      <div className="flex h-full items-center justify-center text-[length:var(--t-sm)] text-content-muted">
        {t("loading")}
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col bg-surface-base">
      {/* Kind tabs */}
      <div className="shrink-0 px-4 pt-3">
        <TabBar
          ariaLabel={t("tabsAria")}
          tabs={KIND_TABS.map(({ value, label }) => ({
            id: value,
            label,
            to: "/library",
            search: value === "all" ? {} : { tab: value },
            active: kindFilter === value,
          }))}
        />
      </div>

      {/* Split body */}
      <div className="min-h-0 flex-1">
        <SplitPane
          id="library"
          master={masterPane}
          detail={detailPane}
          defaultMasterWidth={420}
          minMasterWidth={280}
          maxMasterWidth={560}
          detailActive={detailPaneActive}
          ariaLabelMaster={t("masterAria")}
          ariaLabelDetail={t("detailAria")}
        />
      </div>
    </div>
  );
}

// ── Simple detail (skill / plugin) ─────────────────────────────────────────

interface InvocationStats {
  total: number;
  successRate: number | null;
  lastUsedSec: number | null;
  recent: InvocationSummary[];
}

/**
 * Fetch invocations for a skill or plugin and compute stats client-side.
 * The backend filters by skill but not by plugin, so plugin stats scan the
 * most recent 200 invocations and filter locally.
 */
function useInvocationStats(
  kind: "skill" | "plugin",
  name: string,
): {
  stats: InvocationStats | null;
  loading: boolean;
} {
  const [stats, setStats] = useState<InvocationStats | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- reset stale state before async fetch; setState fires synchronously in effect body only, callbacks are guarded by alive flag
    setStats(null);
    setLoading(true);

    const fetchParams =
      kind === "skill"
        ? listInvocations({ skill: name, limit: 200 })
        : // No plugin filter on the server — fetch all and filter client-side
          listInvocations({ limit: 200 });

    fetchParams
      .then((res) => {
        if (!alive) return;
        const rows =
          kind === "plugin"
            ? res.invocations.filter((inv) => inv.plugin === name)
            : res.invocations;

        const total = rows.length;
        const success = rows.filter((inv) => inv.status === "completed").length;
        const successRate = total > 0 ? Math.round((success / total) * 100) : null;
        const lastUsedSec =
          rows.length > 0 ? Math.max(...rows.map((inv) => inv.ended_at ?? inv.started_at)) : null;
        const recent = rows.slice(0, 5);
        setStats({ total, successRate, lastUsedSec, recent });
        setLoading(false);
      })
      .catch(() => {
        if (!alive) return;
        setStats(null);
        setLoading(false);
      });

    return () => {
      alive = false;
    };
  }, [kind, name]);

  return { stats, loading };
}

function formatAge(epochSec: number): string {
  const diffSec = Math.max(0, Math.floor(Date.now() / 1000) - epochSec);
  if (diffSec < 60) return `${diffSec}s`;
  if (diffSec < 3600) return `${Math.floor(diffSec / 60)}m`;
  if (diffSec < 86400) return `${Math.floor(diffSec / 3600)}h`;
  return `${Math.floor(diffSec / 86400)}d`;
}

interface SimpleDetailProps {
  kind: LibraryKind;
  name: string;
  description?: string;
  onBack?: () => void;
}

function SimpleDetail({ kind, name, description, onBack }: SimpleDetailProps) {
  const t = useTranslations("library.drawer");
  const isStatKind = kind === "skill" || kind === "plugin";
  const { stats, loading: statsLoading } = useInvocationStats(isStatKind ? kind : "skill", name);

  return (
    <div className="flex h-full flex-col overflow-hidden">
      {onBack && <DrawerBackButton onClick={onBack}>{t("back")}</DrawerBackButton>}
      <DrawerHeader name={name} badge={kind} />
      <div className="flex-1 overflow-auto p-4">
        <p className="text-[length:var(--t-sm)] text-content-secondary">
          {description ?? <span className="italic text-content-muted">{t("noDescription")}</span>}
        </p>

        {/* Invocation stats — skill and plugin only */}
        {isStatKind && (
          <div className="mt-4 flex flex-col gap-3">
            {/* Stats strip */}
            <div className="grid grid-cols-3 gap-px overflow-hidden rounded border border-edge bg-edge">
              {[
                {
                  label: t("invocations"),
                  value: statsLoading ? "—" : String(stats?.total ?? 0),
                },
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
                      ? formatAge(stats.lastUsedSec)
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
                <div
                  className="flex flex-col rounded border border-edge"
                  style={{ borderRadius: 4 }}
                >
                  {stats.recent.map((inv, i) => (
                    <Link
                      key={inv.id}
                      to="/history"
                      search={{ tab: "run" }}
                      className="flex items-center gap-2 px-2.5 py-2 font-data text-[length:var(--t-sm)] text-content-primary hover:underline"
                      style={{
                        borderTop: i > 0 ? "1px solid var(--edge-hairline)" : undefined,
                      }}
                    >
                      <span
                        className="flex shrink-0 items-center"
                        style={{
                          color:
                            inv.status === "completed"
                              ? "var(--status-success)"
                              : inv.status === "failed"
                                ? "var(--status-failure)"
                                : "var(--content-muted)",
                        }}
                      >
                        {inv.status === "completed" ? (
                          <IconCheck size={10} strokeWidth={2.5} />
                        ) : inv.status === "failed" ? (
                          <IconClose size={10} strokeWidth={2.5} />
                        ) : (
                          <IconDotFilled size={5} />
                        )}
                      </span>
                      <span className="min-w-0 flex-1 truncate">{inv.skill}</span>
                      <span className="shrink-0 tabular-nums text-[length:var(--t-xs)] text-content-muted">
                        {formatAge(inv.ended_at ?? inv.started_at)}
                      </span>
                    </Link>
                  ))}
                </div>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// ── Engine detail ──────────────────────────────────────────────────────────

interface EngineDetailProps {
  name: string;
  def: EngineDef | null;
  onBack?: () => void;
}

function EngineDetail({ name, def, onBack }: EngineDetailProps) {
  const t = useTranslations("library.drawer");
  return (
    <div className="flex h-full flex-col overflow-hidden">
      {onBack && <DrawerBackButton onClick={onBack}>{t("back")}</DrawerBackButton>}
      <DrawerHeader name={name} badge={def?.kind} />
      <div className="flex-1 overflow-auto p-4">
        {def ? (
          <div className="flex flex-col gap-4">
            <div className="flex flex-wrap gap-x-5 gap-y-2 text-[length:var(--t-xs)]">
              {[
                { label: t("engineKind"), value: def.kind },
                def.model ? { label: t("engineModel"), value: def.model } : null,
                def.max_depth ? { label: t("engineMaxDepth"), value: String(def.max_depth) } : null,
                def.max_agents
                  ? { label: t("engineMaxAgents"), value: String(def.max_agents) }
                  : null,
              ]
                .filter(Boolean)
                .map(
                  (c) =>
                    c && (
                      <div key={c.label} className="flex items-center gap-1.5">
                        <span className="text-content-muted">{c.label}</span>
                        <span className="font-data text-content-primary">{c.value}</span>
                      </div>
                    ),
                )}
            </div>
            {def.description && (
              <p className="text-[length:var(--t-sm)] text-content-secondary">{def.description}</p>
            )}
          </div>
        ) : (
          <p className="italic text-[length:var(--t-sm)] text-content-muted">{t("notFound")}</p>
        )}
      </div>
    </div>
  );
}
