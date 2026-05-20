"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { use, useCallback, useEffect, useRef, useState } from "react";
import Button from "@/components/Button";
import Duration from "@/components/Duration";
import ExecutionDag from "@/components/ExecutionDag";
import type { ExecutionStep } from "@/components/ExecutionDag";
import RunStepCard from "@/components/RunStepCard";
import SegmentedProgress from "@/components/SegmentedProgress";
import StatusPill from "@/components/StatusPill";
import Timestamp from "@/components/Timestamp";
import { getRun, rerunRun, streamRunEvents } from "@/lib/api";
import type { RunDetail, RunStep } from "@/lib/types";

// ── helpers used only in the timeline ──────────────────────────────────────

function fmtDur(sec: number | null | undefined): string {
  if (sec == null) return "";
  if (sec < 60) return `${sec}s`;
  return `${Math.floor(sec / 60)}m${sec % 60}s`;
}

function verdictFromStep(step: RunStep): string | null {
  // Extract verdict from last assistant message in step result
  const VERDICT_RE = /\b(APPROVE-WITH-FIXES|APPROVE-WITH-SUGGESTIONS|APPROVE|REJECT|REQUEST CHANGES|PASS|FAIL|BLOCK)\b/i;
  const messages = step.messages ?? [];
  for (let i = messages.length - 1; i >= 0; i--) {
    if (messages[i].role === "assistant") {
      const m = (messages[i].content || "").match(VERDICT_RE);
      if (m) return m[1].toUpperCase();
    }
  }
  return null;
}

function toolCountFromStep(step: RunStep): { toolCount: number; failedCount: number; durationSec: number | null } {
  const messages = step.messages ?? [];
  const tools = messages.filter((m) => m.role === "tool_call" || m.role === "action");
  const failed = tools.filter((m) => m.status === "error");
  let firstTs: number | null = null;
  let lastTs: number | null = null;
  for (const m of messages) {
    if (m.timestamp == null) continue;
    if (firstTs == null) firstTs = m.timestamp;
    lastTs = m.timestamp;
  }
  const durationSec = firstTs != null && lastTs != null ? Math.round(lastTs - firstTs) : null;
  return { toolCount: tools.length, failedCount: failed.length, durationSec };
}

const VERDICT_TONE: Record<string, string> = {
  APPROVE: "text-status-success",
  "APPROVE-WITH-SUGGESTIONS": "text-status-success",
  "APPROVE-WITH-FIXES": "text-status-warning",
  PASS: "text-status-success",
  REJECT: "text-status-error",
  "REQUEST CHANGES": "text-status-error",
  FAIL: "text-status-error",
  BLOCK: "text-status-error",
};

// ── StepsTimeline component ──────────────────────────────────────────────────

