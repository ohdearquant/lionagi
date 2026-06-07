// TODO(#1020 follow-up): SidePanel form labels need htmlFor/id wiring.
// Tracked as a Track 1 a11y follow-up — substantial refactor of label+input
// pairs across this file.

"use client";

import { useCallback } from "react";
import type { AgentProfileSummary, ModelConfig } from "@/lib/types";
import type { StepNodeData } from "./StepNode";
import type { ConditionEdgeData } from "./ConditionEdge";

const INPUT_CLS =
  "w-full rounded-md border border-edge bg-surface-input px-3 py-1.5 text-body text-content-primary placeholder-content-muted focus:border-interactive-primary focus:outline-none";
const LABEL_CLS = "block text-meta uppercase tracking-[0.06em] text-content-muted mb-1";

// ─── Types ───────────────────────────────────────────────

export type Selection =
  | { type: "none" }
  | { type: "node"; id: string; data: StepNodeData }
  | { type: "edge"; id: string; data: ConditionEdgeData }
  | { type: "exec-result"; id: string; data: StepNodeData; result: Record<string, unknown> };

interface SidePanelProps {
  selection: Selection;
  editable: boolean;
  roles: string[];
  agentProfiles?: AgentProfileSummary[];
  modelOverrides?: Record<string, ModelConfig>;
  onNodeUpdate?: (id: string, data: Partial<StepNodeData>) => void;
  onEdgeUpdate?: (id: string, data: Partial<ConditionEdgeData>) => void;
  onDelete?: (type: "node" | "edge", id: string) => void;
}

// ─── Component ───────────────────────────────────────────

export default function SidePanel({
  selection,
  editable,
  roles,
  agentProfiles = [],
  modelOverrides = {},
  onNodeUpdate,
  onEdgeUpdate,
  onDelete,
}: SidePanelProps) {
  if (selection.type === "none") {
    return (
      <div className="flex h-full items-center justify-center p-6">
        <p className="text-center text-sm text-content-muted">
          Click a step or link to inspect or edit
        </p>
      </div>
    );
  }

  if (selection.type === "exec-result") {
    return <ExecResultPanel id={selection.id} data={selection.data} result={selection.result} />;
  }

  if (selection.type === "edge") {
    return (
      <EdgePanel
        id={selection.id}
        data={selection.data}
        editable={editable}
        onUpdate={onEdgeUpdate}
        onDelete={onDelete}
      />
    );
  }

  return (
    <NodePanel
      id={selection.id}
      data={selection.data}
      editable={editable}
      roles={roles}
      agentProfiles={agentProfiles}
      modelOverrides={modelOverrides}
      onUpdate={onNodeUpdate}
      onDelete={onDelete}
    />
  );
}

// ─── Node Panel ──────────────────────────────────────────

