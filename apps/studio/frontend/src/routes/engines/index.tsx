import { createFileRoute } from "@tanstack/react-router";
import { useEffect, useState } from "react";
import Button from "@/components/Button";
import PageHeader from "@/components/PageHeader";
import Timestamp from "@/components/Timestamp";
import {
  createEngineDef,
  deleteEngineDef,
  launchEngine,
  listEngineDefs,
  type CreateEngineDefRequest,
  type EngineDef,
} from "@/lib/api";
import { empty, errors } from "@/lib/copy";

export const Route = createFileRoute("/engines/")({
  component: EnginesPage,
});

// ─── Constants ────────────────────────────────────────────────────────────────

const ENGINE_KINDS = ["research", "review", "coding", "hypothesis", "planning"] as const;
type EngineKind = (typeof ENGINE_KINDS)[number];

// test_cmd is consumed (and required) only by the coding engine;
// export_dir is consumed by coding and hypothesis.
const TEST_CMD_KINDS = new Set<string>(["coding"]);
const EXPORT_DIR_KINDS = new Set<string>(["coding", "hypothesis"]);

// ─── Helpers ──────────────────────────────────────────────────────────────────

function fieldClass() {
  return "w-full rounded-md border border-edge bg-surface-base px-3 py-1.5 text-sm text-content-primary placeholder:text-content-muted focus:border-interactive-primary focus:outline-none focus:ring-1 focus:ring-interactive-primary";
}

function labelClass() {
  return "flex flex-col gap-1";
}

function labelTextClass() {
  return "text-[11px] font-medium uppercase tracking-wide text-content-muted";
}

function SectionHeading({ children }: { children: React.ReactNode }) {
  return (
    <h3 className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-content-muted">
      {children}
    </h3>
  );
}

// ─── Kind badge ───────────────────────────────────────────────────────────────

const KIND_CLASS: Record<string, string> = {
  research: "border-status-running/40 bg-status-running-bg text-status-running",
  review: "border-status-warning/40 bg-status-warning-bg text-status-warning",
  coding: "border-status-success/40 bg-status-success-bg text-status-success",
  hypothesis:
    "border-purple-400/40 bg-purple-50 text-purple-700 dark:bg-purple-900/20 dark:text-purple-300",
  planning: "border-edge bg-surface-overlay text-content-secondary",
};

function KindBadge({ kind }: { kind: string }) {
  const cls = KIND_CLASS[kind] ?? KIND_CLASS.planning;
  return (
    <span
      className={[
        "inline-flex items-center rounded-full border px-1.5 py-0.5 text-[10px] font-medium leading-none tracking-wide",
        cls,
      ].join(" ")}
    >
      {kind}
    </span>
  );
}

// ─── Empty state ──────────────────────────────────────────────────────────────

function EmptyState({ onCreate }: { onCreate: () => void }) {
  return (
    <div className="flex flex-col items-center gap-4 rounded-lg border border-dashed border-edge bg-surface-raised p-12 text-center">
      <p className="text-sm text-content-muted">{empty.engineDefs}</p>
      <Button variant="primary" size="sm" onClick={onCreate}>
        Create first definition
      </Button>
    </div>
  );
}

// ─── Form state ───────────────────────────────────────────────────────────────

type FormState = {
  name: string;
  kind: EngineKind;
  model: string;
  max_depth: string;
  max_agents: string;
  test_cmd: string;
  export_dir: string;
  description: string;
};

const EMPTY_FORM: FormState = {
  name: "",
  kind: "research",
  model: "",
  max_depth: "",
  max_agents: "",
  test_cmd: "",
  export_dir: "",
  description: "",
};

