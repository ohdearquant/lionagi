"use client";

import { useCallback, useState } from "react";
import type { AgentProfileSummary, ModelConfig } from "@/lib/types";

export interface StepData {
  assignment: string;
  role: string;
  prompt: string;
  capacity?: number;
  timeout?: number | null;
}

export interface StepEditorProps {
  steps: Record<string, StepData>;
  roles: string[];
  agentProfiles?: AgentProfileSummary[];
  modelOverrides?: Record<string, ModelConfig>;
  onChange: (steps: Record<string, StepData>) => void;
}

const INPUT_CLS =
  "w-full rounded border border-neutral-700 bg-neutral-900 px-3 py-1.5 text-sm text-neutral-200 placeholder-neutral-600 focus:border-neutral-500 focus:outline-none";

const LABEL_CLS = "block text-xs uppercase tracking-wide text-neutral-500";

function generateStepName(existing: Record<string, StepData>): string {
  let n = 1;
  while (Object.prototype.hasOwnProperty.call(existing, `step_${n}`)) {
    n += 1;
  }
  return `step_${n}`;
}

interface StepCardProps {
  stepKey: string;
  data: StepData;
  roles: string[];
  agentProfiles?: AgentProfileSummary[];
  modelOverrides?: Record<string, ModelConfig>;
  onChangeName: (oldKey: string, newKey: string) => void;
  onChangeField: (key: string, field: keyof StepData, value: StepData[keyof StepData]) => void;
  onDelete: (key: string) => void;
}