function NodePanel({
  id,
  data,
  editable,
  roles,
  agentProfiles,
  modelOverrides,
  onUpdate,
  onDelete,
}: {
  id: string;
  data: StepNodeData;
  editable: boolean;
  roles: string[];
  agentProfiles: AgentProfileSummary[];
  modelOverrides: Record<string, ModelConfig>;
  onUpdate?: (id: string, data: Partial<StepNodeData>) => void;
  onDelete?: (type: "node" | "edge", id: string) => void;
}) {
  const update = useCallback(
    (field: string, value: unknown) => {
      onUpdate?.(id, { [field]: value });
    },
    [id, onUpdate],
  );

  const profile = agentProfiles.find((p) => p.name === data.role);
  const override = modelOverrides[data.role];

  return (
    <div className="flex flex-col gap-4 p-4 overflow-y-auto">
      <div className="flex items-center justify-between">
        <h3 className="font-mono text-sm font-semibold text-content-primary">{data.label}</h3>
        {editable && onDelete && (
          <button
            onClick={() => onDelete("node", id)}
            className="text-xs text-content-muted hover:text-status-error"
          >
            delete
          </button>
        )}
      </div>

      {/* Role */}
      <div>
        <label htmlFor={`${id}-role`} className={LABEL_CLS}>
          Role
        </label>
        {editable ? (
          <select
            id={`${id}-role`}
            value={data.role}
            onChange={(e) => update("role", e.target.value)}
            className={INPUT_CLS}
          >
            <option value="">— select —</option>
            {roles.map((r) => {
              const isP = agentProfiles.some((p) => p.name === r);
              const isO = r in modelOverrides;
              const tag =
                isP && isO
                  ? " (profile + override)"
                  : isP
                    ? " (profile)"
                    : isO
                      ? " (override)"
                      : "";
              return (
                <option key={r} value={r}>
                  {r}
                  {tag}
                </option>
              );
            })}
          </select>
        ) : (
          <div className="text-sm text-content-secondary">{data.role || "—"}</div>
        )}

        {data.role && (profile || override) && (
          <div className="mt-1 flex items-center gap-2 text-xs text-content-muted">
            <span>
              via {override ? "override" : "profile"}:{" "}
              <span className="font-mono text-content-secondary">
                {(override?.provider ?? profile?.provider) || "?"}/
                {(override?.model ?? profile?.model) || "?"}
              </span>
            </span>
          </div>
        )}
      </div>

      {/* Assignment */}
      <div>
        <label htmlFor={`${id}-assignment`} className={LABEL_CLS}>
          Assignment
        </label>
        {editable ? (
          <input
            id={`${id}-assignment`}
            type="text"
            value={data.assignment}
            onChange={(e) => update("assignment", e.target.value)}
            placeholder="inputs -> outputs"
            className={INPUT_CLS}
          />
        ) : (
          <div className="font-mono text-sm text-content-secondary">{data.assignment || "—"}</div>
        )}
      </div>

      {/* Prompt */}
      <div>
        <label htmlFor={`${id}-prompt`} className={LABEL_CLS}>
          Prompt Template
        </label>
        {editable ? (
          <textarea
            id={`${id}-prompt`}
            value={data.prompt}
            onChange={(e) => update("prompt", e.target.value)}
            placeholder="Use {field} syntax for inputs"
            rows={5}
            className={`${INPUT_CLS} resize-y font-mono`}
          />
        ) : (
          <pre className="whitespace-pre-wrap break-words rounded border border-edge bg-surface-base p-2 font-mono text-xs text-content-secondary">
            {data.prompt || "—"}
          </pre>
        )}
      </div>

      {/* Capacity + Timeout */}
      <div className="flex gap-3">
        <div className="flex-1">
          <label htmlFor={`${id}-capacity`} className={LABEL_CLS}>
            Capacity
          </label>
          {editable ? (
            <input
              id={`${id}-capacity`}
              type="number"
              min={1}
              value={data.capacity ?? 1}
              onChange={(e) => update("capacity", Number(e.target.value) || 1)}
              className={INPUT_CLS}
            />
          ) : (
            <div className="text-sm text-content-secondary">{data.capacity ?? 1}</div>
          )}
        </div>
        <div className="flex-1">
          <label htmlFor={`${id}-timeout`} className={LABEL_CLS}>
            Timeout (s)
          </label>
          {editable ? (
            <input
              id={`${id}-timeout`}
              type="number"
              min={0}
              value={data.timeout ?? ""}
              placeholder="none"
              onChange={(e) =>
                update("timeout", e.target.value === "" ? null : Number(e.target.value))
              }
              className={INPUT_CLS}
            />
          ) : (
            <div className="text-sm text-content-secondary">{data.timeout ?? "none"}</div>
          )}
        </div>
      </div>
    </div>
  );
}

// ─── Edge Panel ──────────────────────────────────────────