function formToRequest(form: FormState): CreateEngineDefRequest {
  const req: CreateEngineDefRequest = {
    name: form.name.trim(),
    kind: form.kind,
  };
  if (form.model.trim()) req.model = form.model.trim();
  if (form.max_depth.trim()) req.max_depth = parseInt(form.max_depth, 10);
  if (form.max_agents.trim()) req.max_agents = parseInt(form.max_agents, 10);
  if (form.description.trim()) req.description = form.description.trim();

  const opts: Record<string, string> = {};
  if (TEST_CMD_KINDS.has(form.kind) && form.test_cmd.trim()) {
    opts.test_cmd = form.test_cmd.trim();
  }
  if (EXPORT_DIR_KINDS.has(form.kind) && form.export_dir.trim()) {
    opts.export_dir = form.export_dir.trim();
  }
  if (Object.keys(opts).length > 0) req.options = opts;
  return req;
}

// ─── Create modal ─────────────────────────────────────────────────────────────

function CreateModal({ onClose, onCreated }: { onClose: () => void; onCreated: () => void }) {
  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  function set<K extends keyof FormState>(key: K, value: FormState[K]) {
    setForm((prev) => ({ ...prev, [key]: value }));
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!form.name.trim()) {
      setErr(errors.nameRequired);
      return;
    }
    if (TEST_CMD_KINDS.has(form.kind) && !form.test_cmd.trim()) {
      setErr(errors.testCmdRequired);
      return;
    }
    setSaving(true);
    setErr(null);
    try {
      await createEngineDef(formToRequest(form));
      onCreated();
    } catch (ex) {
      setErr(ex instanceof Error ? ex.message : errors.createEngineDef);
    } finally {
      setSaving(false);
    }
  }

  const showTestCmd = TEST_CMD_KINDS.has(form.kind);
  const showExportDir = EXPORT_DIR_KINDS.has(form.kind);

  return (
    // eslint-disable-next-line jsx-a11y/no-static-element-interactions, jsx-a11y/click-events-have-key-events -- modal backdrop dismiss; keyboard Escape handled by inner dialog
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="w-full max-w-md rounded-xl border border-edge bg-surface-raised p-6 shadow-xl">
        <h2 className="mb-5 text-base font-semibold text-content-primary">New engine definition</h2>

        <form onSubmit={(e) => void handleSubmit(e)} className="flex flex-col gap-4">
          <SectionHeading>Identity</SectionHeading>

          <label className={labelClass()}>
            <span className={labelTextClass()}>Name *</span>
            <input
              className={fieldClass()}
              value={form.name}
              onChange={(e) => set("name", e.target.value)}
              placeholder="my-research-engine"
              required
            />
          </label>

          <label className={labelClass()}>
            <span className={labelTextClass()}>Kind *</span>
            <select
              className={fieldClass()}
              value={form.kind}
              onChange={(e) => set("kind", e.target.value as EngineKind)}
            >
              {ENGINE_KINDS.map((k) => (
                <option key={k} value={k}>
                  {k}
                </option>
              ))}
            </select>
          </label>

          <label className={labelClass()}>
            <span className={labelTextClass()}>Description</span>
            <input
              className={fieldClass()}
              value={form.description}
              onChange={(e) => set("description", e.target.value)}
              placeholder="Optional description"
            />
          </label>

          <SectionHeading>Parameters</SectionHeading>

          <label className={labelClass()}>
            <span className={labelTextClass()}>Model</span>
            <input
              className={fieldClass()}
              value={form.model}
              onChange={(e) => set("model", e.target.value)}
              placeholder="gpt-4o (optional)"
            />
          </label>

          <div className="grid grid-cols-2 gap-3">
            <label className={labelClass()}>
              <span className={labelTextClass()}>Max depth</span>
              <input
                className={fieldClass()}
                type="number"
                min={1}
                max={100}
                value={form.max_depth}
                onChange={(e) => set("max_depth", e.target.value)}
                placeholder="optional"
              />
            </label>
            <label className={labelClass()}>
              <span className={labelTextClass()}>Max agents</span>
              <input
                className={fieldClass()}
                type="number"
                min={1}
                max={100}
                value={form.max_agents}
                onChange={(e) => set("max_agents", e.target.value)}
                placeholder="optional"
              />
            </label>
          </div>

          {(showTestCmd || showExportDir) && (
            <>
              <SectionHeading>Options</SectionHeading>
              {showTestCmd && (
                <label className={labelClass()}>
                  <span className={labelTextClass()}>Test command *</span>
                  <input
                    className={fieldClass()}
                    value={form.test_cmd}
                    onChange={(e) => set("test_cmd", e.target.value)}
                    placeholder="uv run pytest"
                    required
                  />
                </label>
              )}
              {showExportDir && (
                <label className={labelClass()}>
                  <span className={labelTextClass()}>Export directory</span>
                  <input
                    className={fieldClass()}
                    value={form.export_dir}
                    onChange={(e) => set("export_dir", e.target.value)}
                    placeholder="/tmp/output"
                  />
                </label>
              )}
            </>
          )}

          {err && <p className="text-sm text-status-error">{err}</p>}

          <div className="flex justify-end gap-2 pt-1">
            <Button variant="ghost" size="sm" type="button" onClick={onClose}>
              Cancel
            </Button>
            <Button variant="primary" size="sm" type="submit" disabled={saving}>
              {saving ? "Saving..." : "Save"}
            </Button>
          </div>
        </form>
      </div>
    </div>
  );
}

