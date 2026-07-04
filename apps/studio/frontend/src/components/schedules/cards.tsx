/**
 * Board cards. ScheduleCard renders a standing automation (Upcoming/Today/
 * Paused lanes); RunCard renders one firing (Running/Done lanes). A schedule
 * may legitimately appear in both halves of the board at once — its next
 * firing is queued while its last firing sits in Done.
 */
import { useState } from "react";
import { useNavigate } from "@tanstack/react-router";
import { useLocale, useTranslations } from "use-intl";
import StatusPill from "@/components/ui/StatusPill";
import { triggerSchedule, getInvocation } from "@/lib/api";
import type { ScheduleSummary } from "@/lib/types";
import { useToast } from "@/components/ui/Toast";
import EnabledToggle from "./EnabledToggle";
import { formatDelta, formatInterval, toMs, type RunRow } from "./data";

const STATUS_DOT: Record<string, string> = {
  running: "var(--status-running)",
  completed: "var(--status-success)",
  failed: "var(--status-error)",
  skipped: "var(--content-muted)",
  cancelled: "var(--content-muted)",
};

// Statuses with a history.status translation; unknown values fall back to
// StatusPill's built-in humanization.
export const KNOWN_RUN_STATUSES = new Set([
  "running",
  "completed",
  "failed",
  "cancelled",
  "pending",
  "queued",
  "timed_out",
  "aborted",
  "skipped",
]);

function chipClass(mono = false) {
  return [
    "inline-flex max-w-[140px] items-center truncate rounded border border-edge bg-surface-overlay px-1.5 py-0.5 text-meta leading-none text-content-secondary",
    mono ? "font-data" : "",
  ]
    .filter(Boolean)
    .join(" ");
}

/** One line of truth about when this schedule fires: cron expr, interval, or repo. */
function triggerText(s: ScheduleSummary, every: (interval: string) => string): string {
  if (s.trigger_type === "cron" && s.cron_expr) return s.cron_expr;
  if (s.trigger_type === "interval" && s.interval_sec != null)
    return every(formatInterval(s.interval_sec));
  if (s.trigger_type === "github_poll" && s.github_repo) {
    const poll =
      s.poll_interval_sec != null ? ` · ${every(formatInterval(s.poll_interval_sec))}` : "";
    return `${s.github_repo}${poll}`;
  }
  return s.trigger_type;
}

export function ScheduleCard({
  schedule,
  lane,
  lastRun,
  nowMs,
  onChanged,
  onOpen,
}: {
  schedule: ScheduleSummary;
  lane: "today" | "upcoming" | "paused";
  lastRun?: RunRow;
  nowMs: number;
  onChanged: () => void;
  onOpen?: (id: string) => void;
}) {
  const t = useTranslations("schedules.card");
  const locale = useLocale();
  const { toast } = useToast();
  const [triggering, setTriggering] = useState(false);

  const enabled = Boolean(schedule.enabled);
  const actionDetail =
    schedule.action_model ?? schedule.action_playbook ?? schedule.action_agent ?? null;

  async function handleTrigger() {
    setTriggering(true);
    try {
      const res = await triggerSchedule(schedule.id);
      toast(t("runStarted", { id: res.run_id.slice(0, 8) }), "success");
      onChanged();
    } catch {
      toast(t("triggerFailed"), "error");
    } finally {
      setTriggering(false);
    }
  }

  // Fire line: Today shows clock time + countdown, Upcoming shows date + countdown.
  // github_poll schedules are event-driven — next_fire_at is not maintained for
  // them, so a countdown (or "overdue") computed from it would be a lie. Show
  // the watching state instead.
  let fireLine: React.ReactNode = null;
  if (lane !== "paused" && schedule.trigger_type === "github_poll") {
    fireLine = (
      <div className="flex items-baseline gap-1.5 text-meta text-content-secondary">
        <span>{t("watching")}</span>
      </div>
    );
  } else if (lane !== "paused" && schedule.next_fire_at != null) {
    const fireMs = toMs(schedule.next_fire_at);
    const delta = fireMs - nowMs;
    const overdue = delta < 0;
    const soon = !overdue && delta < 3_600_000;
    const when =
      lane === "today"
        ? new Date(fireMs).toLocaleTimeString(locale, {
            hour: "2-digit",
            minute: "2-digit",
            hour12: false,
          })
        : new Date(fireMs).toLocaleDateString(locale, { month: "short", day: "numeric" });
    // Fire line color is data-driven (computed from delta values) — kept inline.
    fireLine = (
      <div
        className="flex items-baseline gap-1.5 text-meta"
        style={{
          color: overdue
            ? "var(--status-warning)"
            : soon
              ? "var(--accent)"
              : "var(--content-secondary)",
        }}
      >
        <span className="font-data">{when}</span>
        <span>
          {overdue
            ? t("overdue", { delta: formatDelta(-delta) })
            : t("in", { delta: formatDelta(delta) })}
        </span>
      </div>
    );
  }

  return (
    // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions
    <article
      role={onOpen ? "button" : undefined}
      tabIndex={onOpen ? 0 : undefined}
      onClick={() => onOpen?.(schedule.id)}
      onKeyDown={(e) => {
        if (onOpen && (e.key === "Enter" || e.key === " ")) {
          e.preventDefault();
          onOpen(schedule.id);
        }
      }}
      className={[
        "flex flex-col gap-1.5 rounded-md border border-edge bg-surface-raised p-2.5 transition-colors duration-150",
        lane === "paused" ? "opacity-60 hover:opacity-90" : "hover:border-edge-strong",
        onOpen ? "cursor-pointer hover:ring-1 hover:ring-edge-strong" : "",
      ].join(" ")}
    >
      <div className="flex items-center gap-2">
        <span
          className="min-w-0 flex-1 truncate font-data text-body font-semibold text-content-primary"
          title={schedule.description ?? schedule.name}
        >
          {schedule.name}
        </span>
        {/* stopPropagation so toggle/run don't open the modal */}
        {/* eslint-disable-next-line jsx-a11y/no-static-element-interactions */}
        <div onClick={(e) => e.stopPropagation()} onKeyDown={(e) => e.stopPropagation()}>
          <EnabledToggle scheduleId={schedule.id} enabled={enabled} onToggled={onChanged} />
        </div>
      </div>

      <div className="truncate font-data text-meta text-content-muted">
        {triggerText(schedule, (interval) => t("every", { interval }))}
      </div>

      <div className="flex flex-wrap items-center gap-1">
        <span className={chipClass()}>{schedule.action_kind}</span>
        {actionDetail && (
          <span className={chipClass(true)} title={actionDetail}>
            {actionDetail}
          </span>
        )}
        {schedule.project && <span className={chipClass()}>{schedule.project}</span>}
      </div>

      {fireLine}

      <div className="flex items-center justify-between gap-2 border-t border-edge pt-1.5">
        <span className="flex min-w-0 items-center gap-1.5 text-meta text-content-muted">
          {lastRun ? (
            <>
              <span
                aria-hidden="true"
                className="h-1.5 w-1.5 shrink-0 rounded-full"
                style={{ background: STATUS_DOT[lastRun.status] ?? "var(--content-muted)" }}
              />
              <span className="truncate" title={lastRun.status}>
                {t("last")} {formatDelta(nowMs - toMs(lastRun.fired_at))}
              </span>
            </>
          ) : (
            <span>{t("never")}</span>
          )}
        </span>
        <button
          type="button"
          disabled={triggering}
          onClick={(e) => {
            e.stopPropagation();
            void handleTrigger();
          }}
          className="shrink-0 rounded border border-edge px-2 py-0.5 text-meta text-content-secondary transition-colors duration-100 hover:border-edge-strong hover:text-content-primary disabled:opacity-50"
        >
          {triggering ? t("triggering") : t("runNow")}
        </button>
      </div>
    </article>
  );
}

