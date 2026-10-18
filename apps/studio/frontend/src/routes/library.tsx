import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { useTranslations } from "use-intl";
import { AgentDetail } from "@/components/library/AgentDetail";
import { CreateAgentPanel } from "@/components/library/CreateAgentPanel";
import { WorkflowDetail, CreateWorkflowPanel } from "@/components/library/WorkflowDetail";
import { PlaybookTemplateDetail } from "@/components/library/PlaybookTemplateDetail";
import { McpServerDetail, CreateMcpServerPanel } from "@/components/library/McpServerDetail";
import { SkillDetail } from "@/components/library/SkillDetail";
import { PluginDetail } from "@/components/library/PluginDetail";
import { KindBadge } from "@/components/library/KindBadge";
import HooksView from "@/components/library/HooksView";
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
  "hooks",
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

type KindLoadStatus = "loaded" | "failed";

// One list endpoint each. A tab's catalog is assembled from the sources its
// kind needs; e.g. the Playbook tab reads both bundled templates and the
// user's own playbooks.
type CatalogSource =
  | "agents"
  | "builtinPlaybooks"
  | "playbooks"
  | "workflows"
  | "skills"
  | "plugins"
  | "engines"
  | "mcpServers";

const KIND_SOURCES: Record<LibraryKind, CatalogSource[]> = {
  agent: ["agents"],
  playbook: ["builtinPlaybooks", "playbooks"],
  workflow: ["workflows"],
  skill: ["skills"],
  plugin: ["plugins"],
  engine: ["engines"],
  mcp: ["mcpServers"],
};

// Rows one source contributes, plus the detail lookups a routed pane reads
// (agent profiles, engine defs).
interface SourceData {
  items: LibraryItem[];
  agents?: AgentProfileSummary[];
  engines?: EngineDef[];
}

// A source's settled result, held for the page's life so revisiting a tab
// renders what it already fetched instead of requesting it again. "failed"
// records a rejected request so a retry can target just that source.
type SourceCache = Partial<Record<CatalogSource, SourceData | "failed">>;

// A create, delete, or clone names the kind it changed. Dropping that kind's
// sources from the cache makes every tab that lists them fetch them again —
// including a tab the completion did not resolve on.
interface CatalogInvalidation {
  token: number;
  kinds: LibraryKind[];
}

const SOURCE_REQUESTS: Record<CatalogSource, () => Promise<SourceData>> = {
  agents: async () => {
    const value = await listAgents();
    return {
      agents: value.agents,
      items: value.agents.map((a) => ({
        key: `agent:${a.name}`,
        kind: "agent",
        name: a.name,
        description: a.description ?? undefined,
        meta: a.model ?? undefined,
      })),
    };
  },
  // Built-in templates first, then the user's own playbooks — surfacing the
  // shipped templates the Workflows page was missing entirely
  // (DESIGN-BRIEF §3). These are agent+prompt templates, not graphs, so they
  // live under the "playbook" kind, separate from "workflow" (DB-backed graph
  // designs, edited inline via the Library drawer).
  builtinPlaybooks: async () => {
    const value = await listBuiltinPlaybooks();
    return {
      items: value.playbooks.map((p) => ({
        key: `playbook:builtin:${p.name}`,
        kind: "playbook",
        subKind: "builtin",
        name: p.name,
        description: p.description,
        meta: p.description,
      })),
    };
  },
  playbooks: async () => {
    const value = await listPlaybooks();
    return {
      items: value.playbooks.map((p) => ({
        key: `playbook:custom:${p.name}`,
        kind: "playbook",
        subKind: "custom",
        name: p.name,
        description: p.description ?? undefined,
        meta: p.description ?? undefined,
      })),
    };
  },
  workflows: async () => {
    const value = await listWorkflowDefs();
    return {
      items: value.map((w) => ({
        key: `workflow:${w.id}`,
        kind: "workflow",
        name: w.name,
        description: w.description ?? undefined,
        meta: w.id,
      })),
    };
  },
  skills: async () => {
    const value = await listSkills();
    return {
      items: value.skills.map((s) => ({
        key: `skill:${s.name}`,
        kind: "skill",
        name: s.name,
        description: s.description ?? undefined,
      })),
    };
  },
  plugins: async () => {
    const value = await listPlugins();
    // The same plugin can be listed by several sources (marketplace +
    // installed cache); detail lookup is by name, so one row suffices.
    const seenPlugins = new Set<string>();
    const items: LibraryItem[] = [];
    for (const p of value.plugins) {
      if (seenPlugins.has(p.name)) continue;
      seenPlugins.add(p.name);
      items.push({
        key: `plugin:${p.name}`,
        kind: "plugin",
        name: p.name,
        description: p.description ?? undefined,
        meta: `v${p.version}`,
      });
    }
    return { items };
  },
  engines: async () => {
    const value = await listEngineDefs();
    return {
      engines: value,
      items: value.map((e) => ({
        key: `engine:${e.id}`,
        kind: "engine",
        name: e.name,
        description: e.description ?? undefined,
        meta: e.kind,
      })),
    };
  },
  mcpServers: async () => {
    const value = await listMcpServers();
    return {
      items: value.servers.map((s) => ({
        key: `mcp:${s.name}`,
        kind: "mcp",
        name: s.name,
        description: s.command ?? s.url ?? undefined,
        meta: s.enabled ? s.transport : `${s.transport} · disabled`,
      })),
    };
  },
};