function StepsTimeline({
  steps,
  totalNodes,
  totalDurationSec,
  activeStep,
  onNavigate,
}: {
  steps: RunStep[];
  totalNodes: number;
  totalDurationSec: number | null;
  activeStep: string | null;
  onNavigate: (stepId: string) => void;
}) {
  const completedCount = steps.filter((s) => s.status === "completed").length;
  const failedCount = steps.filter((s) => s.status === "failed").length;
  const runningCount = steps.filter((s) => s.status === "running").length;
  const pendingCount = Math.max(0, totalNodes - completedCount - failedCount - runningCount);

  // Sort: failed/running first, then completed, then pending — keeps abnormality on top.
  const sortRank = (s: RunStep): number => {
    if (s.status === "failed") return 0;
    if (s.status === "running") return 1;
    if (s.status === "blocked") return 2;
    if (s.status === "completed") return 3;
    return 4;
  };
  const sortedSteps = [...steps].sort((a, b) => sortRank(a) - sortRank(b));

  return (
    <div className="rounded border border-edge bg-surface-raised">
      {/* Header: segmented progress + total duration */}
      <div className="border-b border-edge px-3 py-2">
        <div className="mb-1.5 flex items-center justify-between gap-2">
          <span className="text-meta font-semibold uppercase tracking-[0.06em] text-content-muted">
            Steps
          </span>
          <span className="font-mono text-meta tabular-nums text-content-muted">
            {completedCount}/{totalNodes || steps.length}
            {totalDurationSec != null && totalDurationSec >= 0 ? (
              <span className="ml-1.5 opacity-70">
                <Duration value={totalDurationSec} />
              </span>
            ) : null}
          </span>
        </div>
        <SegmentedProgress
          completed={completedCount}
          failed={failedCount}
          running={runningCount}
          pending={pendingCount}
        />
      </div>

      {/* Timeline rows: sorted by abnormality (failed/running first) */}
      <ol className="flex flex-col py-1.5">
        {sortedSteps.map((step, i) => {
          const execIndex = steps.indexOf(step) + 1;
          const isLast = i === sortedSteps.length - 1;
          const isActive = activeStep === step.step;
          const { toolCount, failedCount, durationSec } = toolCountFromStep(step);
          const verdict = verdictFromStep(step);

          const dotClass =
            step.status === "completed"
              ? "bg-status-success"
              : step.status === "failed"
                ? "bg-status-error"
                : step.status === "running"
                  ? "bg-status-running animate-pulse"
                  : "bg-edge-strong";

          const rowBg = isActive
            ? "bg-surface-overlay"
            : "hover:bg-surface-overlay/60";

          return (
            <li key={step.step} className="relative flex items-stretch">
              <div className="relative flex w-7 shrink-0 flex-col items-center">
                {!isLast && (
                  <div className="absolute left-1/2 top-0 h-full w-px -translate-x-1/2 bg-edge" />
                )}
                <span
                  className="relative z-10 mt-2 font-mono text-meta tabular-nums text-content-muted"
                  title={`Step ${execIndex}`}
                >
                  {execIndex}
                </span>
              </div>

              <button
                type="button"
                onClick={() => onNavigate(step.step)}
                className={`flex min-w-0 flex-1 flex-col gap-0.5 rounded-r py-1.5 pr-2 text-left transition-colors ${rowBg}`}
              >
                <div className="flex items-center gap-1.5">
                  <span className={`inline-block h-2 w-2 shrink-0 rounded-full ${dotClass}`} />
                  <span
                    className={`min-w-0 flex-1 truncate font-mono text-body ${
                      isActive ? "text-content-primary" : "text-content-secondary"
                    }`}
                  >
                    {step.step}
                  </span>
                  {durationSec != null && durationSec >= 0 ? (
                    <span className="shrink-0 font-mono text-meta tabular-nums text-content-muted">
                      <Duration value={durationSec} />
                    </span>
                  ) : null}
                </div>

                {(toolCount > 0 || failedCount > 0 || verdict) && (
                  <div className="flex items-center gap-1.5 pl-3.5">
                    {toolCount > 0 && (
                      <span className="font-mono text-meta tabular-nums text-content-muted">
                        {toolCount}t
                      </span>
                    )}
                    {failedCount > 0 && (
                      <span className="font-mono text-meta tabular-nums text-status-error">
                        {failedCount}✗
                      </span>
                    )}
                    {verdict && (
                      <span
                        className={`font-mono text-meta font-medium ${VERDICT_TONE[verdict] ?? "text-content-muted"}`}
                      >
                        {verdict}
                      </span>
                    )}
                    {isActive && (
                      <span className="ml-auto h-1 w-1 rounded-full bg-status-running" />
                    )}
                  </div>
                )}
              </button>
            </li>
          );
        })}
      </ol>
    </div>
  );
}

function runDurationSec(started: number | null, finished: number | null): number | null {
  if (!started) return null;
  const end = finished ?? Date.now() / 1000;
  const seconds = end - started;
  // Negative durations indicate timestamp ordering bugs upstream — render as
  // "—" rather than expose the broken value. See Duration component.
  if (seconds < 0) return -1;
  return seconds;
}

function mergeRunChunk(
  prev: RunDetail,
  chunk: Record<string, unknown>,
): RunDetail {
  if (chunk.type === "step" && typeof chunk.step === "string") {
    const existing = prev.steps ?? [];
    const updated: RunStep = {
      step: chunk.step,
      status: "completed",
      result: (chunk.result as Record<string, unknown>) ?? undefined,
      timestamp: (chunk.timestamp as number) ?? null,
    };
    const idx = existing.findIndex((s) => s.step === chunk.step);
    const steps =
      idx >= 0
        ? existing.map((s, i) => (i === idx ? updated : s))
        : [...existing, updated];
    return { ...prev, steps };
  }
  if (chunk.type === "status" && typeof chunk.status === "string") {
    return { ...prev, status: chunk.status };
  }
  return prev;
}

