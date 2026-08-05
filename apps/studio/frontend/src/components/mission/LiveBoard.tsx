/**
 * Live board — cards for currently-running runs and invocations.
 *
 * Status dot pulses via CSS animation (opacity + transform only).
 * Elapsed duration ticks every second client-side via nowSec from reducer.
 * prefers-reduced-motion: static dot, no animation.
 *
 * Health axis is heartbeat freshness only, NEVER duration — a session up
 * for days with recent activity is healthy, not alarming. Every card shows
 * both facts side by side: total uptime, and how long since last activity.
 * Only a genuinely stale heartbeat (effective_health) renders the
 * "quiet — check?" flag; duration alone never does.
 */

import { useState } from "react";
import { Link } from "@tanstack/react-router";
import { useTranslations } from "use-intl";
import SectionLabel from "@/components/ui/SectionLabel";
import StatusDot from "@/components/ui/StatusDot";
import Chip from "@/components/ui/Chip";
import Skeleton from "@/components/ui/Skeleton";
import type { RunSummary } from "@/lib/types";
import type { InvocationSummary } from "@/lib/api";
import { runDeepLink, invocationDeepLink } from "@/lib/runDeepLink";
import { runCreationKey, invocationCreationKey } from "./boardReducer";

/**
 * Health states meaning the process is gone even though the run is
 * non-terminal. TODO(unify): route through deriveDisplayStatus once
 * status/verdict/health derivation is unified into one shared function.
 */
export const DEAD_HEALTH = new Set(["stale", "orphaned", "zombie", "unresponsive"]);

/** Whether a run's effective_health means the process is gone (never based on duration). */
export function isDeadHealth(health: string | null | undefined): boolean {
  return health != null && DEAD_HEALTH.has(health);
}

/** Placeholder card count while the first fetch is in flight. */
const SKELETON_CARDS = 4;

interface Props {
  activeRuns: RunSummary[];
  activeInvocations: InvocationSummary[];
  nowSec: number;
}

function elapsedSec(startedAt: number | null | undefined, nowSec: number): number | null {
  if (startedAt == null) return null;
  return Math.max(0, Math.floor(nowSec - startedAt));
}

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

function RunCard({ run, nowSec }: { run: RunSummary; nowSec: number }) {
  const t = useTranslations("mission");
  const elapsed = elapsedSec(run.started_at ?? undefined, nowSec);
  // Last activity falls back to started_at when no heartbeat has landed yet
  // — never to "no data", since a fresh run has always at least started.
  const lastActivity = elapsedSec(run.last_message_at ?? run.started_at ?? undefined, nowSec);
  const name = run.playbook_name ?? run.agent_name ?? run.run_id.slice(-12);
  // Honest staleness: a process-dead run must not render as a live one.
  // Health axis only — duration never factors into this flag.
  // TODO(unify): route through deriveDisplayStatus once status/verdict/
  // health derivation is unified into one shared function.
  const dead = isDeadHealth(run.effective_health);

  return (
    <Link
      {...runDeepLink(run.run_id)}
      className="group flex flex-col gap-2 rounded border border-edge bg-surface-raised p-3 transition-colors duration-100"
    >
      <div className="flex items-center gap-2">
        <StatusDot status={dead ? "stale" : "running"} />
        <span className="min-w-0 flex-1 truncate font-data text-[length:var(--t-sm)] font-medium text-content-primary group-hover:opacity-80">
          {name}
        </span>
        {dead && (
          <span className="shrink-0 font-data text-[length:var(--t-xs)] uppercase text-content-muted">
            {t("liveBoard.staleLabel")}
          </span>
        )}
      </div>

      <div className="flex items-center gap-2">
        <span
          className={`min-w-0 flex-1 truncate font-data tabular-nums text-[length:var(--t-xs)] ${dead ? "text-content-muted" : "text-status-running"}`}
        >
          {t("liveBoard.durationStatus", {
            duration: formatElapsed(elapsed),
            age: formatElapsed(lastActivity),
          })}
        </span>
      </div>

      <div className="flex items-center gap-2">
        <Chip mono>run</Chip>
        <span className="min-w-0 flex-1 truncate font-data text-[length:var(--t-xs)] text-content-muted">
          {run.run_id.slice(-16)}
        </span>
        {run.invocation_kind && (
          <span className="shrink-0 font-data text-[length:var(--t-xs)] text-content-muted">
            {run.invocation_kind}
          </span>
        )}
      </div>
    </Link>
  );
}

