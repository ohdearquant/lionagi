import { useState, useCallback, useEffect, useMemo, useRef } from "react";
import { Link } from "@tanstack/react-router";
import { useTranslations } from "use-intl";
import { listRuns } from "@/lib/api";
import { useFleet } from "./useFleet";
import { createHistoryPager, terminalRecentRowsServerOrder } from "./fleetReducer";
import type { HistoryPager } from "./fleetReducer";
import type { OrgUnit, AgentRow, RecentRow } from "./fleetReducer";
import SessionDetail from "./SessionDetail";
import FleetStaleBadge from "./FleetStaleBadge";
import ProjectFilter from "./ProjectFilter";
import SplitPane from "@/components/ui/SplitPane";
import StatusDot from "@/components/ui/StatusDot";
import { deriveDisplayStatus } from "@/lib/runStatus";
import { formatCostUsd } from "@/lib/usageFormat";
import { Route } from "@/routes/fleet";
import type { RetiredSearchValue } from "@/lib/retiredRoutes";
import { formatElapsed as formatElapsedShared } from "@/lib/elapsed";

// ─── Helpers ─────────────────────────────────────────────────────────────────

export function formatElapsed(sec: number | null): string {
  return formatElapsedShared(sec, { capAtDays: true });
}

// Compact count for a legibility-sensitive slot: under 1000 renders the exact
// integer, at or above 1000 renders one decimal place with a "k" suffix
// (truncated, not rounded, so the digit shown never overstates the count).
export function formatCompactCount(n: number): string {
  if (n < 1000) return `${n}`;
  return `${Math.floor(n / 100) / 10}k`;
}

// ─── Agent row ────────────────────────────────────────────────────────────────

// invocation_kind values that mean "this row is the root of a multi-agent
// execution, not a single agent" (issue #2842) — the closed vocabulary lives
// in lionagi/state/db.py's sessions.invocation_kind CHECK constraint.
const PLAY_ROOT_KINDS = new Set(["play", "flow", "fanout", "show-play"]);
const TERMINAL_RUN_STATUSES = [
  "completed",
  "completed_empty",
  "failed",
  "timed_out",
  "aborted",
  "cancelled",
];

export function isPlayRoot(invocationKind: string | null | undefined): boolean {
  return invocationKind != null && PLAY_ROOT_KINDS.has(invocationKind);
}

function AgentRowItem({
  agent,
  selected,
  onSelect,
}: {
  agent: AgentRow;
  selected: boolean;
  onSelect: (id: string) => void;
}) {
  const t = useTranslations("fleet");
  return (
    <button
      type="button"
      onClick={() => onSelect(agent.id)}
      className={`flex w-full items-center gap-3 border-t border-edge border-l-2 px-4 py-2 text-left transition-colors duration-100 hover:bg-surface-overlay ${
        selected ? "border-l-accent bg-surface-overlay" : "border-l-transparent"
      }`}
      aria-pressed={selected}
      aria-label={t("agentRow.ariaLabel", { name: agent.name })}
    >
      <StatusDot status={agent.status} />
      <span className="min-w-0 flex-1 truncate font-data text-[length:var(--t-sm)] text-content-primary">
        {agent.name}
      </span>
      <span
        className="min-w-[28px] shrink-0 font-data tabular-nums text-[length:var(--t-xs)] text-content-muted"
        title={t("agentRow.branchesTitle", { count: agent.branch_count })}
      >
        {agent.branch_count > 0
          ? t("agentRow.branches", { count: formatCompactCount(agent.branch_count) })
          : "—"}
      </span>
      <span
        className="min-w-[28px] shrink-0 font-data tabular-nums text-[length:var(--t-xs)] text-content-muted"
        title={t("agentRow.messagesTitle", { count: agent.message_count })}
      >
        {agent.message_count > 0
          ? t("agentRow.messages", { count: formatCompactCount(agent.message_count) })
          : "—"}
      </span>
      <span className="min-w-[48px] shrink-0 font-data tabular-nums text-[length:var(--t-xs)] text-status-running">
        {formatElapsed(agent.elapsedSec)}
      </span>
    </button>
  );
}

export function AgentSections({
  agents,
  selectedId,
  onSelectAgent,
}: {
  agents: AgentRow[];
  selectedId: string | null;
  onSelectAgent: (id: string) => void;
}) {
  const t = useTranslations("fleet");
  const orchestrations = agents.filter((agent) => isPlayRoot(agent.invocationKind));
  const singleAgents = agents.filter((agent) => !isPlayRoot(agent.invocationKind));
  const groups = [
    { key: "orchestrations", rows: orchestrations },
    { key: "agents", rows: singleAgents },
  ] as const;

  return groups.map(({ key, rows }) =>
    rows.length > 0 ? (
      <section
        key={key}
        data-fleet-group={key}
        aria-label={t(`counts.${key}`, { count: rows.length })}
      >
        <div className="border-t border-edge bg-surface-overlay px-4 py-1.5 font-ui text-[length:var(--t-xs)] font-semibold uppercase tracking-[0.08em] text-content-muted">
          {t(`counts.${key}`, { count: rows.length })}
        </div>
        {rows.map((agent) => (
          <AgentRowItem
            key={agent.id}
            agent={agent}
            selected={selectedId === agent.id}
            onSelect={onSelectAgent}
          />
        ))}
      </section>
    ) : null,
  );
}

