"use client";

import { useCallback } from "react";

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
        <div className="mb-1 grid grid-cols-[1fr_1fr_auto] gap-1 text-xs text-neutral-500">
          <span>from field</span>
          <span>to field</span>
          <span />
        </div>
      ) : null}

      {entries.map(([key, value], idx) => (
        <div key={idx} className="grid grid-cols-[1fr_1fr_auto] items-center gap-1">
          <input
            type="text"
            value={key}
            onChange={(e) => updateKey(key, e.target.value)}
            placeholder="source_field"
            className="rounded border border-neutral-700 bg-neutral-900 px-2 py-1 font-mono text-xs text-neutral-200 placeholder-neutral-600 focus:border-neutral-500 focus:outline-none"
          />
          <input
            type="text"
            value={value}
            onChange={(e) => updateValue(key, e.target.value)}
            placeholder="dest_field"
            className="rounded border border-neutral-700 bg-neutral-900 px-2 py-1 font-mono text-xs text-neutral-200 placeholder-neutral-600 focus:border-neutral-500 focus:outline-none"
          />
          <button
            type="button"
            onClick={() => removeEntry(key)}
            aria-label="Remove field mapping"
            className="flex h-6 w-6 items-center justify-center rounded text-neutral-500 hover:bg-neutral-800 hover:text-neutral-300"
          >
            x
          </button>
        </div>
      ))}

      <button
        type="button"
        onClick={addEntry}
        className="mt-1 self-start rounded border border-neutral-700 px-2 py-0.5 text-xs text-neutral-400 hover:border-neutral-500 hover:text-neutral-200"
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
    <div className="flex flex-col gap-3 rounded border border-neutral-800 bg-neutral-950 p-4">
      {/* Header row: from → to + mode toggle + delete */}
      <div className="flex flex-wrap items-center gap-3">
        {/* From */}
        <div className="flex min-w-0 flex-1 flex-col gap-0.5">
          <label className="text-xs uppercase text-neutral-500">from</label>
          <select
            value={link.from}
            onChange={(e) => set({ from: e.target.value })}
            className="rounded border border-neutral-700 bg-neutral-900 px-2 py-1 text-sm text-neutral-200 focus:border-neutral-500 focus:outline-none"
          >
            {stepNames.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
            {stepNames.length === 0 ? <option value="">— no steps defined —</option> : null}
          </select>
        </div>

        {/* Arrow */}
        <span className="mt-4 shrink-0 text-neutral-600" aria-hidden>
          →
        </span>

        {/* To */}
        <div className="flex min-w-0 flex-1 flex-col gap-0.5">
          <label className="text-xs uppercase text-neutral-500">to</label>
          <select
            value={link.to}
            onChange={(e) => set({ to: e.target.value })}
            className="rounded border border-neutral-700 bg-neutral-900 px-2 py-1 text-sm text-neutral-200 focus:border-neutral-500 focus:outline-none"
          >
            {stepNames.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
            {stepNames.length === 0 ? <option value="">— no steps defined —</option> : null}
          </select>
        </div>

        {/* Mode toggle (segmented control) */}
        <div className="mt-4 flex shrink-0 overflow-hidden rounded border border-neutral-700">
          {(["simple", "code"] as const).map((m) => (
            <label
              key={m}
              className={[
                "cursor-pointer px-3 py-1 text-xs transition-colors",
                mode === m
                  ? "bg-neutral-700 text-neutral-100"
                  : "bg-neutral-900 text-neutral-500 hover:bg-neutral-800 hover:text-neutral-300",
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
          className="mt-4 shrink-0 rounded border border-neutral-800 px-2 py-1 text-xs text-neutral-500 hover:border-red-900 hover:bg-red-950 hover:text-red-400"
        >
          Delete
        </button>
      </div>

      {/* Simple mode body */}
      {mode === "simple" ? (
        <div className="flex flex-col gap-3">
          {/* Condition */}
          <div className="flex flex-col gap-1">
            <label className="text-xs uppercase text-neutral-500">
              condition
              <span className="ml-1 normal-case text-neutral-600">(optional)</span>
            </label>
            <input
              type="text"
              value={link.condition ?? ""}
              onChange={(e) => set({ condition: e.target.value })}
              placeholder='e.g. "not approved"'
              className="rounded border border-neutral-700 bg-neutral-900 px-2 py-1.5 text-sm text-neutral-200 placeholder-neutral-600 focus:border-neutral-500 focus:outline-none"
            />
          </div>

          {/* Field map */}
          <div className="flex flex-col gap-1">
            <span className="text-xs uppercase text-neutral-500">field map</span>
            <FieldMapEditor map={link.map ?? {}} onChange={(map) => set({ map })} />
          </div>
        </div>
      ) : null}

      {/* Code mode body */}
      {mode === "code" ? (
        <div className="flex flex-col gap-1">
          <label className="text-xs uppercase text-neutral-500">handler</label>
          <textarea
            value={link.handler ?? ""}
            onChange={(e) => set({ handler: e.target.value })}
            rows={6}
            spellCheck={false}
            placeholder={"def handler(ctx):\n    return ctx"}
            className="rounded border border-neutral-700 bg-neutral-900 px-3 py-2 font-mono text-xs leading-5 text-neutral-200 placeholder-neutral-600 focus:border-neutral-500 focus:outline-none"
          />
        </div>
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
        <p className="rounded border border-neutral-800 px-4 py-6 text-center text-sm text-neutral-500">
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
        className="self-start rounded border border-neutral-700 px-3 py-1.5 text-sm text-neutral-400 hover:border-neutral-500 hover:text-neutral-200"
      >
        + Add Link
      </button>
    </div>
  );
}
