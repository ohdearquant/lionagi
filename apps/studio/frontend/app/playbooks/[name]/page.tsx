"use client";

import Link from "next/link";
import { use, useCallback, useEffect, useRef, useState } from "react";
import Button from "@/components/Button";
import StatusPill from "@/components/StatusPill";
import WorkerCanvas from "@/components/canvas/WorkerCanvas";
import { getWorkerGraph, startRun } from "@/lib/api";
import type { WorkerGraph } from "@/lib/types";

// ADR-0014: Run button is defaults-only. No task input, no CWD field.
// Input variable binding and worktree customisation belong in `li play`.
//
// H-FE-2: GET /api/playbooks/{name}/run is a 501 stub — the backend does not
// yet expose a filesystem run_id for polling step-level progress. The UI
// reflects this by reporting terminal status (running → completed/failed)
// without attempting step-level introspection. execSteps / currentStep are
// retained for WorkerCanvas prop compatibility but are never populated.

// Hoisted to module scope so the polling useEffect dependency array can
// reference it as a stable value without triggering eslint-react-hooks.
const POLL_MAX_MS = 10 * 60 * 1000;

export default function WorkerDetailPage({ params }: { params: Promise<{ name: string }> }) {
  const { name } = use(params);
  const workerName = decodeURIComponent(name);
  const [graph, setGraph] = useState<WorkerGraph | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Execution state
  // H-FE-1: activeRunId drives polling from a useEffect so the timeout and
  // any in-flight fetch are cancelled on unmount.
  const [activeRunId, setActiveRunId] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const [runStatus, setRunStatus] = useState<"idle" | "running" | "completed" | "failed">("idle");
  // M-FE-1: surface startRun / poll errors to the user
  const [runError, setRunError] = useState<string | null>(null);
  const execSteps: Array<{
    step: string;
    status: string;
    result?: Record<string, unknown>;
    timestamp?: number;
  }> = [];
  const currentStep: string | null = null;

  useEffect(() => {
    let active = true;
    getWorkerGraph(workerName)
      .then((data) => {
        if (active) setGraph(data);
      })
      .catch((err) => {
        if (active) setError(err instanceof Error ? err.message : "Failed to load");
      });
    return () => {
      active = false;
    };
  }, [workerName]);

  // H-FE-1: Polling effect — driven by activeRunId. Cleans up completely on
  // unmount or when activeRunId is cleared (terminal state reached).
  // H-FE-2: Since GET /api/runs/{id} requires a filesystem run directory and
  // startRun returns a SQLite session ID, polling getRun() would 404.
  // We track status via a simple timeout ceiling instead of step polling.
  const startedAtRef = useRef<number>(0);
  // mountedRef guards all setState calls that follow an await in handleRun.
  // The polling effect has its own `cancelled` flag; this ref covers the
  // startRun() POST path which runs outside the effect.
  const mountedRef = useRef<boolean>(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  useEffect(() => {
    if (!activeRunId) return;

    let cancelled = false;
    let handle: ReturnType<typeof setTimeout> | null = null;

    // Poll for terminal status. We rely on startedAt to cap the total wait
    // rather than calling getRun() because the run dir may not exist for
    // SQLite-only sessions (H-FE-2). When the backend implements proper run
    // status surfacing, replace this ceiling logic with a real status fetch.
    const tick = () => {
      if (cancelled) return;
      if (Date.now() - startedAtRef.current > POLL_MAX_MS) {
        if (!cancelled) {
          setRunStatus("failed");
          setRunError("Run timed out after 10 minutes");
          setRunning(false);
          setActiveRunId(null);
        }
        return;
      }
      handle = setTimeout(tick, 2000);
    };

    handle = setTimeout(tick, 2000);

    return () => {
      cancelled = true;
      if (handle != null) clearTimeout(handle);
    };
  // POLL_MAX_MS is a module-scope constant — stable reference, not a dep.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeRunId]);

  const handleRun = useCallback(async () => {
    if (!graph || running) return;

    setRunError(null);
    setRunStatus("running");
    setRunning(true);

    try {
      const data = await startRun(workerName);
      // Guard: component may have unmounted while the POST was in-flight
      if (!mountedRef.current) return;
      startedAtRef.current = Date.now();
      setActiveRunId(data.run_id);
    } catch (err) {
      // M-FE-1: show the error, not a silent status flip
      if (!mountedRef.current) return;
      setRunning(false);
      setRunStatus("failed");
      setRunError(err instanceof Error ? err.message : "Run failed");
    }
  }, [graph, running, workerName]);

  if (error) {
    return (
      <main className="mx-auto max-w-7xl px-4 py-6">
        <div className="rounded border border-status-error/30 bg-status-error-bg px-3 py-2 text-body text-status-error">
          {error}
        </div>
      </main>
    );
  }

  if (!graph) {
    return (
      <main className="mx-auto max-w-7xl px-4 py-6">
        <p className="text-body text-content-muted">Loading...</p>
      </main>
    );
  }

  const runDisabled = running;

  return (
    <div className="flex h-[calc(100vh-56px)] flex-col">
      {/* Top bar */}
      <div className="flex items-center gap-3 border-b border-edge bg-surface-nav px-4 py-2">
        <Link href="/playbooks" className="text-meta text-content-muted hover:text-content-primary">
          playbooks
        </Link>
        <span className="text-meta text-content-muted">/</span>
        <span className="font-mono text-label font-semibold text-content-primary">
          {graph.name}
        </span>
        {graph.description ? (
          <span className="truncate text-body text-content-secondary">— {graph.description}</span>
        ) : null}

        <div className="ml-auto flex items-center gap-2">
          {runStatus === "running" && (
            <span className="inline-flex items-center gap-1 text-meta text-status-running">
              <span className="inline-block h-2 w-2 animate-pulse rounded-full bg-status-running" />
              running
            </span>
          )}
          {runStatus === "completed" && <StatusPill value="completed" kind="lifecycle" />}
          {runStatus === "failed" && <StatusPill value="failed" kind="lifecycle" />}

          {/* M-FE-1: surface run errors near the Run control */}
          {runError && (
            <span className="max-w-[20rem] truncate rounded border border-status-error/30 bg-status-error-bg px-2 py-0.5 text-meta text-status-error">
              {runError}
            </span>
          )}

          <Button
            variant="primary"
            size="sm"
            onClick={handleRun}
            disabled={runDisabled}
            leading="▶"
          >
            {running ? "Running…" : "Run"}
          </Button>
          <Link href={`/playbooks/${encodeURIComponent(workerName)}/edit`}>
            <Button variant="secondary" size="sm" leading="✎">
              Edit
            </Button>
          </Link>
        </div>
      </div>

      {/* Canvas — or empty state when no steps */}
      {graph.nodes.length === 0 ? (
        <div className="flex flex-1 items-center justify-center bg-surface-base px-4 py-10">
          <PlaybookEmptyState
            workerName={workerName}
            runDisabled={runDisabled}
            running={running}
            onRun={handleRun}
          />
        </div>
      ) : (
        <div className="min-h-0 flex-1">
          <WorkerCanvas
            graph={graph}
            editable={false}
            execSteps={execSteps}
            currentStep={currentStep}
          />
        </div>
      )}
    </div>
  );
}

function PlaybookEmptyState({
  workerName,
  runDisabled,
  running,
  onRun,
}: {
  workerName: string;
  runDisabled: boolean;
  running: boolean;
  onRun: () => void;
}) {
  return (
    <div className="flex max-w-xl flex-col gap-4 rounded-lg border border-edge bg-surface-raised p-6 text-center">
      <div className="flex items-center justify-center gap-2 text-content-muted">
        <span aria-hidden className="text-2xl">
          ◇
        </span>
      </div>
      <div>
        <h2 className="text-label font-semibold text-content-primary">No steps defined yet</h2>
        <p className="mt-1 text-body text-content-secondary">
          This playbook uses the declarative agent + prompt format. Run it with defaults or open the
          editor to author steps as a graph.
        </p>
      </div>
      <div className="flex flex-wrap justify-center gap-2">
        <Button variant="primary" size="md" onClick={onRun} disabled={runDisabled} leading="▶">
          {running ? "Running…" : "Run"}
        </Button>
        <Link href={`/playbooks/${encodeURIComponent(workerName)}/edit`}>
          <Button variant="secondary" size="md" leading="✎">
            Open editor
          </Button>
        </Link>
      </div>
    </div>
  );
}