function useLibraryData(
  tab: LibraryTab | undefined,
  refresh: {
    reloadToken: number;
    retryToken: number;
    invalidation: CatalogInvalidation;
  },
  hidden = false,
) {
  const { reloadToken, retryToken, invalidation } = refresh;
  const [cache, setCache] = useState<SourceCache>({});
  const requestVersion = useRef<Partial<Record<CatalogSource, number>>>({});
  const prevReloadToken = useRef(reloadToken);
  const prevRetryToken = useRef(retryToken);
  const prevInvalidationToken = useRef(invalidation.token);

  const wants = (kind: LibraryKind) =>
    (tab === kind && (hidden || !UNFINISHED_KINDS.has(kind))) ||
    (tab === "all" && !UNFINISHED_KINDS.has(kind));
  const wantedKinds = LIBRARY_KINDS.filter(wants);
  const wantedSources = Array.from(new Set(wantedKinds.flatMap((kind) => KIND_SOURCES[kind])));
  // The effect runs on the scope and refresh tokens; wantedKinds is derived
  // from the scope, so a stable key stands in for the wanted set.
  const scope = `${tab ?? ""}:${hidden}`;

  useEffect(() => {
    const reload = reloadToken !== prevReloadToken.current;
    const retry = !reload && retryToken !== prevRetryToken.current;
    const invalidated = invalidation.token !== prevInvalidationToken.current;
    prevReloadToken.current = reloadToken;
    prevRetryToken.current = retryToken;
    prevInvalidationToken.current = invalidation.token;

    // A create, delete, or clone drops the completed kind's sources here, so a
    // tab that lists them fetches them again on its next visit even when the
    // completion resolved on another tab. Sources the same refresh already
    // re-requests are left in place, so the list does not blink empty under a
    // viewer who is still on the completed tab.
    if (invalidated) {
      const dropped = new Set(invalidation.kinds.flatMap((kind) => KIND_SOURCES[kind]));
      const refetched = reload ? new Set(wantedSources) : new Set<CatalogSource>();
      setCache((prev) => {
        let changed = false;
        const next = { ...prev };
        for (const source of dropped) {
          if (refetched.has(source)) continue;
          if (source in next) {
            delete next[source];
            changed = true;
          }
        }
        return changed ? next : prev;
      });
      // A dropped source may still have a request in flight from before the
      // completion. Bump its version so a late response for it cannot refill
      // the cache the drop just cleared.
      for (const source of dropped) {
        if (refetched.has(source)) continue;
        requestVersion.current[source] = (requestVersion.current[source] ?? 0) + 1;
      }
    }

    const sources = wantedSources.filter((source) => {
      const cached = cache[source];
      if (reload) return true;
      if (retry) return cached === "failed";
      // A failed source must be attempted again on a revisit; holding it
      // would leave the tab permanently stuck on its error state.
      return cached === undefined || cached === "failed";
    });
    if (sources.length === 0) return;

    for (const source of sources) {
      // A source being retried is no longer in its failed state: clear the
      // recorded failure so the tab shows its loading state instead of the
      // stale error banner until the response arrives.
      if (cache[source] === "failed") {
        setCache((prev) => {
          if (prev[source] !== "failed") return prev;
          const next = { ...prev };
          delete next[source];
          return next;
        });
      }
      const version = (requestVersion.current[source] ?? 0) + 1;
      requestVersion.current[source] = version;
      SOURCE_REQUESTS[source]().then(
        (data) => {
          if (requestVersion.current[source] !== version) return;
          setCache((prev) => ({ ...prev, [source]: data }));
        },
        () => {
          if (requestVersion.current[source] !== version) return;
          setCache((prev) => ({ ...prev, [source]: "failed" }));
        },
      );
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- wantedSources derives from tab/hidden; cache is read only to pick what to request
  }, [scope, reloadToken, retryToken, invalidation.token, hidden]);

  const items = wantedKinds.flatMap((kind) =>
    KIND_SOURCES[kind].flatMap((source) => {
      const entry = cache[source];
      return entry && entry !== "failed" ? entry.items : [];
    }),
  );

  const kindStatus: Partial<Record<LibraryKind, KindLoadStatus>> = {};
  for (const kind of wantedKinds) {
    const sources = KIND_SOURCES[kind];
    if (sources.some((source) => cache[source] === "failed")) kindStatus[kind] = "failed";
    else if (sources.every((source) => cache[source] !== undefined)) kindStatus[kind] = "loaded";
  }

  const agentsEntry = wants("agent") ? cache["agents"] : undefined;
  const enginesEntry = wants("engine") ? cache["engines"] : undefined;
  return {
    items,
    loading: wantedSources.some((source) => cache[source] === undefined),
    error: wantedKinds.some((kind) => kindStatus[kind] === "failed") ? "degraded" : null,
    allAgents: agentsEntry && agentsEntry !== "failed" ? (agentsEntry.agents ?? []) : [],
    allEngines: enginesEntry && enginesEntry !== "failed" ? (enginesEntry.engines ?? []) : [],
    kindStatus,
  };
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
  const navigate = useNavigate({ from: "/library" });
  const { tab, sel } = Route.useSearch();
  const kindFilter: LibraryTab = tab ?? "all";
  // A create, clone, or delete completion can outlive the tab where it began.
  // What it writes closes or reshapes the detail pane, so those writes are
  // scoped to that tab the same way its selection write already is: a
  // completion that resolves after the user moved on must not touch the new
  // tab's pane.
  const activeTab = useRef(kindFilter);
  useEffect(() => {
    activeTab.current = kindFilter;
  }, [kindFilter]);
  const completionOnTab = (captured: LibraryTab) => activeTab.current === captured;
  const parsedSel = parseSel(sel);
  const selectedHiddenKind =
    kindFilter !== "hooks" && parsedSel && UNFINISHED_KINDS.has(parsedSel.kind)
      ? parsedSel.kind
      : undefined;
  const mounted = useRef(false);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  const [reloadToken, setReloadToken] = useState(0);
  const [visibleRetryToken, setVisibleRetryToken] = useState(0);
  const [hiddenRetryToken, setHiddenRetryToken] = useState(0);
  // A create, delete, or clone names the kind it changed. Recording that kind
  // here lets every tab that lists it fetch it again on its next visit, even
  // when the completion resolved after the viewer moved to another tab.
  const [invalidation, setInvalidation] = useState<CatalogInvalidation>({ token: 0, kinds: [] });
  // Completion callbacks can outlive the tab where they were created. Refresh
  // the current effects instead of issuing requests for a captured tab.
  const reload = useCallback(() => setReloadToken((token) => token + 1), []);
  const invalidate = useCallback(
    (kind: LibraryKind) => setInvalidation((prev) => ({ token: prev.token + 1, kinds: [kind] })),
    [],
  );
  const retryVisible = useCallback(() => setVisibleRetryToken((token) => token + 1), []);
  const retryHidden = useCallback(() => setHiddenRetryToken((token) => token + 1), []);
  const visible = useLibraryData(kindFilter, {
    reloadToken,
    retryToken: visibleRetryToken,
    invalidation,
  });
  // Detail navigation must not reload or hide the visible catalog.
  const hidden = useLibraryData(
    selectedHiddenKind,
    { reloadToken, retryToken: hiddenRetryToken, invalidation },
    true,
  );
  const items = [...visible.items, ...hidden.items];
  const { loading, allAgents } = visible;
  const { allEngines } = hidden;
  const error = visible.error;
  const kindStatus = { ...visible.kindStatus };
  if (selectedHiddenKind) kindStatus[selectedHiddenKind] = hidden.kindStatus[selectedHiddenKind];
  const selectionLoading = !!selectedHiddenKind && hidden.loading;

  const [search, setSearch] = useState("");
  const [showCreate, setShowCreate] = useState(false);
  // Collapsed split-pane: show detail when a selection exists or create is
  // open. The row the catalog picks on its own is not a selection: it fills
  // the detail pane beside the list but must not replace the catalog in the
  // collapsed layout, so only a deep link, a row click, or the create form
  // sets this.
  const [detailActive, setDetailActive] = useState(false);
  // Set while the create form's own navigation drops the row the URL names,
  // so that drop is not read as a tab link.
  const openingCreate = useRef(false);
  // A create form belongs to the tab that opened it, and the detail pane's
  // collapsed state belongs to the selection; switching tabs closes the form
  // and returns to the list rather than leaving the previous tab's pane up.
  // The All tab has two addresses — `?tab=all` and no `tab` at all — and they
  // are the same tab. A tab picked while the page already shows it closes the
  // form too: its link drops the selection, and the All tab's also drops `tab`.
  const previousTab = useRef({ tab, sel });
  useEffect(() => {
    const prior = previousTab.current;
    previousTab.current = { tab, sel };
    const kindChanged = (prior.tab ?? "all") !== kindFilter;
    const tabChanged = prior.tab !== tab;
    const selDropped = prior.sel !== undefined && sel === undefined;
    const pickingTab = kindChanged || tabChanged || (selDropped && !openingCreate.current);
    if (!pickingTab) return;
    setShowCreate(false);
    setDetailActive(false);
  }, [kindFilter, tab, sel]);
  // A URL that stops naming a selection also returns the collapsed layout to
  // the list, so Back to the catalog does not leave its last row's detail up.
  // A selection that appears while a create form is open is a request for that
  // row — browser Back onto it, or a link — so the form closes and the row's
  // detail takes the pane, which it opens on its own.
  const previousSel = useRef(sel);
  // Opening the create form drops the row the URL names, so the form's pane
  // stays up rather than a selection returning behind it.
  const openCreate = () => {
    openingCreate.current = !!sel;
    setShowCreate(true);
    setDetailActive(true);
    void navigate({
      search: (prev) => {
        if (prev.sel === undefined) return prev;
        const next = { ...prev };
        delete next.sel;
        return next;
      },
      replace: false,
    });
  };
  useEffect(() => {
    const dropped = !!previousSel.current && !sel;
    const selected = !previousSel.current && !!sel;
    previousSel.current = sel;
    const keptOpen = openingCreate.current || showCreate;
    openingCreate.current = false;
    if (selected) setShowCreate(false);
    if (dropped && !keptOpen) setDetailActive(false);
  }, [sel, showCreate]);
  const [optimisticWorkflow, setOptimisticWorkflow] = useState<CreatedWorkflowDef | null>(null);

  const ALL_KIND_TABS: Array<{ value: LibraryTab; label: string }> = [
    { value: "all", label: t("filterAll") },
    { value: "agent", label: t("filterAgent") },
    { value: "workflow", label: t("filterWorkflow") },
    { value: "playbook", label: t("filterPlaybook") },
    { value: "skill", label: t("filterSkill") },
    { value: "plugin", label: t("filterPlugin") },
    { value: "engine", label: t("filterEngine") },
    { value: "mcp", label: t("filterMcp") },
    { value: "hooks", label: t("filterHooks") },
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

  const scopedItems = items.filter(
    (item) =>
      !UNFINISHED_KINDS.has(item.kind) && (kindFilter === "all" || item.kind === kindFilter),
  );

  // The row the detail pane shows when the URL names no selection: the first
  // row of the tab, or the first search match.
  const fallbackItem =
    kindFilter === "hooks" || loading || selectionLoading ? null : (filtered[0] ?? null);

  // A workflow's detail loads by id, not by name, so a selection is only a
  // row while the catalog (or the create that just made it) still has it.
  const selectedWorkflowId =
    parsedSel?.kind === "workflow"
      ? (items.find((i) => i.kind === "workflow" && i.name === parsedSel.name)?.meta ??
        (optimisticWorkflow?.name === parsedSel.name ? optimisticWorkflow.id : null))
      : null;

  // A selection belongs to the tab that shows it. A selection whose catalog
  // can still tell whether the row exists — a hidden workflow or engine being
  // fetched, or a source that failed — is kept: those details render their own
  // loading, error, or not-found state with a Back control. Any other
  // selection that names no row the tab lists is no selection, so the tab
  // shows its first row as it does without one instead of opening a pane that
  // can only hide the catalog. The text search does not narrow this: a row the
  // search hides still has its detail.
  const urlSelection =
    parsedSel &&
    (selectedHiddenKind
      ? hidden.loading ||
        hidden.kindStatus[selectedHiddenKind] === "failed" ||
        kindStatus[parsedSel.kind] === "failed" ||
        !!selectedWorkflowId ||
        // The engine detail renders a not-found state with its Back control.
        parsedSel.kind === "engine"
      : scopedItems.some(
          (item) =>
            item.kind === parsedSel.kind &&
            item.name === parsedSel.name &&
            (parsedSel.kind !== "playbook" || item.subKind === parsedSel.subKind),
        ) || kindStatus[parsedSel.kind] === "failed")
      ? parsedSel
      : null;

  // An auto-selected row fills the detail pane beside the list when the URL
  // names no selection, but it is not a detail request: the URL is not
  // rewritten and `detailPaneActive` does not follow it.
  const fallbackParsed =
    !urlSelection && fallbackItem
      ? parseSel(encodeSel(fallbackItem.kind, fallbackItem.name, fallbackItem.subKind))
      : null;

  const selectItem = useCallback(
    (item: LibraryItem) => {
      setShowCreate(false);
      setDetailActive(true);
      void navigate({
        search: (prev) => ({ ...prev, sel: encodeSel(item.kind, item.name, item.subKind) }),
        replace: false,
      });
    },
    [navigate, setShowCreate, setDetailActive],
  );

  // The detail pane shows the selection the URL names on this tab, and the
  // auto-selected row when it names none.
  const parsed = urlSelection ?? fallbackParsed;

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

  const isEmpty = !loading && filtered.length === 0;
  // A search is only responsible for an empty result when clearing it would
  // actually reveal rows. An empty kind tab is a genuinely empty collection,
  // even when other Library kinds contain items.
  const isFiltered = search.trim().length > 0 && scopedItems.length > 0;
  // A skill/plugin tab with nothing on it and no active text search is
  // "nothing installed here", not "a search matched nothing" — it earns its
  // own empty-state copy pointing at where these are discovered from.
  const isEmptyLibraryTab = NO_CREATE_KINDS.has(kindFilter as LibraryKind) && !isFiltered;
  // A single-kind tab whose backing source(s) failed to load can't assert
  // its list is actually empty — an empty result there is "unknown", not
  // "zero items", so it must not offer the create CTA that a genuine empty
  // collection gets. The "all" tab has the same problem: if any visible
  // kind's source failed and the fulfilled kinds happen to have nothing in
  // them, the aggregate list is empty for the same "unknown" reason, not
  // because every catalog is genuinely empty — so it must not show the
  // generic empty state with a "New Workflow" CTA either.
  const kindLoadFailed =
    kindFilter === "hooks"
      ? false
      : kindFilter === "all"
        ? LIBRARY_KINDS.some(
            (kind) => !UNFINISHED_KINDS.has(kind) && visible.kindStatus[kind] === "failed",
          )
        : visible.kindStatus[kindFilter as LibraryKind] === "failed";

  const createLabel =
    kindFilter === "mcp"
      ? t("newMcpServer")
      : kindFilter === "agent"
        ? t("newAgent")
        : t("newWorkflow");

  // Only the URL, a click, or a create form asks for detail; the row the
  // catalog fills in on its own does not. A pane opens only with a detail that
  // offers Back, or with the create form, so the collapsed layout can always
  // return to the catalog.
  const detailPaneActive = showCreate || !!urlSelection || (detailActive && !!parsed);

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
          aria-label={t("searchPlaceholder")}
          className="min-w-0 flex-1 rounded border border-edge bg-surface-overlay px-2 py-1 font-ui text-[length:var(--t-sm)] text-content-primary focus:outline-none"
        />
        {!NO_CREATE_KINDS.has(kindFilter as LibraryKind) && (
          <Button size="sm" variant="primary" onClick={openCreate}>
            + {createLabel}
          </Button>
        )}
      </div>

      {/* Error banner */}
      {error && (
        <div
          role="alert"
          className="flex shrink-0 items-center justify-between gap-3 border-b border-edge bg-status-error-bg px-3 py-2 text-body text-content-secondary"
        >
          <span>
            {t("loadError")}{" "}
            {ALL_KIND_TABS.filter(
              (tab) => visible.kindStatus[tab.value as LibraryKind] === "failed",
            )
              .map((tab) => tab.label)
              .join(", ")}
          </span>
          <Button size="sm" variant="secondary" onClick={retryVisible}>
            {tDaemon("retry")}
          </Button>
        </div>
      )}

      {/* Catalog */}
      <div className="flex-1 overflow-y-auto">
        {loading ? (
          <CatalogSkeleton />
        ) : isEmpty && kindLoadFailed ? (
          <EmptyState
            glyph="▤"
            title={t("loadError")}
            action={
              <div className="flex items-center gap-2">
                {isFiltered && (
                  <Button size="sm" variant="secondary" onClick={() => setSearch("")}>
                    {t("empty.clearFilters")}
                  </Button>
                )}
                <Button size="sm" variant="secondary" onClick={retryVisible}>
                  {tDaemon("retry")}
                </Button>
              </div>
            }
            className="px-6 py-16"
          />
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
              isFiltered ? (
                <Button size="sm" variant="secondary" onClick={() => setSearch("")}>
                  {t("empty.clearFilters")}
                </Button>
              ) : !NO_CREATE_KINDS.has(kindFilter as LibraryKind) ? (
                <Button size="sm" variant="primary" onClick={openCreate}>
                  + {createLabel}
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
                  !showCreate &&
                  parsed?.kind === item.kind &&
                  parsed.name === item.name &&
                  (item.kind !== "playbook" || parsed.subKind === item.subKind);
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

  // Leaving the detail pane drops its selection as well as its state: a URL
  // still naming the row would reopen the pane.
  function handleBack() {
    setDetailActive(false);
    setShowCreate(false);
    void navigate({
      search: (prev) => {
        const next = { ...prev };
        delete next.sel;
        return next;
      },
      replace: false,
    });
  }

  // A delete can resolve after the viewer has moved on: to another row, to the
  // create form, or to another tab. The pane state belongs to whatever is on
  // screen now, so the completion clears it only while the address on this tab
  // still names the deleted item.
  function handleDeleted(kind: LibraryKind, name: string) {
    if (!completionOnTab(kindFilter) || showCreate) return;
    const selected = parseSel(sel);
    if (!selected || selected.kind !== kind || selected.name !== name) return;
    setDetailActive(false);
  }

  let detailPane: ReactNode;

  if (showCreate && kindFilter === "mcp") {
    detailPane = (
      <div className="flex h-full flex-col overflow-hidden">
        <CreateMcpServerPanel
          onCreated={(name) => {
            if (!mounted.current) return;
            if (completionOnTab(kindFilter)) setShowCreate(false);
            invalidate("mcp");
            void reload();
            void navigate({
              search: (prev) =>
                (prev.tab ?? "all") === kindFilter && prev.sel === sel
                  ? { ...prev, sel: encodeSel("mcp", name) }
                  : prev,
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
          if (!mounted.current) return;
          if (completionOnTab(kindFilter)) setShowCreate(false);
          invalidate("agent");
          void reload();
          void navigate({
            search: (prev) =>
              (prev.tab ?? "all") === kindFilter && prev.sel === sel
                ? { ...prev, sel: encodeSel("agent", name) }
                : prev,
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
            if (!mounted.current) return;
            setOptimisticWorkflow(workflow);
            if (completionOnTab(kindFilter)) setShowCreate(false);
            invalidate("workflow");
            void reload();
            void navigate({
              search: (prev) =>
                (prev.tab ?? "all") === kindFilter && prev.sel === sel
                  ? { ...prev, sel: encodeSel("workflow", workflow.name) }
                  : prev,
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
  } else if (
    selectedHiddenKind &&
    !selectedWorkflowId &&
    (hidden.loading || hidden.kindStatus[selectedHiddenKind] === "failed")
  ) {
    const failed = !hidden.loading && hidden.kindStatus[selectedHiddenKind] === "failed";
    detailPane = (
      <div className="flex h-full flex-col overflow-hidden">
        <DrawerBackButton onClick={handleBack}>{t("drawer.back")}</DrawerBackButton>
        <DrawerHeader
          name={parsed!.name}
          badge={ALL_KIND_TABS.find((tab) => tab.value === selectedHiddenKind)?.label}
        />
        <div className="flex items-center gap-3 p-4" role={failed ? "alert" : "status"}>
          <span>{t(failed ? "loadError" : "loading")}</span>
          {failed && (
            <Button size="sm" variant="secondary" onClick={retryHidden}>
              {tDaemon("retry")}
            </Button>
          )}
        </div>
      </div>
    );
  } else if (parsed?.kind === "mcp") {
    detailPane = (
      <McpServerDetail
        name={parsed.name}
        onBack={handleBack}
        onDeleted={() => {
          if (!mounted.current) return;
          const deleted = parsed.name;
          handleDeleted("mcp", deleted);
          void reload();
          invalidate("mcp");
          void navigate({
            search: (prev) => {
              if ((prev.tab ?? "all") !== kindFilter) return prev;
              const current = parseSel(prev.sel);
              if (current?.kind !== "mcp" || current.name !== deleted) return prev;
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
          if (!mounted.current) return;
          const deleted = parsed.name;
          handleDeleted("agent", deleted);
          invalidate("agent");
          void reload();
          void navigate({
            search: (prev) => {
              if ((prev.tab ?? "all") !== kindFilter) return prev;
              const current = parseSel(prev.sel);
              if (current?.kind !== "agent" || current.name !== deleted) return prev;
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
    detailPane = (
      <WorkflowDetail
        id={selectedWorkflowId}
        onBack={handleBack}
        onDeleted={() => {
          if (!mounted.current) return;
          const deleted = parsed.name;
          handleDeleted("workflow", deleted);
          if (optimisticWorkflow?.name === deleted) setOptimisticWorkflow(null);
          invalidate("workflow");
          void reload();
          void navigate({
            search: (prev) => {
              if ((prev.tab ?? "all") !== kindFilter) return prev;
              const current = parseSel(prev.sel);
              if (current?.kind !== "workflow" || current.name !== deleted) return prev;
              const next = { ...prev };
              delete next.sel;
              return next;
            },
            replace: false,
          });
        }}
      />
    );
  } else if (parsed?.kind === "playbook") {
    detailPane = (
      <PlaybookTemplateDetail
        name={parsed.name}
        isBuiltin={parsed.subKind === "builtin"}
        onBack={handleBack}
        onCloned={(clonedName) => {
          if (!mounted.current) return;
          invalidate("playbook");
          void reload();
          void navigate({
            search: (prev) =>
              (prev.tab ?? "all") === kindFilter && prev.sel === sel
                ? { ...prev, sel: encodeSel("playbook", clonedName, "custom") }
                : prev,
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

      {/* Split body — the Hooks tab is a single configuration surface (the
          shared hook library + the Operator's assembly), not a catalog of
          selectable items, so it takes the whole body instead of the split. */}
      <div className="min-h-0 flex-1">
        {kindFilter === "hooks" ? (
          <HooksView />
        ) : (
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