function InvocationCard({ inv, nowSec }: { inv: InvocationSummary; nowSec: number }) {
  const t = useTranslations("mission");
  const elapsed = elapsedSec(inv.started_at, nowSec);
  // InvocationSummary carries no per-message heartbeat field — updated_at is
  // the coarsest available proxy for "last activity" (bumped on any change
  // to the invocation row, not strictly a heartbeat). Flagged as a data-field
  // gap: a real invocation heartbeat would need a backend addition.
  const lastActivity = elapsedSec(inv.updated_at ?? inv.started_at, nowSec);

  return (
    <Link
      {...invocationDeepLink()}
      className="group flex flex-col gap-2 rounded border border-edge bg-surface-raised p-3 transition-colors duration-100"
    >
      <div className="flex items-center gap-2">
        <StatusDot status="running" />
        <span className="min-w-0 flex-1 truncate font-data text-[length:var(--t-sm)] font-medium text-content-primary group-hover:opacity-80">
          {inv.skill}
        </span>
      </div>

      <div className="flex items-center gap-2">
        <span className="min-w-0 flex-1 truncate font-data tabular-nums text-[length:var(--t-xs)] text-status-running">
          {t("liveBoard.durationStatus", {
            duration: formatElapsed(elapsed),
            age: formatElapsed(lastActivity),
          })}
        </span>
      </div>

      <div className="flex items-center gap-2">
        <Chip mono>invoke</Chip>
        <span className="min-w-0 flex-1 truncate font-data text-[length:var(--t-xs)] text-content-muted">
          {inv.id.slice(-16)}
        </span>
        {inv.plugin && (
          <span className="shrink-0 font-data text-[length:var(--t-xs)] text-content-muted">
            {inv.plugin}
          </span>
        )}
      </div>
    </Link>
  );
}

