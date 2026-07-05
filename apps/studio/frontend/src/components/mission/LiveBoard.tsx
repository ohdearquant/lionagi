/**
 * Live board — cards for currently-running runs and invocations.
 *
 * Status dot pulses via CSS animation (opacity + transform only).
 * Elapsed duration ticks every second client-side via nowSec from reducer.
 * prefers-reduced-motion: static dot, no animation.
 */

import { Link } from "@tanstack/react-router";
import { useTranslations } from "use-intl";
import SectionLabel from "@/components/ui/SectionLabel";
import StatusDot from "@/components/ui/StatusDot";
import Chip from "@/components/ui/Chip";
import type { RunSummary } from "@/lib/types";
import type { InvocationSummary } from "@/lib/api";
import { runDeepLink, invocationDeepLink } from "@/lib/runDeepLink";

/** Health states meaning the process is gone even though the run is non-terminal. */
const DEAD_HEALTH = new Set(["stale", "orphaned", "zombie", "unresponsive"]);

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
  const name = run.playbook_name ?? run.agent_name ?? run.run_id.slice(-12);
  // Honest staleness: a process-dead run must not render as a live one.
  const dead = run.effective_health != null && DEAD_HEALTH.has(run.effective_health);

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
        <span
          className={`shrink-0 font-data tabular-nums text-[length:var(--t-xs)] ${dead ? "text-content-muted" : "text-status-running"}`}
        >
          {formatElapsed(elapsed)}
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
  const elapsed = elapsedSec(inv.started_at, nowSec);

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
        <span className="shrink-0 font-data tabular-nums text-[length:var(--t-xs)] text-status-running">
          {formatElapsed(elapsed)}
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

export default function LiveBoard({ activeRuns, activeInvocations, nowSec }: Props) {
  const t = useTranslations("mission");
  const total = activeRuns.length + activeInvocations.length;

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
      ) : (
        <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3 2xl:grid-cols-4">
          {activeRuns.map((run) => (
            <RunCard key={run.run_id} run={run} nowSec={nowSec} />
          ))}
          {activeInvocations.map((inv) => (
            <InvocationCard key={inv.id} inv={inv} nowSec={nowSec} />
          ))}
        </div>
      )}
    </section>
  );
}
