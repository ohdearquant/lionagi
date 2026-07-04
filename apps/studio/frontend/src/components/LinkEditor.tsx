"use client";

import { useCallback } from "react";
import IconButton from "@/components/ui/IconButton";
import { IconArrowRight } from "@/components/ui/icons";
import { FieldLabel, Input, Select, TextArea } from "@/components/ui/Field";

export interface LinkData {
  from: string;
  to: string;
  condition?: string;
  map?: Record<string, string>;
  handler?: string;
  /** Internal mode flag — not part of the persisted schema but needed for the editor. */
  _mode?: "simple" | "code";
}

export interface LinkEditorProps {
  links: LinkData[];
  stepNames: string[];
  onChange: (links: LinkData[]) => void;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function effectiveMode(link: LinkData): "simple" | "code" {
  if (link._mode) return link._mode;
  return link.handler !== undefined && link.handler !== "" ? "code" : "simple";
}

function blankLink(stepNames: string[]): LinkData {
  return {
    from: stepNames[0] ?? "",
    to: stepNames[1] ?? stepNames[0] ?? "",
    condition: "",
    map: {},
    handler: undefined,
    _mode: "simple",
  };
}

// ---------------------------------------------------------------------------
// Sub-component: FieldMapEditor
// ---------------------------------------------------------------------------

interface FieldMapEditorProps {
  map: Record<string, string>;
  onChange: (map: Record<string, string>) => void;
}

function FieldMapEditor({ map, onChange }: FieldMapEditorProps) {
  const entries = Object.entries(map);

  const updateKey = (oldKey: string, newKey: string) => {
    const next: Record<string, string> = {};
    for (const [k, v] of Object.entries(map)) {
      next[k === oldKey ? newKey : k] = v;
    }
    onChange(next);
  };

  const updateValue = (key: string, value: string) => {
    onChange({ ...map, [key]: value });
  };

  const removeEntry = (key: string) => {
    const next = { ...map };
    delete next[key];
    onChange(next);
  };

  const addEntry = () => {
    // Generate a unique blank key so multiple blank rows don't collide
    let key = "";
    let suffix = 0;
    while (key in map) {
      key = `key${suffix === 0 ? "" : suffix}`;
      suffix++;
    }
    onChange({ ...map, [key]: "" });
  };

  return (
    <div className="flex flex-col gap-1">
      {entries.length > 0 ? (
        <div className="mb-1 grid grid-cols-[1fr_1fr_auto] gap-1 text-meta text-content-muted">
          <span>from field</span>
          <span>to field</span>
          <span />
        </div>
      ) : null}

      {entries.map(([key, value], idx) => (
        <div key={idx} className="grid grid-cols-[1fr_1fr_auto] items-center gap-1">
          <Input
            type="text"
            aria-label={`Field mapping ${idx + 1} source`}
            value={key}
            onChange={(e) => updateKey(key, e.target.value)}
            placeholder="source_field"
            mono
          />
          <Input
            type="text"
            aria-label={`Field mapping ${idx + 1} destination`}
            value={value}
            onChange={(e) => updateValue(key, e.target.value)}
            placeholder="dest_field"
            mono
          />
          <IconButton aria-label="Remove field mapping" onClick={() => removeEntry(key)}>
            x
          </IconButton>
        </div>
      ))}

      <button
        type="button"
        onClick={addEntry}
        className="mt-1 self-start rounded border border-edge px-2 py-0.5 text-meta text-content-muted hover:border-edge-strong hover:text-content-secondary"
      >
        + Add field
      </button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Sub-component: LinkCard
// ---------------------------------------------------------------------------

interface LinkCardProps {
  link: LinkData;
  index: number;
  stepNames: string[];
  onUpdate: (index: number, updated: LinkData) => void;
  onDelete: (index: number) => void;
}

function LinkCard({ link, index, stepNames, onUpdate, onDelete }: LinkCardProps) {
  const mode = effectiveMode(link);

  const set = useCallback(
    (patch: Partial<LinkData>) => {
      onUpdate(index, { ...link, ...patch });
    },
    [index, link, onUpdate],
  );

  const switchMode = (next: "simple" | "code") => {
    if (next === "code") {
      set({ _mode: "code", condition: undefined, map: undefined });
    } else {
      set({ _mode: "simple", handler: undefined, condition: "", map: {} });
    }
  };

  return (
    <div className="flex flex-col gap-3 rounded border border-edge bg-surface-raised p-4">
      {/* Header row: from → to + mode toggle + delete */}
      <div className="flex flex-wrap items-center gap-3">
        {/* From */}
        <FieldLabel label="from" className="min-w-0 flex-1">
          <Select
            id={`link-${index}-from`}
            value={link.from}
            onChange={(e) => set({ from: e.target.value })}
          >
            {stepNames.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
            {stepNames.length === 0 ? <option value="">— no steps defined —</option> : null}
          </Select>
        </FieldLabel>

        {/* Arrow */}
        <span className="mt-4 flex shrink-0 items-center text-content-muted" aria-hidden>
          <IconArrowRight size={12} strokeWidth={2} />
        </span>

        {/* To */}
        <FieldLabel label="to" className="min-w-0 flex-1">
          <Select
            id={`link-${index}-to`}
            value={link.to}
            onChange={(e) => set({ to: e.target.value })}
          >
            {stepNames.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
            {stepNames.length === 0 ? <option value="">— no steps defined —</option> : null}
          </Select>
        </FieldLabel>

        {/* Mode toggle (segmented control) */}
        <div className="mt-4 flex shrink-0 overflow-hidden rounded border border-edge">
          {(["simple", "code"] as const).map((m) => (
            <label
              key={m}
              className={[
                "cursor-pointer px-3 py-1 text-meta transition-colors",
                mode === m
                  ? "bg-surface-overlay text-content-primary"
                  : "bg-surface-base text-content-muted hover:bg-surface-overlay hover:text-content-secondary",
              ].join(" ")}
            >
              <input
                type="radio"
                name={`link-mode-${index}`}
                value={m}
                checked={mode === m}
                onChange={() => switchMode(m)}
                className="sr-only"
              />
              {m}
            </label>
          ))}
        </div>

        {/* Delete */}
        <button
          type="button"
          onClick={() => onDelete(index)}
          aria-label={`Delete link ${link.from} → ${link.to}`}
          className="mt-4 shrink-0 rounded border border-edge px-2 py-1 text-meta text-content-muted hover:border-status-error/40 hover:bg-status-error-bg hover:text-status-error"
        >
          Delete
        </button>
      </div>

      {/* Simple mode body */}
      {mode === "simple" ? (
        <div className="flex flex-col gap-3">
          {/* Condition */}
          <FieldLabel
            label={
              <>
                condition <span className="normal-case text-content-muted">(optional)</span>
              </>
            }
          >
            <Input
              id={`link-${index}-condition`}
              type="text"
              value={link.condition ?? ""}
              onChange={(e) => set({ condition: e.target.value })}
              placeholder='e.g. "not approved"'
            />
          </FieldLabel>

          {/* Field map */}
          <div className="flex flex-col gap-1">
            <span className="text-meta font-medium text-content-muted">field map</span>
            <FieldMapEditor map={link.map ?? {}} onChange={(map) => set({ map })} />
          </div>
        </div>
      ) : null}

      {/* Code mode body */}
      {mode === "code" ? (
        <FieldLabel label="handler">
          <TextArea
            id={`link-${index}-handler`}
            value={link.handler ?? ""}
            onChange={(e) => set({ handler: e.target.value })}
            rows={6}
            spellCheck={false}
            placeholder={"def handler(ctx):\n    return ctx"}
            mono
          />
        </FieldLabel>
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main export: LinkEditor
// ---------------------------------------------------------------------------

export default function LinkEditor({ links, stepNames, onChange }: LinkEditorProps) {
  const handleUpdate = useCallback(
    (index: number, updated: LinkData) => {
      const next = links.map((link, i) => (i === index ? updated : link));
      onChange(next);
    },
    [links, onChange],
  );

  const handleDelete = useCallback(
    (index: number) => {
      onChange(links.filter((_, i) => i !== index));
    },
    [links, onChange],
  );

  const handleAdd = () => {
    onChange([...links, blankLink(stepNames)]);
  };

  return (
    <div className="flex flex-col gap-3">
      {links.length === 0 ? (
        <p className="rounded border border-edge px-4 py-6 text-center text-body text-content-muted">
          No links defined. Add one below.
        </p>
      ) : null}

      {links.map((link, index) => (
        <LinkCard
          key={index}
          link={link}
          index={index}
          stepNames={stepNames}
          onUpdate={handleUpdate}
          onDelete={handleDelete}
        />
      ))}

      <button
        type="button"
        onClick={handleAdd}
        className="self-start rounded border border-edge px-3 py-1.5 text-body text-content-muted hover:border-edge-strong hover:text-content-secondary"
      >
        + Add Link
      </button>
    </div>
  );
}
