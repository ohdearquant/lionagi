"use client";

import { Suspense, useEffect, useState } from "react";
import Button from "@/components/Button";
import PageHeader from "@/components/PageHeader";
import StatusPill from "@/components/StatusPill";
import Timestamp from "@/components/Timestamp";
import {
  createSchedule,
  deleteSchedule,
  disableSchedule,
  enableSchedule,
  listScheduleRuns,
  listSchedules,
  triggerSchedule,
  type ScheduleListResponse,
} from "@/lib/api";
import type { ScheduleRunSummary, ScheduleSummary } from "@/lib/types";

// ─── Badge helpers ────────────────────────────────────────────────────────────

const TRIGGER_CLASS: Record<string, string> = {
  cron: "border-status-running/40 bg-status-running-bg text-status-running",
  interval: "border-status-warning/40 bg-status-warning-bg text-status-warning",
  github_poll: "border-status-success/40 bg-status-success-bg text-status-success",
};

function TriggerBadge({ type }: { type: string }) {
  const cls = TRIGGER_CLASS[type] ?? "border-edge bg-surface-overlay text-content-secondary";
  const label = type === "github_poll" ? "github" : type;
  return (
    <span
      className={[
        "inline-flex items-center rounded-full border px-1.5 py-0.5 text-[10px] font-medium leading-none tracking-wide",
        cls,
      ].join(" ")}
    >
      {label}
    </span>
  );
}

function ActionBadge({ kind, detail }: { kind: string; detail?: string | null }) {
  return (
    <span className="inline-flex items-center gap-1 rounded-full border border-edge bg-surface-overlay px-1.5 py-0.5 text-[10px] font-medium leading-none text-content-secondary">
      {kind}
      {detail ? (
        <span className="text-content-muted truncate max-w-[80px]" title={detail}>
          · {detail}
        </span>
      ) : null}
    </span>
  );
}

// ─── Human-readable interval ──────────────────────────────────────────────────

function formatInterval(sec: number): string {
  if (sec < 60) return `${sec}s`;
  if (sec < 3600) {
    const m = Math.floor(sec / 60);
    const s = sec % 60;
    return s ? `${m}m ${s}s` : `${m}m`;
  }
  if (sec < 86400) {
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    return m ? `${h}h ${m}m` : `${h}h`;
  }
  const d = Math.floor(sec / 86400);
  const h = Math.floor((sec % 86400) / 3600);
  return h ? `${d}d ${h}h` : `${d}d`;
}

// ─── Enabled toggle ───────────────────────────────────────────────────────────

function EnabledToggle({
  scheduleId,
  enabled,
  onToggled,
}: {
  scheduleId: string;
  enabled: boolean;
  onToggled: () => void;
}) {
  const [busy, setBusy] = useState(false);

  async function handleClick(e: React.MouseEvent) {
    e.stopPropagation();
    setBusy(true);
    try {
      if (enabled) {
        await disableSchedule(scheduleId);
      } else {
        await enableSchedule(scheduleId);
      }
      onToggled();
    } catch {
      // silent — UI stays consistent until next reload
    } finally {
      setBusy(false);
    }
  }

  return (
    <button
      type="button"
      onClick={(e) => void handleClick(e)}
      disabled={busy}
      aria-label={enabled ? "Disable schedule" : "Enable schedule"}
      aria-pressed={enabled}
      title={enabled ? "Click to disable" : "Click to enable"}
      className={[
        "relative inline-flex h-4 w-7 shrink-0 items-center rounded-full border transition-colors duration-150 focus:outline-none focus:ring-2 focus:ring-interactive-primary focus:ring-offset-1 focus:ring-offset-surface-base",
        enabled ? "border-status-success/50 bg-status-success" : "border-edge bg-surface-overlay",
        busy ? "opacity-60 cursor-not-allowed" : "cursor-pointer",
      ]
        .filter(Boolean)
        .join(" ")}
    >
      <span
        className={[
          "inline-block h-2.5 w-2.5 rounded-full bg-white shadow transition-transform duration-150",
          enabled ? "translate-x-3" : "translate-x-0.5",
        ].join(" ")}
      />
    </button>
  );
}

// ─── Schedule card ────────────────────────────────────────────────────────────

