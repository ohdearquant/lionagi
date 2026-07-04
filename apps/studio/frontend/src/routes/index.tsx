import { createFileRoute } from "@tanstack/react-router";
import { useEffect, useMemo, useRef, useState } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import PageHeader from "@/components/PageHeader";
import StatusPill from "@/components/StatusPill";
import type { StatusTone } from "@/components/StatusPill";
import Timestamp from "@/components/Timestamp";
import Duration from "@/components/Duration";
import Button from "@/components/Button";
import { useProject } from "@/lib/project-context";
import { aggregateRuns, type Run, type SourceErrors } from "@/lib/run-model";
import type { DerivedStatus, RunSource } from "@/lib/derive-run-status";
import RunSlideOver from "@/components/operations/RunSlideOver";

type ViewMode = "stream" | "board" | "table";
type WindowValue = "1h" | "24h" | "7d" | "all";

interface OperationsSearch {
  view?: ViewMode;
  // Accepts string[] too — the retired list-page redirects (/runs,
  // /invocations, /kanban, /playfield, /shows) forward their own
  // multi-value `status` search param through this same shape.
  status?: string | string[];
  source?: RunSource;
  window?: WindowValue;
  q?: string;
  run?: string;
  live?: boolean;
}

function coerceView(value: unknown): ViewMode | undefined {
  return value === "stream" || value === "board" || value === "table" ? value : undefined;
}
function coerceSource(value: unknown): RunSource | undefined {
  return value === "agent" || value === "schedule" || value === "script" || value === "flow"
    ? value
    : undefined;
}
function coerceWindow(value: unknown): WindowValue | undefined {
  return value === "1h" || value === "24h" || value === "7d" || value === "all" ? value : undefined;
}
function coerceStatus(value: unknown): string | string[] | undefined {
  if (typeof value === "string") return value;
  if (Array.isArray(value)) {
    const strings = value.filter((v): v is string => typeof v === "string");
    return strings.length > 0 ? strings : undefined;
  }
  return undefined;
}
function firstStatus(status: string | string[] | undefined): string | undefined {
  return Array.isArray(status) ? status[0] : status;
}

export const Route = createFileRoute("/")({
  validateSearch: (search: Record<string, unknown>): OperationsSearch => ({
    view: coerceView(search.view),
    status: coerceStatus(search.status),
    source: coerceSource(search.source),
    window: coerceWindow(search.window),
    q: typeof search.q === "string" ? search.q : undefined,
    run: typeof search.run === "string" ? search.run : undefined,
    live: search.live === "1" || search.live === true ? true : undefined,
  }),
  component: OperationsPage,
});

function windowSeconds(w: WindowValue): number | null {
  if (w === "1h") return 3600;
  if (w === "24h") return 86400;
  if (w === "7d") return 86400 * 7;
  return null;
}

const STATUS_TONE: Record<DerivedStatus, StatusTone> = {
  running: "running",
  completed: "ok",
  failed: "failed",
  stale: "pending",
  expired: "neutral",
  cancelled: "neutral",
  pending: "pending",
};

const STATUS_LABEL: Record<DerivedStatus, string> = {
  running: "Running",
  completed: "Completed",
  failed: "Failed",
  stale: "Stale",
  expired: "Expired",
  cancelled: "Cancelled",
  pending: "Pending",
};

const SOURCE_LABEL: Record<RunSource, string> = {
  agent: "Agent",
  schedule: "Schedule",
  script: "Script",
  flow: "Flow",
};

const SOURCE_ERROR_LABEL: Record<keyof SourceErrors, string> = {
  ...SOURCE_LABEL,
  health: "Liveness check",
};

const PAGE_SIZE = 100;

