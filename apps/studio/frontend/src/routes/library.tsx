import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { useCallback, useEffect, useState, type ReactNode } from "react";
import { useTranslations } from "use-intl";
import { AgentDetail } from "@/components/library/AgentDetail";
import { CreateAgentPanel } from "@/components/library/CreateAgentPanel";
import { WorkflowDetail, CreateWorkflowPanel } from "@/components/library/WorkflowDetail";
import { PlaybookTemplateDetail } from "@/components/library/PlaybookTemplateDetail";
import { McpServerDetail, CreateMcpServerPanel } from "@/components/library/McpServerDetail";
import { SkillDetail } from "@/components/library/SkillDetail";
import { PluginDetail } from "@/components/library/PluginDetail";
import { KindBadge } from "@/components/library/KindBadge";
import SplitPane from "@/components/ui/SplitPane";
import TabBar from "@/components/shell/TabBar";
import EmptyState from "@/components/ui/EmptyState";
import Skeleton from "@/components/ui/Skeleton";
import Button from "@/components/ui/Button";
import DrawerBackButton from "@/components/ui/DrawerBackButton";
import DrawerHeader from "@/components/ui/DrawerHeader";
import type { LibraryKind } from "@/components/library/KindBadge";
import {
  listAgents,
  listWorkflowDefs,
  listSkills,
  listPlugins,
  listEngineDefs,
  listBuiltinPlaybooks,
  listPlaybooks,
  listMcpServers,
} from "@/lib/api";
import type { AgentProfileSummary } from "@/lib/types";
import type { CreatedWorkflowDef, EngineDef } from "@/lib/api";

// Kinds with no creation flow at all — the toolbar's "+ New" button and the
// empty-state's create CTA are both meaningless here (skills come from
// ~/.lionagi/skills/, plugins from the marketplace/installed-plugin cache;
// neither is created from this page).
const NO_CREATE_KINDS = new Set<LibraryKind>(["skill", "plugin"]);

const LIBRARY_TABS = [
  "all",
  "agent",
  "workflow",
  "playbook",
  "skill",
  "plugin",
  "engine",
  "mcp",
] as const;
type LibraryTab = (typeof LIBRARY_TABS)[number];

// Kinds whose surfaces are not finished yet. They stay in LIBRARY_TABS so an
// existing deep link still parses, but they get no tab and their items are
// withheld from every list, including "all". Drop a kind from this set to
// bring its surface back.
const UNFINISHED_KINDS = new Set<string>(["workflow", "engine"]);

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

// Sub-partitions of the "playbook" kind: read-only bundled templates vs. the
// user's own materialized copies. Distinct from LibraryKind (which stays a
// closed, shared union) so the KindBadge component doesn't need to know
// about this split.
type PlaybookSubKind = "builtin" | "custom";

