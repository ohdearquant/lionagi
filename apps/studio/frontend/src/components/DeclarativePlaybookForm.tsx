"use client";

import { useCallback, useState } from "react";
import Button from "@/components/ui/Button";
import SectionLabel from "@/components/ui/SectionLabel";
import { FieldLabel, Input, Select, TextArea } from "@/components/ui/Field";
import IconButton from "@/components/ui/IconButton";
import { IconClose } from "@/components/ui/icons";
import type { DeclarativeArgSpec, DeclarativePlaybookData } from "@/lib/types";

interface DeclarativePlaybookFormProps {
  initial: DeclarativePlaybookData;
  onSave: (data: DeclarativePlaybookData) => Promise<void>;
  saving?: boolean;
  errors?: string[];
}

const EFFORT_OPTIONS = ["", "low", "medium", "high", "xhigh"] as const;
const ARG_TYPE_OPTIONS = ["str", "int", "float", "bool"] as const;

const CHECKBOX_LABEL = "flex items-center gap-2 text-meta text-content-secondary";

export default function DeclarativePlaybookForm({
  initial,
  onSave,
  saving = false,
  errors = [],
}: DeclarativePlaybookFormProps) {
  const [form, setForm] = useState<DeclarativePlaybookData>(initial);

  const handleSubmit = useCallback(
    async (e: React.FormEvent) => {
      e.preventDefault();
      await onSave(form);
    },
    [form, onSave],
  );

  const updateArg = useCallback((index: number, patch: Partial<DeclarativeArgSpec>) => {
    setForm((prev) => {
      const next = [...prev.args];
      next[index] = { ...next[index], ...patch };
      return { ...prev, args: next };
    });
  }, []);

  const addArg = useCallback(() => {
    setForm((prev) => ({
      ...prev,
      args: [...prev.args, { name: "", type: "str", default: "", help: "" }],
    }));
  }, []);

  const removeArg = useCallback((index: number) => {
    setForm((prev) => ({
      ...prev,
      args: prev.args.filter((_, i) => i !== index),
    }));
  }, []);

  const isValid = form.prompt.trim().length > 0;

  return (
    <form onSubmit={handleSubmit} className="flex flex-col gap-6">
      {/* Basics */}
      <section className="flex flex-col gap-3">
        <div>
          <SectionLabel className="text-label font-semibold text-content-primary">
            Basics
          </SectionLabel>
          <p className="text-meta text-content-muted">
            What this playbook does and how it&apos;s invoked
          </p>
        </div>

        <FieldLabel label="Description">
          <Input
            id="pb-description"
            type="text"
            value={form.description}
            onChange={(e) => setForm((prev) => ({ ...prev, description: e.target.value }))}
            placeholder="One-line summary shown in the playbook list"
          />
        </FieldLabel>

        <FieldLabel
          label="Argument hint"
          hint="Shown next to the playbook name in CLI help. Free-form string."
        >
          <Input
            id="pb-arg-hint"
            type="text"
            value={form.argumentHint}
            onChange={(e) => setForm((prev) => ({ ...prev, argumentHint: e.target.value }))}
            placeholder="[--scope SCOPE]"
            mono
          />
        </FieldLabel>
      </section>

      {/* Execution */}
      <section className="flex flex-col gap-3">
        <div>
          <SectionLabel className="text-label font-semibold text-content-primary">
            Execution
          </SectionLabel>
          <p className="text-meta text-content-muted">
            Which agent runs this and how it&apos;s configured
          </p>
        </div>

        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <FieldLabel label="Agent">
            <Input
              id="pb-agent"
              type="text"
              value={form.agent}
              onChange={(e) => setForm((prev) => ({ ...prev, agent: e.target.value }))}
              placeholder="orchestrator"
              mono
            />
          </FieldLabel>

          <FieldLabel label="Effort">
            <Select
              id="pb-effort"
              value={form.effort}
              onChange={(e) => setForm((prev) => ({ ...prev, effort: e.target.value }))}
            >
              {EFFORT_OPTIONS.map((e) => (
                <option key={e || "default"} value={e}>
                  {e || "— (default)"}
                </option>
              ))}
            </Select>
          </FieldLabel>

          <FieldLabel label="Max ops">
            <Input
              id="pb-max-ops"
              type="number"
              min={0}
              value={form.maxOps ?? ""}
              onChange={(e) =>
                setForm((prev) => ({
                  ...prev,
                  maxOps: e.target.value === "" ? null : Number(e.target.value),
                }))
              }
              placeholder="unlimited"
            />
          </FieldLabel>
        </div>

        <div className="flex flex-wrap items-center gap-4 pt-1">
          <label className={CHECKBOX_LABEL}>
            <input
              type="checkbox"
              checked={form.yolo}
              onChange={(e) => setForm((prev) => ({ ...prev, yolo: e.target.checked }))}
              className="h-4 w-4 rounded border-edge bg-surface-input text-interactive-primary focus:ring-interactive-primary"
            />
            <span>yolo (auto-approve tool calls)</span>
          </label>
          <label className={CHECKBOX_LABEL}>
            <input
              type="checkbox"
              checked={form.showGraph}
              onChange={(e) => setForm((prev) => ({ ...prev, showGraph: e.target.checked }))}
              className="h-4 w-4 rounded border-edge bg-surface-input text-interactive-primary focus:ring-interactive-primary"
            />
            <span>show-graph (render DAG during run)</span>
          </label>
        </div>
      </section>

      {/* Args */}
      <section className="flex flex-col gap-3">
        <div className="flex items-end justify-between">
          <div>
            <SectionLabel className="text-label font-semibold text-content-primary">
              Arguments
            </SectionLabel>
            <p className="text-meta text-content-muted">CLI flags this playbook accepts</p>
          </div>
          <Button type="button" variant="secondary" size="sm" onClick={addArg}>
            + Add argument
          </Button>
        </div>

        {form.args.length === 0 ? (
          <p className="rounded border border-dashed border-edge px-3 py-4 text-center text-meta text-content-muted">
            No arguments. Add one to expose a CLI flag.
          </p>
        ) : (
          <div className="overflow-x-auto rounded border border-edge">
            <table className="w-full text-meta">
              <thead className="bg-surface-raised">
                <tr>
                  <th className="px-2 py-1.5 text-left font-semibold text-content-secondary">
                    Name
                  </th>
                  <th className="px-2 py-1.5 text-left font-semibold text-content-secondary">
                    Type
                  </th>
                  <th className="px-2 py-1.5 text-left font-semibold text-content-secondary">
                    Default
                  </th>
                  <th className="px-2 py-1.5 text-left font-semibold text-content-secondary">
                    Help
                  </th>
                  <th className="w-10 px-2 py-1.5"></th>
                </tr>
              </thead>
              <tbody>
                {form.args.map((arg, i) => (
                  <tr key={i} className="border-t border-edge">
                    <td className="px-2 py-1">
                      <Input
                        type="text"
                        aria-label={`Argument ${i + 1} name`}
                        value={arg.name}
                        onChange={(e) => updateArg(i, { name: e.target.value })}
                        placeholder="scope"
                        mono
                      />
                    </td>
                    <td className="px-2 py-1">
                      <Select
                        aria-label={`Argument ${i + 1} type`}
                        value={arg.type}
                        onChange={(e) => updateArg(i, { type: e.target.value })}
                      >
                        {ARG_TYPE_OPTIONS.map((t) => (
                          <option key={t} value={t}>
                            {t}
                          </option>
                        ))}
                      </Select>
                    </td>
                    <td className="px-2 py-1">
                      <Input
                        type="text"
                        aria-label={`Argument ${i + 1} default value`}
                        value={arg.default}
                        onChange={(e) => updateArg(i, { default: e.target.value })}
                        placeholder="auto"
                      />
                    </td>
                    <td className="px-2 py-1">
                      <Input
                        type="text"
                        aria-label={`Argument ${i + 1} help text`}
                        value={arg.help}
                        onChange={(e) => updateArg(i, { help: e.target.value })}
                        placeholder="What this flag does"
                      />
                    </td>
                    <td className="px-2 py-1 text-right">
                      <IconButton
                        aria-label={`Remove argument ${arg.name || String(i + 1)}`}
                        onClick={() => removeArg(i)}
                        className="hover:text-status-error"
                      >
                        <IconClose size={11} strokeWidth={2.25} />
                      </IconButton>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {/* Prompt */}
      <section className="flex flex-col gap-3">
        <div>
          <SectionLabel className="text-label font-semibold text-content-primary">
            Prompt
          </SectionLabel>
          <p className="text-meta text-content-muted">
            The instruction sent to the agent. Markdown-friendly. Use{" "}
            <code className="rounded bg-surface-overlay px-1 font-data">{"{{scope}}"}</code> to
            interpolate args.
          </p>
        </div>
        <TextArea
          aria-label="Prompt"
          value={form.prompt}
          onChange={(e) => setForm((prev) => ({ ...prev, prompt: e.target.value }))}
          placeholder="# Task title&#10;&#10;## Shape&#10;research/report. Parallel researchers → analyst → critic.&#10;..."
          rows={16}
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
          {saving ? "Saving..." : "Save changes"}
        </Button>
        {!isValid ? <span className="text-meta text-content-muted">Prompt is required</span> : null}
      </div>
    </form>
  );
}
