/**
 * Definitions space — browses every versioned agent/playbook definition
 * across kinds, with a bulk "snapshot all" checkpoint action. Per-item
 * editing already lives on the Library page (AgentDetail's version
 * history/rollback UI); rows here link straight into it rather than
 * duplicating that editor.
 */
import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { useCallback, useEffect, useState } from "react";
import { useTranslations } from "use-intl";
import Button from "@/components/ui/Button";
import EmptyState from "@/components/ui/EmptyState";
import Timestamp from "@/components/ui/Timestamp";
import { listDefinitions, snapshotDefinitions } from "@/lib/api";
import type { DefinitionSummary } from "@/lib/api";

export interface DefinitionsRouteSearch {
  kind?: string;
}

export function validateDefinitionsSearch(search: Record<string, unknown>): DefinitionsRouteSearch {
  return typeof search.kind === "string" && search.kind ? { kind: search.kind } : {};
}

export const Route = createFileRoute("/definitions/")({
  validateSearch: validateDefinitionsSearch,
  component: DefinitionsSpace,
});

const KINDS = ["agent", "playbook"] as const;

/** Where a definition's existing per-item editor already lives — Library. */
export function libraryHref(def: DefinitionSummary): { tab: "agent" | "playbook"; sel: string } {
  if (def.kind === "playbook") {
    return { tab: "playbook", sel: `playbook:custom:${def.name}` };
  }
  return { tab: "agent", sel: `agent:${def.name}` };
}

function useDefinitionsData(kind: string) {
  const [defs, setDefs] = useState<DefinitionSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);

  const load = useCallback(() => {
    let alive = true;
    setLoading(true);
    setError(false);
    listDefinitions(kind || undefined)
      .then((res) => {
        if (alive) setDefs(res.definitions);
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
  }, [kind]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- load() calls setState inside async callbacks; synchronous reset clears stale definitions before the fetch resolves
    return load();
  }, [load]);

  return { defs, loading, error, refresh: load };
}

function DefinitionsSpace() {
  const t = useTranslations("definitions");
  const tDaemon = useTranslations("daemon");
  const navigate = useNavigate({ from: "/definitions/" });
  const search = Route.useSearch();
  const kind = search.kind ?? "";

  const { defs, loading, error, refresh } = useDefinitionsData(kind);
  const [snapshotting, setSnapshotting] = useState(false);
  const [snapshotResult, setSnapshotResult] = useState<string | null>(null);

  const openInLibrary = (def: DefinitionSummary) => {
    void navigate({ to: "/library", search: libraryHref(def) });
  };

  function setKind(next: string) {
    void navigate({
      to: "/definitions",
      search: next ? { kind: next } : {},
      replace: true,
    });
  }

  async function handleSnapshotAll() {
    setSnapshotting(true);
    setSnapshotResult(null);
    try {
      const res = await snapshotDefinitions(kind || undefined);
      setSnapshotResult(t("snapshotDone", { count: res.snapshots_created }));
      refresh();
    } catch {
      setSnapshotResult(t("snapshotFailed"));
    } finally {
      setSnapshotting(false);
    }
  }

  return (
    <main className="flex h-full w-full flex-col animate-page-enter">
      <header className="flex shrink-0 flex-col gap-3 px-4 pb-4 pt-5 sm:flex-row sm:items-end sm:justify-between sm:px-6">
        <div className="flex flex-col gap-0.5">
          <h1 className="text-page-title font-semibold text-content-primary">{t("title")}</h1>
          <p className="text-body text-content-muted">{t("subtitle")}</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <select
            value={kind}
            onChange={(e) => setKind(e.target.value)}
            aria-label={t("filterKind")}
            className="rounded border border-edge bg-surface-overlay px-2 py-1 text-body text-content-primary focus:outline-none"
          >
            <option value="">{t("filterKindAll")}</option>
            {KINDS.map((k) => (
              <option key={k} value={k}>
                {k}
              </option>
            ))}
          </select>
          <Button
            variant="primary"
            size="sm"
            onClick={() => void handleSnapshotAll()}
            disabled={snapshotting}
          >
            {snapshotting ? t("snapshotting") : t("snapshotAll")}
          </Button>
        </div>
      </header>

      {snapshotResult && (
        <div className="mx-4 mb-3 rounded border border-edge bg-surface-raised px-3 py-2 text-body text-content-secondary sm:mx-6">
          {snapshotResult}
        </div>
      )}

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
      ) : defs.length === 0 ? (
        <EmptyState glyph="▤" title={t("emptyTitle")} body={t("emptyBody")} className="pb-16" />
      ) : (
        <div className="min-h-0 flex-1 overflow-y-auto px-4 pb-6 sm:px-6">
          <table className="w-full text-left" style={{ borderCollapse: "collapse" }}>
            <thead>
              <tr className="border-b border-edge text-[length:var(--t-xs)] uppercase tracking-[0.08em] text-content-muted">
                <th className="py-2 pr-2 font-medium">{t("table.kind")}</th>
                <th className="py-2 pr-2 font-medium">{t("table.name")}</th>
                <th className="py-2 pr-2 font-medium">{t("table.version")}</th>
                <th className="py-2 pr-2 font-medium">{t("table.updated")}</th>
              </tr>
            </thead>
            <tbody>
              {defs.map((def) => (
                <tr
                  key={`${def.kind}:${def.name}`}
                  onClick={() => openInLibrary(def)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      openInLibrary(def);
                    }
                  }}
                  tabIndex={0}
                  className="cursor-pointer border-b border-edge-subtle hover:bg-surface-overlay focus-visible:outline focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-accent"
                >
                  <td className="py-2.5 pr-2 text-body text-content-secondary">{def.kind}</td>
                  <td className="py-2.5 pr-2 font-data text-[length:var(--t-base)] text-content-primary">
                    {def.name}
                  </td>
                  <td className="py-2.5 pr-2 text-body text-content-secondary">
                    {def.has_versions ? `v${def.version}` : t("table.noVersions")}
                  </td>
                  <td className="py-2.5 pr-2 text-body text-content-muted">
                    <Timestamp value={def.updated_at} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </main>
  );
}
