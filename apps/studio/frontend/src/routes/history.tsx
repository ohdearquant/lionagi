import { createFileRoute, Link, useNavigate } from "@tanstack/react-router";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { useLocale, useTranslations } from "use-intl";
import SplitPane from "@/components/ui/SplitPane";
import SectionLabel from "@/components/ui/SectionLabel";
import EmptyState from "@/components/ui/EmptyState";
import TabBar from "@/components/shell/TabBar";
import {
  IconArrowLeft,
  IconCheck,
  IconClose,
  IconDotFilled,
  IconDotHalf,
  IconDotOutline,
  IconRun,
} from "@/components/ui/icons";
import RunDetail from "@/components/history/RunDetail";
import { listRuns } from "@/lib/api";
import type { RunSummary } from "@/lib/types";

const HISTORY_TABS = ["all", "run"] as const;
type HistoryTab = (typeof HISTORY_TABS)[number];

// URL-level status filter (?status=failed) — also the surface Leo drives when
// the operator asks to see e.g. recent failures. Each filter value covers the
// raw status spellings that mean the same outcome.
const STATUS_FILTERS = ["failed", "running", "completed", "cancelled", "pending"] as const;
type StatusFilter = (typeof STATUS_FILTERS)[number];

const STATUS_FILTER_MATCHES: Record<StatusFilter, string[]> = {
  failed: ["failed", "failure"],
  running: ["running"],
  completed: ["completed", "success"],
  cancelled: ["cancelled"],
  pending: ["pending", "queued"],
};

function matchesStatusFilter(entryStatus: string, filter: StatusFilter): boolean {
  return STATUS_FILTER_MATCHES[filter].includes(entryStatus.toLowerCase());
}

export const Route = createFileRoute("/history")({
  validateSearch: (
    search: Record<string, unknown>,
  ): { tab?: HistoryTab; sel?: string; status?: StatusFilter } => {
    const tab = search.tab;
    const sel = typeof search.sel === "string" ? search.sel : undefined;
    const status = search.status;
    return {
      ...(HISTORY_TABS.includes(tab as HistoryTab) ? { tab: tab as HistoryTab } : {}),
      ...(sel ? { sel } : {}),
      ...(STATUS_FILTERS.includes(status as StatusFilter)
        ? { status: status as StatusFilter }
        : {}),
    };
  },
  component: HistoryPage,
});

type HistoryKind = "run";

interface HistoryEntry {
  key: string;
  kind: HistoryKind;
  name: string;
  status: string;
  startedAt: number;
  endedAt?: number | null;
  raw: RunSummary;
}

const STATUS_COLORS: Record<string, string> = {
  running: "var(--status-running)",
  completed: "var(--status-success)",
  success: "var(--status-success)",
  failed: "var(--status-failure)",
  failure: "var(--status-failure)",
  cancelled: "var(--content-muted)",
  pending: "var(--status-pending)",
  queued: "var(--status-pending)",
};

const STATUS_GLYPHS: Record<string, ReactNode> = {
  running: <IconDotHalf size={10} strokeWidth={2.5} />,
  completed: <IconCheck size={11} strokeWidth={2.5} />,
  success: <IconCheck size={11} strokeWidth={2.5} />,
  failed: <IconClose size={11} strokeWidth={2.5} />,
  failure: <IconClose size={11} strokeWidth={2.5} />,
  cancelled: <IconDotOutline size={10} strokeWidth={2.5} />,
  pending: <IconDotOutline size={10} strokeWidth={2.5} />,
  queued: <IconDotOutline size={10} strokeWidth={2.5} />,
};

function statusColor(s: string) {
  return STATUS_COLORS[s.toLowerCase()] ?? "var(--content-muted)";
}

function statusGlyph(s: string): ReactNode {
  return STATUS_GLYPHS[s.toLowerCase()] ?? <IconDotFilled size={6} />;
}

