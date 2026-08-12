/**
 * RunDetail — run detail pane (DESIGN-SYSTEM §4 master-detail).
 *
 * Renders the full run content inline: summary grid, branches, errors, files,
 * events. Used as the Fleet split-pane detail; the caller (SessionDetail) owns
 * the scroll container.
 */

import {
  lazy,
  Suspense,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import type { MouseEvent as ReactMouseEvent } from "react";
import { useTranslations } from "use-intl";
import InvocationSection from "@/components/history/InvocationDetail";
import OperationGraphSection from "@/components/history/OperationGraphSection";
import StatusVerdictChips from "@/components/ui/StatusVerdictChips";
import ExpectedArtifacts from "@/components/runs/ExpectedArtifacts";
import ResumeRun from "@/components/history/ResumeRun";
import RunStepCard, { extractFilePaths } from "@/components/RunStepCard";
import { IconChevronDown, IconChevronRight } from "@/components/ui/icons";
import { ApiError, getInvocation, getSession, streamSession, streamSignals } from "@/lib/api";
import type { SessionDetail, SessionBranch, SessionMessage, SignalEvent } from "@/lib/api";
import {
  buildNodeStatusesByName,
  buildOperationGraph,
  laneFor,
  transitiveReduceDisplay,
} from "@/lib/operationGraph";
import type { LaneSignal, OperationStatus } from "@/lib/operationGraph";
import { buildNodeActivityByName } from "@/lib/nodeActivity";
import type { NodeActivitySnapshot } from "@/lib/nodeActivity";
import {
  deriveDisplayStatus,
  deriveVerdict,
  isEffectivelyActive,
  isUnsuccessfulTerminal,
} from "@/lib/runStatus";
import type { Verdict } from "@/lib/runStatus";
import type {
  OperatorCommandProposal,
  RunMessage,
  RunResumeResponse,
  RunStep,
  WorkerGraph,
} from "@/lib/types";
import type { NodeExecStatus } from "@/components/canvas/StepNode";
import {
  deriveProgressCounts,
  computeElapsedSeconds,
  formatElapsed,
  reconcileNodeStatuses,
} from "@/lib/execGraphProgress";
import type { ProgressCounts } from "@/lib/execGraphProgress";
import {
  applyExecutablePath,
  confirmRunControl,
  controlKindFor,
  derivePausePhase,
  hasAnyExecutablePath,
  pauseControlState,
  proposeRunControl,
  resumeControlState,
  steerControlState,
} from "@/lib/runControls";
import type { ControlKind, ControlReasonCode, ControlVerb, PausePhase } from "@/lib/runControls";

const WorkerCanvas = lazy(() => import("@/components/canvas/WorkerCanvas"));

// ── Helpers ───────────────────────────────────────────────────────────────────

/** A value whose entire content is one identifier, as a short id — or null for
 * anything else. Deliberately narrow: only an object whose single key is `id`
 * qualifies, so shortening can never drop a sibling field the reader would
 * have wanted. Anything richer keeps its full rendering. */
export function refShortId(v: unknown): string | null {
  if (!v || typeof v !== "object" || Array.isArray(v)) return null;
  const keys = Object.keys(v as object);
  if (keys.length !== 1 || keys[0] !== "id") return null;
  const id = (v as { id: unknown }).id;
  return typeof id === "string" && id ? id.slice(0, 8) : null;
}

export function compactValue(v: unknown): string {
  if (v == null) return "";
  if (typeof v === "object") {
    // Ref-shaped values are the most common payload value in the event
    // stream — every message row carries one. Dumped as JSON they spend the
    // whole line restating the key name and then truncate the only part that
    // separates one row from the next, so a page of them reads as identical.
    const ref = refShortId(v);
    if (ref) return ref;
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
  // Node count deliberately does not enter this. The question is which SOURCE
  // to draw from, not whether the authored graph is complete in itself. A
  // one-node snapshot has nothing to draw an edge between, and that is the
  // reason it should yield to a runtime graph that does have one rather than
  // a reason to prefer it: treating it as authoritative rendered a real
  // two-node runtime DAG as a flat list.
  const isEdgeless = edgeCount === 0;
  return !(isEdgeless && opGraph.edges.length > 0);
}

// ── Graph/list view (ADR-0113 D1, D6) ───────────────────────────────────────

export type RunDetailView = "graph" | "list";

const RUN_DETAIL_VIEW_STORAGE_KEY = "studio.runDetail.view";
const RUN_DETAIL_VIEW_QUERY_KEY = "view";
const RUN_DETAIL_NODE_QUERY_KEY = "node";

// localStorage can throw (private-browsing quotas) or simply be absent from
// a test/SSR environment's window shim — never let a preference read/write
// take the page down.
function readStoredView(): string | null {
  try {
    return window.localStorage.getItem(RUN_DETAIL_VIEW_STORAGE_KEY);
  } catch {
    return null;
  }
}

function writeStoredView(next: RunDetailView): void {
  try {
    window.localStorage.setItem(RUN_DETAIL_VIEW_STORAGE_KEY, next);
  } catch {
    // Best-effort — the URL query param (also written by the caller) still
    // carries the choice for this navigation even if persistence fails.
  }
}

export function parseRunDetailView(value: string | null | undefined): RunDetailView | null {
  return value === "graph" || value === "list" ? value : null;
}

// shouldRenderAuthoredGraph decides whether a graph CAN be drawn at all
// (falling back to the runtime opGraph, or nothing). This decides whether
// that graph is worth defaulting TO: a graph with no edges is a scatter of
// boxes with nothing to say about concurrency or dependency, and per D1 "a
// canvas with one node and no edges is not a canvas" — that reasoning holds
// however many disconnected nodes there are, not just at exactly one.
export function hasResolvableGraph(
  runGraph: { nodes: unknown[]; edges: unknown[] | null | undefined } | null,
  opGraph: { nodes: unknown[]; edges: unknown[] },
): boolean {
  if (runGraph && shouldRenderAuthoredGraph(runGraph, opGraph)) {
    return (runGraph.edges?.length ?? 0) > 0;
  }
  return opGraph.nodes.length > 0 && opGraph.edges.length > 0;
}

// D6's precedence rule, made explicit: default is graph, but a user's own
// choice — whether carried on the URL (a deep link someone pasted) or in
// their stored preference — always wins over the default, on every load,
// not just the first. The URL outranks the stored preference so a shared
// link reproduces what was shared even if the recipient has their own
// pinned preference. See RunDetail.test.tsx's precedence test: it fails
// if the default is ever allowed to win over an explicit choice.
export function resolveInitialView(input: {
  urlView: RunDetailView | null;
  storedPreference: RunDetailView | null;
  hasResolvableGraph: boolean;
}): RunDetailView {
  if (input.urlView) return input.urlView;
  if (input.storedPreference) return input.storedPreference;
  return input.hasResolvableGraph ? "graph" : "list";
}

// The planner persists depends_on endpoints as 1-BASED STEP NUMBERS while
// nodes are keyed by role name, so a persisted edge can arrive as
// {source: "1", target: "tester"}. Fed to dagre unresolved, every numeric
// endpoint becomes a phantom zero-size node and the layout shatters into
// disconnected clusters (measured on a live 30-node run: 125/125 edges
// unresolvable). Resolve numeric endpoints by position in the nodes array —
// assignment order — and drop edges that resolve nowhere: a missing edge
// degrades to a sparser DAG, a phantom node corrupts the whole layout.
//
// Exact id match deliberately wins over positional reading. This function
// also sees authored graphs whose endpoints ARE node ids, and there a node
// literally named "2" must resolve to itself — id-first can never break a
// well-formed graph, while position-first would. The residual ambiguity (a
// planner graph whose role names are numeric strings) does not occur: roles
// are words.
export function resolveGraphEdges(
  nodes: WorkerGraph["nodes"],
  edges: WorkerGraph["edges"] | null | undefined,
): WorkerGraph["edges"] {
  if (!edges || edges.length === 0) return [];
  const ids = new Set(nodes.map((n) => n.id));
  const resolve = (ref: string): string | null => {
    if (ids.has(ref)) return ref;
    if (/^\d+$/.test(ref)) {
      const byPosition = nodes[Number(ref) - 1];
      if (byPosition) return byPosition.id;
    }
    return null;
  };
  // Resolution can collapse distinct refs onto one endpoint pair (a numeric
  // ref and the id it names, arriving as two edges), and a defective producer
  // can repeat an edge id — either survives as a doubled edge / React
  // duplicate key. A source === target edge is the degenerate form of the
  // same collapse: depends_on edges form an acyclic dependency DAG, so a
  // self-edge is never semantics, only a ref pair naming one node twice.
  // When a pair collapses, the edge carrying more information wins — the
  // duplicates differ exactly when one arrived bare (a numeric planner ref)
  // and the other carries the authored condition/handler/map.
  const richness = (e: WorkerGraph["edges"][number]): number =>
    (e.condition ? 1 : 0) + (e.map ? 1 : 0) + (e.handler ? 1 : 0) + (e.mode === "code" ? 1 : 0);
  const seenIds = new Set<string>();
  const byPair = new Map<string, WorkerGraph["edges"][number]>();
  const pairOrder: string[] = [];
  for (const edge of edges) {
    const source = resolve(edge.source);
    const target = resolve(edge.target);
    if (source === null || target === null || source === target) continue;
    if (seenIds.has(edge.id)) continue;
    seenIds.add(edge.id);
    const pair = `${source}\u0000${target}`;
    const resolved = { ...edge, source, target };
    const kept = byPair.get(pair);
    if (!kept) {
      byPair.set(pair, resolved);
      pairOrder.push(pair);
    } else if (richness(resolved) > richness(kept)) {
      // Replace in place — the pair keeps its first position in the output.
      byPair.set(pair, resolved);
    }
  }
  return pairOrder.map((pair) => byPair.get(pair)!);
}

// The authored graph's resolved edges (resolveGraphEdges output) include
// every ancestor the designer wired, direct and transitive alike — the same
// "one depends_on entry per predecessor" redundancy transitiveReduce exists
// for on the runtime path, just authored instead of engine-emitted. Reduce
// it the same way, display-time only and with the semantic guard
// (transitiveReduceDisplay never drops a condition/handler/map/code edge),
// so a viewer sees the minimal picture by default with a one-click escape
// hatch back to the full resolved set — never a dependency silently erased.
export function computeDisplayEdges(
  edges: WorkerGraph["edges"],
  showImpliedEdges: boolean,
  visibleNodeIds?: string[],
): { displayEdges: WorkerGraph["edges"]; hiddenCount: number } {
  const visibleNodes = visibleNodeIds ? new Set(visibleNodeIds) : undefined;
  const { kept, hidden } = transitiveReduceDisplay(edges, { visibleNodes });
  return { displayEdges: showImpliedEdges ? edges : kept, hiddenCount: hidden.length };
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

// The BranchesSection identity every branch renders under (RunStepCard's
// `id="step-${step.step}"` anchor and expandedSteps entry). Factored out so
// the graph drill-down (matchGraphNodeToBranch → this) and the step list
// (branchToRunStep → this) can never key a branch two different ways.
export function stepKeyForBranch(branch: SessionBranch): string {
  return branch.name || branch.id.slice(0, 8);
}

// Execution-graph nodes are keyed by authored role/assignment name
// (WorkerStepNode.id). branch.name is the durable, unique identity a
// session assigns per branch; agent_name is only a role label shared by
// every branch filling that role (e.g. two "implementer" branches from a
// fan-out), so trying it first can match the WRONG branch whenever a role
// repeats. Prefer the unique identity first: exact branch name, then an id
// prefix, and only fall back to agent_name when exactly one branch carries
// it — with two or more candidates it cannot disambiguate, so it does not
// guess.
export function matchGraphNodeToBranch(
  nodeId: string,
  branches: SessionBranch[],
): SessionBranch | null {
  const byName = branches.find((b) => b.name === nodeId);
  if (byName) return byName;

  const byIdPrefix = branches.find((b) => b.id.slice(0, 8) === nodeId);
  if (byIdPrefix) return byIdPrefix;

  const byAgentName = branches.filter((b) => b.agent_name === nodeId);
  return byAgentName.length === 1 ? byAgentName[0] : null;
}

// Single source of truth for BOTH the always-visible progress summary and
// the graph nodes' own coloring (WorkerCanvas's nodeStatuses prop): both
// callers pass the SAME reconciled map returned here, so they cannot
// diverge. Applies the terminal-run invariants (descendant-terminal
// suppression, unknown-status collapse on a done run) from
// lib/execGraphProgress before either consumer sees the map.
export function computeReconciledNodeStatuses(
  runGraph: Pick<WorkerGraph, "nodes" | "edges"> | null,
  nodeStatuses: Record<string, NodeExecStatus> | undefined,
  done: boolean,
): Record<string, NodeExecStatus> | undefined {
  if (!runGraph) return nodeStatuses;
  return reconcileNodeStatuses(
    runGraph.nodes.map((n) => n.id),
    runGraph.edges.map((e) => ({ source: e.source, target: e.target })),
    nodeStatuses,
    done,
  );
}

export function computeProgressCountsForGraph(
  runGraph: Pick<WorkerGraph, "nodes"> | null,
  reconciledStatuses: Record<string, NodeExecStatus> | undefined,
): ProgressCounts | null {
  if (!runGraph) return null;
  return deriveProgressCounts(
    runGraph.nodes.map((n) => n.id),
    reconciledStatuses,
  );
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
    step: stepKeyForBranch(branch),
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
  edgeCount,
  hiddenCount,
  onToggleImplied,
  showImplied,
  trailing,
}: {
  label: string;
  count?: number;
  errorTone?: boolean;
  /** Edge count of whatever graph is actually being rendered (reduced by
   * default, or the full resolved set when showImplied is on). */
  edgeCount?: number;
  /** Edges transitiveReduceDisplay dropped as transitively implied — the
   * toggle button and "N implied hidden" badge render only when > 0. */
  hiddenCount?: number;
  onToggleImplied?: () => void;
  showImplied?: boolean;
  trailing?: ReactNode;
}) {
  const t = useTranslations("history.detail");
  return (
    <div className="mb-2 flex flex-wrap items-center gap-2">
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
      {edgeCount != null && (
        <span className="rounded px-1.5 py-0 font-mono text-[length:var(--t-xs)] bg-surface-overlay text-content-muted">
          {t("graphEdgeCount", { count: edgeCount })}
        </span>
      )}
      {hiddenCount != null && hiddenCount > 0 && (
        <>
          <span className="font-mono text-[length:var(--t-xs)] text-content-muted">
            {t("graphImpliedHidden", { count: hiddenCount })}
          </span>
          <button
            type="button"
            onClick={onToggleImplied}
            className="rounded border border-edge px-1.5 py-0 font-mono text-[length:var(--t-xs)] text-content-secondary transition-colors hover:border-accent/50 hover:text-content-primary"
          >
            {showImplied ? t("graphHideImplied") : t("graphShowImplied")}
          </button>
        </>
      )}
      {trailing}
    </div>
  );
}

// ── Execution-graph progress summary ────────────────────────────────────────
// Always visible beside the graph header — never scrolled out of view or
// hidden behind a click — so a viewer answers "how far along, what's
// running, did anything fail" without touching the graph. counts is derived
// from the exact reconciled status map passed to WorkerCanvas's
// nodeStatuses prop (see reconciledNodeStatuses above), so this bar can
// never disagree with what the graph nodes render.

function ProgressSummaryBar({
  counts,
  elapsedLabel,
  t,
}: {
  counts: ProgressCounts;
  elapsedLabel: string;
  t: ReturnType<typeof useTranslations>;
}) {
  const pending = counts.pending + counts.queued + counts.awaitingApproval + counts.paused;
  return (
    <div
      data-testid="run-progress-summary"
      role={counts.hasFailure ? "alert" : undefined}
      className={`mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 rounded border px-3 py-1.5 font-mono text-[length:var(--t-xs)] ${
        counts.hasFailure
          ? "border-status-error bg-status-error-bg text-status-error"
          : "border-edge bg-surface-raised text-content-secondary"
      }`}
    >
      <span>
        {t("progressTotal")} {counts.total}
      </span>
      <span>
        {t("progressCompleted")} {counts.completed}
      </span>
      <span>
        {t("progressRunning")} {counts.running}
      </span>
      <span className={counts.hasFailure ? "font-semibold" : undefined}>
        {counts.hasFailure ? "⚠ " : ""}
        {t("progressFailed")} {counts.failed}
      </span>
      <span className={counts.escalated > 0 ? "font-semibold text-status-warning" : undefined}>
        {t("progressEscalated")} {counts.escalated}
      </span>
      <span>
        {t("progressPending")} {pending}
      </span>
      <span className="ml-auto">
        {t("progressElapsed")} {elapsedLabel}
      </span>
    </div>
  );
}

// ── Overview section ──────────────────────────────────────────────────────────

interface OverviewData {
  status: string;
  /** Why the run ended this way, for terminal statuses that need explaining. */
  statusReason?: string | null;
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
        {data.statusReason && (
          <div className="mt-3 border-t border-edge-subtle pt-3">
            <span className="text-[length:var(--t-xs)] font-semibold uppercase tracking-wider text-content-muted">
              {t("statStatus")}
            </span>
            <p className="mt-0.5 text-meta text-content-secondary">{data.statusReason}</p>
          </div>
        )}
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
  selectedStepKey,
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
  /** The step (RunStepCard) a graph-node drill-down resolved to, or that the
   * reader opened directly in the list — ringed so the selection is
   * unmistakable, and durable (ADR-0113 D6: selection survives a view
   * switch), not a fading pulse. */
  selectedStepKey?: string | null;
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
            <div
              key={step.step}
              data-selected={step.step === selectedStepKey || undefined}
              className={
                step.step === selectedStepKey
                  ? "rounded ring-2 ring-accent ring-offset-2 ring-offset-surface-base transition-shadow"
                  : undefined
              }
            >
              <RunStepCard
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
            </div>
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

export interface GateOutcome {
  verdict: Verdict;
  major: number;
  minor: number;
  /** True when the emission carried a findings list (a review-style verdict);
   *  false for a bare pass/fail gate, which has no severity breakdown. */
  hasFindings: boolean;
}

const BLOCKING_FINDING_SEVERITIES = new Set(["critical", "high"]);

// Flow-layer DAG gates (lionagi/operations/flow.py's is_gate contract) never
// emit a StructuredOutput signal — a rejecting gate's verdict lives in the
// operation result the executor inspects internally, and surfaces to the
// outside world only as this session-level terminal reason code (set by the
// CLI teardown in lionagi/cli/_runs.py once the DAG completes with at least
// one short-circuited gate). It is the one shape that channel actually
// reaches the run-detail payload through today (SessionDetail.status_reason_code,
// via services/sessions.py get_session) — mirrored here as a literal rather
// than imported, same pattern as runStatus.ts's ZOMBIE_REASON_CODE.
const GATE_REJECTED_REASON_CODE = "run.completed.gate_rejected";

// Runtime errors (tool-call failures) and a gate/review step's verdict are
// different populations — a run can have zero of the former and still carry
// a "request changes" finding from the latter. Scanning newest-first mirrors
// how a reader thinks about it: the most recent structured verdict is the
// one that matters, not the first one emitted.
//
// A bare `verdict: string` or `passed: boolean` field is NOT a reliable gate
// signal — plenty of unrelated structured outputs across the codebase carry
// exactly those field names (e.g. a coding-engine result's `passed`, a
// hypothesis-engine result's `passed`, a generic Verdict/ComplianceVerdict's
// `verdict`) and would otherwise render a false "Gate" badge. Only the
// dedicated `gate_verdict` / `gate_passed` keys — which no non-gate emission
// in the codebase uses — identify an output AS a gate result.
export function deriveGateOutcome(
  events: SignalEvent[],
  runStatus?: { status_reason_code?: string | null } | null,
): GateOutcome | null {
  for (let i = events.length - 1; i >= 0; i--) {
    const ev = events[i];
    if (ev.kind !== "StructuredOutput") continue;
    const data = ev.payload?.data;
    if (!data || typeof data !== "object" || Array.isArray(data)) continue;
    const d = data as Record<string, unknown>;
    if (typeof d.gate_verdict === "string" && d.gate_verdict) {
      const findings = Array.isArray(d.findings) ? d.findings : [];
      let major = 0;
      let minor = 0;
      for (const f of findings) {
        const severity =
          f && typeof f === "object" ? (f as Record<string, unknown>).severity : null;
        if (typeof severity === "string" && BLOCKING_FINDING_SEVERITIES.has(severity)) major += 1;
        else minor += 1;
      }
      return { verdict: deriveVerdict(d.gate_verdict), major, minor, hasFindings: true };
    }
    if (typeof d.gate_passed === "boolean") {
      return {
        verdict: d.gate_passed ? "approve" : "reject",
        major: 0,
        minor: 0,
        hasFindings: false,
      };
    }
  }
  // No direct-producer StructuredOutput carried a verdict — fall back to the
  // DAG-gate reason code. This channel only ever reports a rejection (there is
  // no matching "a gate passed" reason code to derive "approve" from), so it
  // never contradicts a StructuredOutput-derived approve/approve-with-fixes above.
  if (runStatus?.status_reason_code === GATE_REJECTED_REASON_CODE) {
    return { verdict: "reject", major: 0, minor: 0, hasFindings: false };
  }
  return null;
}

const GATE_OUTCOME_TONE: Record<Verdict, string> = {
  approve: "bg-status-success-bg text-status-success",
  "approve-with-fixes": "bg-status-warning-bg text-status-warning",
  "request-changes": "bg-status-error-bg text-status-error",
  reject: "bg-status-error-bg text-status-error",
  none: "bg-surface-overlay text-content-muted",
};

function GateOutcomeBadge({ outcome }: { outcome: GateOutcome }) {
  const t = useTranslations("history.detail");
  const label = outcome.hasFindings
    ? t("gateOutcome", { verdict: outcome.verdict, major: outcome.major, minor: outcome.minor })
    : t("gateOutcomeSimple", { verdict: outcome.verdict });
  return (
    <span
      className={`rounded px-1.5 py-0 font-mono text-[length:var(--t-xs)] font-semibold ${GATE_OUTCOME_TONE[outcome.verdict]}`}
    >
      {label}
    </span>
  );
}

function ErrorsSection({
  errors,
  partial,
  gateOutcome,
}: {
  errors: ErrorEntry[];
  partial?: boolean;
  gateOutcome?: GateOutcome | null;
}) {
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
        trailing={gateOutcome ? <GateOutcomeBadge outcome={gateOutcome} /> : undefined}
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
  // Muted, not an error tone: an edge condition passed this node over, which
  // is the gate working rather than the step breaking.
  NodeSkipped: { label: "skipped", tone: "bg-surface-overlay text-content-muted" },
  NodeAwaitingApproval: { label: "approval", tone: "bg-status-warning-bg text-status-warning" },
  NodeEscalated: { label: "escalated", tone: "bg-status-warning-bg text-status-warning" },
  GateDenied: { label: "gate-denied", tone: "bg-status-error-bg text-status-error" },
  RunStart: { label: "run-start", tone: "bg-status-running-bg text-status-running" },
  RunEnd: { label: "run-end", tone: "bg-status-success-bg text-status-success" },
  RunFailed: { label: "run-failed", tone: "bg-status-error-bg text-status-error" },
  MessageAdded: { label: "message", tone: "bg-surface-overlay text-content-muted" },
  HookSignal: { label: "hook", tone: "bg-surface-overlay text-content-muted" },
  StructuredOutput: { label: "output", tone: "bg-surface-overlay text-content-secondary" },
};

// A NodeEscalated with route="notify" is a soft ("fyi") help signal rather
// than a terminal escalation. Both use the attention tone, while the soft
// route keeps its distinct label because the node itself continues working.
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
  skipped: "bg-surface-overlay text-content-muted",
  escalated: "bg-status-warning-bg text-status-warning",
};

interface LaneSummary {
  op_id: string;
  lane: LaneState;
  count: number;
}

// Rendered-row cap for the events list — independent of lane summaries/counts
// above, which always aggregate over the full `events` array so a job whose
// terminal event falls outside the rendered window still reports correctly.
// The window keeps the NEWEST rows (the tail is where a reader looks after a
// run ends); older rows page in on demand instead of being dropped.
const EVENTS_RENDER_STEP = 500;

// Element/Signal attach these on every row: `created_at` duplicates the row's
// own timestamp column, `schema_version` is a constant, and `metadata` is
// populated for a minority of signals — noise in the one-line summary, not
// information a reader is looking for.
const NOISY_PAYLOAD_KEYS = new Set(["schema_version", "created_at"]);

function isEmptyPayloadValue(v: unknown): boolean {
  if (v == null || v === "") return true;
  if (typeof v === "object") return Object.keys(v as object).length === 0;
  return false;
}

export function visibleEventPayloadEntries(
  payload: Record<string, unknown> | undefined,
): [string, unknown][] {
  if (!payload) return [];
  return Object.entries(payload).filter(
    ([k, v]) => k !== "op_id" && !NOISY_PAYLOAD_KEYS.has(k) && !isEmptyPayloadValue(v),
  );
}

// HookSignal rows are the most common row in the stream. Their payload is a
// `point` (which hook fired) plus a `kwargs` bag whose shape varies per
// point — pick whichever of these names what the hook actually touched, so
// the row reads as "tool.pre · read_file" instead of a struct dump.
const HOOK_SUMMARY_KWARGS = ["tool_name", "branch_id", "model", "role"];

export function summarizeHookEvent(ev: SignalEvent): string | null {
  if (ev.kind !== "HookSignal") return null;
  const point = typeof ev.payload?.point === "string" ? ev.payload.point : null;
  if (!point) return null;
  const kwargs =
    ev.payload?.kwargs && typeof ev.payload.kwargs === "object"
      ? (ev.payload.kwargs as Record<string, unknown>)
      : {};
  const detail = HOOK_SUMMARY_KWARGS.map((k) => kwargs[k]).find(
    (v): v is string => typeof v === "string" && v.length > 0,
  );
  return detail ? `${point} · ${detail}` : point;
}

export function EventsSection({
  events,
  live,
  renderStep = EVENTS_RENDER_STEP,
}: {
  events: SignalEvent[];
  live: boolean;
  /** Paging window size; defaults to EVENTS_RENDER_STEP. Overridable so tests
   *  can exercise the "show older" page-back without rendering hundreds of rows. */
  renderStep?: number;
}) {
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

  const [renderCap, setRenderCap] = useState(renderStep);
  // A switch to a new (empty) run must reset the render window immediately,
  // not after a post-render effect — adjusted during render (React's
  // documented pattern for resetting state on a prop change) rather than in
  // a useEffect, which would cascade an extra render for every run switch.
  const isEmpty = events.length === 0;
  const [wasEmpty, setWasEmpty] = useState(isEmpty);
  if (isEmpty !== wasEmpty) {
    setWasEmpty(isEmpty);
    if (isEmpty) setRenderCap(renderStep);
  }

  const [expandedEvents, setExpandedEvents] = useState<Set<string>>(new Set());
  const toggleExpanded = useCallback((id: string) => {
    setExpandedEvents((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const hiddenOlderEvents = Math.max(0, events.length - renderCap);
  const visibleEvents = hiddenOlderEvents > 0 ? events.slice(hiddenOlderEvents) : events;

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
          {hiddenOlderEvents > 0 && (
            <button
              type="button"
              onClick={() => setRenderCap((c) => c + renderStep)}
              className="w-full border-b border-edge px-3 py-1.5 text-center font-mono text-[length:var(--t-xs)] text-content-muted hover:bg-surface-overlay hover:text-content-secondary"
            >
              {t("showOlderEvents", { count: Math.min(hiddenOlderEvents, renderStep) })}
            </button>
          )}
          <div className="flex flex-col divide-y divide-edge-subtle">
            {visibleEvents.map((ev) => {
              const badge = badgeForEvent(ev);
              const hookSummary = summarizeHookEvent(ev);
              const visibleEntries = visibleEventPayloadEntries(ev.payload);
              const hasRawPayload = ev.payload && Object.keys(ev.payload).length > 0;
              const isExpanded = expandedEvents.has(ev.id);
              return (
                <div key={ev.id} className="hover:bg-surface-overlay">
                  <div className="flex items-start gap-2 px-3 py-1.5">
                    <span className="mt-0.5 shrink-0 font-mono text-[length:var(--t-xs)] tabular-nums text-content-muted">
                      {new Date(ev.ts).toLocaleTimeString([], {
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
                    {hookSummary ? (
                      <span className="min-w-0 truncate font-mono text-[length:var(--t-xs)] text-content-muted">
                        {hookSummary}
                      </span>
                    ) : (
                      visibleEntries.length > 0 && (
                        <span className="min-w-0 truncate font-mono text-[length:var(--t-xs)] text-content-muted">
                          {visibleEntries
                            .slice(0, 3)
                            .map(([k, v]) => {
                              const s = compactValue(v);
                              return `${k}=${s.length > 40 ? s.slice(0, 40) + "…" : s}`;
                            })
                            .join("  ")}
                        </span>
                      )
                    )}
                    {hasRawPayload && (
                      <button
                        type="button"
                        aria-expanded={isExpanded}
                        aria-label={t("expandEventDetails")}
                        onClick={() => toggleExpanded(ev.id)}
                        className="ml-auto shrink-0 text-content-muted hover:text-content-secondary"
                      >
                        {isExpanded ? (
                          <IconChevronDown size={10} strokeWidth={2.25} />
                        ) : (
                          <IconChevronRight size={10} strokeWidth={2.25} />
                        )}
                      </button>
                    )}
                  </div>
                  {isExpanded && (
                    <pre className="max-h-48 overflow-auto border-t border-edge-subtle bg-surface-base px-3 py-2 font-mono text-[length:var(--t-xs)] leading-relaxed text-content-secondary">
                      {JSON.stringify(ev.payload, null, 2)}
                    </pre>
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

// ── Run controls (ADR-0113 D4 / rows 8, 9) ──────────────────────────────────
//
// Pause/resume/steer never apply directly — every click proposes a command
// through the ADR-0083 operator conversation, and nothing takes effect until
// the reader explicitly confirms the proposal that comes back. That is the
// same propose-then-confirm path every other operator command already rides;
// this does not add a second one.

type ControlDialog = {
  verb: ControlVerb;
  conversationId: string;
  proposal: OperatorCommandProposal;
};

function RunControls({
  runId,
  project,
  kind,
  runTerminal,
  pausePhase,
  onPauseAccepted,
  onResumeAccepted,
}: {
  runId: string;
  project?: string | null;
  kind: ControlKind | null;
  runTerminal: boolean;
  pausePhase: PausePhase;
  onPauseAccepted: () => void;
  onResumeAccepted: () => void;
}) {
  const t = useTranslations("history.detail");
  const [dialog, setDialog] = useState<ControlDialog | null>(null);
  const [busy, setBusy] = useState<ControlVerb | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [steerOpen, setSteerOpen] = useState(false);
  const [steerText, setSteerText] = useState("");

  if (!kind) return null;

  // applyExecutablePath layers command availability on top of the run's own
  // state. The proposal-backed commands exist for all three verbs; the state
  // machines still disable unsupported kinds and invalid phases explicitly.
  const pauseState = applyExecutablePath("pause", pauseControlState(kind, runTerminal, pausePhase));
  const resumeState = applyExecutablePath(
    "resume",
    resumeControlState(kind, runTerminal, pausePhase),
  );
  const steerState = applyExecutablePath("message", steerControlState(kind, runTerminal));

  async function propose(verb: ControlVerb, message?: string) {
    setBusy(verb);
    setError(null);
    try {
      const { conversationId, proposal } = await proposeRunControl(runId, kind!, verb, {
        message,
        project,
      });
      setDialog({ verb, conversationId, proposal });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : t("controls.proposeFailed"));
    } finally {
      setBusy(null);
    }
  }

  async function confirmDialog() {
    if (!dialog) return;
    setBusy(dialog.verb);
    setError(null);
    try {
      // Binds the proposal to this verb and run, and reads the status the
      // confirm call returns -- both throw rather than reporting a control as
      // accepted when it was refused, rejected, or never applied. The local
      // "accepted" callbacks below run only once the command actually landed.
      await confirmRunControl(dialog.verb, runId, dialog.conversationId, dialog.proposal);
      if (dialog.verb === "pause") onPauseAccepted();
      else if (dialog.verb === "resume") onResumeAccepted();
      else {
        setSteerOpen(false);
        setSteerText("");
      }
      setDialog(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : t("controls.confirmFailed"));
    } finally {
      setBusy(null);
    }
  }

  const reasonText = (code: ControlReasonCode | null) =>
    code ? t(`controls.reason.${code}`) : null;

  // Every offered-but-disabled control states its refusal in text, not only in
  // a tooltip — a refusal the reader has to hover to discover is not a legible
  // one. Deduplicated by code because the common case is several controls
  // refusing for the same reason, and repeating one sentence three times reads
  // as three separate problems.
  const refusals = Array.from(
    new Set(
      [pauseState, resumeState, steerState]
        .filter((state) => state.offered && state.disabled && state.reasonCode !== null)
        .map((state) => state.reasonCode as ControlReasonCode),
    ),
  );

  return (
    <div
      data-testid="run-controls"
      className="flex flex-col gap-2 rounded border border-edge bg-surface-raised p-2.5"
    >
      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          data-testid="run-controls-pause"
          disabled={pauseState.disabled || busy !== null}
          title={reasonText(pauseState.reasonCode) ?? undefined}
          onClick={() => void propose("pause")}
          className="rounded border border-edge px-2 py-1 font-mono text-[length:var(--t-xs)] text-content-secondary transition-colors hover:border-accent/50 hover:text-content-primary disabled:cursor-not-allowed disabled:opacity-50"
        >
          {pausePhase === "pausing"
            ? t("controls.pausePhasePausing")
            : pausePhase === "paused"
              ? t("controls.pausePhasePaused")
              : t("controls.pause")}
        </button>
        {resumeState.offered && (
          <button
            type="button"
            data-testid="run-controls-resume"
            disabled={resumeState.disabled || busy !== null}
            title={reasonText(resumeState.reasonCode) ?? undefined}
            onClick={() => void propose("resume")}
            className="rounded border border-edge px-2 py-1 font-mono text-[length:var(--t-xs)] text-content-secondary transition-colors hover:border-accent/50 hover:text-content-primary disabled:cursor-not-allowed disabled:opacity-50"
          >
            {t("controls.resume")}
          </button>
        )}
        {steerState.offered && (
          <button
            type="button"
            data-testid="run-controls-steer"
            disabled={steerState.disabled || busy !== null}
            title={reasonText(steerState.reasonCode) ?? undefined}
            onClick={() => setSteerOpen((v) => !v)}
            className="rounded border border-edge px-2 py-1 font-mono text-[length:var(--t-xs)] text-content-secondary transition-colors hover:border-accent/50 hover:text-content-primary disabled:cursor-not-allowed disabled:opacity-50"
          >
            {t("controls.steer")}
          </button>
        )}
        {refusals.map((code) => (
          <span
            key={code}
            data-testid={`run-controls-reason-${code}`}
            className="text-[length:var(--t-xs)] text-content-muted"
          >
            {reasonText(code)}
          </span>
        ))}
      </div>

      {steerOpen && steerState.offered && (
        <div className="flex items-center gap-2">
          <input
            type="text"
            value={steerText}
            onChange={(event) => setSteerText(event.target.value)}
            placeholder={t("controls.steerPlaceholder")}
            className="focus-ring h-7 min-w-0 flex-1 rounded border border-edge bg-surface-base px-2 text-[length:var(--t-xs)] text-content-primary"
          />
          <button
            type="button"
            disabled={!steerText.trim() || busy !== null}
            onClick={() => void propose("message", steerText)}
            className="rounded border border-edge px-2 py-1 font-mono text-[length:var(--t-xs)] text-content-secondary transition-colors hover:border-accent/50 hover:text-content-primary disabled:cursor-not-allowed disabled:opacity-50"
          >
            {t("controls.steerSend")}
          </button>
        </div>
      )}

      {dialog && (
        <div
          data-testid="run-controls-confirm"
          className="flex flex-col gap-1 rounded border border-edge bg-surface-overlay px-2 py-1.5 text-[length:var(--t-xs)] text-content-secondary"
        >
          <div className="flex flex-wrap items-center gap-2">
            <span>{t(`controls.confirm.${dialog.verb}`)}</span>
            <button
              type="button"
              disabled={busy !== null}
              onClick={() => void confirmDialog()}
              className="rounded border border-edge px-2 py-0.5 text-content-primary transition-colors hover:border-accent/50"
            >
              {t("controls.confirmYes")}
            </button>
            <button
              type="button"
              disabled={busy !== null}
              onClick={() => setDialog(null)}
              className="rounded border border-edge px-2 py-0.5 text-content-primary transition-colors hover:border-accent/50"
            >
              {t("controls.confirmCancel")}
            </button>
          </div>
          {/* What the proposal would actually do, read off the proposal rather
              than off the verb that was asked for. The two can disagree: the
              turn carries a natural-language instruction, so the command that
              comes back is whichever one the operator model chose, and this
              line is where a reader sees "Cancel run …" under "Confirm
              pause?" instead of confirming it blind. Server-supplied text, so
              it is rendered as-is rather than translated. */}
          <div
            data-testid="run-controls-confirm-proposal"
            className="flex flex-wrap items-baseline gap-2 text-content-muted"
          >
            {dialog.proposal.commandType && (
              <code data-testid="run-controls-confirm-command-type" className="font-mono">
                {dialog.proposal.commandType}
              </code>
            )}
            <span data-testid="run-controls-confirm-summary">{dialog.proposal.summary}</span>
            {dialog.proposal.target && (
              <span data-testid="run-controls-confirm-target" className="font-mono">
                {dialog.proposal.target.kind} {dialog.proposal.target.id}
              </span>
            )}
          </div>
        </div>
      )}

      {error && (
        <p role="alert" className="text-[length:var(--t-xs)] text-status-failure">
          {error}
        </p>
      )}
    </div>
  );
}

// ── Public component ──────────────────────────────────────────────────────────

// Floor for the execution-graph panel — keeps a tiny pipeline from collapsing
// into a sliver. No ceiling: the card sizes to the laid-out graph's reported
// bounding-box height (post-reduction, post-depth-scaled-ranksep) so fitView
// has room to keep a deep chain's cards above the readability floor
// (WorkerCanvas's FIT_ZOOM_FLOOR); a capped card would force fitView below
// that floor for every graph taller than the cap. The enclosing page scrolls
// past a tall card; the canvas itself only pans once it's still wider/taller
// than the floor-zoomed viewport.
//
// This is an intentional floor/grow-only POLICY, not a best-effort
// approximation of computeReservedHeight's exact number: a mid-run layout
// that computes a smaller height than what's already committed is NOT
// applied (growing then shrinking the panel mid-stream would jump the page
// under the reader — see onDagLayoutHeight below), and a computed height
// below DAG_MIN_HEIGHT is floored rather than passed through. So the panel
// height can legitimately exceed the graph's actual rendered height for a
// run that shrank its layout after growing it, or for one that never
// exceeded the floor. Pinned by
// "the dag panel height policy is floor/grow-only" in RunDetail.test.tsx.
const DAG_MIN_HEIGHT = 280;

export interface RunDetailProps {
  /** Session ID to load. */
  id: string;
}

export default function RunDetail({ id }: RunDetailProps) {
  const t = useTranslations("history.detail");
  const [session, setSession] = useState<SessionDetail | null>(null);
  const [runGraph, setRunGraph] = useState<WorkerGraph | null>(null);
  // Execution-graph panel height, driven by the LAYOUT's bounding box rather
  // than node count: a linear pipeline stays short however many steps it has,
  // and a wrapped fan-out gets the room its grid needs. Grow-only while the
  // run streams (shrinking mid-stream would jump the page under the reader).
  // The state carries the run id it was measured for, so switching runs falls
  // back to the floor by derivation instead of a reset effect.
  const [dagHeightFor, setDagHeightFor] = useState<{ id: string; height: number }>({
    id,
    height: DAG_MIN_HEIGHT,
  });
  const dagHeight = dagHeightFor.id === id ? dagHeightFor.height : DAG_MIN_HEIGHT;
  const onDagLayoutHeight = useCallback(
    (height: number) => {
      const clamped = Math.max(DAG_MIN_HEIGHT, Math.ceil(height));
      setDagHeightFor((prev) => ({
        id,
        height: Math.max(prev.id === id ? prev.height : DAG_MIN_HEIGHT, clamped),
      }));
    },
    [id],
  );
  const [live, setLive] = useState(false);
  const [done, setDone] = useState(false);
  // Stable no-op: the expanded overlay doesn't drive the inline panel's
  // height, but an inline arrow here would be a fresh reference every render
  // and re-trigger WorkerCanvas's layout effect, resetting execStatus on
  // every unrelated RunDetail rerender.
  const noopLayoutHeight = useCallback(() => {}, []);
  const [error, setError] = useState<string | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const [expandedSteps, setExpandedSteps] = useState<Set<string>>(new Set());
  // Cross-view selection (ADR-0113 D6): the step a graph-node click resolved
  // to, or a list step the reader opened — either sets this, and it survives
  // a graph/list toggle rather than fading, since selecting in one view and
  // switching to the other is the whole point of D6's shared-selection
  // contract. `unmatchedNodeId` covers a click that found NO branch (shown
  // as an explicit not-started/no-branch state instead of a silent no-op).
  const [selectedStepKey, setSelectedStepKey] = useState<string | null>(null);
  const [unmatchedNodeId, setUnmatchedNodeId] = useState<string | null>(null);
  // Full-width expand overlay for the execution graph — closed by its own
  // button or Escape; never by anything else, so a stray keypress elsewhere
  // can't dismiss it.
  const [graphExpanded, setGraphExpanded] = useState(false);
  // ADR-0113 D6: the two raw sources resolveInitialView weighs against the
  // default — the URL's `view` param (a pasted deep link) and the stored
  // per-user preference — read once at mount (lazy useState initializers,
  // never written again) rather than a ref, so reading them during render
  // is safe. `userView` is a click during THIS session; once set it
  // outranks both, same as a fresh choice always would.
  const [initialUrlView] = useState<RunDetailView | null>(() =>
    typeof window === "undefined"
      ? null
      : parseRunDetailView(
          new URLSearchParams(window.location.search).get(RUN_DETAIL_VIEW_QUERY_KEY),
        ),
  );
  const [initialStoredView] = useState<RunDetailView | null>(() =>
    typeof window === "undefined" ? null : parseRunDetailView(readStoredView()),
  );
  const [userView, setUserView] = useState<RunDetailView | null>(null);
  const handleSetView = useCallback((next: RunDetailView) => {
    setUserView(next);
    if (typeof window === "undefined") return;
    writeStoredView(next);
    const url = new URL(window.location.href);
    url.searchParams.set(RUN_DETAIL_VIEW_QUERY_KEY, next);
    window.history.replaceState(window.history.state, "", url);
  }, []);
  // ADR-0113 D6: the selected node is URL-addressable the same way `view`
  // is — read once at mount (never re-read after) and restored against the
  // first session load's branches. `initialNodeAppliedRef` bounds that
  // restore to the run that was live when the component first mounted, so
  // a later run swap (the Fleet view keeps this component mounted and only
  // changes `id`) never re-applies a stale deep link onto the new run.
  const [initialUrlNodeKey] = useState<string | null>(() =>
    typeof window === "undefined"
      ? null
      : new URLSearchParams(window.location.search).get(RUN_DETAIL_NODE_QUERY_KEY),
  );
  const initialNodeAppliedRef = useRef(false);
  const writeSelectedNodeToUrl = useCallback((key: string | null) => {
    if (typeof window === "undefined") return;
    const url = new URL(window.location.href);
    if (key) url.searchParams.set(RUN_DETAIL_NODE_QUERY_KEY, key);
    else url.searchParams.delete(RUN_DETAIL_NODE_QUERY_KEY);
    window.history.replaceState(window.history.state, "", url);
  }, []);
  // ADR-0113 D4/row 9: whether THIS client has asked this run to pause.
  // There is no session-level "paused"/"pausing" status column yet (only
  // per-node NodeExecStatus models it) — the pause gate is enforced by the
  // executor in-process and observed here only through node signals, so the
  // request itself is tracked locally rather than re-derived from a status
  // string that does not exist. A reload loses it, same as any other
  // client-only UI state; see the report for the follow-up this implies.
  const [pauseRequested, setPauseRequested] = useState(false);
  const [signalEvents, setSignalEvents] = useState<SignalEvent[]>([]);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const loadingOlderRef = useRef(false);
  // Set when the server rejects the held anchor as no longer present in the
  // branch's progression (HTTP 400 MessageCursorError) — the anchor points at
  // a message id that aged out. Loading older history from this point on is
  // unrecoverable for the current session load; the reader is offered a full
  // reconversation reload instead of a dead retry loop.
  const [olderLoadFailed, setOlderLoadFailed] = useState(false);
  const [resumeWatch, setResumeWatch] = useState<RunResumeResponse | null>(null);
  // State rather than a ref because the affordance for loading older history
  // is rendered from it: a cursor the server stopped handing back means there
  // is nothing older left to ask for, and the reader has to see that.
  const [olderCursor, setOlderCursor] = useState<string | null>(null);
  const suppressAutoScrollRef = useRef(false);
  const initialScrollDoneRef = useRef(false);
  const olderSentinelRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!id) return;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- reset stale state before async fetch; setState only fires in the effect body synchronously, not in callbacks
    setSession(null);
    setRunGraph(null);
    setLive(false);
    setDone(false);
    setError(null);
    setSignalEvents([]);
    // The pause request is scoped to the run it was made on. The Fleet view
    // keeps this component mounted and swaps `id`, so leaving it set let run
    // B derive its pause phase from run A's request: B could show pausing or
    // paused, disable its own Pause, and offer a Resume that would then send
    // a command carrying B's run id even though only A was ever paused.
    setPauseRequested(false);
    setLoadingOlder(false);
    loadingOlderRef.current = false;
    setOlderLoadFailed(false);
    setOlderCursor(null);
    initialScrollDoneRef.current = false;
    // The Fleet view keeps this component mounted and swaps `id` — the
    // selection is scoped to the run it was made on, so a run switch must
    // drop it rather than let it read as the new run's selection.
    // `initialNodeAppliedRef` only flips true once the FIRST run's session
    // has resolved (below), so this leaves the deep-link restore below
    // untouched on that very first load.
    if (initialNodeAppliedRef.current) {
      setSelectedStepKey(null);
      setUnmatchedNodeId(null);
      writeSelectedNodeToUrl(null);
    }
    getSession(id)
      .then((s) => {
        setSession(s);
        setOlderCursor(s.message_next_cursor ?? null);
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
        const branchExpansion =
          s.branches.length <= 3
            ? new Set(s.branches.map((b) => b.name || b.id.slice(0, 8)))
            : s.branches[0]
              ? new Set([s.branches[0].name || s.branches[0].id.slice(0, 8)])
              : null;
        // Restore a deep-linked node selection against THIS run's branches,
        // once only, ever — a later run swap must not reapply it (see the
        // clear-on-id-change block above).
        if (!initialNodeAppliedRef.current) {
          initialNodeAppliedRef.current = true;
          if (initialUrlNodeKey) {
            const match = matchGraphNodeToBranch(initialUrlNodeKey, s.branches);
            if (match) {
              const key = stepKeyForBranch(match);
              branchExpansion?.add(key);
              setSelectedStepKey(key);
            }
          }
        }
        if (branchExpansion) setExpandedSteps(branchExpansion);
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
            edges: resolveGraphEdges(graph.nodes, graph.edges),
          });
        }
      })
      .catch((e: unknown) => setError(String(e)));
  }, [id, initialUrlNodeKey, writeSelectedNodeToUrl]);

  useEffect(() => {
    if (!id || !resumeWatch || resumeWatch.run_id !== id) return;
    let cancelled = false;
    let timer: number | null = null;

    const poll = async () => {
      try {
        // Observe invocation state first. If it is terminal, the session read
        // that follows is ordered after the worker's terminal transition and
        // therefore includes its final persisted messages.
        const invocation = await getInvocation(resumeWatch.invocation_id);
        const fresh = await getSession(id);
        if (cancelled) return;
        setSession((previous) =>
          previous && previous.id === fresh.id ? mergeCompletedSession(previous, fresh) : fresh,
        );
        if (isEffectivelyActive(invocation)) {
          setDone(false);
          setLive(true);
        } else {
          setDone(true);
          setLive(false);
          setResumeWatch(null);
          return;
        }
      } catch {
        // A just-created invocation can race its first detail read, and a
        // transient daemon disconnect must not strand a successfully accepted
        // continuation. Retry until the component unmounts or activity reaches
        // a terminal state.
      }
      if (!cancelled) timer = window.setTimeout(() => void poll(), 750);
    };

    void poll();
    return () => {
      cancelled = true;
      if (timer != null) window.clearTimeout(timer);
    };
  }, [id, resumeWatch]);

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

  // Opening a step in the list is choosing to look at it — the same act
  // that selecting its node in the graph performs, so it sets the same
  // cross-view selection (ADR-0113 D6). Collapsing does not clear the
  // selection: closing a card is not the same act as selecting a different
  // one.
  const handleToggleExpand = useCallback(
    (stepId: string, next: boolean) => {
      setExpandedSteps((prev) => {
        const updated = new Set(prev);
        if (next) updated.add(stepId);
        else updated.delete(stepId);
        return updated;
      });
      if (next) {
        setSelectedStepKey(stepId);
        writeSelectedNodeToUrl(stepId);
      }
    },
    [writeSelectedNodeToUrl],
  );

  // Graph-node drill-down. ReactFlow renders each node wrapper with a
  // `data-id` attribute (its own internals, not StepNode/WorkerCanvas
  // markup we own) — delegating the click here means the graph can be wired
  // to the branch list without adding a callback prop to WorkerCanvas.
  const handleGraphNodeClick = useCallback(
    (nodeId: string) => {
      const match = matchGraphNodeToBranch(nodeId, session?.branches ?? []);
      if (!match) {
        setUnmatchedNodeId(nodeId);
        setSelectedStepKey(null);
        writeSelectedNodeToUrl(null);
        return;
      }
      setUnmatchedNodeId(null);
      const key = stepKeyForBranch(match);
      setExpandedSteps((prev) => (prev.has(key) ? prev : new Set(prev).add(key)));
      setSelectedStepKey(key);
      writeSelectedNodeToUrl(key);
    },
    [session, writeSelectedNodeToUrl],
  );

  const handleDagPanelClick = useCallback(
    (event: ReactMouseEvent<HTMLDivElement>) => {
      const target = (event.target as HTMLElement).closest<HTMLElement>(
        ".react-flow__node[data-id]",
      );
      const nodeId = target?.dataset.id;
      if (nodeId) handleGraphNodeClick(nodeId);
    },
    [handleGraphNodeClick],
  );

  // Scrolls the selected step into view whenever the selection changes AND
  // the list is the visible view (the element only exists in the DOM then —
  // in graph view this is a harmless no-op). Unlike the old drill-down
  // pulse, the selection itself does not fade; only the scroll is one-shot
  // per change.
  useEffect(() => {
    if (!selectedStepKey) return;
    const el = document.getElementById(`step-${selectedStepKey}`);
    el?.scrollIntoView({ behavior: "smooth", block: "center" });
  }, [selectedStepKey]);

  useEffect(() => {
    if (!graphExpanded) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setGraphExpanded(false);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [graphExpanded]);

  const hiddenOlderCount = useMemo(() => {
    // The cursor gates the arithmetic instead of sitting beside it. The
    // per-branch subtraction counts every message not loaded, which is older
    // history only until the tail grows: after that it also counts newer
    // messages, which arrive on their own and are not reachable through this
    // cursor at all. Offering those as older history renders a control that
    // is enabled and can never do anything, so once the server stops handing
    // back a cursor there is nothing older to load and the count is zero.
    if (!session || olderCursor === null) return 0;
    return session.branches.reduce((n, b) => {
      const total = b.message_total ?? b.messages.length;
      return n + Math.max(0, total - b.messages.length);
    }, 0);
  }, [session, olderCursor]);

  const handleLoadOlder = useCallback(() => {
    const cursor = olderCursor;
    // The in-flight test reads a ref, not the `loadingOlder` state beside it.
    // The scroll sentinel can deliver two intersections within a single turn,
    // and React state does not update between them, so both callbacks would
    // see this handler as idle and issue the same cursor request twice. The
    // state is still what the UI renders from; only the exclusion has to be
    // synchronous.
    if (!id || loadingOlderRef.current || olderLoadFailed || !cursor) return;
    loadingOlderRef.current = true;
    setLoadingOlder(true);
    suppressAutoScrollRef.current = true;
    getSession(id, { messageCursor: cursor })
      .then((older) => {
        setOlderCursor(older.message_next_cursor ?? null);
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
      .catch((e: unknown) => {
        // A stale anchor (HTTP 400) means the branch's progression moved on
        // and this cursor no longer resolves — surface the dead-end instead
        // of retrying the same broken request on every scroll-up tick.
        if (e instanceof ApiError && e.status === 400) {
          setOlderLoadFailed(true);
          return;
        }
        setError(String(e));
      })
      .finally(() => {
        loadingOlderRef.current = false;
        setLoadingOlder(false);
      });
  }, [id, olderLoadFailed, olderCursor]);

  const handleReloadConversation = useCallback(() => {
    if (!id) return;
    suppressAutoScrollRef.current = true;
    setOlderLoadFailed(false);
    setLoadingOlder(true);
    getSession(id)
      .then((fresh) => {
        setOlderCursor(fresh.message_next_cursor ?? null);
        setSession(fresh);
      })
      .catch((e: unknown) => setError(String(e)))
      .finally(() => setLoadingOlder(false));
  }, [id]);

  // Scroll-up trigger: an always-mounted sentinel just above the message
  // list. handleLoadOlder no-ops without a cursor or mid-flight, so this can
  // fire freely as the sentinel scrolls in and out of view.
  //
  // It reads the handler through a ref so that scrolling stays the only thing
  // that drives it. Depending on the handler directly made the observer
  // re-arm itself: a completed page sets a new cursor, which is one of
  // handleLoadOlder's dependencies, so the effect tore the observer down and
  // re-observed the sentinel — and a freshly observed target always receives
  // an immediate initial observation. While the sentinel sat on screen that
  // observation requested the next page straight away, so the run walked its
  // own history backwards without anyone scrolling.
  const handleLoadOlderRef = useRef(handleLoadOlder);
  useEffect(() => {
    handleLoadOlderRef.current = handleLoadOlder;
  }, [handleLoadOlder]);
  // The sentinel renders below the loading branch's early return, so it is
  // absent from the DOM on the first commit. This tracks the one transition
  // that puts it there, which is what the observer has to wait for — an empty
  // dependency list would run once against a null ref and never attach.
  // Session polling keeps replacing `session` itself, so depending on the
  // object rather than this boolean would re-arm on every refresh.
  const sentinelMounted = session != null;
  useEffect(() => {
    const el = olderSentinelRef.current;
    if (!el || typeof IntersectionObserver === "undefined") return;
    const io = new IntersectionObserver((entries) => {
      if (entries.some((e) => e.isIntersecting)) handleLoadOlderRef.current();
    });
    io.observe(el);
    return () => io.disconnect();
  }, [sentinelMounted]);

  const handleResumed = useCallback(
    async (result: RunResumeResponse) => {
      setDone(false);
      setLive(true);
      setResumeWatch(result);
      try {
        const fresh = await getSession(id);
        setSession((previous) =>
          previous && previous.id === fresh.id ? mergeCompletedSession(previous, fresh) : fresh,
        );
      } catch {
        // The accepted resume remains visible in ResumeRun. The existing SSE
        // subscriptions continue to deliver activity even if this eager
        // refresh races a transient daemon disconnect.
      }
    },
    [id],
  );

  // Confirming a pause proposal marks the request locally; derivePausePhase
  // (above) turns that into "pausing" vs "paused" against the live running
  // count, and confirming a resume clears it — a fresh pause() later
  // installs its own gate, mirrored by allowing pauseRequested to be set
  // again from idle.
  const handlePauseAccepted = useCallback(() => setPauseRequested(true), []);
  const handleResumeAccepted = useCallback(() => setPauseRequested(false), []);

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
  // ADR-0113 D1/D6: the graph view's in-place node detail is the SAME
  // RunStepCard the list renders for the same step — no separate detail
  // shape to keep in sync with it.
  const selectedGraphStep = useMemo(
    () => (selectedStepKey ? (steps.find((s) => s.step === selectedStepKey) ?? null) : null),
    [steps, selectedStepKey],
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
    return Array.from(set).sort();
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

  const gateOutcome = useMemo(
    () => deriveGateOutcome(signalEvents, session),
    [signalEvents, session],
  );

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
  // edges are exactly what the designer wired, resolved (resolveGraphEdges,
  // above) but not yet reduced. Like the runtime-derived opGraph below, a
  // depends_on-style ancestor set can carry edges a shorter chain already
  // implies; unlike opGraph, an authored edge can also carry a
  // condition/handler/map/code mode the designer put there on purpose. So
  // this is reduced too, but display-time only and through
  // transitiveReduceDisplay (not the runtime transitiveReduce): it never
  // drops a semantically rich edge, and a hidden dependency is always one
  // toggle away from view, never silently gone.
  const [showImpliedEdges, setShowImpliedEdges] = useState(false);
  const { displayEdges, hiddenCount } = useMemo(
    () =>
      runGraph
        ? computeDisplayEdges(
            runGraph.edges,
            showImpliedEdges,
            runGraph.nodes.map((n) => n.id),
          )
        : { displayEdges: [] as WorkerGraph["edges"], hiddenCount: 0 },
    [runGraph, showImpliedEdges],
  );

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

  // What each node is DOING inside its running state, correlated from the same
  // stream and by the same authored-name rule as nodeStatuses above. Kept to
  // the planned graph's own nodes so a signal for something the graph does not
  // draw cannot grow the map.
  const nodeActivity = useMemo((): Map<string, NodeActivitySnapshot> | undefined => {
    if (!runGraph) return undefined;
    const byName = buildNodeActivityByName(signalEvents);
    const result = new Map<string, NodeActivitySnapshot>();
    for (const node of runGraph.nodes) {
      const live = byName.get(node.id);
      if (live) result.set(node.id, live);
    }
    return result;
  }, [runGraph, signalEvents]);

  // The SAME reconciled map feeds both the always-visible progress summary
  // and the graph nodes below (WorkerCanvas nodeStatuses prop) — one source,
  // so the header can never disagree with what a node renders. Reconciling
  // here (rather than trusting the raw signal-derived map) also applies the
  // terminal-run invariants: a node cannot still read "running" once a
  // descendant has reached a terminal status, and once the run itself is
  // done, any node with no terminal signal collapses to "pending" (absence
  // of information) instead of visually reading as live work.
  const reconciledNodeStatuses = useMemo(
    () => computeReconciledNodeStatuses(runGraph, nodeStatuses, done),
    [runGraph, nodeStatuses, done],
  );

  const progressCounts = useMemo(
    () => computeProgressCountsForGraph(runGraph, reconciledNodeStatuses),
    [runGraph, reconciledNodeStatuses],
  );

  // What the soft-pause gate counts as still running. The authored graph is
  // the preferred source, because it is the same count the progress bar and
  // the canvas already show. But computeProgressCountsForGraph answers null
  // for a run with no authored graph, and a run can be entirely runtime —
  // real operations, real edges, no early_graph. Passing null on to the pause
  // phase as zero would report "nothing left running" for exactly those runs,
  // so fall through to the runtime operation graph, and answer null only when
  // neither source can say anything at all.
  const pauseRunningCount = useMemo((): number | null => {
    if (progressCounts) return progressCounts.running;
    if (opGraph.nodes.length === 0) return null;
    return opGraph.nodes.filter((n) => n.status === "running").length;
  }, [progressCounts, opGraph]);

  // Elapsed wall-clock ticks once a second only while the run is actually
  // live; a finished or not-yet-loaded run has nothing left to advance.
  const [elapsedNow, setElapsedNow] = useState<number>(() => Date.now() / 1000);
  useEffect(() => {
    if (!live || done) return;
    const interval = setInterval(() => setElapsedNow(Date.now() / 1000), 1000);
    return () => clearInterval(interval);
  }, [live, done]);

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
  const elapsedLabel = formatElapsed(computeElapsedSeconds(startRef, endRef, elapsedNow));
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
  const runTerminal =
    displayStatus === "completed" || displayStatus === "failed" || displayStatus === "cancelled";
  const controlKind = controlKindFor(session.invocation_kind ?? null);
  const pausePhase: PausePhase = derivePausePhase(pauseRequested, pauseRunningCount);

  // ADR-0113 D1/D6: a graph with edges is the default view; anything with no
  // edges to draw (including "no graph at all") opens on the list.
  // hasResolvableGraph is the single source of truth for "is there a graph
  // worth rendering" — tab visibility, the default-view resolution below,
  // and the render branch further down all gate on this SAME call, so they
  // cannot disagree about a single-node or edgeless graph the way two
  // separately-maintained predicates could.
  const canRenderGraph = hasResolvableGraph(runGraph, opGraph);
  const view: RunDetailView =
    userView ??
    resolveInitialView({
      urlView: initialUrlView,
      storedPreference: initialStoredView,
      hasResolvableGraph: canRenderGraph,
    });
  const effectiveView: RunDetailView = canRenderGraph ? view : "list";

  const overviewData: OverviewData = {
    status: displayStatus,
    // A run that ends badly owes the operator a sentence. The backend already
    // writes one (e.g. "Run exceeded the configured timeout." for a blown
    // deadline); without this it never reaches the page, and a timed-out run
    // shows a bare "cancelled" beside two branches reading "failed".
    statusReason: isUnsuccessfulTerminal(runForStatus)
      ? (session.status_reason_summary ?? null)
      : null,
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
      {controlKind && hasAnyExecutablePath() && (
        <RunControls
          runId={session.id}
          project={session.project}
          kind={controlKind}
          runTerminal={runTerminal}
          pausePhase={pausePhase}
          onPauseAccepted={handlePauseAccepted}
          onResumeAccepted={handleResumeAccepted}
        />
      )}
      <ResumeRun
        key={session.id}
        runId={session.id}
        invocationKind={session.invocation_kind ?? null}
        branches={session.branches}
        onResumed={handleResumed}
      />
      {session.invocation_id && (
        <InvocationSection invocationId={session.invocation_id} currentSessionId={session.id} />
      )}
      <ExpectedArtifacts
        contract={session.artifact_contract_json}
        verification={session.artifact_verification_json}
      />
      {canRenderGraph && (
        <div
          role="tablist"
          aria-label={t("viewToggleLabel")}
          className="flex items-center gap-1 self-start rounded border border-edge bg-surface-raised p-0.5"
        >
          <button
            type="button"
            role="tab"
            aria-selected={effectiveView === "graph"}
            data-testid="run-detail-view-graph"
            onClick={() => handleSetView("graph")}
            className={`rounded px-2 py-1 font-mono text-[length:var(--t-xs)] transition-colors ${
              effectiveView === "graph"
                ? "bg-surface-overlay text-content-primary"
                : "text-content-muted hover:text-content-secondary"
            }`}
          >
            {t("viewGraph")}
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={effectiveView === "list"}
            data-testid="run-detail-view-list"
            onClick={() => handleSetView("list")}
            className={`rounded px-2 py-1 font-mono text-[length:var(--t-xs)] transition-colors ${
              effectiveView === "list"
                ? "bg-surface-overlay text-content-primary"
                : "text-content-muted hover:text-content-secondary"
            }`}
          >
            {t("viewList")}
          </button>
          {selectedStepKey && effectiveView === "graph" && (
            <span
              data-testid="run-detail-selected-node"
              className="ml-2 truncate font-mono text-[length:var(--t-xs)] text-content-muted"
            >
              {t("selectedNode", { node: selectedStepKey })}
            </span>
          )}
        </div>
      )}
      {effectiveView === "graph" && runGraph && shouldRenderAuthoredGraph(runGraph, opGraph) ? (
        <div id="run-dag" className="w-full scroll-mt-4">
          <SectionHeader
            label={t("sectionExecutionGraph")}
            count={runGraph.nodes.length}
            edgeCount={displayEdges.length}
            hiddenCount={hiddenCount}
            onToggleImplied={() => setShowImpliedEdges((v) => !v)}
            showImplied={showImpliedEdges}
            trailing={
              <button
                type="button"
                onClick={() => setGraphExpanded(true)}
                aria-label={t("expandGraph")}
                className="ml-auto rounded border border-edge px-2 py-0.5 font-mono text-[length:var(--t-xs)] text-content-secondary transition-colors hover:border-accent/50 hover:text-content-primary"
              >
                {t("expandGraph")}
              </button>
            }
          />
          {progressCounts && (
            <ProgressSummaryBar counts={progressCounts} elapsedLabel={elapsedLabel} t={t} />
          )}
          {/* A fan-out of dozens of workers cannot be legible in the same
              280px that fits a five-step pipeline — the panel takes its height
              from the laid-out graph's bounding box so fitView has room to
              keep nodes readable, and a linear pipeline stays short no matter
              how many steps it has. Full content width: no max-width wrapper
              constrains this panel narrower than its flex parent. */}
          {/* eslint-disable-next-line jsx-a11y/no-static-element-interactions, jsx-a11y/click-events-have-key-events -- delegates to ReactFlow's own node buttons (WorkerCanvas/StepNode own their keyboard handling); this div only reads the bubbled click's data-id */}
          <div
            style={{ height: dagHeight }}
            className="mt-2 w-full rounded border border-edge bg-surface-raised shadow-card overflow-hidden"
            onClick={handleDagPanelClick}
          >
            <Suspense fallback={null}>
              <WorkerCanvas
                graph={{ ...runGraph, edges: displayEdges }}
                editable={false}
                execSteps={execSteps}
                nodeStatuses={reconciledNodeStatuses}
                nodeActivity={nodeActivity}
                compact
                onLayoutHeight={onDagLayoutHeight}
                live={live}
                done={done}
              />
            </Suspense>
          </div>
          {graphExpanded && (
            <div
              role="dialog"
              aria-modal="true"
              aria-label={t("sectionExecutionGraph")}
              className="fixed inset-4 z-50 flex flex-col rounded border border-edge bg-surface-raised shadow-card"
            >
              <div className="flex items-center justify-between gap-2 border-b border-edge px-3 py-2">
                <SectionHeader
                  label={t("sectionExecutionGraph")}
                  count={runGraph.nodes.length}
                  edgeCount={displayEdges.length}
                  hiddenCount={hiddenCount}
                  onToggleImplied={() => setShowImpliedEdges((v) => !v)}
                  showImplied={showImpliedEdges}
                />
                <button
                  type="button"
                  onClick={() => setGraphExpanded(false)}
                  aria-label={t("collapseGraph")}
                  className="rounded border border-edge px-2 py-0.5 font-mono text-[length:var(--t-xs)] text-content-secondary transition-colors hover:border-accent/50 hover:text-content-primary"
                >
                  {t("closeExpandedGraph")}
                </button>
              </div>
              {progressCounts && (
                <div className="px-3 pt-2">
                  <ProgressSummaryBar counts={progressCounts} elapsedLabel={elapsedLabel} t={t} />
                </div>
              )}
              {/* eslint-disable-next-line jsx-a11y/no-static-element-interactions, jsx-a11y/click-events-have-key-events -- see note on the inline panel's identical delegation above */}
              <div className="min-h-0 flex-1 p-3" onClick={handleDagPanelClick}>
                <Suspense fallback={null}>
                  <WorkerCanvas
                    graph={{ ...runGraph, edges: displayEdges }}
                    editable={false}
                    execSteps={execSteps}
                    nodeStatuses={reconciledNodeStatuses}
                    nodeActivity={nodeActivity}
                    compact
                    onLayoutHeight={noopLayoutHeight}
                    live={live}
                    done={done}
                  />
                </Suspense>
              </div>
            </div>
          )}
        </div>
      ) : (
        effectiveView === "graph" &&
        opGraph.nodes.length > 0 && (
          <div id="run-dag" className="scroll-mt-4">
            <SectionHeader label={t("sectionExecutionGraph")} count={opGraph.nodes.length} />
            <OperationGraphSection
              state={opGraph}
              live={live && !done}
              onNodeClick={handleGraphNodeClick}
            />
          </div>
        )
      )}
      {/* ADR-0113 D1/D6: a node selected in EITHER graph path (authored via
          handleDagPanelClick, runtime via OperationGraphSection's onNodeClick)
          expands in place here — the same RunStepCard the list view shows for
          that step, not a second detail shape. A click that resolved no
          branch gets the same explicit no-branch state regardless of which
          graph produced it. */}
      {effectiveView === "graph" && unmatchedNodeId && (
        <div
          role="status"
          data-testid="run-dag-unmatched-node"
          className="mt-2 rounded border border-edge bg-surface-overlay px-3 py-1.5 font-mono text-[length:var(--t-xs)] text-content-secondary"
        >
          {t("nodeNoBranch", { node: unmatchedNodeId })}
        </div>
      )}
      {effectiveView === "graph" && selectedGraphStep && (
        <div className="mt-2 scroll-mt-4">
          <RunStepCard
            step={selectedGraphStep}
            expanded
            onToggleExpand={handleToggleExpand}
            runId={session.id}
            artifactRoot={session.artifacts_path}
            runFiles={runFiles}
            onLoadOlder={handleLoadOlder}
            olderMessagesRemaining={hiddenOlderCount}
            loadingOlder={loadingOlder}
          />
        </div>
      )}
      {/* Scroll-up trigger: reaching the top of the conversation loads the
          next older page, same handler as the explicit button below. Kept
          mounted regardless of view — an IntersectionObserver effect
          attaches to it once, on session load (see sentinelMounted above),
          and gating it on the list view would leave that effect watching a
          ref that goes null whenever the reader starts on the graph. */}
      <div ref={olderSentinelRef} aria-hidden="true" className="h-px" />
      {effectiveView === "list" && (
        <>
          {olderLoadFailed ? (
            <div className="flex items-center gap-2 self-start rounded border border-edge bg-surface-raised px-3 py-1.5 font-mono text-[length:var(--t-xs)] text-content-secondary">
              <span>{t("olderUnavailable")}</span>
              <button
                type="button"
                onClick={handleReloadConversation}
                disabled={loadingOlder}
                className="rounded border border-edge px-2 py-0.5 text-content-primary transition-colors hover:border-accent/50 disabled:opacity-50"
              >
                {t("reloadConversation")}
              </button>
            </div>
          ) : (
            hiddenOlderCount > 0 && (
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
            )
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
            selectedStepKey={selectedStepKey}
          />
        </>
      )}
      <ErrorsSection errors={errors} partial={partialWindow} gateOutcome={gateOutcome} />
      <FilesSection files={runFiles} partial={partialWindow} />
      <EventsSection events={signalEvents} live={live && !done} />
      <div ref={bottomRef} />
    </div>
  );

  return content;
}