function OperationsPage() {
  const search = Route.useSearch();
  const navigate = Route.useNavigate();
  const { project } = useProject();

  const view: ViewMode = search.view ?? "stream";
  const win: WindowValue = search.window ?? "24h";
  const status = firstStatus(search.status);

  const [runs, setRuns] = useState<Run[] | null>(null);
  const [sourceErrors, setSourceErrors] = useState<SourceErrors>({});
  // Set only when the aggregator itself rejects (the primary agent-run
  // fetch failed) — distinct from sourceErrors, which is per-source
  // degradation the aggregator already absorbed and still returned data for.
  const [error, setError] = useState<string | null>(null);
  // Set when a LIVE poll refresh fails outright — the canvas keeps showing
  // the last known-good `runs` rather than blanking to empty/loading, with
  // this noted so "no runs" is never confused with "couldn't refresh".
  const [staleSince, setStaleSince] = useState<number | null>(null);
  const [visibleCount, setVisibleCount] = useState(PAGE_SIZE);
  const [now, setNow] = useState(() => Math.floor(Date.now() / 1000));

  useEffect(() => {
    let active = true;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- load() calls setState, data-fetch pattern matching the rest of the codebase
    setRuns(null);
    setError(null);
    setSourceErrors({});
    setStaleSince(null);
    aggregateRuns({ project: project || undefined })
      .then((r) => {
        if (!active) return;
        setRuns(r.runs);
        setSourceErrors(r.sourceErrors);
      })
      .catch((err) => {
        if (active) setError(err instanceof Error ? err.message : String(err));
      });
    const poll = search.live
      ? setInterval(() => {
          aggregateRuns({ project: project || undefined })
            .then((r) => {
              if (!active) return;
              setRuns(r.runs);
              setSourceErrors(r.sourceErrors);
              setStaleSince(null);
            })
            .catch(() => {
              if (active) setStaleSince((prev) => prev ?? Math.floor(Date.now() / 1000));
            });
        }, 5000)
      : null;
    return () => {
      active = false;
      if (poll) clearInterval(poll);
    };
  }, [project, search.live]);

  useEffect(() => {
    const tick = setInterval(() => setNow(Math.floor(Date.now() / 1000)), 30000);
    return () => clearInterval(tick);
  }, []);

  const windowSec = windowSeconds(win);

  const windowed = useMemo(() => {
    if (!runs) return [];
    if (windowSec == null) return runs;
    return runs.filter((r) => {
      const anchor = r.updatedAt ?? r.startedAt;
      return anchor != null && now - anchor <= windowSec;
    });
  }, [runs, windowSec, now]);

  const chipCounts = useMemo(() => {
    const counts = { running: 0, failed: 0, stale: 0, slow: 0 };
    for (const r of windowed) {
      if (r.status === "running") counts.running++;
      if (r.status === "failed") counts.failed++;
      if (r.status === "stale") counts.stale++;
      if (r.isSlow) counts.slow++;
    }
    return counts;
  }, [windowed]);

  const filtered = useMemo(() => {
    const q = search.q?.trim().toLowerCase();
    return windowed.filter((r) => {
      if (status && r.status !== status) return false;
      if (search.source && r.source !== search.source) return false;
      if (q && !r.name.toLowerCase().includes(q)) return false;
      return true;
    });
  }, [windowed, status, search.source, search.q]);

  const visible = filtered.slice(0, visibleCount);
  const hasMore = filtered.length > visible.length;

  const setSearch = (patch: Partial<OperationsSearch>) => {
    void navigate({ search: (prev) => ({ ...prev, ...patch }) });
  };

  // "Slow" isn't its own status — it's a flag on running runs. Clicking the
  // chip narrows to running (the only bucket isSlow currently applies to);
  // the row-level "slow" tag does the rest of the pointing.
  const applyChipFilter = (candidate: DerivedStatus | "slow") => {
    const target = candidate === "slow" ? "running" : candidate;
    setSearch({ status: status === target ? undefined : target });
  };

  const selectedRun = search.run ? (filtered.find((r) => r.id === search.run) ?? null) : null;

  const degradedSources = useMemo(() => {
    return (Object.entries(sourceErrors) as [keyof SourceErrors, string][]).map(
      ([key, message]) => `${SOURCE_ERROR_LABEL[key]}: ${message}`,
    );
  }, [sourceErrors]);

  return (
    <div className="flex flex-1 flex-col">
      <PageHeader
        title="Operations"
        subtitle="What is happening, and what happened?"
        actions={
          <div className="flex items-center gap-1 rounded border border-edge bg-surface-overlay p-0.5 shadow-card">
            {(["stream", "board", "table"] as const).map((v) => (
              <Button
                key={v}
                size="sm"
                variant={view === v ? "primary" : "ghost"}
                onClick={() => setSearch({ view: v })}
                className={view === v ? "" : "border-transparent"}
              >
                {v}
              </Button>
            ))}
          </div>
        }
      />

      <div className="flex flex-wrap items-center gap-2 border-b border-edge px-4 py-2">
        <AttentionChip
          label="Running"
          count={chipCounts.running}
          tone="running"
          active={status === "running"}
          onClick={() => applyChipFilter("running")}
        />
        <AttentionChip
          label="Failed"
          count={chipCounts.failed}
          tone="failed"
          active={status === "failed"}
          onClick={() => applyChipFilter("failed")}
        />
        <AttentionChip
          label="Stale"
          count={chipCounts.stale}
          tone="pending"
          active={status === "stale"}
          onClick={() => applyChipFilter("stale")}
        />
        <AttentionChip
          label="Slow"
          count={chipCounts.slow}
          tone="pending"
          active={false}
          onClick={() => applyChipFilter("slow")}
        />
      </div>

      <div className="flex flex-wrap items-center gap-2 border-b border-edge px-4 py-2">
        <select
          className="h-7 rounded border border-edge bg-surface-input px-2 text-meta text-content-primary"
          value={status ?? ""}
          onChange={(e) => setSearch({ status: e.target.value || undefined })}
        >
          <option value="">All statuses</option>
          {(Object.keys(STATUS_LABEL) as DerivedStatus[]).map((s) => (
            <option key={s} value={s}>
              {STATUS_LABEL[s]}
            </option>
          ))}
        </select>
        <select
          className="h-7 rounded border border-edge bg-surface-input px-2 text-meta text-content-primary"
          value={search.source ?? ""}
          onChange={(e) =>
            setSearch({ source: (e.target.value || undefined) as RunSource | undefined })
          }
        >
          <option value="">All sources</option>
          {(Object.keys(SOURCE_LABEL) as RunSource[]).map((s) => (
            <option key={s} value={s}>
              {SOURCE_LABEL[s]}
            </option>
          ))}
        </select>
        <div className="flex items-center gap-0.5 rounded border border-edge bg-surface-overlay p-0.5">
          {(["1h", "24h", "7d", "all"] as const).map((w) => (
            <Button
              key={w}
              size="sm"
              variant={win === w ? "primary" : "ghost"}
              onClick={() => setSearch({ window: w })}
              className={win === w ? "" : "border-transparent"}
            >
              {w}
            </Button>
          ))}
        </div>
        <input
          type="text"
          placeholder="Filter by name…"
          value={search.q ?? ""}
          onChange={(e) => setSearch({ q: e.target.value || undefined })}
          className="h-7 min-w-[10rem] flex-1 rounded border border-edge bg-surface-input px-2 text-meta text-content-primary placeholder:text-content-muted"
        />
        <Button
          size="sm"
          variant={search.live ? "primary" : "ghost"}
          onClick={() => setSearch({ live: search.live ? undefined : true })}
        >
          {search.live ? "Live ●" : "Live"}
        </Button>
      </div>

      {error && (
        <div className="border-b border-edge bg-status-error-bg px-4 py-2 text-meta text-status-error">
          API unreachable — {error}
        </div>
      )}

      {degradedSources.length > 0 && (
        <div className="border-b border-edge bg-status-warning-bg px-4 py-2 text-meta text-status-warning">
          Partial data — {degradedSources.join("; ")}. Some runs may be missing.
        </div>
      )}

      {staleSince != null && (
        <div className="border-b border-edge bg-status-warning-bg px-4 py-2 text-meta text-status-warning">
          Live refresh failed — showing the last known data (as of <Timestamp value={staleSince} />
          ).
        </div>
      )}

      <div className="flex-1 overflow-hidden">
        {runs == null ? (
          <div className="p-4 text-body text-content-muted">Loading…</div>
        ) : filtered.length === 0 ? (
          <div className="p-8 text-center text-body text-content-muted">
            {degradedSources.length > 0
              ? "No runs match the current filters — note the partial-data notice above."
              : "No runs match the current filters."}
          </div>
        ) : view === "table" ? (
          <TableView runs={visible} onSelect={(id) => setSearch({ run: id })} />
        ) : view === "board" ? (
          <BoardView runs={filtered} onSelect={(id) => setSearch({ run: id })} />
        ) : (
          <StreamView runs={visible} onSelect={(id) => setSearch({ run: id })} />
        )}
        {hasMore && view !== "board" && (
          <div className="flex justify-center border-t border-edge p-3">
            <Button
              size="sm"
              variant="secondary"
              onClick={() => setVisibleCount((c) => c + PAGE_SIZE)}
            >
              Load more ({filtered.length - visible.length} remaining)
            </Button>
          </div>
        )}
      </div>

      {selectedRun && (
        <RunSlideOver run={selectedRun} onClose={() => setSearch({ run: undefined })} />
      )}
    </div>
  );
}

