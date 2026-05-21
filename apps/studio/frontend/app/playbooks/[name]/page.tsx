"use client";

import Link from "next/link";
import { use, useCallback, useEffect, useState } from "react";
import Button from "@/components/Button";
import StatusPill from "@/components/StatusPill";
import WorkerCanvas from "@/components/canvas/WorkerCanvas";
import { getRun, getWorkerGraph, startRun } from "@/lib/api";
import type { RunStep, WorkerGraph } from "@/lib/types";

// Status set per ADR-0017 session lifecycle vocabulary.
const TERMINAL_OK = new Set(["completed"]);
const TERMINAL_FAIL = new Set(["failed", "aborted"]);
const POLL_INTERVAL_MS = 1000;
const POLL_MAX_MS = 10 * 60 * 1000; // 10 min ceiling

// ADR-0014: Run button is defaults-only. No task input, no CWD field.
// Input variable binding and worktree customisation belong in `li play`.

export default function WorkerDetailPage({ params }: { params: Promise<{ name: string }> }) {
  const { name } = use(params);
  const workerName = decodeURIComponent(name);
  const [graph, setGraph] = useState<WorkerGraph | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Execution state
  const [running, setRunning] = useState(false);
  const [runStatus, setRunStatus] = useState<"idle" | "running" | "completed" | "failed">("idle");
  const [execSteps, setExecSteps] = useState<
    Array<{ step: string; status: string; result?: Record<string, unknown>; timestamp?: number }>
  >([]);
  const [currentStep, setCurrentStep] = useState<string | null>(null);

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

  const handleRun = useCallback(async () => {
    if (!graph || running) return;

    setExecSteps([]);
    setCurrentStep(null);
    setRunStatus("running");
    setRunning(true);

    let runId: string;
    try {
      const data = await startRun(workerName);
      runId = data.run_id;
    } catch {
      setRunning(false);
      setRunStatus("failed");
      return;
    }

    const startedAt = Date.now();

    const tick = async (): Promise<void> => {
      try {
        const detail = await getRun(runId);
        const steps: RunStep[] = detail.steps ?? [];
        const completed = steps.filter((s) => s.status === "completed");
        setExecSteps(
          completed.map((s) => ({
            step: s.step,
            status: s.status,
            result: s.result,
            timestamp: s.timestamp ?? undefined,
          })),
        );
        const active = steps.find((s) => s.status === "running");
        setCurrentStep(active?.step ?? null);

        if (TERMINAL_OK.has(detail.status)) {
          setRunStatus("completed");
          setRunning(false);
          setCurrentStep(null);
          return;
        }
        if (TERMINAL_FAIL.has(detail.status)) {
          setRunStatus("failed");
          setRunning(false);
          setCurrentStep(null);
          return;
        }
        if (Date.now() - startedAt > POLL_MAX_MS) {
          setRunStatus("failed");
          setRunning(false);
          setCurrentStep(null);
          return;
        }
        setTimeout(tick, POLL_INTERVAL_MS);
      } catch {
        setRunning(false);
        setRunStatus("failed");
        setCurrentStep(null);
      }
    };

    void tick();
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