// ─── Org unit group ───────────────────────────────────────────────────────────

function OrgUnitGroup({
  unit,
  selectedId,
  onSelectAgent,
}: {
  unit: OrgUnit;
  selectedId: string | null;
  onSelectAgent: (id: string) => void;
}) {
  const t = useTranslations("fleet");
  const isDirect = unit.id === "__direct__";
  const label = isDirect ? t("group.direct") : unit.skill;

  return (
    <div className="border-b border-edge">
      {/* Group header */}
      <div className="flex items-center gap-3 bg-surface-raised px-4 py-2">
        <span className="min-w-0 flex-1 truncate font-ui text-[length:var(--t-xs)] font-semibold uppercase tracking-[0.08em] text-content-muted">
          {label}
        </span>

        {!isDirect && unit.plugin && (
          <span className="shrink-0 rounded bg-surface-overlay px-1 py-0.5 font-data text-[length:var(--t-xs)] uppercase tracking-wider text-content-muted">
            {unit.plugin}
          </span>
        )}

        <span className="shrink-0 font-data tabular-nums text-[length:var(--t-xs)] text-content-muted">
          {t("group.sessions", { count: unit.session_count })}
        </span>

        {unit.needsAttention && (
          <span
            className="shrink-0 rounded px-1.5 py-0.5 font-data text-[length:var(--t-xs)] font-semibold uppercase tracking-wider text-accent"
            style={{ background: "color-mix(in srgb, var(--accent) 15%, transparent)" }}
          >
            {t("group.attention")}
          </span>
        )}

        <span className="min-w-[48px] shrink-0 font-data tabular-nums text-[length:var(--t-xs)] text-status-running">
          {formatElapsed(unit.elapsedSec)}
        </span>
      </div>

      <AgentSections agents={unit.agents} selectedId={selectedId} onSelectAgent={onSelectAgent} />

      {unit.agents.length === 0 && (
        <div className="border-t border-edge px-4 py-2">
          <span className="font-data text-[length:var(--t-xs)] text-content-muted">
            {t("group.noAgents")}
          </span>
        </div>
      )}
    </div>
  );
}

// ─── Counts strip ─────────────────────────────────────────────────────────────

function CountsStrip({
  orchestrations,
  agents,
  attention,
}: {
  orchestrations: number;
  agents: number;
  attention: number;
}) {
  const t = useTranslations("fleet");
  return (
    <div className="flex items-center gap-4 border-b border-edge px-4 py-2">
      <span className="font-data tabular-nums text-[length:var(--t-xs)] text-content-secondary">
        {t("counts.orchestrations", { count: orchestrations })}
      </span>
      <span className="text-edge">·</span>
      <span className="font-data tabular-nums text-[length:var(--t-xs)] text-content-secondary">
        {t("counts.agents", { count: agents })}
      </span>
      {attention > 0 && (
        <>
          <span className="text-edge">·</span>
          <span className="font-data tabular-nums text-[length:var(--t-xs)] font-semibold text-accent">
            {t("counts.attention", { count: attention })}
          </span>
        </>
      )}
    </div>
  );
}

// ─── Zero state ───────────────────────────────────────────────────────────────

function EmptyState({ recent, nowSec }: { recent: RecentRow[]; nowSec: number }) {
  const t = useTranslations("fleet");
  return (
    <div className="flex flex-1 flex-col items-center justify-center gap-4 px-6 py-16">
      <svg
        width="40"
        height="40"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden="true"
        className="text-content-muted"
      >
        <circle cx="12" cy="12" r="3" />
        <path d="M12 1v4M12 19v4M4.22 4.22l2.83 2.83M16.95 16.95l2.83 2.83M1 12h4M19 12h4M4.22 19.78l2.83-2.83M16.95 7.05l2.83-2.83" />
      </svg>
      <p className="max-w-[280px] text-center text-[length:var(--t-base)] text-content-secondary">
        {t("empty.message")}
      </p>

      {recent.length > 0 ? (
        <div className="flex w-full max-w-md flex-col">
          <span className="px-1 pb-2 font-ui text-[length:var(--t-xs)] font-semibold uppercase tracking-[0.08em] text-content-muted">
            {t("empty.recent")}
          </span>
          <div className="overflow-hidden rounded-md border border-edge">
            {recent.map((row) => (
              <Link
                key={row.id}
                to="/fleet"
                search={{ s: row.id }}
                className="flex items-center gap-3 border-t border-edge px-3 py-2 transition-colors duration-100 hover:bg-surface-overlay"
              >
                <StatusDot status={deriveDisplayStatus(row)} />
                <span className="min-w-0 flex-1 truncate font-data text-[length:var(--t-sm)] text-content-primary">
                  {row.name}
                </span>
                <span className="shrink-0 font-data tabular-nums text-[length:var(--t-xs)] text-content-muted">
                  {row.endedAtSec != null
                    ? t("empty.ago", {
                        delta: formatElapsed(Math.max(0, Math.floor(nowSec - row.endedAtSec))),
                      })
                    : "—"}
                </span>
              </Link>
            ))}
          </div>
        </div>
      ) : null}

      <span className="font-data text-[length:var(--t-xs)] text-content-muted">
        {t("empty.hint")}
      </span>
    </div>
  );
}