export function RunCard({ run, nowMs }: { run: RunRow; nowMs: number }) {
  const t = useTranslations("schedules.run");
  const tStatus = useTranslations("history.status");
  const navigate = useNavigate();
  const firedMs = toMs(run.fired_at);
  const isRunning = run.status === "running";
  const statusLabel = KNOWN_RUN_STATUSES.has(run.status)
    ? tStatus(run.status as Parameters<typeof tStatus>[0])
    : undefined;

  // Sub-second durations floor to "0s" — show only the relative age then.
  const durationMs = run.ended_at != null ? toMs(run.ended_at) - firedMs : 0;
  const timing = isRunning
    ? t("elapsed", { delta: formatDelta(nowMs - firedMs) })
    : durationMs >= 1000
      ? `${formatDelta(durationMs)} · ${formatDelta(nowMs - firedMs)}`
      : formatDelta(nowMs - firedMs);

  const body = (
    <>
      <div className="flex items-center gap-2">
        <StatusPill value={run.status} taxonomy="session" label={statusLabel} />
        <span className="min-w-0 flex-1 truncate font-data text-body font-semibold text-content-primary">
          {run.scheduleName}
        </span>
      </div>
      <div className="flex items-center justify-between gap-2 text-meta text-content-muted">
        <span>{run.action_kind}</span>
        <span className="font-data tabular-nums">{timing}</span>
      </div>
      {run.status === "failed" && run.error_detail && (
        <div className="truncate text-meta text-status-error" title={run.error_detail}>
          {run.error_detail}
        </div>
      )}
    </>
  );

  const cardClass =
    "flex flex-col gap-1.5 rounded-md border border-edge bg-surface-raised p-2.5 transition-colors duration-150";

  async function handleRunClick() {
    if (!run.invocation_id) {
      await navigate({ to: "/history", search: { tab: "run" } });
      return;
    }
    try {
      const inv = await getInvocation(run.invocation_id);
      const sessionId = inv.sessions[0]?.id;
      if (sessionId) {
        await navigate({ to: "/history", search: { tab: "run", sel: `run:${sessionId}` } });
      } else {
        await navigate({ to: "/history", search: { tab: "run" } });
      }
    } catch {
      await navigate({ to: "/history", search: { tab: "run" } });
    }
  }

  return (
    <div
      role="button"
      tabIndex={0}
      onClick={() => void handleRunClick()}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          void handleRunClick();
        }
      }}
      title={t("openRun")}
      className={`${cardClass} cursor-pointer hover:border-edge-strong hover:ring-1 hover:ring-edge-strong`}
    >
      {body}
    </div>
  );
}