function AttentionChip({
  label,
  count,
  tone,
  active,
  onClick,
}: {
  label: string;
  count: number;
  tone: StatusTone;
  active: boolean;
  onClick: () => void;
}) {
  const toneClass: Record<StatusTone, string> = {
    ok: "border-status-success/40 text-status-success",
    running: "border-status-running/40 text-status-running",
    failed: "border-status-error/40 text-status-error",
    pending: "border-status-warning/40 text-status-warning",
    blocked: "border-status-selected/40 text-status-selected",
    neutral: "border-edge text-content-muted",
  };
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={[
        "flex items-center gap-1.5 rounded-full border px-3 py-1 text-meta font-medium transition-colors",
        toneClass[tone],
        active ? "bg-surface-overlay" : "bg-surface-raised hover:bg-surface-overlay",
      ].join(" ")}
    >
      <span>{label}</span>
      <span className="font-data tabular-nums">{count}</span>
    </button>
  );
}

function RunRow({ run, onSelect }: { run: Run; onSelect: (id: string) => void }) {
  return (
    <button
      type="button"
      onClick={() => onSelect(run.id)}
      className="flex w-full items-center gap-3 border-b border-edge px-4 py-2 text-left hover:bg-surface-overlay"
    >
      <StatusPill tone={STATUS_TONE[run.status]} label={STATUS_LABEL[run.status]} />
      <span className="w-16 shrink-0 text-meta uppercase text-content-muted">
        {SOURCE_LABEL[run.source]}
      </span>
      <span className="flex-1 truncate text-body text-content-primary">{run.name}</span>
      {run.isSlow && (
        <span className="rounded border border-status-warning/40 px-1.5 py-0.5 text-meta text-status-warning">
          slow
        </span>
      )}
      <span className="font-data text-meta text-content-muted">
        <Duration value={run.durationSeconds} fallback="—" />
      </span>
      <span className="w-32 shrink-0 text-right text-meta text-content-muted">
        <Timestamp value={run.updatedAt ?? run.startedAt} />
      </span>
    </button>
  );
}