function LoadingState() {
  const t = useTranslations("fleet");
  return (
    <div className="flex flex-1 items-center justify-center">
      <span className="text-[length:var(--t-sm)] text-content-muted">{t("loading")}</span>
    </div>
  );
}

function ErrorState({ message }: { message: string | null }) {
  const t = useTranslations("fleet");
  return (
    <div className="flex flex-1 flex-col items-center justify-center gap-3 px-6">
      <span className="font-data text-[length:var(--t-sm)] text-status-failure">
        {t("error.message", { detail: message ?? t("error.unreachable") })}
      </span>
    </div>
  );
}

// ─── Session history (terminal runs, selectable in place) ────────────────────

type HistFilter = "all" | "completed" | "failed";

// The orchestration-kind facet vocabulary, mirroring the server's
// VALID_KIND_FILTERS (services/runs.py). "show" also admits "show-play"
// rows server-side.
const KIND_FACETS = ["agent", "play", "flow", "fanout", "show"] as const;
type KindFacet = (typeof KIND_FACETS)[number];

// Filters against the normalized display status, not the raw run.status —
// a phantom-reaped row (raw status "failed") is orphaned housekeeping, not a
// failure, so it must not match the "failed" chip (design-brief §0/§4).
function matchesHistFilter(displayStatus: string, filter: HistFilter): boolean {
  if (filter === "all") return true;
  if (filter === "failed") return displayStatus === "failed";
  return displayStatus === "completed";
}

export function HistorySection({
  rows,
  filter,
  sort,
  onSort,
  kind,
  onKind,
  selectedId,
  onSelect,
  nowSec,
  visibleCount,
  serverHasMore,
  loadingMore,
  onLoadMore,
}: {
  rows: RecentRow[];
  filter: HistFilter;
  sort: "recent" | "cost";
  onSort: (s: "recent" | "cost") => void;
  kind: KindFacet | null;
  onKind: (k: KindFacet | null) => void;
  selectedId: string | null;
  onSelect: (id: string) => void;
  nowSec: number;
  visibleCount: number;
  serverHasMore: boolean;
  loadingMore: boolean;
  onLoadMore: () => void;
}) {
  const t = useTranslations("fleet");
  // Cost sort is already the server's own order (see FleetView's
  // costSortedRows fetch) — re-filtering by status here is still correct,
  // but must not re-sort, so the status filter uses a stable sort.
  const allFiltered = rows.filter((r) => matchesHistFilter(deriveDisplayStatus(r), filter));
  const filtered = allFiltered.slice(0, visibleCount);
  const hasMore = allFiltered.length > visibleCount || serverHasMore;

  // Lazy loading: the load-more button doubles as the sentinel — scrolling it
  // into view fetches the next slice without a click (click still works).
  const moreRef = useRef<HTMLButtonElement | null>(null);
  // The observer reads the handler through a ref so that re-arming is driven
  // only by the button appearing or disappearing. Depending on the handler
  // itself made this self-re-arming: revealing rows changes `onLoadMore`'s
  // identity, which tore the observer down and re-observed the button, and a
  // newly observed target always gets an immediate initial observation. With
  // the button still on screen that observation fired the handler again, so
  // the page revealed and re-paged its way through the entire run history in
  // one burst — fast enough for React to abort the render as a runaway
  // update loop. Threshold crossings are what should drive this, and a
  // stable observer reports each crossing exactly once.
  const onLoadMoreRef = useRef(onLoadMore);
  useEffect(() => {
    onLoadMoreRef.current = onLoadMore;
  }, [onLoadMore]);
  useEffect(() => {
    const el = moreRef.current;
    if (!el || typeof IntersectionObserver === "undefined") return;
    const io = new IntersectionObserver((entries) => {
      if (entries.some((e) => e.isIntersecting)) onLoadMoreRef.current();
    });
    io.observe(el);
    return () => io.disconnect();
  }, [hasMore]);
  return (
    <div>
      {/* Section header — the status chips moved up to the FilterBar beside
          the other scope controls; the count still reflects the active
          status filter. */}
      <div className="flex items-center gap-2 border-b border-edge bg-surface-raised px-4 py-2">
        <span className="min-w-0 flex-1 truncate font-ui text-[length:var(--t-xs)] font-semibold uppercase tracking-[0.08em] text-content-muted">
          {t("history.label")}
        </span>
        <span className="shrink-0 font-data tabular-nums text-[length:var(--t-xs)] text-content-muted">
          {allFiltered.length}
          {serverHasMore ? "+" : ""}
        </span>
      </div>

      {/* Sort toggle — "Highest cost" is computed server-side (/api/runs/?sort=cost),
          never a client re-sort of the recency-paginated rows above. */}
      <div className="flex items-center gap-1 border-b border-edge bg-surface-raised px-4 py-1.5">
        {(["recent", "cost"] as const).map((s) => (
          <button
            key={s}
            type="button"
            onClick={() => onSort(s)}
            aria-pressed={sort === s}
            className={`shrink-0 rounded px-1.5 py-0.5 font-data text-[length:var(--t-xs)] transition-colors duration-100 ${
              sort === s
                ? "bg-surface-overlay text-content-primary"
                : "text-content-muted hover:text-content-secondary"
            }`}
          >
            {s === "cost" ? t("history.sortCost") : t("history.sortRecent")}
          </button>
        ))}
        {/* Orchestration-kind facet, applied server-side (?kind=…) so it
            composes with pagination and with the status chips above. The
            facet values are the product's own kind vocabulary, shown raw —
            the same strings each row's data already carries. */}
        <span className="flex-1" />
        {KIND_FACETS.map((k) => (
          <button
            key={k}
            type="button"
            onClick={() => onKind(kind === k ? null : k)}
            aria-pressed={kind === k}
            className={`shrink-0 rounded px-1.5 py-0.5 font-data text-[length:var(--t-xs)] transition-colors duration-100 ${
              kind === k
                ? "bg-surface-overlay text-content-primary"
                : "text-content-muted hover:text-content-secondary"
            }`}
          >
            {k}
          </button>
        ))}
      </div>

      {filtered.length === 0 ? (
        <div className="px-4 py-3">
          <span className="font-data text-[length:var(--t-xs)] text-content-muted">
            {t("history.empty")}
          </span>
        </div>
      ) : (
        filtered.map((row) => {
          const derived = deriveDisplayStatus(row);
          return (
            <button
              key={row.id}
              type="button"
              onClick={() => onSelect(row.id)}
              aria-pressed={selectedId === row.id}
              className={`flex w-full items-center gap-3 border-b border-edge border-l-2 px-4 py-2 text-left transition-colors duration-100 hover:bg-surface-overlay ${
                selectedId === row.id
                  ? "border-l-accent bg-surface-overlay"
                  : "border-l-transparent"
              }`}
            >
              <StatusDot status={derived} />
              <span className="min-w-0 flex-1 truncate font-data text-[length:var(--t-sm)] text-content-primary">
                {row.name}
              </span>
              <span className="shrink-0 font-data text-[length:var(--t-xs)] text-content-muted">
                {derived}
              </span>
              {/* Bare formatted value, no label — matches the run detail's
                  branch-row cost cells and avoids a new locale key here. */}
              <span className="min-w-[56px] shrink-0 text-right font-data tabular-nums text-[length:var(--t-xs)] text-content-muted">
                {formatCostUsd(row.totalCostUsd)}
              </span>
              <span className="min-w-[48px] shrink-0 text-right font-data tabular-nums text-[length:var(--t-xs)] text-content-muted">
                {row.endedAtSec != null
                  ? t("empty.ago", {
                      delta: formatElapsed(Math.max(0, Math.floor(nowSec - row.endedAtSec))),
                    })
                  : "—"}
              </span>
            </button>
          );
        })
      )}

      {hasMore && (
        <button
          ref={moreRef}
          type="button"
          onClick={onLoadMore}
          disabled={loadingMore}
          className="flex w-full items-center justify-center border-b border-edge px-4 py-2 font-data text-[length:var(--t-xs)] text-content-muted transition-colors duration-100 hover:bg-surface-overlay hover:text-content-secondary disabled:opacity-60"
        >
          {loadingMore ? t("history.loadingMore") : t("history.loadMore")}
        </button>
      )}
    </div>
  );
}

