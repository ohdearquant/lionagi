"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import type { AgentProfileSummary, ModelConfig, WorkerFormData } from "@/lib/types";
import { listAgents } from "@/lib/api";
import ModelConfigTable from "./ModelConfigTable";
import StepEditor from "./StepEditor";
import type { StepData } from "./StepEditor";
import LinkEditor from "./LinkEditor";
import type { LinkData } from "./LinkEditor";

interface WorkerFormProps {
  initial?: WorkerFormData;
  mode: "create" | "edit";
  onSave: (data: WorkerFormData) => Promise<void>;
  saving?: boolean;
  errors?: string[];
}

const SECTION_LABEL = "text-sm font-semibold text-neutral-200";
const SECTION_DESC = "text-xs text-neutral-500";
const INPUT_CLS =
  "w-full rounded border border-neutral-700 bg-neutral-900 px-3 py-1.5 text-sm text-neutral-200 placeholder-neutral-600 focus:border-green-700 focus:outline-none";

function emptyForm(): WorkerFormData {
  return {
    name: "",
    description: "",
    use: { models: {} },
    steps: {},
    links: [],
  };
}

export default function WorkerForm({
  initial,
  mode,
  onSave,
  saving = false,
  errors = [],
}: WorkerFormProps) {
  const [form, setForm] = useState<WorkerFormData>(initial ?? emptyForm());
  const [agentProfiles, setAgentProfiles] = useState<AgentProfileSummary[]>([]);

  useEffect(() => {
    listAgents()
      .then((data) => setAgentProfiles(data.agents))
      .catch(() => {});
  }, []);

  const allRoleNames = useMemo(() => {
    const profileNames = agentProfiles.map((p) => p.name);
    const overrideNames = Object.keys(form.use.models);
    return Array.from(new Set([...profileNames, ...overrideNames])).sort();
  }, [agentProfiles, form.use.models]);

  const stepNames = useMemo(() => Object.keys(form.steps), [form.steps]);

  const handleModelsChange = useCallback((models: Record<string, ModelConfig>) => {
    setForm((prev) => ({ ...prev, use: { ...prev.use, models } }));
  }, []);

  const handleStepsChange = useCallback((steps: Record<string, StepData>) => {
    setForm((prev) => ({ ...prev, steps }));
  }, []);

  const handleLinksChange = useCallback((links: LinkData[]) => {
    // Strip internal _mode field before storing
    const cleaned = links.map(({ _mode, ...rest }) => rest);
    setForm((prev) => ({ ...prev, links: cleaned }));
  }, []);

  const handleSubmit = useCallback(
    async (e: React.FormEvent) => {
      e.preventDefault();
      await onSave(form);
    },
    [form, onSave],
  );

  const isValid = form.name.trim().length > 0 && Object.keys(form.steps).length > 0;

  return (
    <form onSubmit={handleSubmit} className="flex flex-col gap-6">
      {/* Section 1: Basics */}
      <section className="flex flex-col gap-3">
        <div>
          <h2 className={SECTION_LABEL}>Basics</h2>
          <p className={SECTION_DESC}>Worker name and description</p>
        </div>

        <div className="flex flex-col gap-1">
          <label className="text-xs uppercase text-neutral-500">Name</label>
          <input
            type="text"
            value={form.name}
            onChange={(e) => setForm((prev) => ({ ...prev, name: e.target.value }))}
            placeholder="my_worker"
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
            placeholder="What does this worker do?"
            className={INPUT_CLS}
          />
        </div>
      </section>

      {/* Section 2: Model Overrides */}
      <section className="flex flex-col gap-3">
        <div>
          <h2 className={SECTION_LABEL}>Model Overrides</h2>
          <p className={SECTION_DESC}>
            Per-role overrides — agent profiles provide defaults, these take priority
          </p>
        </div>
        <ModelConfigTable models={form.use.models} onChange={handleModelsChange} />
      </section>

      {/* Section 3: Steps */}
      <section className="flex flex-col gap-3">
        <div>
          <h2 className={SECTION_LABEL}>Steps</h2>
          <p className={SECTION_DESC}>Work steps with assignments and prompt templates</p>
        </div>
        <StepEditor
          steps={form.steps}
          roles={allRoleNames}
          agentProfiles={agentProfiles}
          modelOverrides={form.use.models}
          onChange={handleStepsChange}
        />
      </section>

      {/* Section 4: Links */}
      <section className="flex flex-col gap-3">
        <div>
          <h2 className={SECTION_LABEL}>Links</h2>
          <p className={SECTION_DESC}>Connections between steps with conditions or code handlers</p>
        </div>
        <LinkEditor links={form.links} stepNames={stepNames} onChange={handleLinksChange} />
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
          {saving ? "Saving..." : mode === "create" ? "Create Worker" : "Save Changes"}
        </button>
        {!isValid ? (
          <span className="text-xs text-neutral-500">Name and at least one step required</span>
        ) : null}
      </div>
    </form>
  );
}