function ScheduleCard({
  schedule,
  onRefresh,
}: {
  schedule: ScheduleSummary;
  onRefresh: () => void;
}) {
  const [triggering, setTriggering] = useState(false);
  const [triggerMsg, setTriggerMsg] = useState<string | null>(null);

  const enabled = Boolean(schedule.enabled);

  async function handleTrigger(e: React.MouseEvent) {
    e.stopPropagation();
    setTriggering(true);
    setTriggerMsg(null);
    try {
      const result = await triggerSchedule(schedule.id);
      setTriggerMsg(`Run started: ${result.run_id.slice(0, 8)}`);
      setTimeout(() => setTriggerMsg(null), 4000);
    } catch {
      setTriggerMsg("Failed to trigger");
      setTimeout(() => setTriggerMsg(null), 3000);
    } finally {
      setTriggering(false);
    }
  }

  const actionDetail =
    schedule.action_model ?? schedule.action_playbook ?? schedule.action_agent ?? null;

  return (
    <div
      className={[
        "flex flex-col gap-3 rounded-lg border bg-surface-raised p-4 shadow-card transition-all duration-150",
        enabled
          ? "border-edge hover:border-edge-strong hover:bg-surface-overlay"
          : "border-edge opacity-60 hover:opacity-80",
      ].join(" ")}
    >
      {/* Header row */}
      <div className="flex items-start justify-between gap-2">
        <div className="flex min-w-0 flex-1 flex-col gap-1">
          <span className="truncate font-mono text-[13px] font-semibold text-content-primary">
            {schedule.name}
          </span>
          {schedule.description && (
            <p className="truncate text-meta text-content-secondary">{schedule.description}</p>
          )}
        </div>
        <EnabledToggle scheduleId={schedule.id} enabled={enabled} onToggled={onRefresh} />
      </div>

      {/* Badge row */}
      <div className="flex flex-wrap items-center gap-1.5">
        <TriggerBadge type={schedule.trigger_type} />
        <ActionBadge kind={schedule.action_kind} detail={actionDetail} />
        {schedule.project && (
          <span className="inline-flex items-center rounded-full border border-edge bg-surface-overlay px-1.5 py-0.5 text-[10px] font-medium leading-none text-content-muted">
            {schedule.project}
          </span>
        )}
      </div>

      {/* Trigger detail */}
      <div className="text-meta text-content-secondary">
        {schedule.trigger_type === "cron" && schedule.cron_expr && (
          <span className="font-mono text-[11px] text-content-muted">{schedule.cron_expr}</span>
        )}
        {schedule.trigger_type === "interval" && schedule.interval_sec != null && (
          <span>Every {formatInterval(schedule.interval_sec)}</span>
        )}
        {schedule.trigger_type === "github_poll" && schedule.github_repo && (
          <span className="truncate text-[11px]">
            Polling <span className="font-mono text-content-primary">{schedule.github_repo}</span>
            {schedule.poll_interval_sec != null && (
              <span className="text-content-muted">
                {" "}
                every {formatInterval(schedule.poll_interval_sec)}
              </span>
            )}
          </span>
        )}
      </div>

      {/* Timing row */}
      <div className="flex items-center gap-3 text-meta text-content-muted">
        <span>
          Last:{" "}
          <span className="text-content-secondary">
            <Timestamp value={schedule.last_fired_at} />
          </span>
        </span>
        <span>
          Next:{" "}
          <span className="text-content-secondary">
            <Timestamp value={schedule.next_fire_at} />
          </span>
        </span>
      </div>

      {/* Actions row */}
      <div className="flex items-center justify-between gap-2 border-t border-edge pt-2.5">
        {triggerMsg ? <span className="text-meta text-content-muted">{triggerMsg}</span> : <span />}
        <Button
          variant="ghost"
          size="sm"
          disabled={triggering}
          onClick={(e) => void handleTrigger(e)}
        >
          {triggering ? "Triggering..." : "Trigger Now"}
        </Button>
      </div>
    </div>
  );
}

// ─── Skeleton ─────────────────────────────────────────────────────────────────

function SkeletonCard() {
  return (
    <div className="flex flex-col gap-3 rounded-lg border border-edge bg-surface-raised p-4">
      <div className="skeleton h-4 w-2/3 rounded" />
      <div className="skeleton h-3 w-full rounded" />
      <div className="skeleton h-3 w-1/3 rounded" />
    </div>
  );
}

// ─── Recent runs table ────────────────────────────────────────────────────────