function formatDuration(startMs: number, endMs?: number | null): string {
  const end = endMs ?? Date.now() / 1000;
  const diff = end - startMs;
  if (diff < 60) return `${Math.round(diff)}s`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ${Math.round(diff % 60)}s`;
  return `${Math.floor(diff / 3600)}h ${Math.floor((diff % 3600) / 60)}m`;
}

function formatDay(epochSeconds: number, locale: string, today: string, yesterday: string): string {
  const d = new Date(epochSeconds * 1000);
  const now = new Date();
  if (d.toDateString() === now.toDateString()) return today;
  const yest = new Date(now);
  yest.setDate(yest.getDate() - 1);
  if (d.toDateString() === yest.toDateString()) return yesterday;
  return d.toLocaleDateString(locale, { weekday: "long", month: "short", day: "numeric" });
}

function formatTime(epochSeconds: number, locale: string): string {
  return new Date(epochSeconds * 1000).toLocaleTimeString(locale, {
    hour: "2-digit",
    minute: "2-digit",
  });
}

const KIND_ICONS = {
  run: IconRun,
} as const;

function KindBadge({ kind }: { kind: HistoryKind }) {
  const Icon = KIND_ICONS[kind];
  return (
    <span className="inline-flex shrink-0 items-center gap-1 rounded bg-surface-overlay px-1.5 py-0.5 font-data text-[length:var(--t-xs)] font-medium uppercase tracking-[0.08em] text-content-muted">
      <Icon className="h-2.5 w-2.5" />
      {kind}
    </span>
  );
}

function useHistoryData() {
  const [entries, setEntries] = useState<HistoryEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const terminalCache = useRef<Map<string, HistoryEntry>>(new Map());

  const load = useCallback(() => {
    let alive = true;
    setLoading(true);

    listRuns({ per_page: 100 })
      .then((runsRes) => {
        if (!alive) return;

        const out: HistoryEntry[] = [];

        for (const r of runsRes.runs) {
          const e: HistoryEntry = {
            key: `run:${r.run_id}`,
            kind: "run",
            name: r.playbook_name ?? r.name ?? r.run_id,
            status: r.status,
            startedAt: r.started_at ?? 0,
            endedAt: r.ended_at ?? null,
            raw: r,
          };
          const isTerminal = ["completed", "failed", "cancelled", "success"].includes(
            r.status.toLowerCase(),
          );
          if (isTerminal) terminalCache.current.set(e.key, e);
          out.push(e);
        }

        out.sort((a, b) => b.startedAt - a.startedAt);
        setEntries(out);
        setLoading(false);
      })
      .catch(() => {
        if (!alive) return;
        setLoading(false);
      });

    return () => {
      alive = false;
    };
  }, []);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- load() calls setState inside async callbacks; synchronous reset clears stale entries before the fetch resolves
    void load();
  }, [load]);

  return { entries, loading, reload: load };
}

// ── Detail pane dispatcher ────────────────────────────────────────────────────

function DetailPane({ entry }: { entry: HistoryEntry | null }) {
  const t = useTranslations("history");
  if (!entry) {
    return (
      <div className="flex h-full items-center justify-center text-[length:var(--t-sm)] text-content-muted">
        {t("detail.empty")}
      </div>
    );
  }

  if (entry.kind === "run") {
    return <RunDetail id={entry.raw.run_id} />;
  }

  return null;
}

// ── Master list ───────────────────────────────────────────────────────────────

interface MasterListProps {
  entries: HistoryEntry[];
  loading: boolean;
  search: string;
  onSearchChange: (v: string) => void;
  onReload: () => void;
  selectedKey: string | null;
  onSelect: (key: string) => void;
  kindFilter: HistoryTab;
  statusFilter?: StatusFilter;
  onClearStatus: () => void;
  locale: string;
  t: ReturnType<typeof useTranslations<"history">>;
}

function MasterList({
  entries,
  loading,
  search,
  onSearchChange,
  onReload,
  selectedKey,
  onSelect,
  kindFilter,
  statusFilter,
  onClearStatus,
  locale,
  t,
}: MasterListProps) {
  const todayLabel = t("today");
  const yesterdayLabel = t("yesterday");

  const filtered = useMemo(() => {
    return entries.filter((e) => {
      if (kindFilter !== "all" && e.kind !== kindFilter) return false;
      if (statusFilter && !matchesStatusFilter(e.status, statusFilter)) return false;
      if (search.trim()) {
        const q = search.toLowerCase();
        if (!e.name.toLowerCase().includes(q) && !e.status.toLowerCase().includes(q)) return false;
      }
      return true;
    });
  }, [entries, kindFilter, statusFilter, search]);

  const grouped = useMemo(() => {
    const days = new Map<string, HistoryEntry[]>();
    for (const e of filtered) {
      const dayKey =
        e.startedAt > 0 ? formatDay(e.startedAt, locale, todayLabel, yesterdayLabel) : "—";
      const arr = days.get(dayKey) ?? [];
      arr.push(e);
      days.set(dayKey, arr);
    }
    return Array.from(days.entries());
  }, [filtered, locale, todayLabel, yesterdayLabel]);

  const isEmpty = !loading && filtered.length === 0;
  const isFiltered = kindFilter !== "all" || statusFilter !== undefined || search.trim().length > 0;

  return (
    <div className="flex h-full min-h-0 flex-col bg-surface-base">
      {/* Search + refresh toolbar */}
      <div className="flex shrink-0 items-center gap-2 border-b border-edge bg-surface-raised px-3 py-2">
        <input
          type="text"
          value={search}
          onChange={(e) => onSearchChange(e.target.value)}
          placeholder={t("searchPlaceholder")}
          className="min-w-0 flex-1 rounded border border-edge bg-surface-overlay px-2 py-1 text-[length:var(--t-sm)] text-content-primary focus:outline-none"
        />
        {statusFilter && (
          <button
            type="button"
            onClick={onClearStatus}
            aria-label={t("clearStatusFilter")}
            title={t("clearStatusFilter")}
            className="flex shrink-0 items-center gap-1 rounded px-2 py-1 font-data text-[length:var(--t-xs)]"
            style={{
              color: statusColor(statusFilter),
              border: `1px solid ${statusColor(statusFilter)}`,
              background: `color-mix(in srgb, ${statusColor(statusFilter)} 10%, transparent)`,
            }}
          >
            <span aria-hidden="true" className="flex items-center">
              {statusGlyph(statusFilter)}
            </span>
            {statusFilter}
            <span aria-hidden="true" className="flex items-center" style={{ opacity: 0.7 }}>
              <IconClose size={9} strokeWidth={2.5} />
            </span>
          </button>
        )}
        <button
          type="button"
          onClick={() => void onReload()}
          className="shrink-0 rounded border border-edge px-2 py-1 text-[length:var(--t-xs)] text-content-muted"
        >
          {t("refresh")}
        </button>
      </div>

      {/* Timeline */}
      <div className="flex-1 overflow-y-auto">
        {loading ? (
          <div className="flex items-center justify-center py-12 text-[length:var(--t-sm)] text-content-muted">
            {t("loading")}
          </div>
        ) : isEmpty ? (
          <EmptyState
            glyph="≡"
            title={isFiltered ? t("empty.filtered") : t("empty.all")}
            body={isFiltered ? t("empty.filteredHint") : t("empty.allHint")}
            action={
              !isFiltered ? (
                <Link
                  to="/designer"
                  className="mt-1 inline-block rounded bg-accent px-3 py-1.5 text-[length:var(--t-sm)] font-medium text-accent-contrast"
                >
                  {t("empty.cta")}
                </Link>
              ) : undefined
            }
          />
        ) : (
          <div>
            {grouped.map(([day, dayEntries]) => (
              <div key={day}>
                <div className="sticky top-0 z-[1] border-b border-edge bg-surface-base px-3 py-1">
                  <SectionLabel>{day}</SectionLabel>
                </div>
                {dayEntries.map((entry) => {
                  const isSelected = entry.key === selectedKey;
                  return (
                    <button
                      key={entry.key}
                      type="button"
                      aria-pressed={isSelected}
                      onClick={() => onSelect(entry.key)}
                      className={`flex w-full items-center gap-2 border-b border-edge border-l-2 px-3 py-2 text-left transition-colors hover:bg-surface-overlay ${
                        isSelected ? "border-l-accent bg-surface-overlay" : "border-l-transparent"
                      }`}
                    >
                      {/* Status glyph — color is data-driven, kept inline */}
                      <span
                        className="flex w-4 shrink-0 items-center justify-center"
                        style={{ color: statusColor(entry.status) }}
                      >
                        {statusGlyph(entry.status)}
                      </span>

                      {/* Kind badge */}
                      <KindBadge kind={entry.kind} />

                      {/* Name */}
                      <span className="min-w-0 flex-1 truncate font-data text-[length:var(--t-sm)] font-medium text-content-primary">
                        {entry.name}
                      </span>

                      {/* Duration */}
                      {entry.startedAt > 0 && (
                        <span className="shrink-0 font-data tabular-nums text-[length:var(--t-xs)] text-content-muted">
                          {formatDuration(entry.startedAt, entry.endedAt)}
                        </span>
                      )}

                      {/* Time */}
                      {entry.startedAt > 0 && (
                        <span className="shrink-0 font-data tabular-nums text-[length:var(--t-xs)] text-content-muted">
                          {formatTime(entry.startedAt, locale)}
                        </span>
                      )}
                    </button>
                  );
                })}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

// ── Page ──────────────────────────────────────────────────────────────────────

function HistoryPage() {
  const t = useTranslations("history");
  const locale = useLocale();
  const { entries, loading, reload } = useHistoryData();
  const navigate = useNavigate({ from: "/history" });
  const { tab: kindFilter = "all", sel, status: statusFilter } = Route.useSearch();
  const [search, setSearch] = useState("");

  // Derive filtered list (for auto-select purposes)
  const filtered = useMemo(() => {
    return entries.filter((e) => {
      if (kindFilter !== "all" && e.kind !== kindFilter) return false;
      if (statusFilter && !matchesStatusFilter(e.status, statusFilter)) return false;
      if (search.trim()) {
        const q = search.toLowerCase();
        if (!e.name.toLowerCase().includes(q) && !e.status.toLowerCase().includes(q)) return false;
      }
      return true;
    });
  }, [entries, kindFilter, statusFilter, search]);

  // Auto-select first row when list loads or tab changes, unless ?sel= already valid
  const autoSelectDone = useRef(false);
  useEffect(() => {
    if (loading) {
      autoSelectDone.current = false;
      return;
    }
    if (autoSelectDone.current) return;
    if (filtered.length === 0) return;

    // If ?sel= is already pointing at a valid entry in this filter, keep it.
    const selValid = sel && filtered.some((e) => e.key === sel);
    if (selValid) {
      autoSelectDone.current = true;
      return;
    }

    // Otherwise pick the first filtered entry.
    autoSelectDone.current = true;
    const first = filtered[0];
    if (first) {
      void navigate({
        search: (prev) => ({ ...prev, sel: first.key }),
        replace: true,
      });
    }
  }, [loading, filtered, sel, navigate]);

  // When tab or status filter changes, clear sel so auto-select can re-run.
  const prevFilterRef = useRef(`${kindFilter}|${statusFilter ?? ""}`);
  useEffect(() => {
    const key = `${kindFilter}|${statusFilter ?? ""}`;
    if (prevFilterRef.current !== key) {
      prevFilterRef.current = key;
      autoSelectDone.current = false;
    }
  }, [kindFilter, statusFilter]);

  const handleClearStatus = useCallback(() => {
    void navigate({
      search: (prev) => {
        const rest = { ...prev };
        delete rest.status;
        return rest;
      },
      replace: true,
    });
  }, [navigate]);

  const selectedEntry = useMemo(
    () => (sel ? (entries.find((e) => e.key === sel) ?? null) : null),
    [entries, sel],
  );

  // Stacked (<900px) navigation: only an explicit user selection moves to the
  // detail view — auto-select-first must not hide the list on narrow screens.
  const [explicitSel, setExplicitSel] = useState(false);
  const detailActive = explicitSel && Boolean(selectedEntry);

  const handleSelect = useCallback(
    (key: string) => {
      setExplicitSel(true);
      void navigate({
        search: (prev) => ({ ...prev, sel: key }),
        replace: true,
      });
    },
    [navigate],
  );

  const KIND_TABS: Array<{ value: HistoryTab; label: string }> = [
    { value: "all", label: t("filterAll") },
    { value: "run", label: t("filterRun") },
  ];

  const handleBack = useCallback(() => {
    setExplicitSel(false);
  }, []);

  return (
    <div className="flex h-full flex-col bg-surface-base">
      {/* Kind tabs — URL-backed so deep links and back/forward work */}
      <div className="shrink-0 px-4 pt-3">
        <TabBar
          ariaLabel={t("tabsAria")}
          tabs={KIND_TABS.map(({ value, label }) => ({
            id: value,
            label,
            to: "/history",
            search: {
              ...(value === "all" ? {} : { tab: value }),
              ...(statusFilter ? { status: statusFilter } : {}),
            },
            active: kindFilter === value,
          }))}
        />
      </div>

      {/* Split: master list | detail pane */}
      <div className="min-h-0 flex-1">
        <SplitPane
          id="history"
          defaultMasterWidth={420}
          minMasterWidth={300}
          maxMasterWidth={560}
          detailActive={detailActive}
          ariaLabelMaster={t("masterAria")}
          ariaLabelDetail={t("detailAria")}
          master={
            <MasterList
              entries={entries}
              loading={loading}
              search={search}
              onSearchChange={setSearch}
              onReload={reload}
              selectedKey={sel ?? null}
              onSelect={handleSelect}
              kindFilter={kindFilter}
              statusFilter={statusFilter}
              onClearStatus={handleClearStatus}
              locale={locale}
              t={t}
            />
          }
          detail={
            <div className="flex h-full min-h-0 flex-col">
              {/* Back affordance — stacked mode only; hidden once side-by-side */}
              {detailActive && (
                <button
                  type="button"
                  onClick={handleBack}
                  className="flex shrink-0 items-center gap-1.5 border-b border-edge bg-surface-raised px-3 py-2 text-[length:var(--t-sm)] text-content-secondary min-[960px]:hidden"
                >
                  <IconArrowLeft size={11} strokeWidth={2} /> {t("detail.back")}
                </button>
              )}
              <div className="min-h-0 flex-1 overflow-hidden">
                <DetailPane entry={selectedEntry} />
              </div>
            </div>
          }
        />
      </div>
    </div>
  );
}
