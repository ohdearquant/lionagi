import { useNavigate } from "@tanstack/react-router";
import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslations } from "use-intl";
import Button from "@/components/ui/Button";
import EmptyState from "@/components/ui/EmptyState";
import Modal from "@/components/ui/Modal";
import Timestamp from "@/components/ui/Timestamp";
import { listEngineRuns, getEngineRun } from "@/lib/api";
import type { EngineRunDetail, EngineRunSummary } from "@/lib/api";

export interface EngineRunsRouteSearch {
  kind?: string;
  status?: string;
  session_id?: string;
  s?: string;
}

const STATUSES = ["running", "completed", "failed", "cancelled"] as const;

const PAGE_SIZE = 100;

function useEngineRunsData(kind: string, status: string, sessionId: string) {
  const [runs, setRuns] = useState<EngineRunSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [hasMore, setHasMore] = useState(false);
  const [error, setError] = useState(false);
  const generationRef = useRef(0);
  const nextCursorRef = useRef<string | null>(null);
  const loadingMoreRef = useRef(false);

  const load = useCallback(() => {
    const generation = ++generationRef.current;
    // A new first page supersedes any outstanding page for the old filters.
    // The request itself cannot be aborted through this API, so generation
    // guards discard its result while these refs immediately unlock the new
    // result set's pagination controls.
    loadingMoreRef.current = false;
    setLoadingMore(false);
    setLoading(true);
    setError(false);
    listEngineRuns({
      kind: kind.trim() || undefined,
      status: status.trim() || undefined,
      session_id: sessionId.trim() || undefined,
      limit: PAGE_SIZE,
    })
      .then((page) => {
        if (generation !== generationRef.current) return;
        setRuns(page.items);
        nextCursorRef.current = page.next_cursor;
        setHasMore(page.next_cursor != null);
      })
      .catch(() => {
        if (generation === generationRef.current) setError(true);
      })
      .finally(() => {
        if (generation === generationRef.current) setLoading(false);
      });
    return () => {
      if (generation === generationRef.current) generationRef.current += 1;
    };
  }, [kind, status, sessionId]);

  const loadMore = useCallback(async () => {
    if (loadingMoreRef.current) return;
    const cursor = nextCursorRef.current;
    if (cursor == null) return;
    loadingMoreRef.current = true;
    setLoadingMore(true);
    const generation = generationRef.current;
    try {
      const page = await listEngineRuns({
        kind: kind.trim() || undefined,
        status: status.trim() || undefined,
        session_id: sessionId.trim() || undefined,
        limit: PAGE_SIZE,
        cursor,
      });
      if (generation !== generationRef.current) return;
      nextCursorRef.current = page.next_cursor;
      setRuns((current) => {
        const seen = new Set(current.map((run) => run.id));
        return [...current, ...page.items.filter((run) => !seen.has(run.id))];
      });
      setHasMore(page.next_cursor != null);
    } catch {
      if (generation === generationRef.current) setError(true);
    } finally {
      if (generation === generationRef.current) {
        loadingMoreRef.current = false;
        setLoadingMore(false);
      }
    }
  }, [kind, status, sessionId]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- load() calls setState inside async callbacks; synchronous reset clears stale runs before the fetch resolves
    return load();
  }, [load]);

  return { runs, loading, loadingMore, hasMore, error, refresh: load, loadMore };
}

