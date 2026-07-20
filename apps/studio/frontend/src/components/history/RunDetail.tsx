/**
 * RunDetail — run detail pane (DESIGN-SYSTEM §4 master-detail).
 *
 * Renders the full run content inline: summary grid, branches, errors, files,
 * events. Used as the Fleet split-pane detail; the caller (SessionDetail) owns
 * the scroll container.
 */

import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslations } from "use-intl";
import InvocationSection from "@/components/history/InvocationDetail";
import OperationGraphSection from "@/components/history/OperationGraphSection";
import StatusVerdictChips from "@/components/ui/StatusVerdictChips";
import ExpectedArtifacts from "@/components/runs/ExpectedArtifacts";
import RunStepCard, { extractFilePaths } from "@/components/RunStepCard";
import { IconChevronDown, IconChevronRight } from "@/components/ui/icons";
import { getSession, streamSession, streamSignals, SESSION_MESSAGE_PAGE } from "@/lib/api";
import type { SessionDetail, SessionBranch, SessionMessage, SignalEvent } from "@/lib/api";
import { buildNodeStatusesByName, buildOperationGraph, laneFor } from "@/lib/operationGraph";
import type { LaneSignal, OperationStatus } from "@/lib/operationGraph";
import { deriveDisplayStatus } from "@/lib/runStatus";
import type { RunMessage, RunStep, WorkerGraph } from "@/lib/types";
import type { NodeExecStatus } from "@/components/canvas/StepNode";

const WorkerCanvas = lazy(() => import("@/components/canvas/WorkerCanvas"));

// ── Helpers ───────────────────────────────────────────────────────────────────

function compactValue(v: unknown): string {
  if (v == null) return "";
  if (typeof v === "object") {
    try {
      return JSON.stringify(v);
    } catch {
      return String(v);
    }
  }
  return String(v);
}

function formatDuration(sec: number): string {
  if (sec < 60) return `${sec}s`;
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return s > 0 ? `${m}m ${s}s` : `${m}m`;
}

function classifyLC(lc: string): string {
  if (lc.includes("ActionRequest")) return "action_request";
  if (lc.includes("ActionResponse")) return "action_response";
  if (lc.includes("System")) return "system";
  if (lc.includes("Instruction")) return "user";
  if (lc.includes("AssistantResponse")) return "assistant";
  return "unknown";
}

// The persisted/authored graph (Studio's early_graph) is only meaningful as
// the rendered DAG when it actually carries the designer's edges. Reactive
// runs persist an early snapshot with nodes but zero edges (they're added
// later, and the snapshot is never refreshed) — laid out with no edges, every
// node lands in a single dagre rank, rendering as a meaningless vertical
// column. When that happens and the runtime opGraph has real edges (derived
// from Node* depends_on/parent_id/cause_op_id), prefer opGraph instead. An
// authored graph that already has edges keeps priority, unreduced — see the
// note above nodeStatuses.
export function shouldRenderAuthoredGraph(
  graph: { nodes: unknown[]; edges: unknown[] | null | undefined } | null,
  opGraph: { edges: unknown[] },
): boolean {
  if (!graph) return false;
  const edgeCount = graph.edges?.length ?? 0;
  const isEdgeless = graph.nodes.length >= 2 && edgeCount === 0;
  return !(isEdgeless && opGraph.edges.length > 0);
}

// Raw SSE payloads arrive as an untyped Record — this is the boundary where
// an event is asserted to be a SessionMessage. SessionMessage.timestamp is a
// required number (the server column is REAL NOT NULL), so a malformed or
// future event carrying a non-numeric timestamp must be rejected here rather
// than let the cast manufacture a value the type promises can't happen.
export function isSessionMessageEvent(event: Record<string, unknown>): boolean {
  return !!(event.id && event.role && event.branch_id && typeof event.timestamp === "number");
}

export function appendStreamedMessage(
  session: SessionDetail,
  branchId: string,
  message: SessionMessage,
): SessionDetail {
  const existing = session.branches.find((branch) => branch.id === branchId);
  if (!existing) {
    return {
      ...session,
      branches: [
        ...session.branches,
        {
          id: branchId,
          name: branchId.slice(0, 8),
          created_at: message.timestamp,
          first_message_at: message.timestamp,
          last_message_at: message.timestamp,
          message_total: 1,
          messages: [message],
        },
      ],
    };
  }
  if (existing.messages.some((candidate) => candidate.id === message.id)) return session;

  return {
    ...session,
    branches: session.branches.map((branch) => {
      if (branch.id !== branchId) return branch;
      const firstMessageAt = branch.first_message_at ?? branch.started_at;
      const lastMessageAt = branch.last_message_at ?? branch.ended_at;
      return {
        ...branch,
        messages: [...branch.messages, message],
        message_total:
          Math.max(branch.message_total ?? branch.messages.length, branch.messages.length) + 1,
        first_message_at:
          firstMessageAt == null ? message.timestamp : Math.min(firstMessageAt, message.timestamp),
        last_message_at:
          lastMessageAt == null ? message.timestamp : Math.max(lastMessageAt, message.timestamp),
      };
    }),
  };
}

