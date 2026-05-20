"use client";

import { useCallback, useState } from "react";
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

const SECTION_LABEL = "text-sm font-semibold text-neutral-200";
const SECTION_DESC = "text-xs text-neutral-500";
const INPUT_CLS =
  "w-full rounded border border-neutral-700 bg-neutral-900 px-3 py-1.5 text-sm text-neutral-200 placeholder-neutral-600 focus:border-green-700 focus:outline-none";
const SELECT_CLS =
  "w-full rounded border border-neutral-700 bg-neutral-900 px-3 py-1.5 text-sm text-neutral-200 focus:border-green-700 focus:outline-none";
const TEXTAREA_CLS =
  "w-full resize-y rounded border border-neutral-700 bg-neutral-900 px-3 py-2 font-mono text-sm text-neutral-200 placeholder-neutral-600 focus:border-green-700 focus:outline-none";

function isProvider(value: string): value is Provider {
  return (PROVIDER_OPTIONS as readonly string[]).includes(value);
}

function emptyForm(): AgentProfile {
  return {
    name: "",
    description: "",
    provider: "claude_code",
    model: MODEL_OPTIONS.claude_code[0],
    system_prompt: "",
    guidance: "",
  };
}

export default function AgentProfileForm({
  initial,
  mode,
  onSave,
  saving = false,
  errors = [],
}: AgentProfileFormProps) {
  const [form, setForm] = useState<AgentProfile>(initial ?? emptyForm());

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
          <h2 className={SECTION_LABEL}>Basics</h2>
          <p className={SECTION_DESC}>Agent name and description</p>
        </div>

        <div className="flex flex-col gap-1">
          <label className="text-xs uppercase text-neutral-500">Name</label>
          <input
            type="text"
            value={form.name}
            onChange={(e) => setForm((prev) => ({ ...prev, name: e.target.value }))}
            placeholder="my_agent"
            disabled={mode === "edit"}
            className={`${INPUT_CLS} ${mode === "edit" ? "cursor-not-allowed opacity-60" : ""}`}
          />
        </div>

        <div className="flex flex-col gap-1">
          <label className="text-xs uppercase text-neutral-500">Description</label>
          <input
            type="text"
            value={form.description}
            onChange={(e) => setForm((prev) => ({ ...prev, description: e.target.value }))}
            placeholder="What does this agent do?"
            className={INPUT_CLS}
          />
        </div>
      </section>

      {/* Section 2: Provider & Model */}
      <section className="flex flex-col gap-3">
        <div>
          <h2 className={SECTION_LABEL}>Model</h2>
          <p className={SECTION_DESC}>Provider and model selection</p>
        </div>

        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <div className="flex flex-col gap-1">
            <label className="text-xs uppercase text-neutral-500">Provider</label>
            <select
              value={provider}
              onChange={(e) => handleProviderChange(e.target.value)}
              className={SELECT_CLS}
            >
              {PROVIDER_OPTIONS.map((p) => (
                <option key={p} value={p}>
                  {p}
                </option>
              ))}
            </select>
          </div>

          <div className="flex flex-col gap-1">
            <label className="text-xs uppercase text-neutral-500">Model</label>
            <select
              value={form.model}
              onChange={(e) => setForm((prev) => ({ ...prev, model: e.target.value }))}
              className={SELECT_CLS}
            >
              {availableModels.map((m) => (
                <option key={m} value={m}>
                  {m}
                </option>
              ))}
            </select>
          </div>

          {/* Permission Mode — claude_code only */}
          {provider === "claude_code" ? (
            <div className="flex flex-col gap-1">
              <label className="text-xs uppercase text-neutral-500">Permission Mode</label>
              <select
                value={form.permission_mode ?? "default"}
                onChange={(e) =>
                  setForm((prev) => ({
                    ...prev,
                    permission_mode: e.target.value === "default" ? undefined : e.target.value,
                  }))
                }
                className={SELECT_CLS}
              >
                {PERMISSION_OPTIONS.map((p) => (
                  <option key={p} value={p}>
                    {p}
                  </option>
                ))}
              </select>
            </div>
          ) : null}

          {/* Reasoning Effort — codex only */}
          {provider === "codex" ? (
            <div className="flex flex-col gap-1">
              <label className="text-xs uppercase text-neutral-500">Reasoning Effort</label>
              <select
                value={form.reasoning_effort ?? "none"}
                onChange={(e) =>
                  setForm((prev) => ({
                    ...prev,
                    reasoning_effort: e.target.value === "none" ? undefined : e.target.value,
                  }))
                }
                className={SELECT_CLS}
              >
                {EFFORT_OPTIONS.map((e) => (
                  <option key={e} value={e}>
                    {e}
                  </option>
                ))}
              </select>
            </div>
          ) : null}
        </div>
      </section>

      {/* Section 3: System Prompt */}
      <section className="flex flex-col gap-3">
        <div>
          <h2 className={SECTION_LABEL}>System Prompt</h2>
          <p className={SECTION_DESC}>Base identity and role instructions for this agent</p>
        </div>
        <textarea
          value={form.system_prompt}
          onChange={(e) => setForm((prev) => ({ ...prev, system_prompt: e.target.value }))}
          placeholder="You are a specialized agent that..."
          rows={6}
          className={TEXTAREA_CLS}
        />
      </section>

      {/* Section 4: Guidance */}
      <section className="flex flex-col gap-3">
        <div>
          <h2 className={SECTION_LABEL}>Guidance</h2>
          <p className={SECTION_DESC}>Task-specific instructions and constraints</p>
        </div>
        <textarea
          value={form.guidance}
          onChange={(e) => setForm((prev) => ({ ...prev, guidance: e.target.value }))}
          placeholder="When working on this task, always..."
          rows={4}
          className={TEXTAREA_CLS}
        />
      </section>

      {/* Errors */}
      {errors.length > 0 ? (
        <div className="rounded border border-red-900 bg-red-950/40 px-4 py-3">
          <p className="text-xs font-semibold uppercase text-red-400">Validation errors</p>
          <ul className="mt-1 list-inside list-disc text-sm text-red-300">
            {errors.map((err, i) => (
              <li key={i}>{err}</li>
            ))}
          </ul>
        </div>
      ) : null}

      {/* Submit */}
      <div className="flex items-center gap-3 border-t border-neutral-800 pt-4">
        <button
          type="submit"
          disabled={saving || !isValid}
          className={`rounded px-6 py-2 text-sm font-medium transition ${
            saving || !isValid
              ? "cursor-not-allowed border border-neutral-700 bg-neutral-800 text-neutral-500"
              : "border border-green-700 bg-green-900/50 text-green-300 hover:bg-green-800/50"
          }`}
        >
          {saving ? "Saving..." : mode === "create" ? "Create Agent" : "Save Changes"}
        </button>
        {!isValid ? (
          <span className="text-xs text-neutral-500">Name, provider, and model required</span>
        ) : null}
      </div>
    </form>
  );
}
