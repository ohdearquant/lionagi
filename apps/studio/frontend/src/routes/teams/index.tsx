/**
 * Teams space — read-only browse of `li team` crews: name, member count,
 * last-modified time, and (on selection) the full member list and inbox.
 * Both backend routes are read-only today, so this surface has no write
 * actions to wire up alongside them.
 */
import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { useCallback, useEffect, useState } from "react";
import { useTranslations } from "use-intl";
import Button from "@/components/ui/Button";
import EmptyState from "@/components/ui/EmptyState";
import Modal from "@/components/ui/Modal";
import Timestamp from "@/components/ui/Timestamp";
import { listTeams, getTeam } from "@/lib/api";
import type { TeamSummary, TeamDetail } from "@/lib/api";

export interface TeamsRouteSearch {
  s?: string;
}

export function validateTeamsSearch(search: Record<string, unknown>): TeamsRouteSearch {
  return typeof search.s === "string" && search.s ? { s: search.s } : {};
}

export const Route = createFileRoute("/teams/")({
  validateSearch: validateTeamsSearch,
  component: TeamsSpace,
});

const PAGE_SIZE = 100;

function useTeamsData() {
  const [teams, setTeams] = useState<TeamSummary[]>([]);
  const [total, setTotal] = useState(0);
  const [hasNext, setHasNext] = useState(false);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState(false);

  const load = useCallback(() => {
    let alive = true;
    setLoading(true);
    setError(false);
    listTeams({ limit: PAGE_SIZE, offset: 0 })
      .then((res) => {
        if (!alive) return;
        setTeams(res.teams);
        setTotal(res.total);
        setHasNext(res.has_next);
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
  }, []);

  const loadMore = useCallback(async () => {
    setLoadingMore(true);
    try {
      const res = await listTeams({ limit: PAGE_SIZE, offset: teams.length });
      setTeams((prev) => [...prev, ...res.teams]);
      setHasNext(res.has_next);
    } catch {
      /* leave the current page as-is; the button stays for a retry */
    } finally {
      setLoadingMore(false);
    }
  }, [teams.length]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- load() calls setState inside async callbacks; synchronous reset clears stale teams before the fetch resolves
    return load();
  }, [load]);

  return { teams, total, hasNext, loading, loadingMore, error, refresh: load, loadMore };
}

function formatMemberCount(t: ReturnType<typeof useTranslations>, count: number): string {
  return count === 1 ? t("table.oneMember") : t("table.memberCount", { count });
}

function TeamsSpace() {
  const t = useTranslations("teams");
  const tDaemon = useTranslations("daemon");
  const { teams, hasNext, loading, loadingMore, error, refresh, loadMore } = useTeamsData();
  const navigate = useNavigate({ from: "/teams/" });
  const search = Route.useSearch();

  const openTeam = (id: string) => {
    void navigate({ to: "/teams", search: (prev) => ({ ...prev, s: id }) });
  };
  const closeTeam = () => {
    void navigate({ to: "/teams", search: ({ s: _s, ...rest }) => rest, replace: true });
  };

  return (
    <main className="flex h-full w-full flex-col animate-page-enter">
      <header className="flex shrink-0 flex-col gap-0.5 px-4 pb-4 pt-5 sm:px-6">
        <h1 className="text-page-title font-semibold text-content-primary">{t("title")}</h1>
        <p className="text-body text-content-muted">{t("subtitle")}</p>
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
      ) : teams.length === 0 ? (
        <EmptyState glyph="◈" title={t("emptyTitle")} body={t("emptyBody")} className="pb-16" />
      ) : (
        <div className="min-h-0 flex-1 overflow-y-auto px-4 pb-6 sm:px-6">
          <table className="w-full text-left" style={{ borderCollapse: "collapse" }}>
            <thead>
              <tr className="border-b border-edge text-[length:var(--t-xs)] uppercase tracking-[0.08em] text-content-muted">
                <th className="py-2 pr-2 font-medium">{t("table.name")}</th>
                <th className="py-2 pr-2 font-medium">{t("table.members")}</th>
                <th className="py-2 pr-2 font-medium">{t("table.lastModified")}</th>
              </tr>
            </thead>
            <tbody>
              {teams.map((team) => (
                <tr
                  key={team.id}
                  onClick={() => openTeam(team.id)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      openTeam(team.id);
                    }
                  }}
                  tabIndex={0}
                  className="cursor-pointer border-b border-edge-subtle hover:bg-surface-overlay focus-visible:outline focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-accent"
                >
                  <td className="py-2.5 pr-2 font-data text-[length:var(--t-base)] text-content-primary">
                    {team.name}
                  </td>
                  <td className="py-2.5 pr-2 text-body text-content-secondary">
                    {formatMemberCount(t, team.member_count)}
                  </td>
                  <td className="py-2.5 pr-2 text-body text-content-muted">
                    <Timestamp value={team.last_modified} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          {hasNext && (
            <div className="flex justify-center py-4">
              <Button
                variant="secondary"
                size="sm"
                onClick={() => void loadMore()}
                disabled={loadingMore}
              >
                {loadingMore ? t("loadingMore") : t("loadMore")}
              </Button>
            </div>
          )}
        </div>
      )}

      {search.s && <TeamDetailModal teamId={search.s} onClose={closeTeam} />}
    </main>
  );
}