function EdgePanel({
  id,
  data,
  editable,
  onUpdate,
  onDelete,
}: {
  id: string;
  data: ConditionEdgeData;
  editable: boolean;
  onUpdate?: (id: string, data: Partial<ConditionEdgeData>) => void;
  onDelete?: (type: "node" | "edge", id: string) => void;
}) {
  const update = useCallback(
    (field: string, value: unknown) => {
      onUpdate?.(id, { [field]: value });
    },
    [id, onUpdate],
  );

  return (
    <div className="flex flex-col gap-4 p-4 overflow-y-auto">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold text-content-primary">Link</h3>
        {editable && onDelete && (
          <button
            onClick={() => onDelete("edge", id)}
            className="text-xs text-content-muted hover:text-status-error"
          >
            delete
          </button>
        )}
      </div>

      {/* Mode toggle */}
      <div>
        <p className={LABEL_CLS}>Mode</p>
        {editable ? (
          <div className="flex gap-1">
            {(["simple", "code"] as const).map((m) => (
              <button
                key={m}
                onClick={() => update("mode", m)}
                className={`rounded px-3 py-1 text-xs font-medium ${
                  data.mode === m
                    ? "bg-interactive-secondary text-content-primary"
                    : "bg-surface-input text-content-muted hover:text-content-primary"
                }`}
              >
                {m}
              </button>
            ))}
          </div>
        ) : (
          <div className="text-sm text-content-secondary">{data.mode}</div>
        )}
      </div>

      {data.mode === "simple" ? (
        <>
          {/* Condition */}
          <div>
            <label htmlFor={`${id}-condition`} className={LABEL_CLS}>
              Condition (optional)
            </label>
            {editable ? (
              <input
                id={`${id}-condition`}
                type="text"
                value={data.condition ?? ""}
                onChange={(e) => update("condition", e.target.value)}
                placeholder='e.g. "not approved"'
                className={INPUT_CLS}
              />
            ) : (
              <div className="font-mono text-sm text-content-secondary">
                {data.condition || "unconditional"}
              </div>
            )}
          </div>

          {/* Field map */}
          <div>
            <p className={LABEL_CLS}>Field Map</p>
            {data.map && Object.keys(data.map).length > 0 ? (
              <div className="flex flex-col gap-1">
                {Object.entries(data.map).map(([k, v]) => (
                  <div key={k} className="font-mono text-xs text-content-secondary">
                    {k} &rarr; {v}
                  </div>
                ))}
              </div>
            ) : (
              <div className="text-xs text-content-muted">No field mapping</div>
            )}
          </div>
        </>
      ) : (
        /* Code handler */
        <div>
          <label htmlFor={`${id}-handler`} className={LABEL_CLS}>
            Handler
          </label>
          {editable ? (
            <textarea
              id={`${id}-handler`}
              value={data.handler ?? ""}
              onChange={(e) => update("handler", e.target.value)}
              placeholder="Python code snippet"
              rows={6}
              className={`${INPUT_CLS} resize-y font-mono`}
            />
          ) : (
            <pre className="whitespace-pre-wrap break-words rounded border border-edge bg-surface-base p-2 font-mono text-xs text-content-secondary">
              {data.handler || "—"}
            </pre>
          )}
        </div>
      )}
    </div>
  );
}

// ─── Exec Result Panel ───────────────────────────────────

function ExecResultPanel({
  id,
  data,
  result,
}: {
  id: string;
  data: StepNodeData;
  result: Record<string, unknown>;
}) {
  return (
    <div className="flex flex-col gap-4 p-4 overflow-y-auto">
      <div className="flex items-center gap-2">
        <h3 className="font-mono text-label font-semibold text-status-success">{data.label}</h3>
        <span className="rounded-full border border-status-success/40 bg-status-success-bg px-2 py-0.5 text-meta text-status-success">
          completed
        </span>
      </div>

      {data.role && (
        <div className="text-xs text-content-muted">
          Role: <span className="text-content-secondary">{data.role}</span>
        </div>
      )}

      <div>
        <p className={LABEL_CLS}>Output</p>
        {Object.keys(result).length > 0 ? (
          <div className="flex flex-col gap-2">
            {Object.entries(result).map(([key, val]) => (
              <div key={key}>
                <span className="text-xs text-content-muted">{key}:</span>
                <p className="mt-0.5 whitespace-pre-wrap break-words text-sm text-content-secondary">
                  {typeof val === "string" ? val : JSON.stringify(val, null, 2)}
                </p>
              </div>
            ))}
          </div>
        ) : (
          <div className="text-xs text-content-muted">No output</div>
        )}
      </div>
    </div>
  );
}
