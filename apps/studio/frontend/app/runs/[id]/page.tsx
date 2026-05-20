"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import Badge from "@/components/Badge";
import ExecutionDag from "@/components/ExecutionDag";
import type { ExecutionStep } from "@/components/ExecutionDag";
import { getRun, rerunRun } from "@/lib/api";
import type { RunDetail } from "@/lib/types";

function formatDuration(started: number | null, finished: number | null): string {
  if (!started) return "—";
  const end = finished ?? Date.now() / 1000;
  const seconds = Math.round(end - started);
  if (seconds < 60) return `${seconds}s`;
  const mins = Math.floor(seconds / 60);
  const secs = seconds % 60;
  return `${mins}m ${secs}s`;
}

function formatTime(ts: number | null): string {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleString();
}

const STATUS_TONE: Record<string, "ok" | "pending" | "failed"> = {
  completed: "ok",
  running: "pending",
  failed: "failed",
};

export default function RunDetailPage({ params }: { params: { id: string } }) {
  const runId = params.id;
  const router = useRouter();
  const [run, setRun] = useState<RunDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [rerunning, setRerunning] = useState(false);

  useEffect(() => {
    let active = true;

    async function load() {
      try {
        const data = await getRun(runId);
        if (active) {
          setRun(data);
          setError(null);
        }
      } catch (err) {
        if (active) {
          setError(err instanceof Error ? err.message : "Failed to load run");
        }
      }
    }

    void load();
    const interval = setInterval(load, 3000);
    return () => {
      active = false;
      clearInterval(interval);
    };
  }, [runId]);

  const handleRerun = async () => {
    if (!run || rerunning) return;
    setRerunning(true);
    try {
      const data = await rerunRun(runId);
      router.push(`/runs/${data.run_id}`);
    } catch {
      setRerunning(false);
    }
  };

  const steps = run?.steps ?? [];
  const execSteps: ExecutionStep[] = steps.map((s) => ({
    step: s.step,
    status: "completed" as const,
    result: s.result,
    timestamp: s.timestamp,
  }));

  const nodes = run?.graph?.nodes ?? [];
  const edges = run?.graph?.edges ?? [];

  return (
    <main className="mx-auto flex w-full max-w-[1600px] flex-col gap-4 px-4 py-6 text-neutral-200">
      {/* Header */}
      <header className="flex flex-col gap-3 border-b border-neutral-800 pb-4">
        <Link href="/runs" className="text-sm text-neutral-500 hover:text-neutral-200">
          / runs
        </Link>

        <div className="flex flex-col gap-3 md:flex-row md:items-end md:justify-between">
          <div className="min-w-0">
            <div className="flex items-center gap-3">
              <h1 className="font-mono text-xl font-semibold text-neutral-200">{runId}</h1>
              {run && <Badge tone={STATUS_TONE[run.status] ?? "pending"}>{run.status}</Badge>}
            </div>
            {run && (
              <p className="mt-1 text-sm text-neutral-400">
                Playbook:{" "}
                <Link
                  href={`/playbooks/${encodeURIComponent(run.worker_name)}`}
                  className="text-blue-400 hover:text-blue-300"
                >
                  {run.worker_name}
                </Link>
              </p>
            )}
          </div>

          {run && (
            <div className="flex items-center gap-3">
              <Link
                href={`/playbooks/${encodeURIComponent(run.worker_name)}/edit`}
                className="rounded border border-neutral-700 bg-neutral-900 px-3 py-1 text-sm text-neutral-300 hover:border-neutral-500 hover:text-neutral-200"
              >
                Edit Playbook
              </Link>
              <button
                onClick={handleRerun}
                disabled={rerunning}
                className="rounded border border-green-700 bg-green-900/50 px-3 py-1 text-sm text-green-300 hover:bg-green-800/50 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {rerunning ? "Starting..." : "Re-run"}
              </button>
            </div>
          )}
        </div>
      </header>

      {error && (
        <div className="border border-red-800 bg-neutral-950 px-3 py-2 text-sm text-red-300">
          {error}
        </div>
      )}

      {run && (
        <>
          {/* Metadata row */}
          <section className="grid grid-cols-2 gap-2 md:grid-cols-5">
            <MetricCard label="Playbook" value={run.worker_name} />
            <MetricCard label="Steps" value={steps.length} />
            <MetricCard label="Duration" value={formatDuration(run.started_at, run.finished_at)} />
            <MetricCard label="Started" value={formatTime(run.started_at)} />
            <MetricCard label="CWD" value={run.cwd || "—"} />
          </section>

          {/* Task */}
          {run.task && (
            <section className="rounded border border-neutral-800 bg-neutral-950 p-3">
              <div className="text-xs uppercase text-neutral-500">Task</div>
              <p className="mt-1 whitespace-pre-wrap text-sm text-neutral-300">{run.task}</p>
            </section>
          )}

          {/* Error */}
          {run.error && (
            <section className="rounded border border-red-800/50 bg-red-950/30 p-3">
              <div className="text-xs uppercase text-red-400">Error</div>
              <p className="mt-1 whitespace-pre-wrap font-mono text-sm text-red-300">{run.error}</p>
            </section>
          )}

          {/* Split: DAG left, Results right */}
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            {/* LEFT: DAG */}
            <section className="flex flex-col gap-2">
              <h2 className="text-sm font-semibold text-neutral-200">Execution Graph</h2>
              {nodes.length > 0 ? (
                <ExecutionDag
                  nodes={nodes}
                  edges={edges}
                  executionSteps={execSteps}
                  currentStep={
                    run.status === "running"
                      ? steps.length > 0
                        ? null
                        : (nodes[0]?.id ?? null)
                      : null
                  }
                />
              ) : (
                <div className="border border-neutral-800 bg-neutral-950 px-3 py-10 text-center text-sm text-neutral-500">
                  No graph data
                </div>
              )}
            </section>

            {/* RIGHT: Step Results */}
            <section className="flex flex-col gap-2">
              <h2 className="text-sm font-semibold text-neutral-200">
                Step Results ({steps.length})
              </h2>
              {steps.length === 0 ? (
                <div className="border border-neutral-800 bg-neutral-950 px-3 py-10 text-center text-sm text-neutral-500">
                  {run.status === "running" ? "Waiting for steps..." : "No steps recorded"}
                </div>
              ) : (
                steps.map((step, i) => (
                  <div key={i} className="rounded border border-neutral-800 bg-neutral-950 p-3">
                    <div className="flex items-center gap-2">
                      <span className="font-mono text-sm font-semibold text-green-400">
                        {step.step}
                      </span>
                      <Badge tone="ok">{step.status}</Badge>
                      <span className="ml-auto text-xs text-neutral-600">
                        {formatTime(step.timestamp)}
                      </span>
                    </div>
                    {step.result && Object.keys(step.result).length > 0 && (
                      <div className="mt-2 flex flex-col gap-1">
                        {Object.entries(step.result).map(([key, val]) => (
                          <div key={key}>
                            <span className="text-xs text-neutral-500">{key}:</span>
                            <p className="mt-0.5 whitespace-pre-wrap break-words text-sm text-neutral-300">
                              {typeof val === "string" ? val : JSON.stringify(val)}
                            </p>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                ))
              )}
            </section>
          </div>
        </>
      )}
    </main>
  );
}

function MetricCard({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="min-w-0 border border-neutral-800 bg-neutral-950 p-3">
      <div className="truncate text-xs uppercase text-neutral-500">{label}</div>
      <div className="mt-1 truncate text-sm font-medium text-neutral-200">{value}</div>
    </div>
  );
}