function TeamDetailModal({ teamId, onClose }: { teamId: string; onClose: () => void }) {
  const t = useTranslations("teams");
  const [team, setTeam] = useState<TeamDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);

  useEffect(() => {
    let alive = true;
    /* eslint-disable react-hooks/set-state-in-effect -- synchronous resets clear stale state before the async fetch resolves */
    setLoading(true);
    setError(false);
    /* eslint-enable react-hooks/set-state-in-effect */
    getTeam(teamId)
      .then((d) => {
        if (alive) setTeam(d);
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
  }, [teamId]);

  return (
    <Modal
      title={team?.name ?? teamId}
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
        ) : error || !team ? (
          <p className="text-body text-content-secondary">{t("loadError")}</p>
        ) : (
          <div className="flex flex-col gap-4">
            <div>
              <p className="text-[length:var(--t-xs)] uppercase tracking-[0.08em] text-content-muted">
                {t("detail.members")}
              </p>
              <div className="mt-1.5 flex flex-wrap gap-1.5">
                {team.members.map((m) => (
                  <span
                    key={m}
                    className="rounded-full border border-edge bg-surface-overlay px-2.5 py-1 font-data text-[length:var(--t-xs)] text-content-primary"
                  >
                    {m}
                  </span>
                ))}
              </div>
            </div>

            <div>
              <p className="text-[length:var(--t-xs)] uppercase tracking-[0.08em] text-content-muted">
                {t("detail.created")}
              </p>
              <p className="mt-1 text-body text-content-secondary">
                <Timestamp value={team.created_at} exact />
              </p>
            </div>

            <div>
              <p className="text-[length:var(--t-xs)] uppercase tracking-[0.08em] text-content-muted">
                {t("detail.messages", { count: team.messages.length })}
              </p>
              {team.messages.length === 0 ? (
                <p className="mt-1.5 text-body text-content-muted">{t("detail.noMessages")}</p>
              ) : (
                <div className="mt-1.5 flex flex-col gap-2">
                  {team.messages
                    .slice()
                    .reverse()
                    .map((msg) => (
                      <div key={msg.id} className="rounded border border-edge px-2.5 py-2">
                        <div className="flex items-center justify-between gap-2 text-[length:var(--t-xs)] text-content-muted">
                          <span className="font-data text-content-primary">{msg.from}</span>
                          <Timestamp value={msg.timestamp} />
                        </div>
                        <p className="mt-1 whitespace-pre-wrap text-body text-content-secondary">
                          {msg.content}
                        </p>
                      </div>
                    ))}
                </div>
              )}
            </div>
          </div>
        )}
      </div>
    </Modal>
  );
}
