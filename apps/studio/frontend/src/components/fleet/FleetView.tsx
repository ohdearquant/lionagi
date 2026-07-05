import { useState, useCallback, useEffect, useMemo, useRef } from "react";
import { Link } from "@tanstack/react-router";
import { useTranslations } from "use-intl";
import { listRuns } from "@/lib/api";
import { useFleet } from "./useFleet";
import { createHistoryPager } from "./fleetReducer";
import type { HistoryPager } from "./fleetReducer";
import type { OrgUnit, AgentRow, RecentRow } from "./fleetReducer";
import SessionDetail from "./SessionDetail";
import FleetStaleBadge from "./FleetStaleBadge";
import SplitPane from "@/components/ui/SplitPane";
import StatusDot from "@/components/ui/StatusDot";
import { Route } from "@/routes/fleet";

// ─── Helpers ─────────────────────────────────────────────────────────────────

function formatElapsed(sec: number | null): string {
  if (sec == null) return "—";
  if (sec < 60) return `${sec}s`;
  const m = Math.floor(sec / 60);
  if (m < 60) {
    const s = sec % 60;
    return s > 0 ? `${m}m ${s}s` : `${m}m`;
  }
  const h = Math.floor(m / 60);
  const mm = m - h * 60;
  return mm > 0 ? `${h}h ${mm}m` : `${h}h`;
}

// ─── Agent row ────────────────────────────────────────────────────────────────

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
      <span className="min-w-[28px] shrink-0 font-data text-[length:var(--t-xs)] uppercase tracking-wider text-content-muted">
        {t("agentRow.kind")}
      </span>
      <span className="min-w-[28px] shrink-0 font-data tabular-nums text-[length:var(--t-xs)] text-content-muted">
        {agent.branch_count > 0 ? t("agentRow.branches", { count: agent.branch_count }) : "—"}
      </span>
      <span className="min-w-[28px] shrink-0 font-data tabular-nums text-[length:var(--t-xs)] text-content-muted">
        {agent.message_count > 0 ? t("agentRow.messages", { count: agent.message_count }) : "—"}
      </span>
      <span className="min-w-[48px] shrink-0 font-data tabular-nums text-[length:var(--t-xs)] text-status-running">
        {formatElapsed(agent.elapsedSec)}
      </span>
    </button>
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

      {/* Agent rows */}
      {unit.agents.map((agent) => (
        <AgentRowItem
          key={agent.id}
          agent={agent}
          selected={selectedId === agent.id}
          onSelect={onSelectAgent}
        />
      ))}

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
                to="/history"
                search={{ sel: `run:${row.id}` }}
                className="flex items-center gap-3 border-t border-edge px-3 py-2 transition-colors duration-100 hover:bg-surface-overlay"
              >
                <StatusDot status={row.status} />
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
      ) : (
        <Link
          to="/designer"
          className="rounded px-4 py-2 font-data text-[length:var(--t-sm)] font-semibold text-accent transition-colors duration-100"
          style={{
            background: "color-mix(in srgb, var(--accent) 12%, transparent)",
            border: "1px solid color-mix(in srgb, var(--accent) 30%, transparent)",
          }}
        >
          {t("empty.cta")}
        </Link>
      )}

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

function matchesHistFilter(status: string, filter: HistFilter): boolean {
  if (filter === "all") return true;
  const s = status.toLowerCase();
  if (filter === "failed") return s === "failed" || s === "error" || s === "failure";
  return s === "completed" || s === "done" || s === "success";
}