// ─── Filter bar (project scope + text search) ─────────────────────────────────

function FilterBar({
  searchDraft,
  onSearchDraftChange,
  project,
  projectNull,
  onProjectChange,
  onClear,
  histFilter,
  onHistFilter,
}: {
  searchDraft: string;
  onSearchDraftChange: (v: string) => void;
  project: string | null;
  projectNull: boolean;
  onProjectChange: (next: { project?: string; projectNull?: boolean }) => void;
  onClear: () => void;
  histFilter: HistFilter;
  onHistFilter: (f: HistFilter) => void;
}) {
  const t = useTranslations("fleet");
  const hasFilter = Boolean(searchDraft) || Boolean(project) || projectNull;
  // Status chips live up here with the other scope controls rather than in
  // the history section header: a filter placed inside one section reads as
  // scoped to that section, which is not a promise this control keeps.
  const chips: { key: HistFilter; label: string }[] = [
    { key: "all", label: t("history.all") },
    { key: "completed", label: t("history.completed") },
    { key: "failed", label: t("history.failed") },
  ];
  return (
    <div className="flex items-center gap-2 border-b border-edge px-4 py-2">
      <input
        type="search"
        value={searchDraft}
        onChange={(e) => onSearchDraftChange(e.target.value)}
        placeholder={t("filters.searchPlaceholder")}
        aria-label={t("filters.searchAria")}
        className="min-w-0 flex-1 rounded border border-edge bg-surface-base px-2 py-1 font-data text-[length:var(--t-xs)] text-content-primary placeholder:text-content-muted focus:border-accent/50 focus:outline-none"
      />
      <ProjectFilter project={project} projectNull={projectNull} onChange={onProjectChange} />
      {chips.map((c) => (
        <button
          key={c.key}
          type="button"
          onClick={() => onHistFilter(c.key)}
          aria-pressed={histFilter === c.key}
          className={`shrink-0 rounded px-1.5 py-0.5 font-data text-[length:var(--t-xs)] transition-colors duration-100 ${
            histFilter === c.key
              ? "bg-surface-overlay text-content-primary"
              : "text-content-muted hover:text-content-secondary"
          }`}
        >
          {c.label}
        </button>
      ))}
      {hasFilter && (
        <button
          type="button"
          onClick={onClear}
          className="shrink-0 font-data text-[length:var(--t-xs)] text-content-muted transition-colors hover:text-content-secondary"
        >
          {t("filters.clear")}
        </button>
      )}
    </div>
  );
}