export default function RunDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id: runId } = use(params);
  const router = useRouter();
  const [run, setRun] = useState<RunDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [rerunning, setRerunning] = useState(false);
  const [streaming, setStreaming] = useState(false);
  const closeRef = useRef<(() => void) | null>(null);
  // Track which step card is currently visible in the center column
  const [activeTimelineStep, setActiveTimelineStep] = useState<string | null>(null);
  // Track which step cards are expanded (controlled from parent so graph click can open them)
  const [expandedSteps, setExpandedSteps] = useState<Set<string>>(new Set());

  useEffect(() => {
    let active = true;

    async function init() {
      try {
        const data = await getRun(runId);
        if (!active) return;
        setRun(data);
        setError(null);

        if (data.status === "running") {
          setStreaming(true);
          const close = streamRunEvents(runId, (event) => {
            if (!active) return;
            if (event.type === "done") {
              void getRun(runId).then((final) => {
                if (active) {
                  setRun(final);
                  setStreaming(false);
                }
              });
              close();
              return;
            }
            setRun((prev) => (prev ? mergeRunChunk(prev, event) : prev));
          });
          closeRef.current = close;
        }
      } catch (err) {
        if (active) {
          setError(err instanceof Error ? err.message : "Failed to load run");
        }
      }
    }

    void init();
    return () => {
      active = false;
      closeRef.current?.();
    };
  }, [runId]);

  // IntersectionObserver: highlight whichever step card is most visible
  useEffect(() => {
    const cards = document.querySelectorAll<HTMLElement>("[id^='step-']");
    if (cards.length === 0) return;

    const observer = new IntersectionObserver(
      (entries) => {
        // Pick the entry with the largest intersection ratio that is intersecting
        let best: IntersectionObserverEntry | null = null;
        for (const entry of entries) {
          if (entry.isIntersecting) {
            if (!best || entry.intersectionRatio > best.intersectionRatio) {
              best = entry;
            }
          }
        }
        if (best) {
          // id format is "step-{stepName}"
          const stepName = best.target.id.replace(/^step-/, "");
          setActiveTimelineStep(stepName);
        }
      },
      { threshold: [0, 0.25, 0.5, 0.75, 1], rootMargin: "-10% 0px -10% 0px" },
    );

    cards.forEach((el) => observer.observe(el));
    return () => observer.disconnect();
  }, [run]);

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

  const scrollToTop = useCallback(() => {
    window.scrollTo({ top: 0, behavior: "smooth" });
  }, []);

  const scrollToStep = useCallback((stepId: string) => {
    setActiveTimelineStep(stepId);
    // Expand the card so the user can immediately see its content
    setExpandedSteps((prev) => {
      if (prev.has(stepId)) return prev;
      const next = new Set(prev);
      next.add(stepId);
      return next;
    });
    // Defer scroll one frame so the expanded layout settles before scrollIntoView
    requestAnimationFrame(() => {
      const el = document.getElementById(`step-${stepId}`);
      if (!el) return;
      el.scrollIntoView({ behavior: "smooth", block: "start" });
      el.classList.add("ring-1", "ring-blue-400/60");
      setTimeout(() => el.classList.remove("ring-1", "ring-blue-400/60"), 1200);
    });
  }, []);

  const handleToggleExpand = useCallback((stepId: string, next: boolean) => {
    setExpandedSteps((prev) => {
      const updated = new Set(prev);
      if (next) updated.add(stepId);
      else updated.delete(stepId);
      return updated;
    });
  }, []);

  const steps = run?.steps ?? [];
  const execSteps: ExecutionStep[] = steps.map((s) => ({
    step: s.step,
    status: (s.status as "completed" | "pending" | "running" | "failed") ?? "completed",
    result: s.result,
    timestamp: s.timestamp,
  }));

  const nodes = run?.graph?.nodes ?? [];
  const edges = run?.graph?.edges ?? [];

  const lastCompletedStep = steps.filter((s) => s.status === "completed").at(-1);
  const currentStep =
    run?.status === "running"
      ? lastCompletedStep
        ? null
        : (nodes[0]?.id ?? null)
      : null;

  const manifest = (run?.manifest ?? {}) as Record<string, unknown>;
  const model = (manifest.model_spec as string) || (manifest.model as string) || "—";
  const completedCount = steps.filter((s) => s.status === "completed").length;
  const totalSteps = steps.length || nodes.length;

  return (
    <div className="flex min-h-screen w-full flex-col bg-surface-base text-content-primary">
      {/* Full-width header — sits below Shell (h-11) */}
      <header className="sticky top-11 z-30 flex items-center gap-3 border-b border-edge bg-surface-base px-3 py-1.5 xl:px-4">
        <Link href="/runs" className="shrink-0 text-sm text-content-secondary hover:text-content-primary">
          ← runs
        </Link>
        <span className="text-content-muted">/</span>
        <h1 className="min-w-0 flex-1 truncate font-mono text-base font-semibold text-content-primary">
          {runId}
        </h1>
        {run && <StatusPill value={run.status} kind="lifecycle" />}
        {streaming && (
          <span className="flex shrink-0 items-center gap-1.5 text-xs text-status-success">
            <span className="relative flex h-2 w-2">
              <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-status-success opacity-75" />
              <span className="relative inline-flex h-2 w-2 rounded-full bg-status-success" />
            </span>
            live
          </span>
        )}
        {run && (
          <Button
            variant="primary"
            size="sm"
            onClick={handleRerun}
            disabled={rerunning}
            leading="↻"
            className="shrink-0"
          >
            {rerunning ? "Starting..." : "Re-run"}
          </Button>
        )}
      </header>

      {/* Run Health strip — incident summary directly below the header */}
      {run && (
        <RunHealthStrip
          status={run.status}
          steps={steps}
          totalNodes={nodes.length}
          startedAt={run.started_at}
          finishedAt={run.finished_at}
          error={run.error}
          onJumpToStep={scrollToStep}
        />
      )}

      {error && (
        <div className="px-6 pt-4 xl:px-12">
          <div className="border border-status-error/50 bg-status-error-bg px-3 py-2 text-sm text-status-error">
            {error}
          </div>
        </div>
      )}

      {/* 3-column layout */}
      <div className="flex flex-1 gap-3 px-3 py-3 xl:px-4">

        {/* LEFT SIDEBAR — sticky, ~260px */}
        <aside className="hidden lg:flex lg:w-64 lg:shrink-0 lg:flex-col">
          <div className="sticky top-[5.25rem] flex max-h-[calc(100vh-5.5rem)] flex-col gap-2 overflow-y-auto">
            {run ? (
              <>
                {/* Compact metrics */}
                <div className="rounded border border-edge bg-surface-raised px-3 py-2">
                  <div className="mb-1.5 text-[9px] font-semibold uppercase tracking-wider text-content-muted">
                    Run Summary
                  </div>
                  <dl className="flex flex-col gap-1">
                    <MetricRow label="Kind" value={run.worker_name || "—"} mono={false} />
                    {model !== "—" && <MetricRow label="Model" value={model} mono={true} />}
                    <MetricRow
                      label="Status"
                      value={<StatusPill value={run.status} kind="lifecycle" />}
                    />
                    <MetricRow
                      label="Steps"
                      value={`${completedCount} / ${totalSteps}`}
                    />
                    <MetricRow
                      label="Duration"
                      value={<Duration value={runDurationSec(run.started_at, run.finished_at)} />}
                    />
                    <MetricRow
                      label="Started"
                      value={<Timestamp value={run.started_at ?? null} exact />}
                    />
                    <MetricRow
                      label="Finished"
                      value={<Timestamp value={run.finished_at ?? null} exact />}
                    />
                    {run.cwd && <MetricRow label="CWD" value={run.cwd} mono={true} />}
                  </dl>
                </div>

                {/* Error block */}
                {run.error && (
                  <div className="rounded border border-status-error/40 bg-status-error-bg px-2 py-1.5">
                    <div className="mb-0.5 text-[9px] uppercase text-status-error">Error</div>
                    <p className="whitespace-pre-wrap font-mono text-body text-status-error">{run.error}</p>
                  </div>
                )}

                {/* Task text */}
                {run.task && (
                  <div className="rounded border border-edge bg-surface-raised px-3 py-2">
                    <div className="mb-1 text-[9px] font-semibold uppercase tracking-wider text-content-muted">
                      Task
                    </div>
                    <div className="max-h-[40vh] overflow-y-auto">
                      <p className="whitespace-pre-wrap text-body leading-snug text-content-secondary">
                        {run.task}
                      </p>
                    </div>
                  </div>
                )}

                {/* Back to top */}
                <button
                  type="button"
                  onClick={scrollToTop}
                  className="mt-auto rounded border border-edge px-2 py-1 text-[10px] text-content-muted hover:border-edge-strong hover:text-content-primary"
                >
                  ↑ Back to top
                </button>
              </>
            ) : (
              <div className="rounded border border-edge bg-surface-raised p-3 text-xs text-content-muted">
                Loading…
              </div>
            )}
          </div>
        </aside>

        {/* CENTER — step cards, flex-1 */}
        <main className="min-w-0 flex-1">
          {run && (
            <>
              {/* Mobile-only metric strip */}
              <div className="mb-4 flex flex-wrap gap-2 lg:hidden">
                <MobileMetric label="Steps" value={`${completedCount}/${totalSteps}`} />
                <MobileMetric
                  label="Duration"
                  value={<Duration value={runDurationSec(run.started_at, run.finished_at)} />}
                />
                <MobileMetric label="Kind" value={run.worker_name || "—"} />
              </div>

              {/* Mobile task */}
              {run.task && (
                <div className="mb-4 lg:hidden rounded border border-edge bg-surface-raised p-3">
                  <div className="mb-1 text-[10px] uppercase text-content-muted">Task</div>
                  <p className="max-h-32 overflow-y-auto whitespace-pre-wrap text-xs text-content-secondary">
                    {run.task}
                  </p>
                </div>
              )}

              {/* Execution Graph — horizontal strip above step cards */}
              {nodes.length > 0 && (
                <div className="mb-3 rounded border border-edge bg-surface-raised px-2 pt-1.5 pb-2">
                  <div className="mb-1 text-[9px] font-semibold uppercase tracking-wider text-content-muted">
                    Execution Graph
                  </div>
                  <ExecutionDag
                    nodes={nodes}
                    edges={edges}
                    executionSteps={execSteps}
                    currentStep={currentStep}
                    onNodeClick={scrollToStep}
                    direction="horizontal"
                  />
                </div>
              )}

              {/* Step cards */}
              <div className="flex flex-col gap-1.5">
                {steps.length === 0 ? (
                  <div className="border border-edge bg-surface-base px-3 py-10 text-center text-sm text-content-muted">
                    {run.status === "running" ? (
                      <span className="flex items-center justify-center gap-2">
                        <span className="relative flex h-2 w-2">
                          <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-status-running opacity-75" />
                          <span className="relative inline-flex h-2 w-2 rounded-full bg-status-running" />
                        </span>
                        Waiting for steps…
                      </span>
                    ) : (
                      "No steps recorded"
                    )}
                  </div>
                ) : (
                  steps.map((step, i) => (
                    <RunStepCard
                      key={i}
                      step={step}
                      expanded={expandedSteps.has(step.step)}
                      onToggleExpand={handleToggleExpand}
                    />
                  ))
                )}
              </div>
            </>
          )}
        </main>

        {/* RIGHT SIDEBAR — sticky execution timeline, ~280px */}
        <aside className="hidden xl:flex xl:w-72 xl:shrink-0 xl:flex-col">
          <div className="sticky top-[5.25rem] flex max-h-[calc(100vh-5.5rem)] flex-col gap-2 overflow-y-auto">
            {steps.length > 0 ? (
              <StepsTimeline
                steps={steps}
                totalNodes={nodes.length}
                totalDurationSec={runDurationSec(run?.started_at ?? null, run?.finished_at ?? null)}
                activeStep={activeTimelineStep}
                onNavigate={scrollToStep}
              />
            ) : run && nodes.length === 0 ? (
              <div className="rounded border border-edge bg-surface-raised p-2 text-body text-content-muted">
                Timeline appears once steps run.
              </div>
            ) : null}
          </div>
        </aside>
      </div>
    </div>
  );
}