function StreamView({ runs, onSelect }: { runs: Run[]; onSelect: (id: string) => void }) {
  const parentRef = useRef<HTMLDivElement>(null);
  const virtualizer = useVirtualizer({
    count: runs.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => 41,
    overscan: 12,
  });

  return (
    <div ref={parentRef} className="h-full overflow-y-auto">
      <div style={{ height: virtualizer.getTotalSize(), position: "relative", width: "100%" }}>
        {virtualizer.getVirtualItems().map((item) => {
          const run = runs[item.index];
          return (
            <div
              key={run.id}
              style={{
                position: "absolute",
                top: 0,
                left: 0,
                width: "100%",
                height: item.size,
                transform: `translateY(${item.start}px)`,
              }}
            >
              <RunRow run={run} onSelect={onSelect} />
            </div>
          );
        })}
      </div>
    </div>
  );
}

const BOARD_COLUMNS: DerivedStatus[] = [
  "pending",
  "running",
  "stale",
  "failed",
  "completed",
  "expired",
  "cancelled",
];

function BoardView({ runs, onSelect }: { runs: Run[]; onSelect: (id: string) => void }) {
  const byStatus = useMemo(() => {
    const map = new Map<DerivedStatus, Run[]>();
    for (const col of BOARD_COLUMNS) map.set(col, []);
    for (const r of runs) map.get(r.status)?.push(r);
    return map;
  }, [runs]);

  return (
    <div className="flex h-full gap-3 overflow-x-auto p-3">
      {BOARD_COLUMNS.map((col) => {
        const colRuns = byStatus.get(col) ?? [];
        return (
          <div
            key={col}
            className="flex w-64 shrink-0 flex-col rounded border border-edge bg-surface-raised"
          >
            <div className="flex items-center justify-between border-b border-edge px-3 py-2">
              <span className="text-label text-content-primary">{STATUS_LABEL[col]}</span>
              <span className="font-data text-meta text-content-muted">{colRuns.length}</span>
            </div>
            <BoardColumnList runs={colRuns} onSelect={onSelect} />
          </div>
        );
      })}
    </div>
  );
}

