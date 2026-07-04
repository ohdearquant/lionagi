import { useEffect, useRef, useState } from "react";
import { useTranslations } from "use-intl";
import Button from "@/components/ui/Button";
import Modal from "@/components/ui/Modal";
import SectionLabel from "@/components/ui/SectionLabel";
import { FieldLabel, Input, TextArea, Select } from "@/components/ui/Field";
import ErrorBanner from "@/components/ui/ErrorBanner";
import TemplateVarChips from "./TemplateVarChips";
import { createSchedule } from "@/lib/api";

type TriggerType = "cron" | "interval" | "github_poll";
type ActionKind = "agent" | "flow" | "fanout" | "play";
type GitHubEvent = "pr_merged" | "pr_opened" | "pr_updated" | "pr_closed";

interface CreateForm {
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

const EMPTY_FORM: CreateForm = {
  name: "",
  description: "",
  trigger_type: "cron",
  cron_expr: "0 * * * *",
  interval_sec: "3600",
  github_repo: "",
  poll_interval_sec: "300",
  github_event: "pr_updated",
  github_base: "",
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

const PR_TEMPLATE_VARS = [
  "{{pr_number}}",
  "{{pr_title}}",
  "{{pr_url}}",
  "{{pr_author}}",
  "{{pr_state}}",
  "{{pr_merged_at}}",
  "{{repo}}",
];

export default function CreateScheduleModal({
  onClose,
  onCreated,
  initial,
}: {
  onClose: () => void;
  onCreated: () => void;
  initial?: Partial<CreateForm>;
}) {
  const t = useTranslations("schedules.create");
  const nameWrapRef = useRef<HTMLDivElement>(null);

  const [form, setForm] = useState<CreateForm>(() => {
    if (!initial) return EMPTY_FORM;
    const merged = { ...EMPTY_FORM };
    for (const [key, value] of Object.entries(initial)) {
      if (typeof value === "string" && value) {
        (merged as Record<string, string>)[key] = value;
      }
    }
    return merged;
  });
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const input = nameWrapRef.current?.querySelector("input");
    input?.focus();
  }, []);

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
      const event = form.github_event;
      const base = form.github_base.trim();
      if (event || base) {
        const filter: Record<string, string> = {};
        if (event) filter.event = event;
        if (base) filter.base = base;
        payload.github_filter = filter;
      }
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
        throw new Error(t("invalidJson", { field: "on_success" }));
      }
    }
    if (form.on_fail_json.trim()) {
      try {
        payload.on_fail = JSON.parse(form.on_fail_json);
      } catch {
        throw new Error(t("invalidJson", { field: "on_fail" }));
      }
    }

    return payload;
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!form.name.trim()) {
      setError(t("nameRequired"));
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
      setError(err instanceof Error ? err.message : t("createFailed"));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Modal
      title={t("title")}
      closeLabel={t("close")}
      onClose={onClose}
      maxWidth="max-w-2xl"
      className="flex max-h-[85vh] flex-col"
    >
      <form onSubmit={(e) => void handleSubmit(e)} className="flex min-h-0 flex-1 flex-col">
        <div className="flex-1 overflow-y-auto px-5 py-4">
          <div className="flex flex-col gap-3">
            {/* — Basic info — */}
            <SectionLabel className="border-t border-edge pt-3">{t("sectionBasic")}</SectionLabel>

            <div ref={nameWrapRef}>
              <FieldLabel
                label={
                  <>
                    {t("name")} <span className="text-status-error">*</span>
                  </>
                }
              >
                <Input
                  type="text"
                  value={form.name}
                  onChange={(e) => set("name", e.target.value)}
                  placeholder="nightly-report"
                  mono
                />
              </FieldLabel>
            </div>

            <FieldLabel label={t("description")}>
              <Input
                type="text"
                value={form.description}
                onChange={(e) => set("description", e.target.value)}
                placeholder={t("descriptionPlaceholder")}
              />
            </FieldLabel>

            {/* — Trigger config — */}
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

            {/* — Action config — */}
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
                rows={3}
                placeholder={t("promptPlaceholder")}
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

            {/* — Policies — */}
            <SectionLabel className="mt-1 border-t border-edge pt-3">
              {t("sectionPolicies")}
            </SectionLabel>

            <div className="grid grid-cols-2 gap-3">
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

            {/* — Advanced — */}
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
          </div>
        </div>

        {/* Pinned footer */}
        <div className="flex flex-col gap-2 border-t border-edge px-5 py-3">
          {error && <ErrorBanner>{error}</ErrorBanner>}
          <div className="flex justify-end gap-2">
            <Button variant="ghost" onClick={onClose} type="button">
              {t("cancel")}
            </Button>
            <Button variant="primary" type="submit" disabled={submitting}>
              {submitting ? t("creating") : t("submit")}
            </Button>
          </div>
        </div>
      </form>
    </Modal>
  );
}