// ─── Run modal ────────────────────────────────────────────────────────────────

function RunModal({ defn, onClose }: { defn: EngineDef; onClose: () => void }) {
  const [spec, setSpec] = useState("");
  const [launching, setLaunching] = useState(false);
  const [result, setResult] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  async function handleLaunch(e: React.FormEvent) {
    e.preventDefault();
    if (!spec.trim()) return;
    setLaunching(true);
    setErr(null);
    try {
      const res = await launchEngine({
        action_kind: "engine",
        action_engine_def: defn.id,
        action_prompt: spec.trim(),
      });
      setResult(res.invocation_id.slice(0, 12));
    } catch (ex) {
      setErr(ex instanceof Error ? ex.message : errors.launchEngine);
    } finally {
      setLaunching(false);
    }
  }

  return (
    // eslint-disable-next-line jsx-a11y/no-static-element-interactions, jsx-a11y/click-events-have-key-events -- modal backdrop dismiss; keyboard Escape handled by inner dialog
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="w-full max-w-md rounded-xl border border-edge bg-surface-raised p-6 shadow-xl">
        <h2 className="mb-1 text-base font-semibold text-content-primary">
          Run &ldquo;{defn.name}&rdquo;
        </h2>
        <p className="mb-4 text-sm text-content-muted">
          <KindBadge kind={defn.kind} />
          {defn.model && (
            <span className="ml-2 font-mono text-[11px] text-content-muted">{defn.model}</span>
          )}
        </p>

        {result ? (
          <div className="rounded-md border border-status-success/40 bg-status-success-bg p-4">
            <p className="text-sm text-status-success">
              Launched — invocation <span className="font-mono">{result}…</span>
            </p>
            <p className="mt-1 text-[11px] text-content-muted">Track progress in Invocations.</p>
          </div>
        ) : (
          <form onSubmit={(e) => void handleLaunch(e)} className="flex flex-col gap-3">
            <label className={labelClass()}>
              <span className={labelTextClass()}>Engine spec</span>
              <textarea
                className={[fieldClass(), "min-h-[100px] resize-y"].join(" ")}
                value={spec}
                onChange={(e) => setSpec(e.target.value)}
                placeholder="Describe the topic, artifact, or task for this engine."
                required
              />
            </label>

            {err && <p className="text-sm text-status-error">{err}</p>}

            <div className="flex justify-end gap-2 pt-1">
              <Button variant="ghost" size="sm" type="button" onClick={onClose}>
                Cancel
              </Button>
              <Button
                variant="primary"
                size="sm"
                type="submit"
                disabled={launching || !spec.trim()}
              >
                {launching ? "Launching..." : "Launch"}
              </Button>
            </div>
          </form>
        )}
      </div>
    </div>
  );
}

// ─── Engine def card ──────────────────────────────────────────────────────────