function BoardColumnList({ runs, onSelect }: { runs: Run[]; onSelect: (id: string) => void }) {
  const parentRef = useRef<HTMLDivElement>(null);
  const virtualizer = useVirtualizer({
    count: runs.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => 56,
    overscan: 8,
  });

  return (
    <div ref={parentRef} className="flex-1 overflow-y-auto">
      <div style={{ height: virtualizer.getTotalSize(), position: "relative", width: "100%" }}>
        {virtualizer.getVirtualItems().map((item) => {
          const run = runs[item.index];
          return (
            <div
              key={run.id}
              style={{
                position: "absolute",
                top: 0,
                left: 0,
                width: "100%",
                height: item.size,
                transform: `translateY(${item.start}px)`,
              }}
              className="px-2 py-1"
            >
              <button
                type="button"
                onClick={() => onSelect(run.id)}
                className="w-full rounded border border-edge bg-surface-overlay p-2 text-left hover:border-edge-strong"
              >
                <div className="truncate text-body text-content-primary">{run.name}</div>
                <div className="mt-1 flex items-center justify-between">
                  <span className="font-data text-meta text-content-muted">
                    <Duration value={run.durationSeconds} fallback="—" />
                  </span>
                  {run.isSlow && <span className="text-meta text-status-warning">slow</span>}
                </div>
              </button>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function TableView({ runs, onSelect }: { runs: Run[]; onSelect: (id: string) => void }) {
  return (
    <div className="h-full overflow-auto">
      <table className="w-full border-collapse text-body">
        <thead className="sticky top-0 bg-surface-raised text-meta text-content-muted">
          <tr>
            <th className="border-b border-edge px-3 py-2 text-left">Run</th>
            <th className="border-b border-edge px-3 py-2 text-left">Source</th>
            <th className="border-b border-edge px-3 py-2 text-left">Status</th>
            <th className="border-b border-edge px-3 py-2 text-left">Duration</th>
            <th className="border-b border-edge px-3 py-2 text-left">Updated</th>
          </tr>
        </thead>
        <tbody>
          {runs.map((run) => (
            <tr
              key={run.id}
              onClick={() => onSelect(run.id)}
              className="cursor-pointer border-b border-edge hover:bg-surface-overlay"
            >
              <td className="max-w-xs truncate px-3 py-2 text-content-primary">{run.name}</td>
              <td className="px-3 py-2 text-meta uppercase text-content-muted">
                {SOURCE_LABEL[run.source]}
              </td>
              <td className="px-3 py-2">
                <StatusPill tone={STATUS_TONE[run.status]} label={STATUS_LABEL[run.status]} />
              </td>
              <td className="px-3 py-2 font-data text-content-muted">
                <Duration value={run.durationSeconds} fallback="—" />
              </td>
              <td className="px-3 py-2 text-content-muted">
                <Timestamp value={run.updatedAt ?? run.startedAt} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