/** Shimmering card placeholders, sized to match a real RunCard/InvocationCard. */
export function LiveBoardSkeleton() {
  return (
    <div aria-hidden="true">
      <div className="mb-2">
        <Skeleton className="h-4 w-28" />
      </div>
      <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3 2xl:grid-cols-4">
        {Array.from({ length: SKELETON_CARDS }, (_, i) => (
          <div
            key={i}
            className="flex flex-col gap-2 rounded border border-edge bg-surface-raised p-3"
          >
            <div className="flex items-center gap-2">
              <Skeleton className="h-2.5 w-2.5 shrink-0 rounded-full" />
              <Skeleton className="h-3 flex-1" />
            </div>
            <div className="flex items-center gap-2">
              <Skeleton className="h-3 w-32" />
            </div>
            <div className="flex items-center gap-2">
              <Skeleton className="h-4 w-12 shrink-0 rounded" />
              <Skeleton className="h-3 flex-1" />
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

// The board is one grid of cards, not "runs, then invocations" — a card's
// position is its creation order regardless of which kind it is, so a run
// that started a minute ago sits before an invocation that started just now,
// not after every run. Both key functions live in boardReducer.ts (the same
// rule that orders activeRuns/activeInvocations individually), so the merge
// below can't drift from the per-list order the reducer already guarantees.
type LiveCard =
  | { kind: "run"; id: string; sortKey: number; run: RunSummary }
  | { kind: "invocation"; id: string; sortKey: number; invocation: InvocationSummary };

function buildLiveCards(runs: RunSummary[], invocations: InvocationSummary[]): LiveCard[] {
  const cards: LiveCard[] = [
    ...runs.map((run) => ({
      kind: "run" as const,
      id: run.run_id,
      sortKey: runCreationKey(run),
      run,
    })),
    ...invocations.map((inv) => ({
      kind: "invocation" as const,
      id: inv.id,
      sortKey: invocationCreationKey(inv),
      invocation: inv,
    })),
  ];
  cards.sort((a, b) => a.sortKey - b.sortKey || a.id.localeCompare(b.id));
  return cards;
}

// ─── View preference (cards / table) ───────────────────────────────────────
// Same direct-localStorage pattern as lib/theme.ts / SplitPane / AppShell —
// no separate persistence mechanism for one more UI toggle.

type BoardView = "cards" | "table";

const BOARD_VIEW_STORAGE_KEY = "studio:mission-board-view";

function getStoredBoardView(): BoardView {
  if (typeof window === "undefined") return "cards";
  return window.localStorage.getItem(BOARD_VIEW_STORAGE_KEY) === "table" ? "table" : "cards";
}

function BoardViewToggle({
  view,
  onChange,
}: {
  view: BoardView;
  onChange: (view: BoardView) => void;
}) {
  const t = useTranslations("mission");
  const seg = (v: BoardView, label: string) => (
    <button
      type="button"
      onClick={() => onChange(v)}
      aria-pressed={view === v}
      className={[
        "h-6 shrink-0 rounded px-2 font-data text-[length:var(--t-xs)] font-medium transition-colors duration-100",
        view === v
          ? "bg-surface-overlay text-content-primary"
          : "text-content-muted hover:text-content-primary",
      ].join(" ")}
    >
      {label}
    </button>
  );
  return (
    <div className="flex shrink-0 items-center gap-0.5 rounded border border-edge p-0.5">
      {seg("cards", t("liveBoard.viewCards"))}
      {seg("table", t("liveBoard.viewTable"))}
    </div>
  );
}

// Bounds the board's footprint regardless of how many runs are active — the
// board scrolls internally instead of pushing Pulse/Recent Runs off screen.
const BOARD_MAX_HEIGHT = "max-h-[420px]";

function BoardTableRow({ card, nowSec }: { card: LiveCard; nowSec: number }) {
  const t = useTranslations("mission");
  const isRun = card.kind === "run";
  const name = isRun
    ? (card.run.playbook_name ?? card.run.agent_name ?? card.run.run_id.slice(-12))
    : card.invocation.skill;
  const startedAt = isRun ? (card.run.started_at ?? undefined) : card.invocation.started_at;
  const elapsed = elapsedSec(startedAt, nowSec);
  const lastActivityAt = isRun
    ? (card.run.last_message_at ?? card.run.started_at ?? undefined)
    : (card.invocation.updated_at ?? card.invocation.started_at);
  const lastActivity = elapsedSec(lastActivityAt, nowSec);
  const dead = isRun && isDeadHealth(card.run.effective_health);
  const status = isRun ? card.run.status : card.invocation.status;
  const linkProps = isRun ? runDeepLink(card.run.run_id) : invocationDeepLink();

  return (
    <tr className="border-b border-edge transition-colors duration-100 hover:bg-surface-overlay/60">
      <td className="max-w-0 px-3 py-2">
        <Link
          {...linkProps}
          className="block truncate font-data text-[length:var(--t-sm)] font-medium text-content-primary hover:opacity-80"
        >
          {name}
        </Link>
      </td>
      <td className="px-3 py-2">
        <span className="flex items-center gap-1.5">
          <StatusDot status={dead ? "stale" : status} />
          <span className="font-data text-[length:var(--t-xs)] text-content-secondary">
            {dead ? t("liveBoard.staleLabel") : status}
          </span>
        </span>
      </td>
      <td className="px-3 py-2 font-data tabular-nums text-[length:var(--t-xs)] text-content-secondary">
        {formatElapsed(elapsed)}
      </td>
      <td className="px-3 py-2 font-data tabular-nums text-[length:var(--t-xs)] text-content-secondary">
        {formatElapsed(lastActivity)}
      </td>
      <td className="px-3 py-2">
        <Chip mono>{isRun ? "run" : "invoke"}</Chip>
      </td>
      <td className="px-3 py-2 font-data text-[length:var(--t-xs)] text-content-muted">
        {card.id.slice(-16)}
      </td>
    </tr>
  );
}

function BoardTable({ cards, nowSec }: { cards: LiveCard[]; nowSec: number }) {
  const t = useTranslations("mission");
  return (
    <div className={`${BOARD_MAX_HEIGHT} overflow-auto rounded border border-edge`}>
      <table className="w-full text-left" style={{ borderCollapse: "collapse" }}>
        <thead>
          <tr
            className="border-b border-edge bg-surface-raised text-[length:var(--t-xs)] uppercase tracking-[0.08em] text-content-muted"
            style={{ position: "sticky", top: 0, zIndex: 1 }}
          >
            <th className="px-3 py-2 font-medium">{t("liveBoard.table.colName")}</th>
            <th className="px-3 py-2 font-medium">{t("liveBoard.table.colStatus")}</th>
            <th className="px-3 py-2 font-medium">{t("liveBoard.table.colUptime")}</th>
            <th className="px-3 py-2 font-medium">{t("liveBoard.table.colLastActivity")}</th>
            <th className="px-3 py-2 font-medium">{t("liveBoard.table.colKind")}</th>
            <th className="px-3 py-2 font-medium">{t("liveBoard.table.colId")}</th>
          </tr>
        </thead>
        <tbody>
          {cards.map((card) => (
            <BoardTableRow key={card.id} card={card} nowSec={nowSec} />
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function LiveBoard({ activeRuns, activeInvocations, nowSec }: Props) {
  const t = useTranslations("mission");
  const total = activeRuns.length + activeInvocations.length;
  const [view, setView] = useState<BoardView>(getStoredBoardView);

  function changeView(next: BoardView) {
    setView(next);
    if (typeof window !== "undefined") {
      window.localStorage.setItem(BOARD_VIEW_STORAGE_KEY, next);
    }
  }

  return (
    <section aria-labelledby="live-board-heading">
      <div className="mb-2">
        <SectionLabel
          trailing={
            <>
              {total > 0 && (
                <span
                  className="rounded px-1.5 py-0.5 font-data text-[length:var(--t-xs)] font-semibold tabular-nums"
                  style={{
                    background: "color-mix(in srgb, var(--status-running) 12%, transparent)",
                    color: "var(--status-running)",
                  }}
                >
                  {total}
                </span>
              )}
              {total > 0 && <BoardViewToggle view={view} onChange={changeView} />}
              <Link
                to="/fleet"
                className="font-data text-[length:var(--t-xs)] text-content-muted transition-colors duration-100"
              >
                {t("liveBoard.fleetLink")}
              </Link>
            </>
          }
        >
          <span id="live-board-heading">{t("liveBoard.title")}</span>
        </SectionLabel>
      </div>

      {total === 0 ? (
        <div className="flex flex-col gap-3">
          <p className="text-[length:var(--t-sm)] text-content-muted">
            {t("liveBoard.empty")} {t("liveBoard.emptyHint")}
          </p>
        </div>
      ) : view === "table" ? (
        <BoardTable cards={buildLiveCards(activeRuns, activeInvocations)} nowSec={nowSec} />
      ) : (
        <div
          className={`${BOARD_MAX_HEIGHT} grid grid-cols-1 gap-2 overflow-y-auto pr-1 sm:grid-cols-2 lg:grid-cols-3 2xl:grid-cols-4`}
        >
          {buildLiveCards(activeRuns, activeInvocations).map((card) =>
            card.kind === "run" ? (
              <RunCard key={card.id} run={card.run} nowSec={nowSec} />
            ) : (
              <InvocationCard key={card.id} inv={card.invocation} nowSec={nowSec} />
            ),
          )}
        </div>
      )}
    </section>
  );
}