/* Run Health strip — surfaces verdict, primary failure, slowest step at the top */
function RunHealthStrip({
  status,
  steps,
  totalNodes,
  startedAt,
  finishedAt,
  error,
  onJumpToStep,
}: {
  status: string;
  steps: RunStep[];
  totalNodes: number;
  startedAt: number | null;
  finishedAt: number | null;
  error: string | null;
  onJumpToStep: (stepId: string) => void;
}) {
  // Find primary failure
  const failedStep = steps.find((s) => s.status === "failed");
  // Find slowest step (by intra-step duration)
  let slowest: { name: string; sec: number } | null = null;
  let totalFailedTools = 0;
  for (const step of steps) {
    const { failedCount, durationSec } = toolCountFromStep(step);
    totalFailedTools += failedCount;
    if (durationSec != null && durationSec >= 0) {
      if (!slowest || durationSec > slowest.sec) {
        slowest = { name: step.step, sec: durationSec };
      }
    }
  }

  // Verdict: aggregate from last completed step that has a verdict
  let verdict: string | null = null;
  for (let i = steps.length - 1; i >= 0; i--) {
    const v = verdictFromStep(steps[i]);
    if (v) {
      verdict = v;
      break;
    }
  }

  const runSec = runDurationSec(startedAt, finishedAt);
  const completedCount = steps.filter((s) => s.status === "completed").length;

  return (
    <div className="border-b border-edge bg-surface-overlay px-3 py-2 xl:px-4">
      <div className="flex flex-wrap items-center gap-x-5 gap-y-1.5 text-meta">
        <HealthChip label="Status" tone={status === "failed" ? "failed" : status === "running" ? "running" : "neutral"}>
          <StatusPill value={status} kind="lifecycle" />
        </HealthChip>
        {verdict ? (
          <HealthChip label="Verdict">
            <StatusPill value={verdict} kind="verdict" label={verdict} />
          </HealthChip>
        ) : null}
        <HealthChip label="Progress">
          <span className="font-mono tabular-nums text-content-primary">
            {completedCount} / {totalNodes || steps.length}
          </span>
        </HealthChip>
        <HealthChip label="Duration">
          <Duration value={runSec} className="text-content-primary" />
        </HealthChip>
        {totalFailedTools > 0 ? (
          <HealthChip label="Failed tool calls" tone="failed">
            <span className="font-mono tabular-nums text-status-error">{totalFailedTools}</span>
          </HealthChip>
        ) : null}
        {failedStep ? (
          <HealthChip label="Primary failure" tone="failed">
            <button
              type="button"
              onClick={() => onJumpToStep(failedStep.step)}
              className="font-mono text-status-error hover:underline truncate max-w-[16rem]"
              title={`Jump to ${failedStep.step}`}
            >
              {failedStep.step}
            </button>
          </HealthChip>
        ) : null}
        {slowest && slowest.sec > 30 ? (
          <HealthChip label="Slowest step">
            <button
              type="button"
              onClick={() => onJumpToStep(slowest!.name)}
              className="font-mono text-content-secondary hover:text-content-primary hover:underline truncate max-w-[12rem]"
            >
              {slowest.name}{" "}
              <span className="text-content-muted">
                (<Duration value={slowest.sec} />)
              </span>
            </button>
          </HealthChip>
        ) : null}
      </div>
      {error ? (
        <div className="mt-1.5 truncate font-mono text-meta text-status-error" title={error}>
          {error.split("\n")[0]}
        </div>
      ) : null}
    </div>
  );
}

