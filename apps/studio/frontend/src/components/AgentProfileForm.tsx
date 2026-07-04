"use client";

import { useCallback, useState } from "react";
import Button from "@/components/ui/Button";
import SectionLabel from "@/components/ui/SectionLabel";
import { FieldLabel, Input, Select, TextArea } from "@/components/ui/Field";
import type { AgentProfile } from "@/lib/types";

interface AgentProfileFormProps {
  initial?: AgentProfile;
  mode: "create" | "edit";
  onSave: (data: AgentProfile) => Promise<void>;
  saving?: boolean;
  errors?: string[];
}

const PROVIDER_OPTIONS = ["claude_code", "codex", "gemini_code"] as const;

type Provider = (typeof PROVIDER_OPTIONS)[number];

const MODEL_OPTIONS: Record<Provider, string[]> = {
  claude_code: ["sonnet", "opus", "haiku"],
  codex: ["gpt-5.4-mini", "gpt-5.4", "o3", "o4-mini"],
  gemini_code: ["gemini-2.5-pro", "gemini-2.5-flash"],
};

const PERMISSION_OPTIONS = ["default", "bypassPermissions"] as const;
const EFFORT_OPTIONS = ["none", "low", "medium", "high", "xhigh"] as const;

function isProvider(value: string): value is Provider {
  return (PROVIDER_OPTIONS as readonly string[]).includes(value);
}

function emptyForm(): AgentProfile {
  return {
    name: "",
    path: "",
    description: "",
    provider: "claude_code",
    model: MODEL_OPTIONS.claude_code[0],
    system_prompt: "",
    guidance: "",
  };
}

function normalizeForm(profile: AgentProfile): AgentProfile {
  // Many on-disk agent files predate the provider/model split (model carries
  // the prefix, e.g. "claude/claude-opus-4-6"). Infer provider from the model
  // string when frontmatter doesn't supply one, so the form is immediately
  // valid for save without forcing the user to pick a value that wasn't
  // missing in their data.
  let provider = profile.provider;
  if (!provider) {
    const model = profile.model ?? "";
    if (model.startsWith("claude") || model.includes("claude/")) provider = "claude_code";
    else if (model.startsWith("gpt") || model.startsWith("o3") || model.startsWith("o4"))
      provider = "codex";
    else if (model.startsWith("gemini")) provider = "gemini_code";
    else provider = "claude_code";
  }
  return {
    ...profile,
    provider,
    description: profile.description ?? "",
    system_prompt: profile.system_prompt ?? "",
    guidance: profile.guidance ?? "",
  };
}