function HistorySection({
  rows,
  filter,
  onFilter,
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
  onFilter: (f: HistFilter) => void;
  selectedId: string | null;
  onSelect: (id: string) => void;
  nowSec: number;
  visibleCount: number;
  serverHasMore: boolean;
  loadingMore: boolean;
  onLoadMore: () => void;
}) {
  const t = useTranslations("fleet");
  const allFiltered = rows.filter((r) => matchesHistFilter(r.status, filter));
  const filtered = allFiltered.slice(0, visibleCount);
  const hasMore = allFiltered.length > visibleCount || serverHasMore;

  // Lazy loading: the load-more button doubles as the sentinel — scrolling it
  // into view fetches the next slice without a click (click still works).
  const moreRef = useRef<HTMLButtonElement | null>(null);
  useEffect(() => {
    const el = moreRef.current;
    if (!el || typeof IntersectionObserver === "undefined") return;
    const io = new IntersectionObserver((entries) => {
      if (entries.some((e) => e.isIntersecting)) onLoadMore();
    });
    io.observe(el);
    return () => io.disconnect();
  }, [onLoadMore, hasMore, loadingMore]);
  const chips: { key: HistFilter; label: string }[] = [
    { key: "all", label: t("history.all") },
    { key: "completed", label: t("history.completed") },
    { key: "failed", label: t("history.failed") },
  ];

  return (
    <div>
      {/* Section header with status filter chips */}
      <div className="flex items-center gap-2 border-b border-edge bg-surface-raised px-4 py-2">
        <span className="min-w-0 flex-1 truncate font-ui text-[length:var(--t-xs)] font-semibold uppercase tracking-[0.08em] text-content-muted">
          {t("history.label")}
        </span>
        {chips.map((c) => (
          <button
            key={c.key}
            type="button"
            onClick={() => onFilter(c.key)}
            aria-pressed={filter === c.key}
            className={`shrink-0 rounded px-1.5 py-0.5 font-data text-[length:var(--t-xs)] transition-colors duration-100 ${
              filter === c.key
                ? "bg-surface-overlay text-content-primary"
                : "text-content-muted hover:text-content-secondary"
            }`}
          >
            {c.label}
          </button>
        ))}
        <span className="shrink-0 font-data tabular-nums text-[length:var(--t-xs)] text-content-muted">
          {allFiltered.length}
          {serverHasMore ? "+" : ""}
        </span>
      </div>

      {filtered.length === 0 ? (
        <div className="px-4 py-3">
          <span className="font-data text-[length:var(--t-xs)] text-content-muted">
            {t("history.empty")}
          </span>
        </div>
      ) : (
        filtered.map((row) => (
          <button
            key={row.id}
            type="button"
            onClick={() => onSelect(row.id)}
            aria-pressed={selectedId === row.id}
            className={`flex w-full items-center gap-3 border-b border-edge border-l-2 px-4 py-2 text-left transition-colors duration-100 hover:bg-surface-overlay ${
              selectedId === row.id ? "border-l-accent bg-surface-overlay" : "border-l-transparent"
            }`}
          >
            <StatusDot status={row.status} />
            <span className="min-w-0 flex-1 truncate font-data text-[length:var(--t-sm)] text-content-primary">
              {row.name}
            </span>
            <span className="shrink-0 font-data text-[length:var(--t-xs)] text-content-muted">
              {row.status.toLowerCase()}
            </span>
            <span className="min-w-[48px] shrink-0 text-right font-data tabular-nums text-[length:var(--t-xs)] text-content-muted">
              {row.endedAtSec != null
                ? t("empty.ago", {
                    delta: formatElapsed(Math.max(0, Math.floor(nowSec - row.endedAtSec))),
                  })
                : "—"}
            </span>
          </button>
        ))
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

// ─── First agent id across all units ─────────────────────────────────────────

function firstAgentId(orgUnits: OrgUnit[]): string | null {
  for (const unit of orgUnits) {
    if (unit.agents.length > 0) return unit.agents[0].id;
  }
  return null;
}

// ─── Main view ────────────────────────────────────────────────────────────────

export default function FleetView() {
  const t = useTranslations("fleet");
  const state = useFleet();

  // URL-synced selection: ?s=<runId>
  const search = Route.useSearch();
  const navigate = Route.useNavigate();
  const urlRunId = (search as { s?: string }).s ?? null;

  // Whether the user has explicitly selected a row on narrow screens
  const [narrowExplicit, setNarrowExplicit] = useState(false);
  const [histFilter, setHistFilter] = useState<HistFilter>("all");

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
  const pagerRef = useRef<HistoryPager | null>(null);
  if (pagerRef.current === null) {
    pagerRef.current = createHistoryPager((page) => listRuns({ page, per_page: HIST_PAGE_SIZE }));
  }
  const pager = pagerRef.current;

  // Polled rows win on id collision (fresher status); older pages fill the tail.
  const historyRows = useMemo(() => {
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

  const handleLoadMore = useCallback(() => {
    // Reveal already-loaded rows first; hit the server only when exhausted.
    if (histVisible < historyRows.length) {
      setHistVisible((n) => n + HIST_VISIBLE_STEP);
      return;
    }
    if (!serverHasMore || pager.inFlight()) return;
    setLoadingMore(true);
    void pager.loadNext().then((page) => {
      // null = fetch failed — leave state as-is; the sentinel retries the page.
      if (page) {
        setPagedHasMore(page.hasMore);
        setOlderRows((prev) => [...prev, ...page.rows]);
        setHistVisible((n) => n + HIST_VISIBLE_STEP);
      }
      setLoadingMore(false);
    });
  }, [histVisible, historyRows.length, serverHasMore, pager]);

  // Derive effective selection: URL param first, else auto-select first row.
  // We track whether we've done the auto-select with a ref to avoid loops.
  const autoSelectedRef = useRef<string | null>(null);
  const allAgentIds = state.orgUnits.flatMap((u) => u.agents.map((a) => a.id));
  const urlIdValid =
    urlRunId != null &&
    (allAgentIds.includes(urlRunId) || historyRows.some((r) => r.id === urlRunId));

  // Auto-select first row when data arrives and nothing is selected
  useEffect(() => {
    if (state.orgUnits.length === 0) return;
    const first = firstAgentId(state.orgUnits);
    if (!first) return;
    if (urlRunId) return; // URL already has a selection
    if (autoSelectedRef.current === first) return;
    autoSelectedRef.current = first;
    void navigate({ search: { s: first }, replace: true });
  }, [state.orgUnits, urlRunId, navigate]);

  // Resolved selected id: validated URL param, fallback to first (pre-auto-select)
  const selectedRunId: string | null = urlIdValid ? urlRunId : null;

  const handleSelectAgent = useCallback(
    (id: string) => {
      setNarrowExplicit(true);
      void navigate({ search: { s: id } });
    },
    [navigate],
  );

  const handleBack = useCallback(() => {
    setNarrowExplicit(false);
  }, []);

  const master = (
    <div className="flex h-full flex-col overflow-hidden">
      {/* Header */}
      <div className="flex items-center justify-between border-b border-edge px-4 py-3">
        <div className="flex items-center gap-3">
          <h1 className="text-[length:var(--t-md)] font-semibold text-content-primary">
            {t("page.title")}
          </h1>
          <FleetStaleBadge
            dataState={state.dataState}
            lastUpdatedMs={state.lastUpdatedMs}
            errorMessage={state.errorMessage}
          />
        </div>
        <span className="font-data text-[length:var(--t-xs)] text-content-muted">
          {t("page.subtitle")}
        </span>
      </div>

      {/* Counts strip */}
      {state.dataState !== "loading" && state.dataState !== "error" && (
        <CountsStrip
          orchestrations={state.counts.orchestrations}
          agents={state.counts.agents}
          attention={state.counts.attention}
        />
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
            onFilter={setHistFilter}
            selectedId={selectedRunId}
            onSelect={handleSelectAgent}
            nowSec={state.nowSec}
            visibleCount={histVisible}
            serverHasMore={serverHasMore}
            loadingMore={loadingMore}
            onLoadMore={handleLoadMore}
          />
        )}
      </div>
    </div>
  );

  // Nothing selectable at all → render the master column full-width. The
  // detail pane only earns the split when a live or historical session can be
  // selected; a truly empty fleet reads as one composed state.
  if (state.orgUnits.length === 0 && state.recent.length === 0) {
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
      detailActive={narrowExplicit}
      ariaLabelMaster={t("split.master")}
      ariaLabelDetail={t("split.detail")}
    />
  );
}