function StepCard({
  stepKey,
  data,
  roles,
  agentProfiles = [],
  modelOverrides = {},
  onChangeName,
  onChangeField,
  onDelete,
}: StepCardProps) {
  const [expanded, setExpanded] = useState(true);
  const [localName, setLocalName] = useState(stepKey);
  const [nameError, setNameError] = useState<string | null>(null);

  const handleNameBlur = useCallback(() => {
    const trimmed = localName.trim();
    if (!trimmed) {
      setLocalName(stepKey);
      setNameError(null);
      return;
    }
    if (trimmed === stepKey) {
      setNameError(null);
      return;
    }
    setNameError(null);
    onChangeName(stepKey, trimmed);
  }, [localName, stepKey, onChangeName]);

  const handleNameKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLInputElement>) => {
      if (e.key === "Enter") {
        e.currentTarget.blur();
      } else if (e.key === "Escape") {
        setLocalName(stepKey);
        setNameError(null);
        e.currentTarget.blur();
      }
    },
    [stepKey],
  );

  return (
    <div className="rounded border border-neutral-800 bg-neutral-950 p-4">
      {/* Card header */}
      <div className="flex items-center justify-between gap-3">
        <button
          type="button"
          aria-label={expanded ? "Collapse step" : "Expand step"}
          aria-expanded={expanded}
          onClick={() => setExpanded((v) => !v)}
          className="shrink-0 text-neutral-600 hover:text-neutral-300 focus:outline-none focus-visible:ring-2 focus-visible:ring-neutral-500 rounded"
        >
          <span aria-hidden="true" className="font-mono text-sm">
            {expanded ? "▾" : "▸"}
          </span>
        </button>

        <input
          type="text"
          value={localName}
          onChange={(e) => setLocalName(e.target.value)}
          onBlur={handleNameBlur}
          onKeyDown={handleNameKeyDown}
          aria-label="Step name"
          className="min-w-0 flex-1 rounded border border-transparent bg-transparent px-1 py-0.5 font-mono text-sm font-semibold text-neutral-200 hover:border-neutral-700 focus:border-neutral-500 focus:outline-none"
        />

        {nameError ? <span className="shrink-0 text-xs text-red-400">{nameError}</span> : null}

        <button
          type="button"
          onClick={() => onDelete(stepKey)}
          aria-label={`Delete step ${stepKey}`}
          className="shrink-0 rounded px-2 py-0.5 text-xs text-neutral-600 hover:bg-neutral-800 hover:text-red-400 focus:outline-none focus-visible:ring-2 focus-visible:ring-neutral-500"
        >
          delete
        </button>
      </div>

      {/* Collapsible body */}
      {expanded ? (
        <div className="mt-4 flex flex-col gap-3">
          {/* Role */}
          <div className="flex flex-col gap-1">
            <label htmlFor={`${stepKey}-role`} className={LABEL_CLS}>
              Role
            </label>
            {roles.length > 0 ? (
              <select
                id={`${stepKey}-role`}
                value={data.role}
                onChange={(e) => onChangeField(stepKey, "role", e.target.value)}
                className={INPUT_CLS}
              >
                <option value="">— select role —</option>
                {roles.map((r) => {
                  const isProfile = agentProfiles.some((p) => p.name === r);
                  const isOverride = r in modelOverrides;
                  const suffix =
                    isProfile && isOverride
                      ? " (profile + override)"
                      : isProfile
                        ? " (profile)"
                        : isOverride
                          ? " (override)"
                          : "";
                  return (
                    <option key={r} value={r}>
                      {r}
                      {suffix}
                    </option>
                  );
                })}
              </select>
            ) : (
              <input
                id={`${stepKey}-role`}
                type="text"
                value={data.role}
                onChange={(e) => onChangeField(stepKey, "role", e.target.value)}
                placeholder="e.g. implementer"
                className={INPUT_CLS}
              />
            )}

            {/* Resolved config badge */}
            {data.role &&
              (() => {
                const profile = agentProfiles.find((p) => p.name === data.role);
                const override = modelOverrides[data.role];
                if (!profile && !override) return null;

                const provider = override?.provider ?? profile?.provider ?? "—";
                const model = override?.model ?? profile?.model ?? "—";
                const source = override ? (profile ? "override" : "worker") : "profile";

                return (
                  <div className="flex items-center gap-2 rounded border border-neutral-800 bg-neutral-900/50 px-2 py-1 text-xs text-neutral-400">
                    <span className="text-neutral-500">via {source}:</span>
                    <span className="font-mono text-neutral-300">
                      {provider}/{model}
                    </span>
                  </div>
                );
              })()}
          </div>

          {/* Assignment */}
          <div className="flex flex-col gap-1">
            <label htmlFor={`${stepKey}-assignment`} className={LABEL_CLS}>
              Assignment
            </label>
            <input
              id={`${stepKey}-assignment`}
              type="text"
              value={data.assignment}
              onChange={(e) => onChangeField(stepKey, "assignment", e.target.value)}
              placeholder="inputs -> outputs"
              className={INPUT_CLS}
            />
          </div>

          {/* Prompt template */}
          <div className="flex flex-col gap-1">
            <label htmlFor={`${stepKey}-prompt`} className={LABEL_CLS}>
              Prompt Template
            </label>
            <textarea
              id={`${stepKey}-prompt`}
              value={data.prompt}
              onChange={(e) => onChangeField(stepKey, "prompt", e.target.value)}
              placeholder={
                "Use {field} syntax to reference inputs.\nExample: Analyze {task} and produce a {plan}."
              }
              rows={3}
              className={`${INPUT_CLS} resize-y font-mono`}
            />
          </div>

          {/* Capacity + Timeout row */}
          <div className="flex gap-3">
            <div className="flex flex-1 flex-col gap-1">
              <label htmlFor={`${stepKey}-capacity`} className={LABEL_CLS}>
                Capacity
              </label>
              <input
                id={`${stepKey}-capacity`}
                type="number"
                min={1}
                value={data.capacity ?? 1}
                onChange={(e) => {
                  const v = e.target.value === "" ? 1 : Number(e.target.value);
                  onChangeField(stepKey, "capacity", v);
                }}
                className={INPUT_CLS}
              />
            </div>

            <div className="flex flex-1 flex-col gap-1">
              <label htmlFor={`${stepKey}-timeout`} className={LABEL_CLS}>
                Timeout (s)
              </label>
              <input
                id={`${stepKey}-timeout`}
                type="number"
                min={0}
                value={data.timeout ?? ""}
                placeholder="none"
                onChange={(e) => {
                  const v = e.target.value === "" ? null : Number(e.target.value);
                  onChangeField(stepKey, "timeout", v);
                }}
                className={INPUT_CLS}
              />
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}

export default function StepEditor({
  steps,
  roles,
  agentProfiles,
  modelOverrides,
  onChange,
}: StepEditorProps) {
  const handleChangeName = useCallback(
    (oldKey: string, newKey: string) => {
      if (newKey === oldKey) return;
      if (Object.prototype.hasOwnProperty.call(steps, newKey)) {
        // Silently ignore — duplicate name; StepCard resets to oldKey via blur logic
        return;
      }

      // Preserve insertion order: rebuild object replacing oldKey with newKey at same position
      const next: Record<string, StepData> = {};
      for (const [k, v] of Object.entries(steps)) {
        if (k === oldKey) {
          next[newKey] = v;
        } else {
          next[k] = v;
        }
      }
      onChange(next);
    },
    [steps, onChange],
  );

  const handleChangeField = useCallback(
    (key: string, field: keyof StepData, value: StepData[keyof StepData]) => {
      onChange({ ...steps, [key]: { ...steps[key], [field]: value } });
    },
    [steps, onChange],
  );

  const handleDelete = useCallback(
    (key: string) => {
      const next = { ...steps };
      delete next[key];
      onChange(next);
    },
    [steps, onChange],
  );

  const handleAddStep = useCallback(() => {
    const name = generateStepName(steps);
    onChange({
      ...steps,
      [name]: {
        assignment: "",
        role: roles.length > 0 ? roles[0] : "",
        prompt: "",
        capacity: 1,
        timeout: null,
      },
    });
  }, [steps, roles, onChange]);

  const stepEntries = Object.entries(steps);

  return (
    <div className="flex flex-col gap-3">
      {stepEntries.length === 0 ? (
        <p className="rounded border border-dashed border-neutral-800 px-4 py-6 text-center text-sm text-neutral-600">
          No steps defined. Add one below.
        </p>
      ) : (
        stepEntries.map(([key, data]) => (
          <StepCard
            key={key}
            stepKey={key}
            data={data}
            roles={roles}
            agentProfiles={agentProfiles}
            modelOverrides={modelOverrides}
            onChangeName={handleChangeName}
            onChangeField={handleChangeField}
            onDelete={handleDelete}
          />
        ))
      )}

      <button
        type="button"
        onClick={handleAddStep}
        className="self-start rounded border border-neutral-700 bg-neutral-900 px-4 py-1.5 text-sm text-neutral-300 hover:border-neutral-500 hover:bg-neutral-800 focus:outline-none focus-visible:ring-2 focus-visible:ring-neutral-500"
      >
        + Add Step
      </button>
    </div>
  );
}
