import { useEffect, useRef, useState } from "react";
import { useNavigate } from "@tanstack/react-router";
import { useLocale, useTranslations } from "use-intl";
import Button from "@/components/ui/Button";
import ConfirmButton from "@/components/ui/ConfirmButton";
import SectionLabel from "@/components/ui/SectionLabel";
import { FieldLabel, Input, TextArea, Select } from "@/components/ui/Field";
import IconButton from "@/components/ui/IconButton";
import StatusPill from "@/components/ui/StatusPill";
import { IconArrowUpRight, IconClose } from "@/components/ui/icons";
import { useToast } from "@/components/ui/Toast";
import ErrorBanner from "@/components/ui/ErrorBanner";
import EnabledToggle from "./EnabledToggle";
import TemplateVarChips from "./TemplateVarChips";
import { classifyError } from "./errorClassify";
import { KNOWN_RUN_STATUSES, formatDelta, formatInterval, toMs } from "./data";
import {
  getSchedule,
  getInvocation,
  updateSchedule,
  deleteSchedule,
  triggerSchedule,
  listScheduleRuns,
} from "@/lib/api";
import type { ScheduleDetail, ScheduleRunSummary } from "@/lib/types";

// Run history is a first-class section on the detail page (not a 5-item
// sidebar afterthought) — fetch enough of it to read as a real timeline.
const DETAIL_RUNS_LIMIT = 50;

type TriggerType = "cron" | "interval" | "github_poll";
type ActionKind = "agent" | "flow" | "fanout" | "play";
type GitHubEvent = "pr_merged" | "pr_opened" | "pr_updated" | "pr_closed";

const PR_TEMPLATE_VARS = [
  "{{pr_number}}",
  "{{pr_title}}",
  "{{pr_url}}",
  "{{pr_author}}",
  "{{pr_state}}",
  "{{pr_merged_at}}",
  "{{repo}}",
];

const STATUS_DOT: Record<string, string> = {
  running: "var(--status-running)",
  completed: "var(--status-success)",
  failed: "var(--status-error)",
  skipped: "var(--content-muted)",
  cancelled: "var(--content-muted)",
};

