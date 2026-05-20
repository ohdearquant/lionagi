"use client";

import Link from "next/link";
import { use, useCallback, useEffect, useState } from "react";
import Button from "@/components/Button";
import StatusPill from "@/components/StatusPill";
import WorkerCanvas from "@/components/canvas/WorkerCanvas";
import { getWorkerGraph, startRun, runEventsUrl } from "@/lib/api";
import type { WorkerGraph } from "@/lib/types";

export default function WorkerDetailPage({ params }: { params: Promise<{ name: string }> }) {
  const { name } = use(params);
  const workerName = decodeURIComponent(name);
  const [graph, setGraph] = useState<WorkerGraph | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Execution state
  const [taskInput, setTaskInput] = useState("");
  const [cwdInput, setCwdInput] = useState("");
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
    if (!graph || running || !taskInput.trim()) return;

    setExecSteps([]);
    setCurrentStep(null);
    setRunStatus("running");
    setRunning(true);

    try {
      const data = await startRun(workerName, taskInput.trim(), cwdInput.trim());
      const evtSource = new EventSource(runEventsUrl(data.run_id));

      evtSource.onmessage = (event) => {
        const parsed = JSON.parse(event.data);

        if (parsed.type === "run_started") {
          setCurrentStep(parsed.entry_step);
        } else if (parsed.type === "step_completed") {
          setExecSteps((prev) => [
            ...prev,
            {
              step: parsed.step,
              status: "completed",
              result: parsed.result,
              timestamp: parsed.timestamp,
            },
          ]);
          setCurrentStep(null);
        } else if (parsed.type === "run_completed") {
          setRunStatus("completed");
          setRunning(false);
          setCurrentStep(null);
          evtSource.close();
        } else if (parsed.type === "run_failed") {
          setRunStatus("failed");
          setRunning(false);
          setCurrentStep(null);
          evtSource.close();
        } else if (parsed.type === "done" || parsed.type === "timeout") {
          setRunning(false);
          setCurrentStep(null);
          evtSource.close();
        }
      };

      evtSource.onerror = () => {
        setRunning(false);
        setCurrentStep(null);
        setRunStatus("failed");
        evtSource.close();
      };
    } catch {
      setRunning(false);
      setRunStatus("failed");
    }
  }, [graph, running, workerName, taskInput, cwdInput]);

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

  const taskTrimmed = taskInput.trim();
  const runDisabled = running || !taskTrimmed;

  return (
    <div className="flex h-[calc(100vh-56px)] flex-col">
      {/* Top bar */}
      <div className="flex items-center gap-3 border-b border-edge bg-surface-nav px-4 py-2">
        <Link
          href="/playbooks"
          className="text-meta text-content-muted hover:text-content-primary"
        >
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

          <Link href={`/playbooks/${encodeURIComponent(workerName)}/edit`}>
            <Button variant="secondary" size="sm" leading="✎">
              Edit
            </Button>
          </Link>
        </div>
      </div>

      {/* Task command bar */}
      <div className="flex items-center gap-2 border-b border-edge bg-surface-raised px-4 py-2">
        <div className="flex flex-1 items-center gap-2">
          <label className="hidden text-meta uppercase tracking-[0.06em] text-content-muted md:inline">
            Task
          </label>
          <input
            type="text"
            value={taskInput}
            onChange={(e) => setTaskInput(e.target.value)}
            placeholder="Describe what the playbook should do..."
            disabled={running}
            className="flex-1 rounded-md border border-edge bg-surface-input px-3 py-1.5 text-body text-content-primary placeholder-content-muted focus:border-interactive-primary focus:outline-none disabled:opacity-50"
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                handleRun();
              }
            }}
          />
        </div>
        <div className="flex items-center gap-2">
          <label className="hidden text-meta uppercase tracking-[0.06em] text-content-muted md:inline">
            CWD
          </label>
          <input
            type="text"
            value={cwdInput}
            onChange={(e) => setCwdInput(e.target.value)}
            placeholder="working directory"
            disabled={running}
            className="w-52 rounded-md border border-edge bg-surface-input px-3 py-1.5 font-mono text-meta text-content-primary placeholder-content-muted focus:border-interactive-primary focus:outline-none disabled:opacity-50"
          />
        </div>
        <Button
          variant="primary"
          size="md"
          onClick={handleRun}
          disabled={runDisabled}
          leading="▶"
        >
          {running ? "Running…" : "Run"}
        </Button>
      </div>

      {/* Canvas — or empty state when no steps */}
      {graph.nodes.length === 0 ? (
        <div className="flex flex-1 items-center justify-center bg-surface-base px-4 py-10">
          <PlaybookEmptyState
            workerName={workerName}
            taskTrimmed={taskTrimmed}
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
  taskTrimmed,
  runDisabled,
  running,
  onRun,
}: {
  workerName: string;
  taskTrimmed: string;
  runDisabled: boolean;
  running: boolean;
  onRun: () => void;
}) {
  return (
    <div className="flex max-w-xl flex-col gap-4 rounded-lg border border-edge bg-surface-raised p-6 text-center">
      <div className="flex items-center justify-center gap-2 text-content-muted">
        <span aria-hidden className="text-2xl">◇</span>
      </div>
      <div>
        <h2 className="text-label font-semibold text-content-primary">No steps defined yet</h2>
        <p className="mt-1 text-body text-content-secondary">
          This playbook uses the declarative agent + prompt format. You can run it with a task
          to let the orchestrator generate steps on the fly, or open the editor to author them
          as a graph.
        </p>
      </div>
      <div className="flex flex-wrap justify-center gap-2">
        <Button variant="primary" size="md" onClick={onRun} disabled={runDisabled} leading="▶">
          {running ? "Running…" : taskTrimmed ? "Run with task above" : "Type a task above to run"}
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