export function mergeCompletedSession(
  previous: SessionDetail,
  fresh: SessionDetail,
): SessionDetail {
  const freshById = new Map(fresh.branches.map((branch) => [branch.id, branch]));
  const previousIds = new Set(previous.branches.map((branch) => branch.id));
  const branches = previous.branches.map((branch) => {
    const freshBranch = freshById.get(branch.id);
    if (!freshBranch) return branch;
    const seen = new Set(branch.messages.map((message) => message.id));
    return {
      ...branch,
      ...freshBranch,
      messages: [
        ...branch.messages,
        ...freshBranch.messages.filter((message) => !seen.has(message.id)),
      ],
    };
  });
  for (const freshBranch of fresh.branches) {
    if (!previousIds.has(freshBranch.id)) branches.push(freshBranch);
  }
  return { ...previous, ...fresh, branches };
}

export function branchToRunStep(
  branch: SessionBranch,
  status: string,
  options?: { messageCount: number | null },
): RunStep {
  const msgs = branch.messages;
  const runMessages: RunMessage[] = [];

  const responseById = new Map<string, SessionMessage>();
  for (const m of msgs) {
    if (classifyLC(m.lion_class) === "action_response") {
      responseById.set(m.id, m);
    }
  }
  const pairedResponseIds = new Set<string>();

  for (const m of msgs) {
    const kind = classifyLC(m.lion_class);
    const content = (m.content ?? {}) as Record<string, unknown>;

    if (kind === "system") {
      const text = String(content.system_message ?? content.system ?? content.guidance ?? "");
      if (text)
        runMessages.push({
          role: "system",
          content: text,
          sender: m.sender ?? "",
          timestamp: m.timestamp,
        });
      continue;
    }

    if (kind === "user") {
      runMessages.push({
        role: "user",
        content: String(content.instruction ?? content.text ?? JSON.stringify(content)),
        sender: m.sender ?? "",
        timestamp: m.timestamp,
      });
      continue;
    }

    if (kind === "assistant") {
      runMessages.push({
        role: "assistant",
        content: String(content.assistant_response ?? content.response ?? ""),
        sender: m.sender ?? "",
        timestamp: m.timestamp,
      });
      continue;
    }

    if (kind === "action_request") {
      const fn = String(content.function ?? "");
      const args = (content.arguments ?? {}) as Record<string, unknown>;
      const respId = content.action_response_id ? String(content.action_response_id) : null;
      const respMsg = respId ? responseById.get(respId) : null;
      if (respMsg) pairedResponseIds.add(respMsg.id);

      const respContent = respMsg ? ((respMsg.content ?? {}) as Record<string, unknown>) : {};
      const output = respMsg ? String(respContent.output ?? "") : "";

      const summary = Object.entries(args)
        .slice(0, 2)
        .map(([k, v]) => {
          const s = compactValue(v);
          return s.length > 60 ? `${k}=${s.slice(0, 60)}…` : `${k}=${s}`;
        })
        .join(", ");

      runMessages.push({
        role: "tool_call",
        function: fn,
        summary,
        arguments: args,
        output,
        status: output.toLowerCase().includes("error") ? "error" : "ok",
        sender: m.sender ?? "",
        timestamp: m.timestamp,
      });
      continue;
    }

    if (kind === "action_response" && !pairedResponseIds.has(m.id)) {
      const fn = String(content.function ?? "");
      const output = String(content.output ?? "");
      runMessages.push({
        role: "tool_call",
        function: fn,
        output,
        status: "ok",
        sender: m.sender ?? "",
        timestamp: m.timestamp,
      });
    }
  }

  const rolesCounts: Record<string, number> = {};
  for (const rm of runMessages) {
    rolesCounts[rm.role] = (rolesCounts[rm.role] ?? 0) + 1;
  }

  const firstMessageAt = branch.first_message_at ?? branch.started_at ?? null;
  const lastMessageAt = branch.last_message_at ?? branch.ended_at ?? null;
  const durationSec =
    firstMessageAt != null && lastMessageAt != null
      ? Math.max(0, Math.round(lastMessageAt - firstMessageAt))
      : undefined;
  const messageCount =
    options?.messageCount ??
    (options ? null : Math.max(branch.message_total ?? 0, runMessages.length));

  return {
    step: branch.name || branch.id.slice(0, 8),
    status,
    result: {
      agent: branch.agent_name ?? branch.name ?? branch.id.slice(0, 8),
      model: branch.model ?? branch.provider ?? null,
      message_count: messageCount,
      roles: rolesCounts,
      duration_sec: durationSec,
    },
    messages: runMessages,
    timestamp: branch.created_at,
  };
}

export interface SessionSegment {
  op_id: string;
  branch_id: string;
  branch_name: string;
  status: string;
  started_at: number | null;
  ended_at: number | null;
}

export function buildRunSteps(
  session: SessionDetail,
  sessionStatus: string,
  segments: SessionSegment[],
): RunStep[] {
  const result: RunStep[] = [];
  for (const branch of session.branches) {
    const branchStatus = (branch as unknown as Record<string, unknown>).status as string | null;
    const branchSegments = segments.filter((segment) => segment.branch_id === branch.id);
    if (branchSegments.length <= 1) {
      result.push(branchToRunStep(branch, branchStatus || sessionStatus));
      continue;
    }

    branchSegments.forEach((segment, index) => {
      const segmentMessages = branch.messages.filter((message) => {
        const timestamp = message.timestamp;
        const after = segment.started_at == null || timestamp >= segment.started_at;
        const before = segment.ended_at == null || timestamp <= segment.ended_at + 1;
        return after && before;
      });
      result.push(
        branchToRunStep(
          {
            ...branch,
            messages: segmentMessages,
            name: `${branch.name || branch.id.slice(0, 8)} [${segment.op_id}]`,
            first_message_at: segment.started_at,
            last_message_at: segment.ended_at,
          },
          segment.status || branchStatus || sessionStatus,
          {
            messageCount:
              index === branchSegments.length - 1 ? (branch.message_total ?? null) : null,
          },
        ),
      );
    });
  }
  return result;
}