interface DetailForm {
  name: string;
  description: string;
  trigger_type: TriggerType;
  cron_expr: string;
  interval_sec: string;
  github_repo: string;
  poll_interval_sec: string;
  github_event: GitHubEvent;
  github_base: string;
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

function detailToForm(d: ScheduleDetail): DetailForm {
  return {
    name: d.name,
    description: d.description ?? "",
    trigger_type: d.trigger_type,
    cron_expr: d.cron_expr ?? "0 * * * *",
    interval_sec: String(d.interval_sec ?? 3600),
    github_repo: d.github_repo ?? "",
    poll_interval_sec: String(d.poll_interval_sec ?? 300),
    github_event: (d.github_filter?.event as GitHubEvent | undefined) ?? "pr_updated",
    github_base: d.github_filter?.base ?? "",
    action_kind: d.action_kind,
    action_model: d.action_model ?? "",
    action_prompt: d.action_prompt ?? "",
    action_agent: d.action_agent ?? "",
    action_playbook: d.action_playbook ?? "",
    action_project: d.action_project ?? "",
    missed_fire_policy: d.missed_fire_policy,
    overlap_policy: d.overlap_policy,
    on_success_json: d.on_success != null ? JSON.stringify(d.on_success, null, 2) : "",
    on_fail_json: d.on_fail != null ? JSON.stringify(d.on_fail, null, 2) : "",
  };
}

function isDirty(draft: DetailForm, baseline: DetailForm): boolean {
  return (Object.keys(draft) as Array<keyof DetailForm>).some((k) => draft[k] !== baseline[k]);
}

function buildPayload(form: DetailForm, baseline: DetailForm): Record<string, unknown> {
  const p: Record<string, unknown> = {};

  if (form.name !== baseline.name) p.name = form.name.trim();
  if (form.description !== baseline.description) p.description = form.description.trim() || null;

  if (form.trigger_type !== baseline.trigger_type) p.trigger_type = form.trigger_type;

  if (form.trigger_type === "cron") {
    if (form.cron_expr !== baseline.cron_expr) p.cron_expr = form.cron_expr.trim();
  } else if (form.trigger_type === "interval") {
    if (form.interval_sec !== baseline.interval_sec) p.interval_sec = Number(form.interval_sec);
  } else if (form.trigger_type === "github_poll") {
    if (form.github_repo !== baseline.github_repo) p.github_repo = form.github_repo.trim();
    if (form.poll_interval_sec !== baseline.poll_interval_sec)
      p.poll_interval_sec = Number(form.poll_interval_sec);
    if (form.github_event !== baseline.github_event || form.github_base !== baseline.github_base) {
      const filter: Record<string, string> = {};
      if (form.github_event) filter.event = form.github_event;
      if (form.github_base.trim()) filter.base = form.github_base.trim();
      p.github_filter = filter;
    }
  }

  if (form.action_kind !== baseline.action_kind) p.action_kind = form.action_kind;
  if (form.action_model !== baseline.action_model)
    p.action_model = form.action_model.trim() || null;
  if (form.action_prompt !== baseline.action_prompt)
    p.action_prompt = form.action_prompt.trim() || null;
  if (form.action_agent !== baseline.action_agent)
    p.action_agent = form.action_agent.trim() || null;
  if (form.action_playbook !== baseline.action_playbook)
    p.action_playbook = form.action_playbook.trim() || null;
  if (form.action_project !== baseline.action_project)
    p.action_project = form.action_project.trim() || null;
  if (form.missed_fire_policy !== baseline.missed_fire_policy)
    p.missed_fire_policy = form.missed_fire_policy;
  if (form.overlap_policy !== baseline.overlap_policy) p.overlap_policy = form.overlap_policy;

  return p;
}

function triggerSummary(d: ScheduleDetail, every: (s: string) => string): string {
  if (d.trigger_type === "cron" && d.cron_expr) return d.cron_expr;
  if (d.trigger_type === "interval" && d.interval_sec != null)
    return every(formatInterval(d.interval_sec));
  if (d.trigger_type === "github_poll" && d.github_repo) {
    const poll =
      d.poll_interval_sec != null ? ` · ${every(formatInterval(d.poll_interval_sec))}` : "";
    return `${d.github_repo}${poll}`;
  }
  return d.trigger_type;
}

export default function ScheduleDetailModal({
  scheduleId,
  onClose,
  onChanged,
}: {
  scheduleId: string;
  onClose: () => void;
  onChanged: () => void;
}) {
  const t = useTranslations("schedules.detail");
  const tc = useTranslations("schedules.card");
  const tError = useTranslations("schedules.error");
  const tRun = useTranslations("schedules.run");
  const tStatus = useTranslations("history.status");
  const locale = useLocale();
  const { toast } = useToast();
  const navigate = useNavigate();
  const nameInputRef = useRef<HTMLInputElement>(null);

  const [detail, setDetail] = useState<ScheduleDetail | null>(null);
  const [loadErr, setLoadErr] = useState(false);
  const [recentRuns, setRecentRuns] = useState<ScheduleRunSummary[]>([]);
  const [expandedRunIds, setExpandedRunIds] = useState<Set<string>>(new Set());

  const [form, setForm] = useState<DetailForm | null>(null);
  const [baseline, setBaseline] = useState<DetailForm | null>(null);

  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [triggering, setTriggering] = useState(false);

  // Load detail + recent runs on mount
  useEffect(() => {
    let alive = true;
    void (async () => {
      try {
        const [d, runsResp] = await Promise.all([
          getSchedule(scheduleId),
          listScheduleRuns(scheduleId, { limit: DETAIL_RUNS_LIMIT }),
        ]);
        if (!alive) return;
        setDetail(d);
        const f = detailToForm(d);
        setForm(f);
        setBaseline(f);
        setRecentRuns(runsResp.runs);
      } catch {
        if (alive) setLoadErr(true);
      }
    })();
    return () => {
      alive = false;
    };
  }, [scheduleId]);

  // Focus name input once loaded
  useEffect(() => {
    if (form) nameInputRef.current?.focus();
  }, [form != null]); // eslint-disable-line react-hooks/exhaustive-deps

  // Escape closes
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  function set(key: keyof DetailForm, value: string) {
    setForm((prev) => (prev ? { ...prev, [key]: value } : prev));
  }

  async function handleSave() {
    if (!form || !baseline || !detail) return;
    setSaveError(null);

    // Validate JSON fields
    let onSuccessVal: unknown = undefined;
    let onFailVal: unknown = undefined;
    if (form.on_success_json.trim()) {
      try {
        onSuccessVal = JSON.parse(form.on_success_json);
      } catch {
        setSaveError(t("invalidJson", { field: "on_success" }));
        return;
      }
    }
    if (form.on_fail_json.trim()) {
      try {
        onFailVal = JSON.parse(form.on_fail_json);
      } catch {
        setSaveError(t("invalidJson", { field: "on_fail" }));
        return;
      }
    }

    const payload = buildPayload(form, baseline);
    if (form.on_success_json !== baseline.on_success_json)
      payload.on_success = form.on_success_json.trim() ? onSuccessVal : null;
    if (form.on_fail_json !== baseline.on_fail_json)
      payload.on_fail = form.on_fail_json.trim() ? onFailVal : null;

    setSaving(true);
    try {
      await updateSchedule(detail.id, payload);
      onChanged();
      onClose();
    } catch {
      setSaveError(t("saveFailed"));
    } finally {
      setSaving(false);
    }
  }

  async function handleTrigger() {
    if (!detail) return;
    setTriggering(true);
    try {
      const res = await triggerSchedule(detail.id);
      toast(tc("runStarted", { id: res.run_id.slice(0, 8) }), "success");
      onChanged();
    } catch {
      toast(tc("triggerFailed"), "error");
    } finally {
      setTriggering(false);
    }
  }

  async function handleDeleteConfirmed() {
    if (!detail) return;
    try {
      await deleteSchedule(detail.id);
      onChanged();
      onClose();
    } catch {
      toast(t("deleteFailed"), "error");
    }
  }

  function toggleExpanded(runId: string) {
    setExpandedRunIds((prev) => {
      const next = new Set(prev);
      if (next.has(runId)) next.delete(runId);
      else next.add(runId);
      return next;
    });
  }

  async function handleOpenRun(run: ScheduleRunSummary) {
    if (!run.invocation_id) return;
    try {
      const inv = await getInvocation(run.invocation_id);
      const sessionId = inv.sessions[0]?.id;
      await navigate({ to: "/fleet", search: sessionId ? { s: sessionId } : {} });
    } catch {
      await navigate({ to: "/fleet" });
    }
  }

  const dirty = form && baseline ? isDirty(form, baseline) : false;
  const enabled = detail ? Boolean(detail.enabled) : false;
  // Snapshot once on mount — good enough for relative time display in the runs list
  const [nowMs] = useState(() => Date.now());

  return (
    // eslint-disable-next-line jsx-a11y/no-static-element-interactions, jsx-a11y/click-events-have-key-events
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        className="flex max-h-[88vh] w-full max-w-3xl flex-col rounded-lg border border-edge bg-surface-raised shadow-card"
      >
        {/* ── Header ── */}
        <div className="shrink-0 border-b border-edge px-5 py-3">
          {!detail ? (
            <div className="flex h-10 items-center">
              <span className="text-body text-content-muted">{t("loading")}</span>
            </div>
          ) : (
            <>
              <div className="flex items-center gap-3">
                {/* Trello-style inline name edit */}
                <input
                  ref={nameInputRef}
                  value={form?.name ?? detail.name}
                  onChange={(e) => set("name", e.target.value)}
                  className="min-w-0 flex-1 border-b border-transparent bg-transparent font-data text-[length:var(--t-lg)] font-semibold text-content-primary focus:border-interactive-primary focus:outline-none"
                />
                <EnabledToggle
                  scheduleId={detail.id}
                  enabled={enabled}
                  onToggled={() => {
                    onChanged();
                    // Optimistically flip so the toggle reflects state
                    setDetail((prev) => (prev ? { ...prev, enabled: prev.enabled ? 0 : 1 } : prev));
                  }}
                />
                <IconButton aria-label={t("close")} onClick={onClose}>
                  <IconClose size={12} strokeWidth={2} />
                </IconButton>
              </div>
              {/* Meta strip */}
              <div className="mt-1 flex items-center gap-2 font-data text-[length:var(--t-xs)] text-content-muted">
                <span>{triggerSummary(detail, (s) => tc("every", { interval: s }))}</span>
                {/* A paused schedule never fires — show "Paused", not a stale next-fire time. */}
                {!enabled ? (
                  <>
                    <span aria-hidden>·</span>
                    <span>{tc("paused")}</span>
                  </>
                ) : (
                  detail.next_fire_at != null && (
                    <>
                      <span aria-hidden>·</span>
                      <span>
                        {t("nextFire")}{" "}
                        {new Date(toMs(detail.next_fire_at)).toLocaleString(locale, {
                          month: "short",
                          day: "numeric",
                          hour: "2-digit",
                          minute: "2-digit",
                          hour12: false,
                        })}
                      </span>
                    </>
                  )
                )}
                {detail.recent_runs[0] && (
                  <>
                    <span aria-hidden>·</span>
                    <span
                      className="inline-flex items-center gap-1"
                      style={{
                        color: STATUS_DOT[detail.recent_runs[0].status] ?? "var(--content-muted)",
                      }}
                    >
                      {detail.recent_runs[0].status}
                    </span>
                  </>
                )}
              </div>
            </>
          )}
        </div>

        {/* ── Body ── */}
        {loadErr ? (
          <div className="flex flex-1 items-center justify-center p-8">
            <span className="text-body text-status-error">{t("loadFailed")}</span>
          </div>
        ) : !form || !detail ? (
          <div className="flex flex-1 items-center justify-center p-8">
            <span className="text-body text-content-muted">{t("loading")}</span>
          </div>
        ) : (
          <div className="flex min-h-0 flex-1 overflow-y-auto">
            {/* Main column */}
            <div className="flex flex-1 flex-col gap-3 overflow-y-auto px-5 py-4">
              {/* Description */}
              <SectionLabel className="border-t border-edge pt-3">{t("sectionDesc")}</SectionLabel>
              <TextArea
                value={form.description}
                onChange={(e) => set("description", e.target.value)}
                rows={3}
                placeholder={t("descriptionPlaceholder")}
              />

              {/* Trigger */}
              <SectionLabel className="mt-1 border-t border-edge pt-3">
                {t("sectionTrigger")}
              </SectionLabel>

              <FieldLabel label={t("triggerType")}>
                <Select
                  value={form.trigger_type}
                  onChange={(e) => set("trigger_type", e.target.value as TriggerType)}
                >
                  <option value="cron">{t("triggerCron")}</option>
                  <option value="interval">{t("triggerInterval")}</option>
                  <option value="github_poll">{t("triggerGithub")}</option>
                </Select>
              </FieldLabel>

              {form.trigger_type === "cron" && (
                <FieldLabel label={t("cronExpr")} hint={t("cronHint")}>
                  <Input
                    type="text"
                    value={form.cron_expr}
                    onChange={(e) => set("cron_expr", e.target.value)}
                    placeholder="0 * * * *"
                    mono
                  />
                </FieldLabel>
              )}

              {form.trigger_type === "interval" && (
                <FieldLabel label={t("intervalSec")}>
                  <Input
                    type="number"
                    min={1}
                    value={form.interval_sec}
                    onChange={(e) => set("interval_sec", e.target.value)}
                    placeholder="3600"
                  />
                </FieldLabel>
              )}

              {form.trigger_type === "github_poll" && (
                <>
                  <FieldLabel label={t("githubRepo")}>
                    <Input
                      type="text"
                      value={form.github_repo}
                      onChange={(e) => set("github_repo", e.target.value)}
                      placeholder="owner/repo"
                      mono
                    />
                  </FieldLabel>

                  <FieldLabel label={t("githubEvent")}>
                    <Select
                      value={form.github_event}
                      onChange={(e) => set("github_event", e.target.value as GitHubEvent)}
                    >
                      <option value="pr_merged">{t("githubEventPrMerged")}</option>
                      <option value="pr_opened">{t("githubEventPrOpened")}</option>
                      <option value="pr_updated">{t("githubEventPrUpdated")}</option>
                      <option value="pr_closed">{t("githubEventPrClosed")}</option>
                    </Select>
                  </FieldLabel>

                  <FieldLabel label={t("githubBase")} hint={t("githubBaseHint")}>
                    <Input
                      type="text"
                      value={form.github_base}
                      onChange={(e) => set("github_base", e.target.value)}
                      placeholder="main"
                      mono
                    />
                  </FieldLabel>

                  <FieldLabel label={t("pollIntervalSec")}>
                    <Input
                      type="number"
                      min={60}
                      value={form.poll_interval_sec}
                      onChange={(e) => set("poll_interval_sec", e.target.value)}
                      placeholder="300"
                    />
                  </FieldLabel>
                </>
              )}

              {/* Action */}
              <SectionLabel className="mt-1 border-t border-edge pt-3">
                {t("sectionAction")}
              </SectionLabel>

              <FieldLabel label={t("actionKind")}>
                <Select
                  value={form.action_kind}
                  onChange={(e) => set("action_kind", e.target.value as ActionKind)}
                >
                  <option value="agent">{t("kindAgent")}</option>
                  <option value="flow">{t("kindFlow")}</option>
                  <option value="fanout">{t("kindFanout")}</option>
                  <option value="play">{t("kindPlay")}</option>
                </Select>
              </FieldLabel>

              <FieldLabel label={t("model")} hint={t("modelHint")}>
                <Input
                  type="text"
                  value={form.action_model}
                  onChange={(e) => set("action_model", e.target.value)}
                  placeholder="e.g. claude_code/sonnet"
                />
              </FieldLabel>

              {(form.action_kind === "agent" || form.action_kind === "flow") && (
                <FieldLabel label={t("agentName")}>
                  <Input
                    type="text"
                    value={form.action_agent}
                    onChange={(e) => set("action_agent", e.target.value)}
                    placeholder="my-agent"
                  />
                </FieldLabel>
              )}

              {form.action_kind === "play" && (
                <FieldLabel label={t("playbookName")}>
                  <Input
                    type="text"
                    value={form.action_playbook}
                    onChange={(e) => set("action_playbook", e.target.value)}
                    placeholder="my-playbook"
                  />
                </FieldLabel>
              )}

              <FieldLabel label={t("prompt")}>
                <TextArea
                  value={form.action_prompt}
                  onChange={(e) => set("action_prompt", e.target.value)}
                  rows={6}
                  placeholder={t("promptPlaceholder")}
                  className="font-data"
                />
              </FieldLabel>

              {form.trigger_type === "github_poll" && (
                <TemplateVarChips vars={PR_TEMPLATE_VARS} hint={t("githubVarsHint")} />
              )}

              <FieldLabel label={t("project")}>
                <Input
                  type="text"
                  value={form.action_project}
                  onChange={(e) => set("action_project", e.target.value)}
                  placeholder="project-name"
                />
              </FieldLabel>

              {/* Advanced */}
              <SectionLabel className="mt-1 border-t border-edge pt-3">
                {t("sectionAdvanced")}
              </SectionLabel>

              <FieldLabel label="on_success (JSON)">
                <TextArea
                  value={form.on_success_json}
                  onChange={(e) => set("on_success_json", e.target.value)}
                  rows={2}
                  placeholder='{"notify": "slack"}'
                  mono
                />
              </FieldLabel>

              <FieldLabel label="on_fail (JSON)">
                <TextArea
                  value={form.on_fail_json}
                  onChange={(e) => set("on_fail_json", e.target.value)}
                  rows={2}
                  placeholder='{"notify": "pagerduty"}'
                  mono
                />
              </FieldLabel>

              {/* History — the run timeline that used to live in the kanban
                  Done column; a classified one-liner shows for every failed
                  run, with the raw traceback expandable per-row. */}
              <SectionLabel className="mt-1 border-t border-edge pt-3">
                {t("sectionHistory")}
              </SectionLabel>
              {recentRuns.length === 0 ? (
                <p className="text-meta text-content-muted">{t("noRuns")}</p>
              ) : (
                <div className="flex flex-col gap-1.5">
                  {recentRuns.map((r) => {
                    const firedMs = toMs(r.fired_at);
                    const durationMs = r.ended_at != null ? toMs(r.ended_at) - firedMs : null;
                    const statusLabel = KNOWN_RUN_STATUSES.has(r.status)
                      ? tStatus(r.status as Parameters<typeof tStatus>[0])
                      : undefined;
                    const errorLine =
                      r.status === "failed" ? classifyError(r.error_detail, tError) : null;
                    const expanded = expandedRunIds.has(r.id);
                    return (
                      <div
                        key={r.id}
                        className="flex flex-col gap-1 rounded border border-edge px-2.5 py-1.5"
                      >
                        <div className="flex items-center gap-2 text-meta">
                          <StatusPill value={r.status} taxonomy="session" label={statusLabel} />
                          <span className="min-w-0 flex-1 text-content-secondary">
                            {formatDelta(nowMs - firedMs)}
                            {t("ago")}
                          </span>
                          {durationMs != null && durationMs >= 1000 && (
                            <span className="shrink-0 font-data tabular-nums text-content-muted">
                              {formatDelta(durationMs)}
                            </span>
                          )}
                          {r.invocation_id && (
                            <IconButton
                              aria-label={tRun("openRun")}
                              title={tRun("openRun")}
                              onClick={() => void handleOpenRun(r)}
                            >
                              <IconArrowUpRight size={12} strokeWidth={2} />
                            </IconButton>
                          )}
                        </div>
                        {errorLine && (
                          <div className="flex items-start justify-between gap-2">
                            <span className="min-w-0 flex-1 truncate text-meta text-status-error">
                              {errorLine}
                            </span>
                            <button
                              type="button"
                              onClick={() => toggleExpanded(r.id)}
                              className="shrink-0 text-meta text-content-muted underline-offset-2 hover:text-content-primary hover:underline"
                            >
                              {expanded ? tError("hideDetails") : tError("showDetails")}
                            </button>
                          </div>
                        )}
                        {expanded && r.error_detail && (
                          <pre className="max-h-48 overflow-auto whitespace-pre-wrap break-words rounded bg-surface-overlay p-2 font-data text-[length:var(--t-xs)] text-content-secondary">
                            {r.error_detail}
                          </pre>
                        )}
                      </div>
                    );
                  })}
                </div>
              )}
            </div>

            {/* Sidebar */}
            <div className="hidden shrink-0 flex-col gap-4 border-l border-edge px-4 py-4 md:flex md:w-56">
              {/* Actions rail */}
              <div>
                <SectionLabel className="mb-2">{t("sideActions")}</SectionLabel>
                <div className="flex flex-col gap-1.5">
                  <button
                    type="button"
                    disabled={triggering}
                    onClick={() => void handleTrigger()}
                    className="w-full rounded border border-edge bg-surface-overlay px-3 py-1.5 text-left text-meta text-content-secondary transition-colors hover:border-edge-strong hover:text-content-primary disabled:opacity-50"
                  >
                    {triggering ? t("triggering") : t("runNow")}
                  </button>
                  <ConfirmButton
                    idleLabel={t("delete")}
                    confirmLabel={t("deleteConfirm")}
                    onConfirm={() => {
                      void handleDeleteConfirmed();
                    }}
                  />
                </div>
              </div>

              {/* Policies */}
              <div>
                <SectionLabel className="mb-2">{t("sidePolices")}</SectionLabel>
                <div className="flex flex-col gap-2">
                  <FieldLabel label={t("missedFire")}>
                    <Select
                      value={form.missed_fire_policy}
                      onChange={(e) => set("missed_fire_policy", e.target.value)}
                    >
                      <option value="skip">{t("missedSkip")}</option>
                      <option value="run_once">{t("missedRunOnce")}</option>
                      <option value="run_all">{t("missedRunAll")}</option>
                    </Select>
                  </FieldLabel>

                  <FieldLabel label={t("overlap")}>
                    <Select
                      value={form.overlap_policy}
                      onChange={(e) => set("overlap_policy", e.target.value)}
                    >
                      <option value="skip">{t("overlapSkip")}</option>
                      <option value="queue">{t("overlapQueue")}</option>
                      <option value="kill_old">{t("overlapKillOld")}</option>
                    </Select>
                  </FieldLabel>
                </div>
              </div>
            </div>
          </div>
        )}

        {/* ── Footer ── */}
        <div className="shrink-0 border-t border-edge px-5 py-3">
          {saveError && (
            <ErrorBanner size="meta" className="mb-2">
              {saveError}
            </ErrorBanner>
          )}
          <div className="flex justify-end gap-2">
            <Button variant="ghost" onClick={onClose}>
              {t("cancel")}
            </Button>
            <Button variant="primary" disabled={!dirty || saving} onClick={() => void handleSave()}>
              {saving ? t("saving") : t("save")}
            </Button>
          </div>
        </div>
      </div>
    </div>
  );
}