export default function EngineRunsSpace({ search }: { search: EngineRunsRouteSearch }) {
  const t = useTranslations("engineRuns");
  const tDaemon = useTranslations("daemon");
  const tPagination = useTranslations("fleet.history");
  const navigate = useNavigate({ from: "/engine-runs/" });

  const [kind, setKind] = useState(search.kind ?? "");
  const [status, setStatus] = useState(search.status ?? "");
  const [sessionId, setSessionId] = useState(search.session_id ?? "");
  const [appliedFilters, setAppliedFilters] = useState(() => ({
    kind: search.kind ?? "",
    status: search.status ?? "",
    sessionId: search.session_id ?? "",
  }));

  // The session_id filter is the deep-link case (a run's detail view linking
  // "engine runs for this session"); keep the input in sync if it changes
  // out from under us via navigation rather than a local edit.
  useEffect(() => {
    /* eslint-disable react-hooks/set-state-in-effect -- deliberately re-derives draft and applied filters from external URL navigation */
    setKind(search.kind ?? "");
    setStatus(search.status ?? "");
    setSessionId(search.session_id ?? "");
    setAppliedFilters({
      kind: search.kind ?? "",
      status: search.status ?? "",
      sessionId: search.session_id ?? "",
    });
    /* eslint-enable react-hooks/set-state-in-effect */
  }, [search.kind, search.status, search.session_id]);

  const { runs, loading, loadingMore, hasMore, error, refresh, loadMore } = useEngineRunsData(
    appliedFilters.kind,
    appliedFilters.status,
    appliedFilters.sessionId,
  );

  function applyFilters(e: React.FormEvent) {
    e.preventDefault();
    const next = {
      kind: kind.trim(),
      status: status.trim(),
      sessionId: sessionId.trim(),
    };
    setAppliedFilters(next);
    void navigate({
      to: "/engine-runs",
      search: (prev) => ({
        ...prev,
        kind: next.kind || undefined,
        status: next.status || undefined,
        session_id: next.sessionId || undefined,
      }),
      replace: true,
    });
  }

  const openRun = (id: string) => {
    void navigate({ to: "/engine-runs", search: (prev) => ({ ...prev, s: id }) });
  };
  const closeRun = () => {
    void navigate({ to: "/engine-runs", search: ({ s: _s, ...rest }) => rest, replace: true });
  };

  return (
    <main className="flex h-full w-full flex-col animate-page-enter">
      <header className="flex shrink-0 flex-col gap-3 px-4 pb-4 pt-5 sm:px-6">
        <div className="flex flex-col gap-0.5">
          <h1 className="text-page-title font-semibold text-content-primary">{t("title")}</h1>
          <p className="text-body text-content-muted">{t("subtitle")}</p>
        </div>
        <form onSubmit={applyFilters} className="flex flex-wrap items-center gap-2">
          <input
            type="text"
            value={kind}
            onChange={(e) => setKind(e.target.value)}
            placeholder={t("filterKind")}
            aria-label={t("filterKind")}
            className="min-w-0 flex-1 rounded border border-edge bg-surface-overlay px-2 py-1 text-body text-content-primary focus:outline-none"
          />
          <select
            value={status}
            onChange={(e) => setStatus(e.target.value)}
            aria-label={t("filterStatus")}
            className="rounded border border-edge bg-surface-overlay px-2 py-1 text-body text-content-primary focus:outline-none"
          >
            <option value="">{t("filterStatusAll")}</option>
            {STATUSES.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
          <input
            type="text"
            value={sessionId}
            onChange={(e) => setSessionId(e.target.value)}
            placeholder={t("filterSession")}
            aria-label={t("filterSession")}
            className="min-w-0 flex-1 rounded border border-edge bg-surface-overlay px-2 py-1 text-body text-content-primary focus:outline-none"
          />
          <Button type="submit" variant="secondary" size="sm">
            {t("filterApply")}
          </Button>
        </form>
      </header>

      {error && (
        <div
          role="alert"
          className="mx-4 mb-3 flex flex-wrap items-center justify-between gap-3 rounded border border-status-error/30 bg-status-error-bg px-3 py-2 text-body text-content-secondary sm:mx-6"
        >
          <span>{t("loadError")}</span>
          <Button variant="secondary" size="sm" onClick={refresh}>
            {tDaemon("retry")}
          </Button>
        </div>
      )}

      {loading ? (
        <div className="flex min-h-0 flex-1 flex-col gap-2 px-4 pb-6 sm:px-6">
          {Array.from({ length: 4 }, (_, i) => (
            <div key={i} className="skeleton h-10 rounded-md" />
          ))}
        </div>
      ) : runs.length === 0 ? (
        <EmptyState glyph="⚙" title={t("emptyTitle")} body={t("emptyBody")} className="pb-16" />
      ) : (
        <div className="min-h-0 flex-1 overflow-y-auto px-4 pb-6 sm:px-6">
          <table className="w-full text-left" style={{ borderCollapse: "collapse" }}>
            <thead>
              <tr className="border-b border-edge text-[length:var(--t-xs)] uppercase tracking-[0.08em] text-content-muted">
                <th className="py-2 pr-2 font-medium">{t("table.kind")}</th>
                <th className="py-2 pr-2 font-medium">{t("table.status")}</th>
                <th className="py-2 pr-2 font-medium">{t("table.started")}</th>
                <th className="py-2 pr-2 font-medium">{t("table.session")}</th>
              </tr>
            </thead>
            <tbody>
              {runs.map((run) => (
                <tr
                  key={run.id}
                  onClick={() => openRun(run.id)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      openRun(run.id);
                    }
                  }}
                  tabIndex={0}
                  className="cursor-pointer border-b border-edge-subtle hover:bg-surface-overlay focus-visible:outline focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-accent"
                >
                  <td className="py-2.5 pr-2 font-data text-[length:var(--t-base)] text-content-primary">
                    {run.kind}
                  </td>
                  <td className="py-2.5 pr-2 text-body text-content-secondary">
                    <span
                      className={
                        run.status === "failed"
                          ? "text-status-error"
                          : run.status === "completed"
                            ? "text-status-success"
                            : "text-content-secondary"
                      }
                    >
                      {run.status}
                    </span>
                  </td>
                  <td className="py-2.5 pr-2 text-body text-content-muted">
                    <Timestamp value={run.started_at} />
                  </td>
                  <td className="py-2.5 pr-2 font-mono text-meta text-content-muted">
                    {run.session_id ?? "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {hasMore && (
            <div className="flex justify-center py-4">
              <Button
                variant="secondary"
                size="sm"
                onClick={() => void loadMore()}
                disabled={loadingMore}
              >
                {loadingMore ? tPagination("loadingMore") : tPagination("loadMore")}
              </Button>
            </div>
          )}
        </div>
      )}

      {search.s && <EngineRunDetailModal runId={search.s} onClose={closeRun} />}
    </main>
  );
}

function EngineRunDetailModal({ runId, onClose }: { runId: string; onClose: () => void }) {
  const t = useTranslations("engineRuns");
  const [run, setRun] = useState<EngineRunDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [revealing, setRevealing] = useState(false);
  const [error, setError] = useState(false);

  const shownRunId = useRef(runId);

  useEffect(() => {
    let alive = true;
    shownRunId.current = runId;
    /* eslint-disable react-hooks/set-state-in-effect -- synchronous resets clear stale state before the async fetch resolves */
    setLoading(true);
    setError(false);
    /* eslint-enable react-hooks/set-state-in-effect */
    getEngineRun(runId)
      .then((d) => {
        if (alive) setRun(d);
      })
      .catch(() => {
        if (alive) setError(true);
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [runId]);

  const revealSpec = () => {
    // The modal is reused across selections, so a reveal for the run the user
    // just left would otherwise resolve into the run they just opened.
    const revealedFor = runId;
    setRevealing(true);
    getEngineRun(runId, { includeSpec: true })
      .then((d) => {
        if (shownRunId.current === revealedFor) setRun(d);
      })
      .finally(() => {
        if (shownRunId.current === revealedFor) setRevealing(false);
      });
  };

  return (
    <Modal
      title={run?.kind ?? runId}
      closeLabel={t("detail.close")}
      onClose={onClose}
      maxWidth="max-w-lg"
    >
      <div className="max-h-[70vh] overflow-y-auto px-5 py-4">
        {loading ? (
          <div className="space-y-2">
            <div className="skeleton h-4 w-2/3 rounded" />
            <div className="skeleton h-4 w-1/2 rounded" />
          </div>
        ) : error || !run ? (
          <p className="text-body text-content-secondary">{t("detail.notFound")}</p>
        ) : (
          <div className="flex flex-col gap-4">
            <div className="flex flex-wrap gap-x-5 gap-y-2 text-[length:var(--t-xs)]">
              <div className="flex items-center gap-1.5">
                <span className="text-content-muted">{t("table.status")}</span>
                <span className="font-data text-content-primary">{run.status}</span>
              </div>
              <div className="flex items-center gap-1.5">
                <span className="text-content-muted">{t("detail.started")}</span>
                <Timestamp value={run.started_at} exact />
              </div>
              {run.ended_at != null && (
                <div className="flex items-center gap-1.5">
                  <span className="text-content-muted">{t("detail.ended")}</span>
                  <Timestamp value={run.ended_at} exact />
                </div>
              )}
              {run.session_id && (
                <div className="flex items-center gap-1.5">
                  <span className="text-content-muted">{t("table.session")}</span>
                  <span className="font-mono text-content-primary">{run.session_id}</span>
                </div>
              )}
              {run.export_dir && (
                <div className="flex items-center gap-1.5">
                  <span className="text-content-muted">{t("detail.exportDir")}</span>
                  <span className="font-mono text-content-primary">{run.export_dir}</span>
                </div>
              )}
            </div>

            {run.error && (
              <div>
                <p className="text-[length:var(--t-xs)] uppercase tracking-[0.08em] text-content-muted">
                  {t("detail.error")}
                </p>
                <p className="mt-1 whitespace-pre-wrap text-body text-status-error">{run.error}</p>
              </div>
            )}

            <div>
              <p className="text-[length:var(--t-xs)] uppercase tracking-[0.08em] text-content-muted">
                {t("detail.spec")}
              </p>
              <pre className="mt-1.5 overflow-x-auto rounded border border-edge bg-surface-overlay p-2.5 font-mono text-meta text-content-secondary">
                {JSON.stringify(run.spec_json ?? run.spec_preview, null, 2)}
              </pre>
              {run.spec_json == null && (
                <Button
                  className="mt-2"
                  variant="secondary"
                  size="sm"
                  disabled={revealing}
                  onClick={revealSpec}
                >
                  {t("detail.spec")}
                </Button>
              )}
            </div>
          </div>
        )}
      </div>
    </Modal>
  );
}