// ── Section shared header ─────────────────────────────────────────────────────

function SectionHeader({
  label,
  count,
  errorTone,
}: {
  label: string;
  count?: number;
  errorTone?: boolean;
}) {
  return (
    <div className="mb-2 flex items-center gap-2">
      <h2 className="text-label font-semibold text-content-primary">{label}</h2>
      {count != null && (
        <span
          className={`rounded px-1.5 py-0 font-mono text-[length:var(--t-xs)] ${
            errorTone && count > 0
              ? "bg-status-error-bg text-status-error"
              : "bg-surface-overlay text-content-muted"
          }`}
        >
          {count}
        </span>
      )}
    </div>
  );
}

// ── Overview section ──────────────────────────────────────────────────────────

interface OverviewData {
  status: string;
  durationSec: number | null;
  branchCount: number;
  messageCount: number;
  toolCallCount: number;
  errorCount: number;
  showTopic?: string | null;
  showPlayName?: string | null;
  playbookName?: string | null;
}

function OverviewSection({ data }: { data: OverviewData }) {
  const t = useTranslations("history.detail");
  const stats: Array<{ label: string; value: string; tone?: "ok" | "error" }> = [
    { label: t("statStatus"), value: data.status },
    ...(data.durationSec != null
      ? [{ label: t("statDuration"), value: formatDuration(data.durationSec) }]
      : []),
    { label: t("statBranches"), value: String(data.branchCount) },
    { label: t("statMessages"), value: String(data.messageCount) },
    {
      label: t("statToolCalls"),
      value: String(data.toolCallCount),
    },
    {
      label: t("statErrors"),
      value: String(data.errorCount),
      tone: data.errorCount > 0 ? ("error" as const) : ("ok" as const),
    },
  ];

  const provenance = [
    data.showTopic && { label: t("statTopic"), value: data.showTopic },
    data.showPlayName && { label: t("statPlay"), value: data.showPlayName },
    data.playbookName && { label: t("statPlaybook"), value: data.playbookName },
  ].filter(Boolean) as Array<{ label: string; value: string }>;

  return (
    <div id="run-overview" className="scroll-mt-4">
      <SectionHeader label={t("sectionOverview")} />
      <div className="rounded border border-edge bg-surface-raised px-4 py-3 shadow-card">
        <div className="grid grid-cols-2 gap-x-6 gap-y-3 sm:grid-cols-3">
          {stats.map((s) => (
            <div key={s.label} className="flex flex-col gap-0.5">
              <span className="text-[length:var(--t-xs)] font-semibold uppercase tracking-wider text-content-muted">
                {s.label}
              </span>
              <span
                className={`font-mono text-label font-semibold tabular-nums tracking-tight ${
                  s.tone === "error"
                    ? "text-status-error"
                    : s.tone === "ok"
                      ? "text-status-success"
                      : "text-content-primary"
                }`}
              >
                {s.value}
              </span>
            </div>
          ))}
        </div>
        {provenance.length > 0 && (
          <div className="mt-3 flex flex-wrap gap-3 border-t border-edge-subtle pt-3">
            {provenance.map((p) => (
              <div key={p.label} className="flex items-center gap-1.5">
                <span className="text-[length:var(--t-xs)] uppercase tracking-wide text-content-muted">
                  {p.label}
                </span>
                <span className="font-mono text-meta text-content-secondary">{p.value}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

export function resolveOverviewCounts(
  messageStats: SessionDetail["message_stats"],
  loaded: { toolCallCount: number; errorCount: number },
): { toolCallCount: number; errorCount: number } {
  return {
    toolCallCount: messageStats?.tool_call_count ?? loaded.toolCallCount,
    errorCount: messageStats?.error_count ?? loaded.errorCount,
  };
}

// ── Branches section ──────────────────────────────────────────────────────────

function BranchesSection({
  steps,
  live,
  expandedSteps,
  onToggleExpand,
  runId,
  artifactRoot,
  runFiles,
  onLoadOlder,
  olderMessagesRemaining,
  loadingOlder,
}: {
  steps: RunStep[];
  live: boolean;
  expandedSteps: Set<string>;
  onToggleExpand: (stepId: string, next: boolean) => void;
  runId?: string;
  artifactRoot?: string | null;
  runFiles?: string[];
  onLoadOlder?: () => void;
  olderMessagesRemaining?: number;
  loadingOlder?: boolean;
}) {
  const t = useTranslations("history.detail");
  return (
    <div id="run-branches" className="scroll-mt-4">
      <SectionHeader label={t("sectionBranches")} count={steps.length} />
      <div className="flex flex-col gap-1.5">
        {steps.length === 0 ? (
          <div className="border border-edge bg-surface-base px-3 py-10 text-center text-sm text-content-muted">
            {live ? (
              <span className="flex items-center justify-center gap-2">
                <span className="relative flex h-2 w-2">
                  <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-status-running opacity-75" />
                  <span className="relative inline-flex h-2 w-2 rounded-full bg-status-running" />
                </span>
                {t("waitingMessages")}
              </span>
            ) : (
              t("noMessages")
            )}
          </div>
        ) : (
          steps.map((step) => (
            <RunStepCard
              key={step.step}
              step={step}
              expanded={expandedSteps.has(step.step)}
              onToggleExpand={onToggleExpand}
              runId={runId}
              artifactRoot={artifactRoot}
              runFiles={runFiles}
              onLoadOlder={onLoadOlder}
              olderMessagesRemaining={olderMessagesRemaining}
              loadingOlder={loadingOlder}
            />
          ))
        )}
      </div>
    </div>
  );
}

// ── Errors section ────────────────────────────────────────────────────────────

interface ErrorEntry {
  fn: string;
  branch: string;
  timestamp: number | null;
  output: string;
  summary?: string;
}

function ErrorsSection({ errors, partial }: { errors: ErrorEntry[]; partial?: boolean }) {
  const t = useTranslations("history.detail");
  const groups = useMemo(() => {
    const map = new Map<string, ErrorEntry[]>();
    for (const err of errors) {
      const list = map.get(err.fn) ?? [];
      list.push(err);
      map.set(err.fn, list);
    }
    return Array.from(map.entries()).sort((a, b) => b[1].length - a[1].length);
  }, [errors]);

  const [expandedGroups, setExpandedGroups] = useState<Set<string>>(new Set());

  const toggleGroup = (fn: string) => {
    setExpandedGroups((prev) => {
      const next = new Set(prev);
      if (next.has(fn)) next.delete(fn);
      else next.add(fn);
      return next;
    });
  };

  return (
    <div id="run-errors" className="scroll-mt-4">
      <SectionHeader
        label={t("sectionErrors")}
        count={errors.length}
        errorTone={errors.length > 0}
      />
      {errors.length === 0 ? (
        <div className="flex items-center gap-2 rounded border border-edge bg-surface-raised px-4 py-3 text-sm text-status-success">
          <span>{partial ? t("noBranchErrorsPartial") : t("noBranchErrors")}</span>
        </div>
      ) : (
        <div className="flex flex-col gap-1.5">
          {groups.map(([fn, errs]) => {
            const isOpen = expandedGroups.has(fn);
            const first = errs[0];
            return (
              <div
                key={fn}
                className="rounded border border-l-2 border-edge border-l-status-error bg-surface-raised"
              >
                <button
                  type="button"
                  aria-expanded={isOpen}
                  onClick={() => toggleGroup(fn)}
                  className="flex w-full items-center gap-2 px-3 py-2 text-left hover:bg-surface-overlay"
                >
                  <span className="flex items-center text-content-muted">
                    {isOpen ? (
                      <IconChevronDown size={10} strokeWidth={2.25} />
                    ) : (
                      <IconChevronRight size={10} strokeWidth={2.25} />
                    )}
                  </span>
                  <span className="font-mono text-[length:var(--t-xs)] font-semibold text-status-error">
                    {fn}
                  </span>
                  <span className="rounded bg-status-error-bg px-1.5 py-0 font-mono text-[length:var(--t-xs)] text-status-error">
                    ×{errs.length}
                  </span>
                  <span className="text-[length:var(--t-xs)] text-content-muted">
                    first in{" "}
                    <span className="font-mono text-content-secondary">{first?.branch}</span>
                    {first?.timestamp != null && (
                      <>
                        {" "}
                        ·{" "}
                        {new Date(first.timestamp * 1000).toLocaleTimeString([], {
                          hour: "2-digit",
                          minute: "2-digit",
                          second: "2-digit",
                        })}
                      </>
                    )}
                  </span>
                  {!isOpen && first?.output && (
                    <span className="ml-auto truncate max-w-xs font-mono text-[length:var(--t-xs)] text-content-muted">
                      {first.output.split("\n")[0]?.slice(0, 80)}
                    </span>
                  )}
                </button>
                {isOpen && (
                  <div className="flex flex-col gap-2 border-t border-edge px-3 pb-2 pt-2">
                    {errs.map((err, i) => (
                      <div key={i} className="flex flex-col gap-1">
                        <div className="flex items-center gap-2 text-[length:var(--t-xs)]">
                          <span className="font-mono text-content-secondary">{err.branch}</span>
                          {err.timestamp != null && (
                            <span className="text-content-muted">
                              {new Date(err.timestamp * 1000).toLocaleTimeString([], {
                                hour: "2-digit",
                                minute: "2-digit",
                                second: "2-digit",
                              })}
                            </span>
                          )}
                        </div>
                        {err.summary && (
                          <p className="truncate font-mono text-[length:var(--t-xs)] text-content-secondary">
                            $ {err.summary}
                          </p>
                        )}
                        {err.output && (
                          <pre className="max-h-32 overflow-auto rounded border border-status-error/20 bg-status-error-bg p-2 font-mono text-[length:var(--t-xs)] leading-relaxed text-status-error">
                            {err.output.length > 1500
                              ? err.output.slice(0, 1500) + "\n…[truncated]"
                              : err.output}
                          </pre>
                        )}
                      </div>
                    ))}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

// ── Files section ─────────────────────────────────────────────────────────────

function FilesSection({ files, partial }: { files: string[]; partial?: boolean }) {
  const t = useTranslations("history.detail");
  return (
    <div id="run-files" className="scroll-mt-4">
      <SectionHeader label={t("sectionFiles")} count={files.length} />
      {files.length === 0 ? (
        <div className="rounded border border-edge bg-surface-raised px-4 py-3 text-sm text-content-muted">
          {partial ? t("noFilesPartial") : t("noFiles")}
        </div>
      ) : (
        <div className="max-h-56 overflow-y-auto rounded border border-edge bg-surface-raised px-3 py-2">
          <ul className="flex flex-col gap-0.5">
            {files.map((f) => (
              <li key={f} className="font-mono text-[length:var(--t-xs)] text-content-secondary">
                {f}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

// ── Events section ────────────────────────────────────────────────────────────

const KIND_BADGE: Record<string, { label: string; tone: string }> = {
  NodeQueued: { label: "queued", tone: "bg-surface-overlay text-content-muted" },
  NodeStarted: { label: "started", tone: "bg-status-running-bg text-status-running" },
  NodeCompleted: { label: "done", tone: "bg-status-success-bg text-status-success" },
  NodeFailed: { label: "failed", tone: "bg-status-error-bg text-status-error" },
  NodeAwaitingApproval: { label: "approval", tone: "bg-status-warning-bg text-status-warning" },
  NodeEscalated: { label: "escalated", tone: "bg-status-error-bg text-status-error" },
  GateDenied: { label: "gate-denied", tone: "bg-status-error-bg text-status-error" },
  RunStart: { label: "run-start", tone: "bg-status-running-bg text-status-running" },
  RunEnd: { label: "run-end", tone: "bg-status-success-bg text-status-success" },
  RunFailed: { label: "run-failed", tone: "bg-status-error-bg text-status-error" },
  MessageAdded: { label: "message", tone: "bg-surface-overlay text-content-muted" },
  HookSignal: { label: "hook", tone: "bg-surface-overlay text-content-muted" },
  StructuredOutput: { label: "output", tone: "bg-surface-overlay text-content-secondary" },
};

// A NodeEscalated with route="notify" is a soft ("fyi") help signal, not a
// real escalation — badge it distinctly (warning tone, like an approval
// request) instead of the unconditional error-toned "escalated" label.
export function badgeForEvent(ev: SignalEvent): { label: string; tone: string } {
  if (ev.kind === "NodeEscalated" && ev.payload?.route === "notify") {
    return { label: "notify", tone: "bg-status-warning-bg text-status-warning" };
  }
  return KIND_BADGE[ev.kind] ?? { label: ev.kind, tone: "bg-surface-overlay text-content-muted" };
}

type LaneState = OperationStatus;

const LANE_TONE: Record<LaneState, string> = {
  queued: "bg-surface-overlay text-content-muted",
  running: "bg-status-running-bg text-status-running",
  awaiting_approval: "bg-status-warning-bg text-status-warning",
  paused: "bg-status-warning-bg text-status-warning",
  succeeded: "bg-status-success-bg text-status-success",
  failed: "bg-status-error-bg text-status-error",
  escalated: "bg-status-error-bg text-status-error",
};

interface LaneSummary {
  op_id: string;
  lane: LaneState;
  count: number;
}

function EventsSection({ events, live }: { events: SignalEvent[]; live: boolean }) {
  const t = useTranslations("history.detail");
  const laneSummaries = useMemo((): LaneSummary[] => {
    const byOp = new Map<string, LaneSignal[]>();
    for (const ev of events) {
      if (!ev.op_id) continue;
      const list = byOp.get(ev.op_id) ?? [];
      const route = ev.payload?.route;
      list.push(typeof route === "string" ? { kind: ev.kind, route } : ev.kind);
      byOp.set(ev.op_id, list);
    }
    return Array.from(byOp.entries()).map(([op_id, kinds]) => ({
      op_id,
      lane: laneFor(kinds),
      count: kinds.length,
    }));
  }, [events]);

  return (
    <div id="run-events" className="scroll-mt-4">
      <SectionHeader label={t("sectionEvents")} count={events.length} />

      {laneSummaries.length > 0 && (
        <div className="mb-2 flex flex-wrap gap-1.5">
          {laneSummaries.map(({ op_id, lane, count }) => (
            <div
              key={op_id}
              className="flex items-center gap-1 rounded border border-edge bg-surface-raised px-2 py-0.5"
            >
              <span className="font-mono text-[length:var(--t-xs)] text-content-secondary">
                {op_id}
              </span>
              <span
                className={`rounded px-1.5 py-0 font-mono text-[length:var(--t-xs)] font-semibold ${LANE_TONE[lane]}`}
              >
                {lane}
              </span>
              <span className="font-mono text-[length:var(--t-xs)] text-content-muted">
                ×{count}
              </span>
            </div>
          ))}
        </div>
      )}

      {events.length === 0 ? (
        <div className="rounded border border-edge bg-surface-base px-3 py-10 text-center text-sm text-content-muted">
          {live ? (
            <span className="flex items-center justify-center gap-2">
              <span className="relative flex h-2 w-2">
                <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-status-running opacity-75" />
                <span className="relative inline-flex h-2 w-2 rounded-full bg-status-running" />
              </span>
              {t("waitingEvents")}
            </span>
          ) : (
            t("noEvents")
          )}
        </div>
      ) : (
        <div className="max-h-72 overflow-y-auto rounded border border-edge bg-surface-raised">
          <div className="flex flex-col divide-y divide-edge-subtle">
            {events.map((ev) => {
              const badge = badgeForEvent(ev);
              const hasPayload = ev.payload && Object.keys(ev.payload).length > 0;
              return (
                <div
                  key={ev.id}
                  className="flex items-start gap-2 px-3 py-1.5 hover:bg-surface-overlay"
                >
                  <span className="mt-0.5 shrink-0 font-mono text-[length:var(--t-xs)] tabular-nums text-content-muted">
                    {new Date(ev.ts * 1000).toLocaleTimeString([], {
                      hour: "2-digit",
                      minute: "2-digit",
                      second: "2-digit",
                    })}
                  </span>
                  <span
                    className={`mt-0.5 shrink-0 rounded px-1.5 py-0 font-mono text-[length:var(--t-xs)] font-semibold ${badge.tone}`}
                  >
                    {badge.label}
                  </span>
                  {ev.op_id && (
                    <span className="mt-0.5 shrink-0 font-mono text-[length:var(--t-xs)] text-content-secondary">
                      {ev.op_id}
                    </span>
                  )}
                  {hasPayload && (
                    <span className="min-w-0 truncate font-mono text-[length:var(--t-xs)] text-content-muted">
                      {Object.entries(ev.payload)
                        .filter(([k]) => k !== "op_id")
                        .slice(0, 3)
                        .map(([k, v]) => {
                          const s = compactValue(v);
                          return `${k}=${s.length > 40 ? s.slice(0, 40) + "…" : s}`;
                        })
                        .join("  ")}
                    </span>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}

// ── Public component ──────────────────────────────────────────────────────────

export interface RunDetailProps {
  /** Session ID to load. */
  id: string;
}

export default function RunDetail({ id }: RunDetailProps) {
  const t = useTranslations("history.detail");
  const [session, setSession] = useState<SessionDetail | null>(null);
  const [runGraph, setRunGraph] = useState<WorkerGraph | null>(null);
  const [live, setLive] = useState(false);
  const [done, setDone] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const [expandedSteps, setExpandedSteps] = useState<Set<string>>(new Set());
  const [signalEvents, setSignalEvents] = useState<SignalEvent[]>([]);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const olderOffsetRef = useRef(SESSION_MESSAGE_PAGE);
  const suppressAutoScrollRef = useRef(false);
  const initialScrollDoneRef = useRef(false);

  useEffect(() => {
    if (!id) return;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- reset stale state before async fetch; setState only fires in the effect body synchronously, not in callbacks
    setSession(null);
    setRunGraph(null);
    setLive(false);
    setDone(false);
    setError(null);
    setSignalEvents([]);
    setLoadingOlder(false);
    olderOffsetRef.current = SESSION_MESSAGE_PAGE;
    initialScrollDoneRef.current = false;
    getSession(id)
      .then((s) => {
        setSession(s);
        const ss = (s.status ?? "").toLowerCase();
        if (
          ss === "completed" ||
          ss === "done" ||
          ss === "success" ||
          ss === "failed" ||
          ss === "failure" ||
          ss === "cancelled"
        ) {
          setDone(true);
        }
        if (s.branches.length <= 3) {
          setExpandedSteps(new Set(s.branches.map((b) => b.name || b.id.slice(0, 8))));
        } else {
          const first = s.branches[0];
          if (first) {
            setExpandedSteps(new Set([first.name || first.id.slice(0, 8)]));
          }
        }
        const graph = (s as unknown as Record<string, unknown>).graph as
          | { nodes: WorkerGraph["nodes"]; edges?: WorkerGraph["edges"] | null }
          | null
          | undefined;
        if (graph && graph.nodes && graph.nodes.length > 0) {
          setRunGraph({
            name: s.name || id,
            description: "",
            nodes: graph.nodes,
            // A persisted graph may omit edges entirely; WorkerCanvas maps
            // over the array, so an absent field must normalize to empty.
            edges: graph.edges ?? [],
          });
        }
      })
      .catch((e: unknown) => setError(String(e)));
  }, [id]);

  useEffect(() => {
    if (!id) return;
    let cancelled = false;
    const stop = streamSession(id, (event) => {
      if (event.type === "heartbeat") return;
      if (event.type === "done") {
        setDone(true);
        setLive(false);
        // The initial fetch's status/reason fields are now stale (the run
        // just finished) — refetch so the terminal status/verdict derivation
        // reflects the real outcome instead of the pre-completion snapshot.
        // Guarded on id: if the viewer navigates to a different run before
        // this resolves, it must not clobber that run's freshly-fetched state.
        getSession(id)
          .then((fresh) => {
            if (cancelled) return;
            setSession((prev) =>
              prev && prev.id === fresh.id ? mergeCompletedSession(prev, fresh) : prev,
            );
          })
          .catch(() => {});
        return;
      }
      setLive(true);
      if (isSessionMessageEvent(event)) {
        const msg = event as unknown as SessionMessage;
        setSession((prev) => {
          if (!prev) return prev;
          const branchId = String(event.branch_id);
          return appendStreamedMessage(prev, branchId, msg);
        });
      }
    });
    return () => {
      cancelled = true;
      stop();
    };
  }, [id]);

  useEffect(() => {
    if (!id) return;
    const stop = streamSignals(id, (event) => {
      if ("type" in event) return;
      const sig = event as SignalEvent;
      setSignalEvents((prev) => {
        if (prev.some((e) => e.id === sig.id)) return prev;
        return [...prev, sig];
      });
    });
    return () => {
      stop();
      setSignalEvents([]);
    };
  }, [id]);

  useEffect(() => {
    if (suppressAutoScrollRef.current) {
      suppressAutoScrollRef.current = false;
      return;
    }
    // Scroll to the newest message once when a session first loads; polling
    // refreshes must not yank the operator's scroll position.
    if (session && !initialScrollDoneRef.current) {
      initialScrollDoneRef.current = true;
      bottomRef.current?.scrollIntoView({ behavior: "smooth" });
    }
  }, [session]);

  const handleToggleExpand = useCallback((stepId: string, next: boolean) => {
    setExpandedSteps((prev) => {
      const updated = new Set(prev);
      if (next) updated.add(stepId);
      else updated.delete(stepId);
      return updated;
    });
  }, []);

  const hiddenOlderCount = useMemo(() => {
    if (!session) return 0;
    return session.branches.reduce((n, b) => {
      const total = b.message_total ?? b.messages.length;
      return n + Math.max(0, total - b.messages.length);
    }, 0);
  }, [session]);

  const handleLoadOlder = useCallback(() => {
    if (!id || loadingOlder) return;
    setLoadingOlder(true);
    suppressAutoScrollRef.current = true;
    const offset = olderOffsetRef.current;
    getSession(id, { messageOffset: offset })
      .then((older) => {
        olderOffsetRef.current = offset + SESSION_MESSAGE_PAGE;
        setSession((prev) => {
          if (!prev) return prev;
          const olderById = new Map(older.branches.map((b) => [b.id, b]));
          return {
            ...prev,
            branches: prev.branches.map((b) => {
              const page = olderById.get(b.id);
              if (!page || page.messages.length === 0) return b;
              const have = new Set(b.messages.map((m) => m.id));
              const fresh = page.messages.filter((m) => !have.has(m.id));
              if (fresh.length === 0) return b;
              return {
                ...b,
                messages: [...fresh, ...b.messages],
                message_total: page.message_total ?? b.message_total,
              };
            }),
          };
        });
      })
      .catch((e: unknown) => setError(String(e)))
      .finally(() => setLoadingOlder(false));
  }, [id, loadingOlder]);

  const sessionStatus = done ? "completed" : live ? "running" : "completed";

  const segments = useMemo(() => {
    if (!session) return [] as SessionSegment[];
    const raw = (session as unknown as Record<string, unknown>).segments;
    return (Array.isArray(raw) ? raw : []) as SessionSegment[];
  }, [session]);

  const steps = useMemo(
    () => (session ? buildRunSteps(session, sessionStatus, segments) : []),
    [session, sessionStatus, segments],
  );

  // Run-wide known file surface (union across every step/agent branch) —
  // the file-link resolver's save-root fallback when a bare filename isn't
  // in the emitting agent's own step but was written by a sibling agent.
  // Steps only cover the loaded (tail-windowed) messages, so a reference to
  // a file touched earlier in a long session can't resolve from steps alone
  // — seed/merge with the server's full-session union (message_stats.files),
  // which is computed over every branch's full progression, not the window.
  const runFiles = useMemo(() => {
    const set = new Set<string>(session?.message_stats?.files ?? []);
    for (const step of steps) {
      for (const p of extractFilePaths(step.messages ?? [])) set.add(p);
    }
    return Array.from(set);
  }, [steps, session]);

  const errors = useMemo(() => {
    const errs: ErrorEntry[] = [];
    for (const step of steps) {
      for (const msg of step.messages ?? []) {
        if (msg.role === "tool_call" && msg.status === "error") {
          errs.push({
            fn: msg.function ?? "unknown",
            branch: step.step,
            timestamp: msg.timestamp ?? null,
            output: msg.output ?? "",
            summary: msg.summary,
          });
        }
      }
    }
    return errs;
  }, [steps]);

  const files = useMemo(() => {
    const paths = new Set<string>();
    for (const step of steps) {
      for (const msg of step.messages ?? []) {
        if (msg.role === "tool_call" && msg.arguments) {
          const fp = msg.arguments.file_path || msg.arguments.path;
          if (typeof fp === "string") paths.add(fp);
        }
      }
    }
    return Array.from(paths).sort();
  }, [steps]);

  const opGraph = useMemo(
    () => buildOperationGraph(signalEvents.filter((e) => !!e.op_id)),
    [signalEvents],
  );

  const execSteps = useMemo(
    () =>
      steps.map((s) => ({
        step: s.step,
        status: s.status,
        result: s.result,
        timestamp: s.timestamp ?? undefined,
      })),
    [steps],
  );

  // runGraph is the persisted/authored graph (Studio's early_graph) — its
  // edges are exactly what the designer wired, and may carry conditions that
  // only apply to a specific (possibly transitive-looking) edge. Do NOT
  // transitively reduce it: unlike the runtime-derived opGraph below (whose
  // edges come from depends_on ancestor lists and legitimately need
  // deduplication), every authored edge here is meaningful and must render.

  // Live per-node status correlated by authored step id (Node* payload.name),
  // never by op_id — see lib/operationGraph.ts. Only meaningful when a
  // planned graph exists to correlate against.
  const nodeStatuses = useMemo((): Record<string, NodeExecStatus> | undefined => {
    if (!runGraph) return undefined;
    const byName = buildNodeStatusesByName(signalEvents);
    const result: Record<string, NodeExecStatus> = {};
    for (const node of runGraph.nodes) {
      const live = byName.get(node.id);
      if (live) result[node.id] = live.status === "succeeded" ? "completed" : live.status;
    }
    return result;
  }, [runGraph, signalEvents]);

  if (error) {
    return (
      <div className="flex items-center justify-center py-20">
        <div className="rounded border border-status-error/30 bg-status-error-bg px-4 py-3 text-body text-status-error shadow-card">
          {error}
        </div>
      </div>
    );
  }

  if (!session) {
    return (
      <div className="flex items-center justify-center py-20">
        <div className="flex flex-col items-center gap-3">
          <div className="flex gap-1">
            <span
              className="block h-2 w-2 rounded-full bg-content-muted opacity-60 animate-bounce"
              style={{ animationDelay: "0ms" }}
            />
            <span
              className="block h-2 w-2 rounded-full bg-content-muted opacity-60 animate-bounce"
              style={{ animationDelay: "150ms" }}
            />
            <span
              className="block h-2 w-2 rounded-full bg-content-muted opacity-60 animate-bounce"
              style={{ animationDelay: "300ms" }}
            />
          </div>
          <p className="text-meta text-content-muted">Loading session…</p>
        </div>
      </div>
    );
  }

  const totalMessages = session.branches.reduce(
    (n, b) => n + Math.max(b.message_total ?? 0, b.messages.length),
    0,
  );
  const endRef = session.ended_at ?? (done ? session.updated_at : null);
  const startRef = session.started_at ?? session.created_at;
  const partialWindow = session.branches.some((b) => (b.message_total ?? 0) > b.messages.length);
  const durationSec =
    startRef != null && endRef != null ? Math.max(0, Math.round(endRef - startRef)) : null;
  const loadedToolCallCount = steps.reduce((n, s) => {
    return n + (s.messages ?? []).filter((m) => m.role === "tool_call").length;
  }, 0);
  const { toolCallCount, errorCount } = resolveOverviewCounts(session.message_stats, {
    toolCallCount: loadedToolCallCount,
    errorCount: errors.length,
  });

  // DESIGN-BRIEF §0: derive from the real status_reason fields, not the
  // done/live booleans — those conflate every terminal status (including
  // failed and orphaned) into a hardcoded "completed" label.
  const runForStatus = {
    status: session.status ?? (done ? "completed" : "running"),
    status_reason_code: session.status_reason_code,
    status_reason_summary: session.status_reason_summary,
  };
  const displayStatus = deriveDisplayStatus(runForStatus);

  const overviewData: OverviewData = {
    status: displayStatus,
    durationSec,
    branchCount: session.branches.length,
    messageCount: totalMessages,
    toolCallCount,
    errorCount,
    showTopic: (session as unknown as Record<string, unknown>).show_topic as
      | string
      | null
      | undefined,
    showPlayName: (session as unknown as Record<string, unknown>).show_play_name as
      | string
      | null
      | undefined,
    playbookName: (session as unknown as Record<string, unknown>).playbook_name as
      | string
      | null
      | undefined,
  };

  const content = (
    <div className="flex flex-col gap-6 p-3">
      {/* Compact pane header — name + live badge + elapsed */}
      <div className="flex items-center gap-2 border-b border-edge pb-1">
        <span className="min-w-0 flex-1 truncate font-mono text-[length:var(--t-base)] font-semibold text-content-primary">
          {session.name || session.id.slice(0, 8)}
        </span>
        <StatusVerdictChips run={runForStatus} />
        {live && !done && (
          <span className="flex shrink-0 items-center gap-1 text-[length:var(--t-xs)] text-status-success">
            <span className="relative flex h-1.5 w-1.5">
              <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-status-success opacity-75" />
              <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-status-success" />
            </span>
            {t("live")}
          </span>
        )}
      </div>

      <OverviewSection data={overviewData} />
      {session.invocation_id && (
        <InvocationSection invocationId={session.invocation_id} currentSessionId={session.id} />
      )}
      <ExpectedArtifacts
        contract={session.artifact_contract_json}
        verification={session.artifact_verification_json}
      />
      {runGraph && shouldRenderAuthoredGraph(runGraph, opGraph) ? (
        <div id="run-dag" className="scroll-mt-4">
          <SectionHeader label={t("sectionExecutionGraph")} count={runGraph.nodes.length} />
          <div className="h-[280px] rounded border border-edge bg-surface-raised shadow-card overflow-hidden">
            <Suspense fallback={null}>
              <WorkerCanvas
                graph={runGraph}
                editable={false}
                execSteps={execSteps}
                nodeStatuses={nodeStatuses}
                compact
              />
            </Suspense>
          </div>
        </div>
      ) : (
        opGraph.nodes.length > 0 && (
          <div id="run-dag" className="scroll-mt-4">
            <SectionHeader label={t("sectionExecutionGraph")} count={opGraph.nodes.length} />
            <OperationGraphSection state={opGraph} live={live && !done} />
          </div>
        )
      )}
      {hiddenOlderCount > 0 && (
        <button
          type="button"
          onClick={handleLoadOlder}
          disabled={loadingOlder}
          className="self-start rounded border border-edge bg-surface-raised px-3 py-1.5 font-mono text-[length:var(--t-xs)] text-content-secondary transition-colors hover:border-accent/50 hover:text-content-primary disabled:opacity-50"
        >
          {loadingOlder
            ? "…"
            : `${t("loadOlder")} · ${t("olderRemaining", { count: hiddenOlderCount })}`}
        </button>
      )}
      <BranchesSection
        steps={steps}
        live={live}
        expandedSteps={expandedSteps}
        onToggleExpand={handleToggleExpand}
        runId={session.id}
        artifactRoot={session.artifacts_path}
        runFiles={runFiles}
        onLoadOlder={handleLoadOlder}
        olderMessagesRemaining={hiddenOlderCount}
        loadingOlder={loadingOlder}
      />
      <ErrorsSection errors={errors} partial={partialWindow} />
      <FilesSection files={files} partial={partialWindow} />
      <EventsSection events={signalEvents} live={live && !done} />
      <div ref={bottomRef} />
    </div>
  );

  return content;
}