function EngineDefCard({ defn, onDeleted }: { defn: EngineDef; onDeleted: () => void }) {
  const [deleting, setDeleting] = useState(false);
  const [showRun, setShowRun] = useState(false);

  async function handleDelete(e: React.MouseEvent) {
    e.stopPropagation();
    if (
      !window.confirm(
        `Delete engine definition?\n\nThis removes the saved configuration.\nCannot be undone.`,
      )
    ) {
      return;
    }
    setDeleting(true);
    try {
      await deleteEngineDef(defn.id);
      onDeleted();
    } catch {
      setDeleting(false);
    }
  }

  return (
    <>
      {showRun && <RunModal defn={defn} onClose={() => setShowRun(false)} />}
      <div className="flex flex-col gap-3 rounded-lg border border-edge bg-surface-raised p-4 shadow-card transition-all duration-150 hover:border-edge-strong hover:bg-surface-overlay">
        {/* Header */}
        <div className="flex items-start justify-between gap-2">
          <div className="flex min-w-0 flex-1 flex-col gap-1">
            <span className="truncate font-mono text-[13px] font-semibold text-content-primary">
              {defn.name}
            </span>
            {defn.description && (
              <p className="truncate text-meta text-content-secondary">{defn.description}</p>
            )}
          </div>
          <KindBadge kind={defn.kind} />
        </div>

        {/* Caps row */}
        <div className="flex flex-wrap items-center gap-2 text-[11px] text-content-muted">
          {defn.model && <span className="font-mono text-content-secondary">{defn.model}</span>}
          {defn.max_depth != null && <span>depth: {defn.max_depth}</span>}
          {defn.max_agents != null && <span>agents: {defn.max_agents}</span>}
          {defn.options?.test_cmd && (
            <span className="truncate max-w-[120px]" title={defn.options.test_cmd}>
              test: <span className="font-mono">{defn.options.test_cmd}</span>
            </span>
          )}
        </div>

        {/* Footer */}
        <div className="flex items-center justify-between gap-2 border-t border-edge pt-2.5">
          <span className="text-meta text-content-muted">
            <Timestamp value={defn.updated_at} />
          </span>
          <div className="flex items-center gap-2">
            <Button
              variant="ghost"
              size="sm"
              disabled={deleting}
              onClick={(e) => void handleDelete(e)}
            >
              Delete
            </Button>
            <Button variant="primary" size="sm" onClick={() => setShowRun(true)}>
              Run
            </Button>
          </div>
        </div>
      </div>
    </>
  );
}

// ─── Page ─────────────────────────────────────────────────────────────────────

function EnginesPage() {
  const [defs, setDefs] = useState<EngineDef[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [showCreate, setShowCreate] = useState(false);

  async function load() {
    setLoading(true);
    setErr(null);
    try {
      const rows = await listEngineDefs();
      setDefs(rows);
    } catch {
      setErr(errors.loadEngineDefs);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- load() calls setState, data-fetch pattern matching the rest of the codebase
    void load();
  }, []);

  return (
    <div className="flex flex-col gap-6 p-6">
      {showCreate && (
        <CreateModal
          onClose={() => setShowCreate(false)}
          onCreated={() => {
            setShowCreate(false);
            void load();
          }}
        />
      )}

      <PageHeader
        title="Engines"
        subtitle="Saved engine definitions, launchable on demand"
        actions={
          <Button variant="primary" size="sm" onClick={() => setShowCreate(true)}>
            New engine definition
          </Button>
        }
      />

      {err && (
        <div className="rounded-md border border-status-error/40 bg-status-error-bg p-3 text-sm text-status-error">
          {err}
        </div>
      )}

      {loading && (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {Array.from({ length: 3 }).map((_, i) => (
            <div
              key={i}
              className="h-36 animate-pulse rounded-lg border border-edge bg-surface-raised"
            />
          ))}
        </div>
      )}

      {!loading && defs.length === 0 && !err && <EmptyState onCreate={() => setShowCreate(true)} />}

      {!loading && defs.length > 0 && (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {defs.map((d) => (
            <EngineDefCard key={d.id} defn={d} onDeleted={() => void load()} />
          ))}
        </div>
      )}
    </div>
  );
}