export default function AgentProfileForm({
  initial,
  mode,
  onSave,
  saving = false,
  errors = [],
}: AgentProfileFormProps) {
  const [form, setForm] = useState<AgentProfile>(initial ? normalizeForm(initial) : emptyForm());

  const provider = isProvider(form.provider) ? form.provider : "claude_code";
  const availableModels = MODEL_OPTIONS[provider];

  const handleProviderChange = useCallback((newProvider: string) => {
    if (!isProvider(newProvider)) return;
    const firstModel = MODEL_OPTIONS[newProvider][0];
    setForm((prev) => ({
      ...prev,
      provider: newProvider,
      model: firstModel,
      // Clear provider-specific fields when switching
      permission_mode: newProvider === "claude_code" ? prev.permission_mode : undefined,
      reasoning_effort: newProvider === "codex" ? prev.reasoning_effort : undefined,
    }));
  }, []);

  const handleSubmit = useCallback(
    async (e: React.FormEvent) => {
      e.preventDefault();
      await onSave(form);
    },
    [form, onSave],
  );

  const isValid = form.name.trim().length > 0 && form.provider.length > 0 && form.model.length > 0;

  return (
    <form onSubmit={handleSubmit} className="flex flex-col gap-6">
      {/* Section 1: Basics */}
      <section className="flex flex-col gap-3">
        <div>
          <SectionLabel className="text-label font-semibold text-content-primary">
            Basics
          </SectionLabel>
          <p className="text-meta text-content-muted">Agent name and description</p>
        </div>

        <FieldLabel label="Name">
          <Input
            id="agent-name"
            type="text"
            value={form.name}
            onChange={(e) => setForm((prev) => ({ ...prev, name: e.target.value }))}
            placeholder="my_agent"
            disabled={mode === "edit"}
            mono
            className={mode === "edit" ? "cursor-not-allowed opacity-60" : undefined}
          />
        </FieldLabel>

        <FieldLabel label="Description">
          <Input
            id="agent-description"
            type="text"
            value={form.description}
            onChange={(e) => setForm((prev) => ({ ...prev, description: e.target.value }))}
            placeholder="What does this agent do?"
          />
        </FieldLabel>
      </section>

      {/* Section 2: Provider & Model */}
      <section className="flex flex-col gap-3">
        <div>
          <SectionLabel className="text-label font-semibold text-content-primary">
            Model
          </SectionLabel>
          <p className="text-meta text-content-muted">Provider and model selection</p>
        </div>

        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <FieldLabel label="Provider">
            <Select
              id="agent-provider"
              value={provider}
              onChange={(e) => handleProviderChange(e.target.value)}
            >
              {PROVIDER_OPTIONS.map((p) => (
                <option key={p} value={p}>
                  {p}
                </option>
              ))}
            </Select>
          </FieldLabel>

          <FieldLabel label="Model">
            <Input
              id="agent-model"
              type="text"
              list={`model-options-${provider}`}
              value={form.model}
              onChange={(e) => setForm((prev) => ({ ...prev, model: e.target.value }))}
              placeholder={availableModels[0]}
              mono
            />
            <datalist id={`model-options-${provider}`}>
              {availableModels.map((m) => (
                <option key={m} value={m} />
              ))}
            </datalist>
          </FieldLabel>

          {/* Permission Mode — claude_code only */}
          {provider === "claude_code" ? (
            <FieldLabel label="Permission Mode">
              <Select
                id="agent-perm-mode"
                value={form.permission_mode ?? "default"}
                onChange={(e) =>
                  setForm((prev) => ({
                    ...prev,
                    permission_mode: e.target.value === "default" ? undefined : e.target.value,
                  }))
                }
              >
                {PERMISSION_OPTIONS.map((p) => (
                  <option key={p} value={p}>
                    {p}
                  </option>
                ))}
              </Select>
            </FieldLabel>
          ) : null}

          {/* Reasoning Effort — codex only */}
          {provider === "codex" ? (
            <FieldLabel label="Reasoning Effort">
              <Select
                id="agent-reasoning-effort"
                value={form.reasoning_effort ?? "none"}
                onChange={(e) =>
                  setForm((prev) => ({
                    ...prev,
                    reasoning_effort: e.target.value === "none" ? undefined : e.target.value,
                  }))
                }
              >
                {EFFORT_OPTIONS.map((e) => (
                  <option key={e} value={e}>
                    {e}
                  </option>
                ))}
              </Select>
            </FieldLabel>
          ) : null}
        </div>
      </section>

      {/* Section 3: System Prompt */}
      <section className="flex flex-col gap-3">
        <div>
          <label
            htmlFor="agent-system-prompt"
            className="text-label font-semibold text-content-primary"
          >
            System Prompt
          </label>
          <p className="text-meta text-content-muted">
            Base identity and role instructions for this agent
          </p>
        </div>
        <TextArea
          id="agent-system-prompt"
          value={form.system_prompt ?? ""}
          onChange={(e) => setForm((prev) => ({ ...prev, system_prompt: e.target.value }))}
          placeholder="You are a specialized agent that..."
          rows={6}
          mono
        />
      </section>

      {/* Section 4: Guidance */}
      <section className="flex flex-col gap-3">
        <div>
          <label htmlFor="agent-guidance" className="text-label font-semibold text-content-primary">
            Guidance
          </label>
          <p className="text-meta text-content-muted">Task-specific instructions and constraints</p>
        </div>
        <TextArea
          id="agent-guidance"
          value={form.guidance ?? ""}
          onChange={(e) => setForm((prev) => ({ ...prev, guidance: e.target.value }))}
          placeholder="When working on this task, always..."
          rows={4}
          mono
        />
      </section>

      {/* Errors */}
      {errors.length > 0 ? (
        <div className="rounded border border-status-error/40 bg-status-error-bg px-4 py-3">
          <p className="text-meta font-semibold uppercase tracking-[0.06em] text-status-error">
            Validation errors
          </p>
          <ul className="mt-1 list-inside list-disc text-body text-status-error">
            {errors.map((err, i) => (
              <li key={i}>{err}</li>
            ))}
          </ul>
        </div>
      ) : null}

      {/* Submit */}
      <div className="flex items-center gap-3 border-t border-edge pt-4">
        <Button type="submit" variant="primary" disabled={saving || !isValid}>
          {saving ? "Saving..." : mode === "create" ? "Create Agent" : "Save Changes"}
        </Button>
        {!isValid ? (
          <span className="text-meta text-content-muted">Name, provider, and model required</span>
        ) : null}
      </div>
    </form>
  );
}