function HealthChip({
  label,
  tone = "neutral",
  children,
}: {
  label: string;
  tone?: "neutral" | "running" | "failed";
  children: React.ReactNode;
}) {
  const labelTone =
    tone === "failed"
      ? "text-status-error"
      : tone === "running"
        ? "text-status-running"
        : "text-content-muted";
  return (
    <div className="flex items-center gap-1.5">
      <span className={`uppercase tracking-[0.06em] ${labelTone}`}>{label}</span>
      <span className="flex items-center">{children}</span>
    </div>
  );
}

/* Compact label:value row for left sidebar */
function MetricRow({
  label,
  value,
  mono = false,
}: {
  label: string;
  value: React.ReactNode;
  mono?: boolean;
}) {
  return (
    <div className="flex items-start justify-between gap-2">
      <dt className="shrink-0 text-[10px] text-content-secondary">{label}</dt>
      <dd
        className={`min-w-0 text-right text-body text-content-primary ${mono ? "truncate font-mono text-meta" : ""}`}
      >
        {value}
      </dd>
    </div>
  );
}

/* Pill for mobile metric strip */
function MobileMetric({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="rounded border border-edge bg-surface-raised px-3 py-1.5">
      <div className="text-meta uppercase tracking-[0.06em] text-content-muted">{label}</div>
      <div className="text-body font-medium text-content-primary">{value}</div>
    </div>
  );
}
