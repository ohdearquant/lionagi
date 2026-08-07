/**
 * Engine runs space — browses the engine_runs table: individual engine
 * (model/tool) invocations, a lower-level run type than orchestration
 * sessions. Filters mirror the backend's kind/status/session_id params so a
 * deep link from a session's detail view can scope straight to its runs.
 */
import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { useCallback, useEffect, useState } from "react";
import { useTranslations } from "use-intl";
import Button from "@/components/ui/Button";
import EmptyState from "@/components/ui/EmptyState";
import Modal from "@/components/ui/Modal";
import Timestamp from "@/components/ui/Timestamp";
import { listEngineRuns, getEngineRun } from "@/lib/api";
import type { EngineRunSummary } from "@/lib/api";

export interface EngineRunsRouteSearch {
  kind?: string;
  status?: string;
  session_id?: string;
  s?: string;
}

function str(v: unknown): string | undefined {
  return typeof v === "string" && v ? v : undefined;
}

// Backend enum (lionagi/state/schema_meta.py) — a value outside this set is
// never emitted, so accepting it here means the filter silently matches
// nothing while looking like a legitimate empty result.
const STATUSES = ["running", "completed", "failed", "cancelled"] as const;

function engineStatus(v: unknown): (typeof STATUSES)[number] | undefined {
  const s = str(v);
  return s !== undefined && (STATUSES as readonly string[]).includes(s)
    ? (s as (typeof STATUSES)[number])
    : undefined;
}

export function validateEngineRunsSearch(search: Record<string, unknown>): EngineRunsRouteSearch {
  // A present-but-invalid status (wrong type, or a value the backend never
  // emits) must not silently disappear into "no filter" — that would widen
  // the request to every run instead of rejecting the bad input.
  if (
    search.status !== undefined &&
    search.status !== "" &&
    engineStatus(search.status) === undefined
  ) {
    throw new Error(`invalid engine run status: ${JSON.stringify(search.status)}`);
  }
  return {
    ...(str(search.kind) ? { kind: str(search.kind) } : {}),
    ...(engineStatus(search.status) ? { status: engineStatus(search.status) } : {}),
    ...(str(search.session_id) ? { session_id: str(search.session_id) } : {}),
    ...(str(search.s) ? { s: str(search.s) } : {}),
  };
}

export const Route = createFileRoute("/engine-runs/")({
  validateSearch: validateEngineRunsSearch,
  component: EngineRunsSpace,
});
const PAGE_SIZE = 100;

function useEngineRunsData(kind: string, status: string, sessionId: string) {
  const [runs, setRuns] = useState<EngineRunSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);

  const load = useCallback(() => {
    let alive = true;
    setLoading(true);
    setError(false);
    listEngineRuns({
      kind: kind.trim() || undefined,
      status: status.trim() || undefined,
      session_id: sessionId.trim() || undefined,
      limit: PAGE_SIZE,
    })
      .then((res) => {
        if (alive) setRuns(res);
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
  }, [kind, status, sessionId]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- load() calls setState inside async callbacks; synchronous reset clears stale runs before the fetch resolves
    return load();
  }, [load]);

  return { runs, loading, error, refresh: load };
}

function EngineRunsSpace() {
  const t = useTranslations("engineRuns");
  const tDaemon = useTranslations("daemon");
  const navigate = useNavigate({ from: "/engine-runs/" });
  const search = Route.useSearch();

  const [kind, setKind] = useState(search.kind ?? "");
  const [status, setStatus] = useState(search.status ?? "");
  const [sessionId, setSessionId] = useState(search.session_id ?? "");

  // The session_id filter is the deep-link case (a run's detail view linking
  // "engine runs for this session"); keep the input in sync if it changes
  // out from under us via navigation rather than a local edit.
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- deliberately re-derives the input from the URL when navigation (not local typing) changes it
    setSessionId(search.session_id ?? "");
  }, [search.session_id]);

  const { runs, loading, error, refresh } = useEngineRunsData(kind, status, sessionId);

  function applyFilters(e: React.FormEvent) {
    e.preventDefault();
    void navigate({
      to: "/engine-runs",
      search: (prev) => ({
        ...prev,
        kind: kind || undefined,
        status: status || undefined,
        session_id: sessionId || undefined,
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
        </div>
      )}

      {search.s && <EngineRunDetailModal runId={search.s} onClose={closeRun} />}
    </main>
  );
}

function EngineRunDetailModal({ runId, onClose }: { runId: string; onClose: () => void }) {
  const t = useTranslations("engineRuns");
  const [run, setRun] = useState<EngineRunSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);

  useEffect(() => {
    let alive = true;
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
                {JSON.stringify(run.spec_json, null, 2)}
              </pre>
            </div>
          </div>
        )}
      </div>
    </Modal>
  );
}
