"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import Badge from "@/components/Badge";
import WorkerCanvas from "@/components/canvas/WorkerCanvas";
import { getWorkerGraph, startRun, runEventsUrl } from "@/lib/api";
import type { WorkerGraph } from "@/lib/types";

export default function WorkerDetailPage({ params }: { params: { name: string } }) {
  const workerName = decodeURIComponent(params.name);
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
        <div className="border border-red-800 bg-neutral-950 px-3 py-2 text-sm text-red-300">
          {error}
        </div>
      </main>
    );
  }

  if (!graph) {
    return (
      <main className="mx-auto max-w-7xl px-4 py-6">
        <p className="text-sm text-neutral-500">Loading...</p>
      </main>
    );
  }

  return (
    <div className="flex h-[calc(100vh-56px)] flex-col">
      {/* Top bar */}
      <div className="flex items-center gap-3 border-b border-edge bg-surface-nav px-4 py-2">
        <Link href="/playbooks" className="text-xs text-content-secondary hover:text-content-primary">
          playbooks
        </Link>
        <span className="text-xs text-content-muted">/</span>
        <span className="font-mono text-sm font-semibold text-content-primary">{graph.name}</span>
        {graph.description && (
          <span className="text-xs text-content-secondary truncate">— {graph.description}</span>
        )}

        <div className="ml-auto flex items-center gap-2">
          {runStatus === "running" && (
            <span className="inline-block h-2 w-2 animate-pulse rounded-full bg-status-running" />
          )}
          {runStatus === "completed" && <Badge tone="ok">done</Badge>}
          {runStatus === "failed" && <Badge tone="failed">failed</Badge>}

          <Link
            href={`/playbooks/${encodeURIComponent(workerName)}/edit`}
            className="rounded-md bg-interactive-secondary px-3 py-1 text-xs font-medium text-content-primary hover:bg-interactive-secondary-hover"
          >
            Edit
          </Link>
        </div>
      </div>

      {/* Task input bar */}
      <div className="flex items-center gap-2 border-b border-edge bg-surface-raised px-4 py-2">
        <input
          type="text"
          value={taskInput}
          onChange={(e) => setTaskInput(e.target.value)}
          placeholder="Task — describe what the playbook should do..."
          disabled={running}
          className="flex-1 rounded-md border border-edge bg-surface-input px-3 py-1.5 text-sm text-content-primary placeholder-content-muted focus:border-interactive-primary focus:outline-none disabled:opacity-50"
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              handleRun();
            }
          }}
        />
        <input
          type="text"
          value={cwdInput}
          onChange={(e) => setCwdInput(e.target.value)}
          placeholder="Working directory"
          disabled={running}
          className="w-52 rounded-md border border-edge bg-surface-input px-3 py-1.5 font-mono text-xs text-content-primary placeholder-content-muted focus:border-interactive-primary focus:outline-none disabled:opacity-50"
        />
        <button
          onClick={handleRun}
          disabled={running || !taskInput.trim()}
          className={`shrink-0 rounded-md px-5 py-1.5 text-sm font-medium transition ${
            running || !taskInput.trim()
              ? "cursor-not-allowed bg-interactive-secondary text-content-muted"
              : "bg-interactive-primary text-content-inverse hover:bg-interactive-primary-hover"
          }`}
        >
          {running ? "Running..." : "Run"}
        </button>
      </div>

      {/* Canvas fills remaining space */}
      <div className="flex-1 min-h-0">
        <WorkerCanvas
          graph={graph}
          editable={false}
          execSteps={execSteps}
          currentStep={currentStep}
        />
      </div>
    </div>
  );
}