// ─── First agent id across all units ─────────────────────────────────────────

// FleetSearch's index signature is `RetiredSearchValue` (no `undefined`), so
// an explicit `undefined` in a patch must delete the key rather than be
// assigned — otherwise the object no longer satisfies the router's search type.
export function patchSearch(
  base: Record<string, unknown>,
  patch: Record<string, string | boolean | undefined>,
): Record<string, RetiredSearchValue> {
  const next = { ...base } as Record<string, RetiredSearchValue>;
  for (const [key, value] of Object.entries(patch)) {
    if (value === undefined) delete next[key];
    else next[key] = value;
  }
  return next;
}

function firstAgentId(orgUnits: OrgUnit[]): string | null {
  for (const unit of orgUnits) {
    if (unit.agents.length > 0) return unit.agents[0].id;
  }
  return null;
}

// ─── Main view ────────────────────────────────────────────────────────────────

export default function FleetView() {
  const t = useTranslations("fleet");
  const tShared = useTranslations("shared");

  // URL-synced selection and filters: ?s=<runId>&project=<name>&project_null=true&q=<text>
  const search = Route.useSearch();
  const navigate = Route.useNavigate();
  const urlRunId = (search as { s?: string }).s ?? null;
  const rawInvocationId = (search as { invocation?: unknown }).invocation;
  const urlInvocationId =
    typeof rawInvocationId === "string"
      ? rawInvocationId
      : Array.isArray(rawInvocationId) && typeof rawInvocationId[0] === "string"
        ? rawInvocationId[0]
        : null;
  const urlProject = (search as { project?: string }).project ?? null;
  const urlProjectNull = (search as { project_null?: boolean }).project_null ?? false;
  const urlSearchText = (search as { q?: string }).q ?? "";
  // Orchestration-kind facet (?kind=play etc.) — set by the facet select
  // below or by the Operator's navigate tool; applied server-side so it
  // composes with pagination. Unknown values are dropped rather than sent
  // (the server 422s on them, which would blank the whole list).
  const rawUrlKind = (search as { kind?: unknown }).kind;
  const urlKindCandidate =
    typeof rawUrlKind === "string"
      ? rawUrlKind
      : Array.isArray(rawUrlKind) && typeof rawUrlKind[0] === "string"
        ? rawUrlKind[0]
        : null;
  const urlKind =
    urlKindCandidate !== null && KIND_FACETS.includes(urlKindCandidate as KindFacet)
      ? (urlKindCandidate as KindFacet)
      : null;

  const state = useFleet({
    project: urlProject ?? undefined,
    projectNull: urlProjectNull,
    search: urlSearchText || undefined,
    kind: urlKind ?? undefined,
  });

  // A deep link is already an explicit selection. This matters when the
  // Operator dock narrows Fleet below the split-pane breakpoint: opening a run
  // must reveal its detail instead of landing back on the master list.
  const [narrowExplicit, setNarrowExplicit] = useState(() => Boolean(urlRunId));
  // Initializers only run on mount, but the Operator's navigate tool changes
  // the search params on an already-mounted Fleet (/fleet -> /fleet?s=…), so
  // the deep-link intent must be re-applied whenever the URL's run target
  // changes — during render, the endorsed adjust-on-props-change pattern,
  // which leaves the user's own back/collapse actions (no URL change) alone.
  const autoSelectedRef = useRef<string | null>(null);
  const [lastUrlRunId, setLastUrlRunId] = useState(urlRunId);
  if (lastUrlRunId !== urlRunId) {
    setLastUrlRunId(urlRunId);
    // The auto-select effect below writes ?s= too; a selection this component
    // authored itself is not a deep link, and treating it as one replaces the
    // master list with the detail pane whenever the pane is narrow.
    if (urlRunId !== autoSelectedRef.current) {
      setNarrowExplicit(Boolean(urlRunId));
    }
  }
  // Deep links (and the Operator's navigate tool) carry ?status=…; honor it
  // as the initial history filter instead of silently showing "all".
  const rawUrlStatus = (search as { status?: unknown }).status;
  const urlStatus =
    typeof rawUrlStatus === "string"
      ? rawUrlStatus
      : Array.isArray(rawUrlStatus) && typeof rawUrlStatus[0] === "string"
        ? rawUrlStatus[0]
        : null;
  const [histFilter, setHistFilter] = useState<HistFilter>(() =>
    urlStatus === "failed" || urlStatus === "completed" ? urlStatus : "all",
  );
  // Same adjust-on-change pattern as narrowExplicit above: an in-place
  // navigation carrying ?status=… must move the filter, while a URL without
  // the param expresses no opinion and leaves the user's local choice alone.
  const [lastUrlStatus, setLastUrlStatus] = useState(urlStatus);
  if (lastUrlStatus !== urlStatus) {
    setLastUrlStatus(urlStatus);
    if (urlStatus === "failed" || urlStatus === "completed") {
      setHistFilter(urlStatus);
    }
  }
  const [histSort, setHistSort] = useState<"recent" | "cost">("recent");

  // Text search is debounced into the URL (and from there into the poll and
  // pager) so every keystroke doesn't fire a request — the input itself stays
  // instant, only the committed value lags by SEARCH_DEBOUNCE_MS.
  const SEARCH_DEBOUNCE_MS = 300;
  const [searchDraft, setSearchDraft] = useState(urlSearchText);
  useEffect(() => {
    // Resync the draft from an external URL change (back/forward nav,
    // Clear) — typing itself must not be fought by this effect on every
    // keystroke, so urlSearchText is the sole dependency.
    // eslint-disable-next-line react-hooks/set-state-in-effect -- external->local resync, not a derived-from-props computation the render body could do instead
    setSearchDraft(urlSearchText);
  }, [urlSearchText]);
  useEffect(() => {
    if (searchDraft === urlSearchText) return;
    const timer = window.setTimeout(() => {
      void navigate({
        search: patchSearch(search, { q: searchDraft || undefined }),
        replace: true,
      });
    }, SEARCH_DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchDraft]);

  const handleProjectChange = useCallback(
    (next: { project?: string; projectNull?: boolean }) => {
      void navigate({
        search: patchSearch(search, {
          project: next.project,
          project_null: next.projectNull || undefined,
        }),
      });
    },
    [navigate, search],
  );
  const handleClearFilters = useCallback(() => {
    setSearchDraft("");
    void navigate({
      search: patchSearch(search, {
        project: undefined,
        project_null: undefined,
        q: undefined,
        kind: undefined,
      }),
    });
  }, [navigate, search]);
  const handleKindChange = useCallback(
    (next: string | null) => {
      void navigate({ search: patchSearch(search, { kind: next ?? undefined }) });
    },
    [navigate, search],
  );

  // History pagination. The 3s poll covers page 1 (200 runs); older pages are
  // fetched on demand and kept here — polls never clobber them. The visible
  // window grows in steps so a long history never renders all at once.
  const HIST_PAGE_SIZE = 200;
  const HIST_VISIBLE_STEP = 50;
  const [histVisible, setHistVisible] = useState(HIST_VISIBLE_STEP);
  const [olderRows, setOlderRows] = useState<RecentRow[]>([]);
  // null until the first on-demand fetch; before that the poll's has_next
  // (about page 1) is authoritative, after it the last fetched page's is.
  const [pagedHasMore, setPagedHasMore] = useState<boolean | null>(null);
  const serverHasMore = pagedHasMore ?? state.runsHasNext;
  const [loadingMore, setLoadingMore] = useState(false);
  // The pager serializes fetches with a synchronous guard so a sentinel fire
  // and a click in the same tick can't fetch one page twice and skip the next.
  // Rebuilt (via the effect below) whenever the filter scope changes, so an
  // in-flight or already-fetched older page from a different filter can never
  // be appended to the newly-filtered result set.
  const pagerRef = useRef<HistoryPager | null>(null);
  if (pagerRef.current === null) {
    pagerRef.current = createHistoryPager((page) =>
      listRuns({
        page,
        per_page: HIST_PAGE_SIZE,
        status: TERMINAL_RUN_STATUSES,
        project: urlProject ?? undefined,
        project_null: urlProjectNull,
        search: urlSearchText || undefined,
        kind: urlKind ? [urlKind] : undefined,
      }),
    );
  }
  const pager = pagerRef.current;

  useEffect(() => {
    pagerRef.current = createHistoryPager((page) =>
      listRuns({
        page,
        per_page: HIST_PAGE_SIZE,
        status: TERMINAL_RUN_STATUSES,
        project: urlProject ?? undefined,
        project_null: urlProjectNull,
        search: urlSearchText || undefined,
        kind: urlKind ? [urlKind] : undefined,
      }),
    );
    // Filter scope changed - the previous page's older-history cache/cursor
    // belongs to a different result set and must not be appended to this one.
    // (In-flight continuations from the old pager are dropped by the identity
    // checks in handleLoadMore: the ref now points at the new pager.)
    // eslint-disable-next-line react-hooks/set-state-in-effect -- external filter change, not a render-derivable value
    setOlderRows([]);
    setPagedHasMore(null);
    setHistVisible(HIST_VISIBLE_STEP);
  }, [urlProject, urlProjectNull, urlSearchText, urlKind]);

  // "Highest cost" is computed server-side (/api/runs/?sort=cost) rather than
  // a client re-sort of the live-polled + paginated "recent" history — the
  // poll only ever covers 200 rows in recency order, so a client sort
  // couldn't see cost across the whole store. Status filtering still happens
  // client-side (matchesHistFilter needs the derived display status, which
  // the server's raw status column can't distinguish — e.g. orphaned), so
  // rows matching the active filter beyond the first cost-ranked page are
  // reached the same way the "recent" pager reaches them: by paging further,
  // never by claiming the list is complete when it isn't (histHasMore below
  // reflects the server's own has_next, not a hardcoded false).
  const [costSortedRows, setCostSortedRows] = useState<RecentRow[] | null>(null);
  const [costSortLoading, setCostSortLoading] = useState(false);
  const [costHasMore, setCostHasMore] = useState(false);
  const costPagerRef = useRef<HistoryPager | null>(null);
  useEffect(() => {
    if (histSort !== "cost") return;
    let active = true;
    const fetchCostPage = (page: number) =>
      listRuns({
        page,
        per_page: HIST_PAGE_SIZE,
        project: urlProject ?? undefined,
        project_null: urlProjectNull,
        search: urlSearchText || undefined,
        kind: urlKind ? [urlKind] : undefined,
        sort: "cost",
      });
    costPagerRef.current = createHistoryPager(fetchCostPage, 2, terminalRecentRowsServerOrder);
    // eslint-disable-next-line react-hooks/set-state-in-effect -- reset stale state before async fetch, matching the rest of this file's fetch effects
    setCostSortLoading(true);
    fetchCostPage(1)
      .then((resp) => {
        if (!active) return;
        setCostSortedRows(terminalRecentRowsServerOrder(resp.runs));
        setCostHasMore(resp.has_next);
      })
      .catch(() => {
        if (active) {
          setCostSortedRows([]);
          setCostHasMore(false);
        }
      })
      .finally(() => {
        if (active) setCostSortLoading(false);
      });
    return () => {
      active = false;
    };
  }, [histSort, urlProject, urlProjectNull, urlSearchText, urlKind]);

  // Polled rows win on id collision (fresher status); older pages fill the tail.
  const recentSortedRows = useMemo(() => {
    const seen = new Set(state.recent.map((r) => r.id));
    const merged = [...state.recent];
    for (const row of olderRows) {
      if (!seen.has(row.id)) {
        seen.add(row.id);
        merged.push(row);
      }
    }
    merged.sort((a, b) => (b.endedAtSec ?? 0) - (a.endedAtSec ?? 0));
    return merged;
  }, [state.recent, olderRows]);

  const historyRows = useMemo(
    () => (histSort === "cost" ? (costSortedRows ?? []) : recentSortedRows),
    [histSort, costSortedRows, recentSortedRows],
  );
  // costHasMore mirrors the server's own has_next for the cost-ranked pages
  // fetched so far — never hardcoded, so a status filter that has exhausted
  // the loaded rows still shows "load more" instead of reading as complete.
  const histHasMore = histSort === "cost" ? costHasMore : serverHasMore;

  const handleLoadMore = useCallback(() => {
    if (histSort === "cost") {
      const costPager = costPagerRef.current;
      if (!costHasMore || !costPager || costPager.inFlight()) return;
      setCostSortLoading(true);
      void costPager.loadNext().then((page) => {
        // A filter/sort change swaps the pager under this continuation; its
        // rows belong to the previous result set and must not be appended
        // into the freshly-reset one (has-more included).
        if (costPagerRef.current !== costPager) {
          setCostSortLoading(false);
          return;
        }
        // null = fetch failed — leave state as-is; the sentinel retries the page.
        if (page) {
          setCostSortedRows((prev) => [...(prev ?? []), ...page.rows]);
          setCostHasMore(page.hasMore);
        }
        setCostSortLoading(false);
      });
      return;
    }
    // Reveal already-loaded rows first; hit the server only when exhausted.
    if (histVisible < historyRows.length) {
      setHistVisible((n) => n + HIST_VISIBLE_STEP);
      return;
    }
    if (!serverHasMore || pager.inFlight()) return;
    setLoadingMore(true);
    void pager.loadNext().then((page) => {
      // Same cross-generation guard as the cost branch: a filter change
      // rebuilt the pager and reset the arrays; this continuation's rows
      // belong to the old filter and mixing them in corrupts the list.
      if (pagerRef.current !== pager) {
        setLoadingMore(false);
        return;
      }
      // null = fetch failed — leave state as-is; the sentinel retries the page.
      if (page) {
        setPagedHasMore(page.hasMore);
        setOlderRows((prev) => [...prev, ...page.rows]);
        setHistVisible((n) => n + HIST_VISIBLE_STEP);
      }
      setLoadingMore(false);
    });
  }, [histSort, histVisible, historyRows.length, serverHasMore, pager, costHasMore]);

  // Derive effective selection: URL param first, else auto-select first row.
  // The auto-select is tracked in autoSelectedRef (declared beside the
  // deep-link sync above, which reads it) to avoid loops.
  const allAgents = state.orgUnits.flatMap((u) => u.agents);
  const invocationRunId = urlInvocationId
    ? (allAgents.find((agent) => agent.invocation_id === urlInvocationId)?.id ??
      historyRows.find((row) => row.invocation_id === urlInvocationId)?.id ??
      null)
    : null;
  const requestedRunId = urlRunId ?? invocationRunId;

  useEffect(() => {
    if (!urlRunId && invocationRunId) {
      void navigate({
        search: { ...search, s: invocationRunId },
        replace: true,
      });
    }
  }, [invocationRunId, navigate, search, urlRunId]);

  // Auto-select first row when data arrives and nothing is selected. Under a
  // terminal-status filter the live agents cannot match it, so selecting one
  // would contradict the very filter that produced the view — pick the first
  // matching history row instead.
  useEffect(() => {
    const first =
      histFilter !== "all"
        ? (historyRows.find((row) => matchesHistFilter(deriveDisplayStatus(row), histFilter))?.id ??
          null)
        : (firstAgentId(state.orgUnits) ?? historyRows[0]?.id ?? null);
    if (!first) return;
    if (urlRunId) return; // URL already has a selection
    if (autoSelectedRef.current === first) return;
    autoSelectedRef.current = first;
    // Patched onto the current search, not written over it: the router takes an
    // object-valued search as the whole next search, so selecting a row with a
    // bare `{ s }` would drop the project and text filters that produced the
    // row in the first place, and the next poll would come back unscoped.
    void navigate({ search: patchSearch(search, { s: first }), replace: true });
  }, [state.orgUnits, historyRows, histFilter, urlRunId, navigate, search]);

  // An explicit deep link (Library recent-runs, schedules, Operator navigate)
  // is trusted as-is: the run it names is often older than the loaded history
  // page, and the detail pane fetches by id anyway — a genuinely dead id
  // surfaces as RunDetail's own error state, not a silently empty page.
  const selectedRunId: string | null = requestedRunId;

  const handleSelectAgent = useCallback(
    (id: string) => {
      setNarrowExplicit(true);
      void navigate({ search: patchSearch(search, { s: id }) });
    },
    [navigate, search],
  );

  const handleBack = useCallback(() => {
    setNarrowExplicit(false);
  }, []);

  const master = (
    <div className="flex h-full flex-col overflow-hidden">
      {/* Header */}
      <div className="flex items-start justify-between gap-3 border-b border-edge px-4 py-3">
        <div className="min-w-0">
          <h1 className="text-page-title font-semibold text-content-primary">{t("page.title")}</h1>
          <p className="mt-0.5 truncate text-body text-content-muted">{t("page.subtitle")}</p>
        </div>
        <div className="shrink-0 pt-0.5">
          <FleetStaleBadge
            dataState={state.dataState}
            lastUpdatedMs={state.lastUpdatedMs}
            errorMessage={state.errorMessage}
          />
        </div>
      </div>

      {/* Filter bar — project scope + text search, both URL-persisted and
          applied server-side so they compose with pagination instead of
          filtering an already-truncated page. */}
      <FilterBar
        searchDraft={searchDraft}
        onSearchDraftChange={setSearchDraft}
        project={urlProject}
        projectNull={urlProjectNull}
        onProjectChange={handleProjectChange}
        onClear={handleClearFilters}
        histFilter={histFilter}
        onHistFilter={setHistFilter}
      />

      {/* Counts strip */}
      {state.dataState !== "loading" && state.dataState !== "error" && (
        <>
          <CountsStrip
            orchestrations={state.counts.orchestrations}
            agents={state.counts.agents}
            attention={state.counts.attention}
          />
          {state.activeRunOmitted + state.activeInvocationOmitted > 0 && (
            <p
              role="status"
              className="border-b border-edge bg-status-warning-bg px-4 py-2 font-data text-[length:var(--t-xs)] text-status-warning"
            >
              {tShared("activeSnapshotOmitted", {
                count: state.activeRunOmitted + state.activeInvocationOmitted,
                shown:
                  state.activeRunTotal +
                  state.activeInvocationTotal -
                  state.activeRunOmitted -
                  state.activeInvocationOmitted,
                total: state.activeRunTotal + state.activeInvocationTotal,
              })}
            </p>
          )}
        </>
      )}

      {/* Body — live orchestrations first, then session history (one page) */}
      <div className="flex flex-1 flex-col overflow-y-auto">
        {state.dataState === "loading" && state.orgUnits.length === 0 && <LoadingState />}
        {state.dataState === "error" && state.orgUnits.length === 0 && (
          <ErrorState message={state.errorMessage} />
        )}
        {(state.dataState === "live" || state.dataState === "stale") &&
          state.orgUnits.length === 0 &&
          state.recent.length === 0 && <EmptyState recent={[]} nowSec={state.nowSec} />}
        {state.orgUnits.length > 0 && (
          <div>
            {state.orgUnits.map((unit) => (
              <OrgUnitGroup
                key={unit.id}
                unit={unit}
                selectedId={selectedRunId}
                onSelectAgent={handleSelectAgent}
              />
            ))}
          </div>
        )}
        {state.dataState !== "loading" && state.dataState !== "error" && (
          <HistorySection
            rows={historyRows}
            filter={histFilter}
            sort={histSort}
            onSort={setHistSort}
            kind={urlKind}
            onKind={handleKindChange}
            selectedId={selectedRunId}
            onSelect={handleSelectAgent}
            nowSec={state.nowSec}
            visibleCount={histSort === "cost" ? historyRows.length : histVisible}
            serverHasMore={histHasMore}
            loadingMore={histSort === "cost" ? costSortLoading : loadingMore}
            onLoadMore={handleLoadMore}
          />
        )}
      </div>
    </div>
  );

  // Nothing selectable at all → render the master column full-width. The
  // detail pane only earns the split when a live or historical session can be
  // selected; a truly empty fleet reads as one composed state.
  if (state.orgUnits.length === 0 && state.recent.length === 0 && !selectedRunId) {
    return master;
  }

  const detail = (
    <SessionDetail runId={selectedRunId} onBack={handleBack} showBack={narrowExplicit} />
  );

  return (
    <SplitPane
      id="fleet"
      master={master}
      detail={detail}
      defaultMasterWidth={400}
      collapsible
      detailActive={narrowExplicit || Boolean(invocationRunId)}
      ariaLabelMaster={t("split.master")}
      ariaLabelDetail={t("split.detail")}
    />
  );
}