interface LibraryItem {
  key: string;
  kind: LibraryKind;
  subKind?: PlaybookSubKind;
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
      listBuiltinPlaybooks(),
      listPlaybooks(),
      listWorkflowDefs(),
      listSkills(),
      listPlugins(),
      listEngineDefs(),
      listMcpServers(),
    ]).then(
      ([
        agentsRes,
        builtinsRes,
        playbooksRes,
        workflowsRes,
        skillsRes,
        pluginsRes,
        enginesRes,
        mcpRes,
      ]) => {
        if (!alive) return;

        const out: LibraryItem[] = [];
        const results = [
          agentsRes,
          builtinsRes,
          playbooksRes,
          workflowsRes,
          skillsRes,
          pluginsRes,
          enginesRes,
          mcpRes,
        ];

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
        // Built-in templates first, then the user's own playbooks — surfacing
        // the shipped templates the Workflows page was missing entirely
        // (DESIGN-BRIEF §3). These are agent+prompt templates, not graphs, so
        // they live under the "playbook" kind, separate from "workflow"
        // (DB-backed graph designs, edited inline via the Library drawer).
        if (builtinsRes.status === "fulfilled") {
          for (const p of builtinsRes.value.playbooks) {
            out.push({
              key: `playbook:builtin:${p.name}`,
              kind: "playbook",
              subKind: "builtin",
              name: p.name,
              description: p.description,
              meta: p.description,
            });
          }
        }
        if (playbooksRes.status === "fulfilled") {
          for (const p of playbooksRes.value.playbooks) {
            out.push({
              key: `playbook:custom:${p.name}`,
              kind: "playbook",
              subKind: "custom",
              name: p.name,
              description: p.description ?? undefined,
              meta: p.description ?? undefined,
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

        if (mcpRes.status === "fulfilled") {
          for (const s of mcpRes.value.servers) {
            out.push({
              key: `mcp:${s.name}`,
              kind: "mcp",
              name: s.name,
              description: s.command ?? s.url ?? undefined,
              meta: s.enabled ? s.transport : `${s.transport} · disabled`,
            });
          }
        }

        setItems(out);
        if (results.some((result) => result.status === "rejected")) {
          setError("degraded");
        }
        setLoading(false);
      },
    );

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

const PLAYBOOK_SUB_KINDS: PlaybookSubKind[] = ["builtin", "custom"];
const LIBRARY_KINDS: LibraryKind[] = [
  "agent",
  "workflow",
  "playbook",
  "skill",
  "plugin",
  "engine",
  "mcp",
];

/**
 * Parse a ?sel param into kind + name (+ subKind for "playbook" items, which
 * split two ways: builtin template / user's own installed copy).
 */
function parseSel(
  sel: string | undefined,
): { kind: LibraryKind; name: string; subKind?: PlaybookSubKind } | null {
  if (!sel) return null;
  const colon = sel.indexOf(":");
  if (colon === -1) return null;
  const kind = sel.slice(0, colon) as LibraryKind;
  const rest = sel.slice(colon + 1);
  if (!LIBRARY_KINDS.includes(kind) || !rest) return null;

  if (kind === "playbook") {
    const colon2 = rest.indexOf(":");
    if (colon2 !== -1) {
      const maybeSubKind = rest.slice(0, colon2);
      const name = rest.slice(colon2 + 1);
      if (PLAYBOOK_SUB_KINDS.includes(maybeSubKind as PlaybookSubKind) && name) {
        return { kind, name, subKind: maybeSubKind as PlaybookSubKind };
      }
    }
    return null;
  }

  if (kind === "workflow") {
    // Backward-compat: pre-split "workflow:<subKind>:<name>" links (old
    // bookmarks, the legacy /playbooks/$name redirect shims) predate the
    // playbook/workflow split, where "workflow" carried builtin/custom
    // playbook rows alongside graph designs under a subKind. Graph links
    // drop the subKind entirely now; builtin/custom links resolve to the
    // new "playbook" kind instead of dropping the bookmark.
    const colon2 = rest.indexOf(":");
    if (colon2 !== -1) {
      const legacySubKind = rest.slice(0, colon2);
      const name = rest.slice(colon2 + 1);
      if (legacySubKind === "graph" && name) {
        return { kind: "workflow", name };
      }
      if ((legacySubKind === "builtin" || legacySubKind === "custom") && name) {
        return { kind: "playbook", name, subKind: legacySubKind };
      }
    }
    return { kind, name: rest };
  }

  return { kind, name: rest };
}

function encodeSel(kind: LibraryKind, name: string, subKind?: PlaybookSubKind): string {
  if (kind === "playbook") {
    return `playbook:${subKind ?? "custom"}:${name}`;
  }
  return `${kind}:${name}`;
}

/** Placeholder row count while the first fetch is in flight. */
const CATALOG_SKELETON_ROWS = 8;

/** Shimmering row placeholders, sized to match a real catalog row — avoids the
 * layout jump of a centered "loading" line collapsing once rows land. */
function CatalogSkeleton() {
  return (
    <div aria-hidden="true" className="divide-y divide-edge">
      {Array.from({ length: CATALOG_SKELETON_ROWS }, (_, i) => (
        <div key={i} className="flex items-center gap-3 px-3 py-2.5">
          <Skeleton className="h-4 w-4 shrink-0 rounded-full" />
          <div className="flex min-w-0 flex-1 flex-col gap-1.5">
            <Skeleton className="h-3.5 w-40" />
            <Skeleton className="h-3 w-64 max-w-full" />
          </div>
        </div>
      ))}
    </div>
  );
}

function LibraryPage() {
  const t = useTranslations("library");
  const tDaemon = useTranslations("daemon");
  const { items, loading, error, reload, allAgents, allEngines } = useLibraryData();
  const navigate = useNavigate({ from: "/library" });
  const { tab, sel } = Route.useSearch();
  const kindFilter: LibraryKind | "all" = tab ?? "all";

  const [search, setSearch] = useState("");
  const [showCreate, setShowCreate] = useState(false);
  const [optimisticWorkflow, setOptimisticWorkflow] = useState<CreatedWorkflowDef | null>(null);

  // Collapsed split-pane: show detail when a selection exists or create is open.
  const [detailActive, setDetailActive] = useState(false);

  const ALL_KIND_TABS: Array<{ value: LibraryTab; label: string }> = [
    { value: "all", label: t("filterAll") },
    { value: "agent", label: t("filterAgent") },
    { value: "workflow", label: t("filterWorkflow") },
    { value: "playbook", label: t("filterPlaybook") },
    { value: "skill", label: t("filterSkill") },
    { value: "plugin", label: t("filterPlugin") },
    { value: "engine", label: t("filterEngine") },
    { value: "mcp", label: t("filterMcp") },
  ];
  const KIND_TABS = ALL_KIND_TABS.filter((tab) => !UNFINISHED_KINDS.has(tab.value));

  const filtered = items.filter((item) => {
    if (UNFINISHED_KINDS.has(item.kind)) return false;
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
      const isOptimisticWorkflow =
        parsed?.kind === "workflow" && optimisticWorkflow?.name === parsed.name;
      const isHiddenWorkflow =
        parsed?.kind === "workflow" &&
        items.some((item) => item.kind === "workflow" && item.name === parsed.name);
      if (
        isOptimisticWorkflow ||
        isHiddenWorkflow ||
        (parsed &&
          filtered.some(
            (i) =>
              i.kind === parsed.kind &&
              i.name === parsed.name &&
              (parsed.kind !== "playbook" || i.subKind === parsed.subKind),
          ))
      ) {
        return;
      }
    }

    // Otherwise select first row.
    const first = filtered[0];
    if (first) {
      void navigate({
        search: (prev) => ({ ...prev, sel: encodeSel(first.kind, first.name, first.subKind) }),
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
        search: (prev) => ({ ...prev, sel: encodeSel(item.kind, item.name, item.subKind) }),
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
      ? (items.find((i) => i.kind === "workflow" && i.name === parsed.name)?.meta ??
        (optimisticWorkflow?.name === parsed.name ? optimisticWorkflow.id : null))
      : null;

  const isEmpty = !loading && filtered.length === 0;
  const isFiltered = kindFilter !== "all" || search.trim().length > 0;
  // A skill/plugin tab with nothing on it and no active text search is
  // "nothing installed here", not "a search matched nothing" — it earns its
  // own empty-state copy pointing at where these are discovered from.
  const isEmptyLibraryTab =
    NO_CREATE_KINDS.has(kindFilter as LibraryKind) && search.trim().length === 0;

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
        {!NO_CREATE_KINDS.has(kindFilter as LibraryKind) && (
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
            +{" "}
            {kindFilter === "mcp"
              ? t("newMcpServer")
              : kindFilter === "agent"
                ? t("newAgent")
                : t("newWorkflow")}
          </Button>
        )}
      </div>

      {/* Error banner */}
      {error && (
        <div
          role="alert"
          className="flex shrink-0 items-center justify-between gap-3 border-b border-edge bg-status-error-bg px-3 py-2 text-body text-content-secondary"
        >
          <span>{t("loadError")}</span>
          <Button size="sm" variant="secondary" onClick={reload}>
            {tDaemon("retry")}
          </Button>
        </div>
      )}

      {/* Catalog */}
      <div className="flex-1 overflow-y-auto">
        {loading ? (
          <CatalogSkeleton />
        ) : isEmpty ? (
          <EmptyState
            glyph="▤"
            title={
              // An empty skill/plugin tab with no active text search is a
              // "nothing installed here" state, distinct from "a search
              // matched nothing" — it gets its own copy pointing at where
              // these are discovered from, not the generic filtered message.
              isEmptyLibraryTab
                ? kindFilter === "skill"
                  ? t("empty.allSkill")
                  : t("empty.allPlugin")
                : isFiltered
                  ? t("empty.filtered")
                  : t("empty.all")
            }
            body={
              isEmptyLibraryTab
                ? kindFilter === "skill"
                  ? t("empty.allSkillHint")
                  : t("empty.allPluginHint")
                : isFiltered
                  ? t("empty.filteredHint")
                  : t("empty.allHint")
            }
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
                <th className="w-8 px-3 py-2 font-medium" aria-label={t("drawer.kind")} />
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
                    onKeyDown={(event) => {
                      if (event.key === "Enter" || event.key === " ") {
                        event.preventDefault();
                        selectItem(item);
                      }
                    }}
                    tabIndex={0}
                    aria-selected={isSelected}
                    className="cursor-pointer border-b border-edge focus-visible:outline focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-accent"
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
                      {(item.description || item.meta) && (
                        <div
                          className="overflow-hidden font-data text-[length:var(--t-xs)] leading-snug text-content-muted"
                          title={item.description ?? item.meta}
                          style={{
                            display: "-webkit-box",
                            WebkitBoxOrient: "vertical",
                            WebkitLineClamp: 2,
                          }}
                        >
                          {item.description ?? item.meta}
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

  if (showCreate && kindFilter === "mcp") {
    detailPane = (
      <div className="flex h-full flex-col overflow-hidden">
        <CreateMcpServerPanel
          onCreated={(name) => {
            setShowCreate(false);
            void reload();
            void navigate({
              search: (prev) => ({ ...prev, sel: encodeSel("mcp", name) }),
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
  } else if (showCreate && kindFilter === "agent") {
    detailPane = (
      <CreateAgentPanel
        onCreated={(name) => {
          setShowCreate(false);
          void reload();
          void navigate({
            search: (prev) => ({ ...prev, sel: encodeSel("agent", name) }),
            replace: false,
          });
        }}
        onCancel={() => {
          setShowCreate(false);
          setDetailActive(false);
        }}
      />
    );
  } else if (showCreate) {
    detailPane = (
      <div className="flex h-full flex-col overflow-hidden">
        <CreateWorkflowPanel
          onCreated={(workflow) => {
            setOptimisticWorkflow(workflow);
            setShowCreate(false);
            void reload();
            void navigate({
              search: (prev) => ({ ...prev, sel: encodeSel("workflow", workflow.name) }),
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
  } else if (parsed?.kind === "mcp") {
    detailPane = (
      <McpServerDetail
        name={parsed.name}
        onBack={handleBack}
        onDeleted={() => {
          void reload();
          handleBack();
          void navigate({
            search: (prev) => {
              const next = { ...prev };
              delete next.sel;
              return next;
            },
            replace: false,
          });
        }}
      />
    );
  } else if (parsed?.kind === "agent" && selectedAgent) {
    detailPane = (
      <AgentDetail
        agent={selectedAgent}
        onBack={handleBack}
        onDeleted={() => {
          setDetailActive(false);
          void reload();
          void navigate({
            search: (prev) => {
              const next = { ...prev };
              delete next.sel;
              return next;
            },
            replace: false,
          });
        }}
      />
    );
  } else if (parsed?.kind === "workflow" && selectedWorkflowId) {
    detailPane = <WorkflowDetail id={selectedWorkflowId} onBack={handleBack} />;
  } else if (parsed?.kind === "playbook") {
    detailPane = (
      <PlaybookTemplateDetail
        name={parsed.name}
        isBuiltin={parsed.subKind === "builtin"}
        onBack={handleBack}
        onCloned={(clonedName) => {
          void reload();
          void navigate({
            search: (prev) => ({ ...prev, sel: encodeSel("playbook", clonedName, "custom") }),
            replace: false,
          });
        }}
      />
    );
  } else if (parsed?.kind === "skill") {
    detailPane = <SkillDetail name={parsed.name} onBack={handleBack} />;
  } else if (parsed?.kind === "plugin") {
    detailPane = <PluginDetail name={parsed.name} onBack={handleBack} />;
  } else if (parsed?.kind === "engine") {
    detailPane = (
      <EngineDetail name={parsed.name} def={selectedEngine ?? null} onBack={handleBack} />
    );
  } else {
    detailPane = (
      <EmptyState
        glyph="⌁"
        title={t("detailEmptyTitle")}
        body={t("detailEmptyBody")}
        className="h-full px-8 py-16"
      />
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