function RecentRunsTable({ runs }: { runs: ScheduleRunSummary[] }) {
  if (runs.length === 0) {
    return <p className="py-6 text-center text-body text-content-muted">No recent runs.</p>;
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-body">
        <thead>
          <tr className="border-b border-edge text-left text-meta text-content-muted">
            <th className="pb-2 pr-4 font-medium">Schedule</th>
            <th className="pb-2 pr-4 font-medium">Status</th>
            <th className="pb-2 pr-4 font-medium">Kind</th>
            <th className="pb-2 pr-4 font-medium">Fired</th>
            <th className="pb-2 pr-4 font-medium">Ended</th>
            <th className="pb-2 font-medium">Error</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-edge">
          {runs.map((run) => (
            <tr key={run.id} className="hover:bg-surface-overlay transition-colors">
              <td className="py-2 pr-4 font-mono text-[11px] text-content-muted">
                {run.schedule_id.slice(0, 8)}
              </td>
              <td className="py-2 pr-4">
                <StatusPill value={run.status} taxonomy="session" />
              </td>
              <td className="py-2 pr-4 text-content-secondary">{run.action_kind}</td>
              <td className="py-2 pr-4 text-content-secondary">
                <Timestamp value={run.fired_at} />
              </td>
              <td className="py-2 pr-4 text-content-secondary">
                <Timestamp value={run.ended_at} />
              </td>
              <td className="py-2 max-w-[200px] truncate text-meta text-status-error">
                {run.error_detail ?? "—"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ─── Create modal ─────────────────────────────────────────────────────────────

type TriggerType = "cron" | "interval" | "github_poll";
type ActionKind = "agent" | "flow" | "fanout" | "play";

interface CreateForm {
  name: string;
  description: string;
  trigger_type: TriggerType;
  cron_expr: string;
  interval_sec: string;
  github_repo: string;
  poll_interval_sec: string;
  action_kind: ActionKind;
  action_model: string;
  action_prompt: string;
  action_agent: string;
  action_playbook: string;
  action_project: string;
  missed_fire_policy: string;
  overlap_policy: string;
  on_success_json: string;
  on_fail_json: string;
}

const EMPTY_FORM: CreateForm = {
  name: "",
  description: "",
  trigger_type: "cron",
  cron_expr: "0 * * * *",
  interval_sec: "3600",
  github_repo: "",
  poll_interval_sec: "300",
  action_kind: "agent",
  action_model: "",
  action_prompt: "",
  action_agent: "",
  action_playbook: "",
  action_project: "",
  missed_fire_policy: "skip",
  overlap_policy: "skip",
  on_success_json: "",
  on_fail_json: "",
};

function fieldClass(extra?: string) {
  return [
    "h-8 rounded border border-edge bg-surface-base px-2.5 text-body text-content-primary",
    "placeholder:text-content-muted focus:border-interactive-primary focus:outline-none",
    extra,
  ]
    .filter(Boolean)
    .join(" ");
}

function labelClass() {
  return "flex flex-col gap-1";
}

function labelTextClass() {
  return "text-meta text-content-secondary font-medium";
}

function SectionHeading({ children }: { children: React.ReactNode }) {
  return (
    <p className="mt-1 border-t border-edge pt-3 text-[11px] font-semibold uppercase tracking-wider text-content-muted">
      {children}
    </p>
  );
}

function CreateScheduleModal({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: () => void;
}) {
  const [form, setForm] = useState<CreateForm>(EMPTY_FORM);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function set(key: keyof CreateForm, value: string) {
    setForm((prev) => ({ ...prev, [key]: value }));
  }

  function buildPayload(): Record<string, unknown> {
    const payload: Record<string, unknown> = {
      name: form.name.trim(),
      description: form.description.trim() || null,
      trigger_type: form.trigger_type,
      action_kind: form.action_kind,
      missed_fire_policy: form.missed_fire_policy,
      overlap_policy: form.overlap_policy,
    };

    if (form.trigger_type === "cron") {
      payload.cron_expr = form.cron_expr.trim();
    } else if (form.trigger_type === "interval") {
      payload.interval_sec = Number(form.interval_sec);
    } else if (form.trigger_type === "github_poll") {
      payload.github_repo = form.github_repo.trim();
      payload.poll_interval_sec = Number(form.poll_interval_sec);
    }

    if (form.action_model.trim()) payload.action_model = form.action_model.trim();
    if (form.action_prompt.trim()) payload.action_prompt = form.action_prompt.trim();
    if (form.action_agent.trim()) payload.action_agent = form.action_agent.trim();
    if (form.action_playbook.trim()) payload.action_playbook = form.action_playbook.trim();
    if (form.action_project.trim()) payload.action_project = form.action_project.trim();

    if (form.on_success_json.trim()) {
      try {
        payload.on_success = JSON.parse(form.on_success_json);
      } catch {
        throw new Error("on_success is not valid JSON");
      }
    }
    if (form.on_fail_json.trim()) {
      try {
        payload.on_fail = JSON.parse(form.on_fail_json);
      } catch {
        throw new Error("on_fail is not valid JSON");
      }
    }

    return payload;
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!form.name.trim()) {
      setError("Name is required.");
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      const payload = buildPayload();
      await createSchedule(payload);
      onCreated();
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create schedule.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/50 py-8"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="w-full max-w-lg rounded-lg border border-edge bg-surface-raised shadow-card mx-4">
        {/* Modal header */}
        <div className="flex items-center justify-between border-b border-edge px-5 py-4">
          <h2 className="font-mono text-base font-semibold text-content-primary">New Schedule</h2>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="flex h-6 w-6 items-center justify-center rounded text-content-muted hover:bg-surface-overlay hover:text-content-primary transition-colors"
          >
            ✕
          </button>
        </div>

        {/* Form body */}
        <form onSubmit={(e) => void handleSubmit(e)} className="flex flex-col gap-3 px-5 py-4">
          {/* — Basic info — */}
          <SectionHeading>Basic Info</SectionHeading>

          <label className={labelClass()}>
            <span className={labelTextClass()}>
              Name <span className="text-status-error">*</span>
            </span>
            <input
              type="text"
              value={form.name}
              onChange={(e) => set("name", e.target.value)}
              placeholder="nightly-report"
              className={fieldClass()}
            />
          </label>

          <label className={labelClass()}>
            <span className={labelTextClass()}>Description</span>
            <input
              type="text"
              value={form.description}
              onChange={(e) => set("description", e.target.value)}
              placeholder="Optional description"
              className={fieldClass()}
            />
          </label>

          {/* — Trigger config — */}
          <SectionHeading>Trigger</SectionHeading>

          <label className={labelClass()}>
            <span className={labelTextClass()}>Trigger type</span>
            <select
              value={form.trigger_type}
              onChange={(e) => set("trigger_type", e.target.value as TriggerType)}
              className={fieldClass()}
            >
              <option value="cron">Cron</option>
              <option value="interval">Interval</option>
              <option value="github_poll">GitHub Poll</option>
            </select>
          </label>

          {form.trigger_type === "cron" && (
            <label className={labelClass()}>
              <span className={labelTextClass()}>Cron expression</span>
              <input
                type="text"
                value={form.cron_expr}
                onChange={(e) => set("cron_expr", e.target.value)}
                placeholder="0 * * * *"
                className={fieldClass("font-mono")}
              />
              <span className="text-meta text-content-muted">
                Standard 5-field cron (min hr dom mon dow)
              </span>
            </label>
          )}

          {form.trigger_type === "interval" && (
            <label className={labelClass()}>
              <span className={labelTextClass()}>Interval (seconds)</span>
              <input
                type="number"
                min={1}
                value={form.interval_sec}
                onChange={(e) => set("interval_sec", e.target.value)}
                placeholder="3600"
                className={fieldClass()}
              />
            </label>
          )}

          {form.trigger_type === "github_poll" && (
            <>
              <label className={labelClass()}>
                <span className={labelTextClass()}>GitHub repo</span>
                <input
                  type="text"
                  value={form.github_repo}
                  onChange={(e) => set("github_repo", e.target.value)}
                  placeholder="owner/repo"
                  className={fieldClass("font-mono")}
                />
              </label>
              <label className={labelClass()}>
                <span className={labelTextClass()}>Poll interval (seconds)</span>
                <input
                  type="number"
                  min={60}
                  value={form.poll_interval_sec}
                  onChange={(e) => set("poll_interval_sec", e.target.value)}
                  placeholder="300"
                  className={fieldClass()}
                />
              </label>
            </>
          )}

          {/* — Action config — */}
          <SectionHeading>Action</SectionHeading>

          <label className={labelClass()}>
            <span className={labelTextClass()}>Action kind</span>
            <select
              value={form.action_kind}
              onChange={(e) => set("action_kind", e.target.value as ActionKind)}
              className={fieldClass()}
            >
              <option value="agent">Agent</option>
              <option value="flow">Flow</option>
              <option value="fanout">Fanout</option>
              <option value="play">Play</option>
            </select>
          </label>

          <label className={labelClass()}>
            <span className={labelTextClass()}>Model</span>
            <input
              type="text"
              value={form.action_model}
              onChange={(e) => set("action_model", e.target.value)}
              placeholder="e.g. openai/gpt-4.1"
              className={fieldClass()}
            />
          </label>

          {(form.action_kind === "agent" || form.action_kind === "flow") && (
            <label className={labelClass()}>
              <span className={labelTextClass()}>Agent name</span>
              <input
                type="text"
                value={form.action_agent}
                onChange={(e) => set("action_agent", e.target.value)}
                placeholder="my-agent"
                className={fieldClass()}
              />
            </label>
          )}

          {form.action_kind === "play" && (
            <label className={labelClass()}>
              <span className={labelTextClass()}>Playbook name</span>
              <input
                type="text"
                value={form.action_playbook}
                onChange={(e) => set("action_playbook", e.target.value)}
                placeholder="my-playbook"
                className={fieldClass()}
              />
            </label>
          )}

          <label className={labelClass()}>
            <span className={labelTextClass()}>Prompt</span>
            <textarea
              value={form.action_prompt}
              onChange={(e) => set("action_prompt", e.target.value)}
              rows={3}
              placeholder="Task prompt for the scheduled run..."
              className="rounded border border-edge bg-surface-base px-2.5 py-1.5 text-body text-content-primary placeholder:text-content-muted focus:border-interactive-primary focus:outline-none resize-none"
            />
          </label>

          <label className={labelClass()}>
            <span className={labelTextClass()}>Project</span>
            <input
              type="text"
              value={form.action_project}
              onChange={(e) => set("action_project", e.target.value)}
              placeholder="project-name"
              className={fieldClass()}
            />
          </label>

          {/* — Policies — */}
          <SectionHeading>Policies</SectionHeading>

          <div className="grid grid-cols-2 gap-3">
            <label className={labelClass()}>
              <span className={labelTextClass()}>Missed fire</span>
              <select
                value={form.missed_fire_policy}
                onChange={(e) => set("missed_fire_policy", e.target.value)}
                className={fieldClass()}
              >
                <option value="skip">Skip</option>
                <option value="run_once">Run once</option>
                <option value="run_all">Run all</option>
              </select>
            </label>

            <label className={labelClass()}>
              <span className={labelTextClass()}>Overlap</span>
              <select
                value={form.overlap_policy}
                onChange={(e) => set("overlap_policy", e.target.value)}
                className={fieldClass()}
              >
                <option value="skip">Skip</option>
                <option value="queue">Queue</option>
                <option value="kill_old">Kill old</option>
              </select>
            </label>
          </div>

          {/* — Advanced — */}
          <SectionHeading>Advanced</SectionHeading>

          <label className={labelClass()}>
            <span className={labelTextClass()}>on_success (JSON)</span>
            <textarea
              value={form.on_success_json}
              onChange={(e) => set("on_success_json", e.target.value)}
              rows={2}
              placeholder='{"notify": "slack"}'
              className="rounded border border-edge bg-surface-base px-2.5 py-1.5 font-mono text-[11px] text-content-primary placeholder:text-content-muted focus:border-interactive-primary focus:outline-none resize-none"
            />
          </label>

          <label className={labelClass()}>
            <span className={labelTextClass()}>on_fail (JSON)</span>
            <textarea
              value={form.on_fail_json}
              onChange={(e) => set("on_fail_json", e.target.value)}
              rows={2}
              placeholder='{"notify": "pagerduty"}'
              className="rounded border border-edge bg-surface-base px-2.5 py-1.5 font-mono text-[11px] text-content-primary placeholder:text-content-muted focus:border-interactive-primary focus:outline-none resize-none"
            />
          </label>

          {/* Error */}
          {error && (
            <div className="rounded border border-status-error/30 bg-status-error-bg px-3 py-2 text-body text-status-error">
              {error}
            </div>
          )}

          {/* Submit row */}
          <div className="flex justify-end gap-2 border-t border-edge pt-3">
            <Button variant="ghost" onClick={onClose} type="button">
              Cancel
            </Button>
            <Button variant="primary" type="submit" disabled={submitting}>
              {submitting ? "Creating..." : "Create Schedule"}
            </Button>
          </div>
        </form>
      </div>
    </div>
  );
}

// ─── Empty state ──────────────────────────────────────────────────────────────

function EmptyState({ onNew }: { onNew: () => void }) {
  return (
    <div className="col-span-full py-16 text-center">
      <div className="mx-auto mb-4 flex h-12 w-12 items-center justify-center rounded-full border border-edge bg-surface-raised text-content-muted text-xl">
        ◷
      </div>
      <p className="mb-1 text-body font-medium text-content-secondary">No schedules yet</p>
      <p className="mb-4 text-meta text-content-muted">
        Create a schedule to automate agent runs on cron, interval, or GitHub events.
      </p>
      <Button variant="primary" size="sm" onClick={onNew}>
        + New Schedule
      </Button>
    </div>
  );
}

// ─── Main page ────────────────────────────────────────────────────────────────

function SchedulesPageInner() {
  const [data, setData] = useState<ScheduleListResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showModal, setShowModal] = useState(false);
  const [recentRuns, setRecentRuns] = useState<ScheduleRunSummary[]>([]);
  const [runsLoading, setRunsLoading] = useState(false);

  async function load() {
    setLoading(true);
    try {
      const result = await listSchedules();
      setData(result);
      setError(null);

      // Load recent runs from each schedule (up to 3 runs each, first 5 schedules)
      const schedules = result.schedules.slice(0, 5);
      if (schedules.length > 0) {
        setRunsLoading(true);
        const runResults = await Promise.allSettled(
          schedules.map((s) => listScheduleRuns(s.id, { limit: 3 })),
        );
        const allRuns: ScheduleRunSummary[] = [];
        for (const r of runResults) {
          if (r.status === "fulfilled") {
            allRuns.push(...r.value.runs);
          }
        }
        // Sort by fired_at descending
        allRuns.sort((a, b) => b.fired_at - a.fired_at);
        setRecentRuns(allRuns.slice(0, 20));
        setRunsLoading(false);
      }
    } catch {
      setError("Failed to load schedules.");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- load() calls setState, but this is a data-fetch pattern matching the rest of the codebase
    void load();
  }, []);

  const schedules = data?.schedules ?? [];

  return (
    <main className="mx-auto flex w-full max-w-7xl flex-col gap-5 px-4 py-6 animate-page-enter">
      <PageHeader
        title="Schedules"
        subtitle="Automated agent runs on schedule or events"
        density="tight"
        badges={
          !loading && data ? (
            <span className="text-meta text-content-muted tabular-nums">
              {schedules.length} schedule{schedules.length !== 1 ? "s" : ""}
            </span>
          ) : null
        }
        actions={
          <Button variant="primary" size="sm" onClick={() => setShowModal(true)}>
            + New Schedule
          </Button>
        }
      />

      {error && (
        <div className="rounded border border-status-error/30 bg-status-error-bg px-3 py-2 text-body text-status-error">
          {error}
        </div>
      )}

      {/* Schedule grid */}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {loading ? (
          <>
            <SkeletonCard />
            <SkeletonCard />
            <SkeletonCard />
          </>
        ) : schedules.length === 0 ? (
          <EmptyState onNew={() => setShowModal(true)} />
        ) : (
          schedules.map((s) => (
            <ScheduleCard key={s.id} schedule={s} onRefresh={() => void load()} />
          ))
        )}
      </div>

      {/* Recent runs section */}
      {!loading && schedules.length > 0 && (
        <section className="flex flex-col gap-3">
          <div className="flex items-center justify-between border-t border-edge pt-4">
            <h2 className="font-mono text-sm font-semibold text-content-primary">Recent Runs</h2>
            {runsLoading && <span className="text-meta text-content-muted">Loading...</span>}
          </div>
          <div className="rounded-lg border border-edge bg-surface-raised p-4 shadow-card">
            <RecentRunsTable runs={recentRuns} />
          </div>
        </section>
      )}

      {showModal && (
        <CreateScheduleModal onClose={() => setShowModal(false)} onCreated={() => void load()} />
      )}
    </main>
  );
}

export default function SchedulesPage() {
  return (
    <Suspense>
      <SchedulesPageInner />
    </Suspense>
  );
}
