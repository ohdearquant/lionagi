"use client";

import { memo, useEffect, useRef, useState } from "react";
import { Handle, Position } from "reactflow";
import type { NodeProps } from "reactflow";
import { useTranslations } from "use-intl";
import { IconCheck, IconClose, IconPause, IconWarning } from "@/components/ui/icons";
import { NODE_HEIGHT, NODE_WIDTH } from "./useLayout";
import { isStalled, pulseDurationMs, STALL_TIMEOUT_MS } from "@/lib/nodeActivity";
import type { NodeActivityKind } from "@/lib/nodeActivity";

// What the bottom-right corner says before there is a duration to put there.
// "pending" has no lifecycle signal to report yet, so its placeholder is a
// language-neutral dash rather than a translated word.
const STATUS_WORD_KEY: Record<Exclude<NodeExecStatus, "pending">, string> = {
  queued: "graphNodeStatusQueued",
  running: "graphNodeStatusRunning",
  awaiting_approval: "graphNodeStatusApproval",
  paused: "graphNodeStatusPaused",
  completed: "graphNodeStatusDone",
  failed: "graphNodeStatusFailed",
  skipped: "graphNodeStatusSkipped",
  escalated: "graphNodeStatusEscalated",
};

function usePrefersReducedMotion(): boolean {
  const [reduced, setReduced] = useState(false);
  useEffect(() => {
    const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
    // eslint-disable-next-line react-hooks/set-state-in-effect -- SSR hydration guard: window.matchMedia unavailable during server render
    setReduced(mq.matches);
    const handler = (e: MediaQueryListEvent) => setReduced(e.matches);
    mq.addEventListener("change", handler);
    return () => mq.removeEventListener("change", handler);
  }, []);
  return reduced;
}

// "Nothing animates outside the viewport" (ADR-0113 D3). No IntersectionObserver
// (SSR, or a test environment that doesn't polyfill it) reads as visible —
// the safe default is to animate, matching behavior before this hook existed,
// rather than silently going static everywhere a test happens not to stub it.
function useInViewport(ref: React.RefObject<Element | null>): boolean {
  const [visible, setVisible] = useState(true);
  useEffect(() => {
    if (typeof IntersectionObserver === "undefined" || !ref.current) return;
    const observer = new IntersectionObserver(
      (entries) => {
        const entry = entries[entries.length - 1];
        if (entry) setVisible(entry.isIntersecting);
      },
      { threshold: 0 },
    );
    observer.observe(ref.current);
    return () => observer.disconnect();
  }, [ref]);
  return visible;
}

// Global concurrent-animation budget (ADR-0113 D3: "the number of
// concurrently animating nodes is capped, with the excess falling back to
// the static state" — a 30-node canvas must stay responsive). A module-level
// registry rather than context/store plumbing: every StepNode instance
// competes for one of a fixed number of slots, first-claimed-first-served,
// released on unmount or whenever the node no longer wants to animate.
//
// The slot belongs to the card, not to the node it is showing. The run detail
// keeps its inline canvas mounted while the expanded graph is open, so the
// same run — and therefore the same node ID — is on screen twice at once.
// Keyed by node ID, that arrangement breaks the budget in both directions: the
// second card finds its ID already registered and animates without taking a
// slot, so twice the cap moves while the registry counts the cap; and whichever
// card unmounts first releases the entry the other is still animating on, after
// which fresh nodes claim slots that are already in use. Keyed by card, the two
// simply compete like any other pair.
export const MAX_ANIMATING_NODES = 10;
const animatingCards = new Set<symbol>();

function useAnimationSlot(wantsToAnimate: boolean): boolean {
  const [granted, setGranted] = useState(false);
  // This card's identity in the registry: allocated once per mount, never
  // shared, and never rendered. Lazily-initialized state rather than the node
  // ID precisely because two live cards can carry the same node ID.
  const [slot] = useState(() => Symbol("animation-slot"));
  useEffect(() => {
    if (!wantsToAnimate) {
      animatingCards.delete(slot);
      // eslint-disable-next-line react-hooks/set-state-in-effect -- syncing local render state FROM the shared module-level slot registry (the external system here), not deriving it from props/state React already has
      setGranted(false);
      return;
    }
    const canClaim = animatingCards.size < MAX_ANIMATING_NODES;
    if (canClaim) animatingCards.add(slot);
    setGranted(canClaim);
    return () => {
      animatingCards.delete(slot);
    };
  }, [slot, wantsToAnimate]);
  return granted;
}

// Returns to a static "stalled" reading STALL_TIMEOUT_MS after the last live
// signal, rather than polling: a single timer fires exactly at the deadline
// implied by `liveSignalAt`, so a node that keeps reporting work never stalls
// (each new signal reschedules the timer) and one that stops gets caught the
// instant its window closes — this is "a test, not a comment" per the ADR's
// Consequences section, see StepNode.test.tsx.
//
// The input is `liveSignalAt`, not `lastEventAt`, and the difference is the
// whole correctness of this hook. A null means the node has never reported
// work at all, so there is no stream to have stopped and the honest answer is
// "not stalled" — the same answer this returns for a node that is not running.
function useStallState(liveSignalAt: number | null | undefined, isRunning: boolean): boolean {
  const [stalled, setStalled] = useState(false);
  useEffect(() => {
    if (!isRunning || liveSignalAt == null) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- syncing to the external clock (Date.now()), which React has no other way to read
      setStalled(false);
      return;
    }
    setStalled(isStalled(liveSignalAt, Date.now()));
    const remaining = liveSignalAt + STALL_TIMEOUT_MS - Date.now();
    if (remaining <= 0) return;
    const timer = setTimeout(() => setStalled(true), remaining);
    return () => clearTimeout(timer);
  }, [liveSignalAt, isRunning]);
  return stalled;
}

// Pulse speed tracks the actual event rate rather than a fixed cadence — a
// node emitting many events per second reads as busier than one emitting one
// every few seconds. A sliding 5s window of observed `lastEventAt` values,
// entirely local to this node (no cross-node coordination needed).
function useEventRatePulse(lastEventAt: number | null | undefined): number {
  const windowRef = useRef<number[]>([]);
  const [duration, setDuration] = useState(1500);
  useEffect(() => {
    if (lastEventAt == null) return;
    const seen = windowRef.current;
    seen.push(lastEventAt);
    const cutoff = lastEventAt - 5000;
    while (seen.length && seen[0]! < cutoff) seen.shift();
    setDuration(pulseDurationMs(seen.length));
  }, [lastEventAt]);
  return duration;
}

const ACTIVITY_LABEL: Record<NodeActivityKind, string> = {
  thinking: "thinking",
  tool: "tool",
  streaming: "streaming",
  waiting: "waiting",
};

const ROLE_VAR: Record<string, string> = {
  researcher: "var(--role-researcher)",
  implementer: "var(--role-implementer)",
  reviewer: "var(--role-reviewer)",
  critic: "var(--role-critic)",
  analyst: "var(--role-analyst)",
  architect: "var(--role-architect)",
  tester: "var(--role-tester)",
};

// "pending" = no lifecycle signal observed at all (never queued); "queued" =
// an explicit NodeQueued signal was seen but execution has not started. Both
// render as the same neutral card — the distinction matters for correctness
// (a queued node must never be painted as running), not for a separate look.
export type NodeExecStatus =
  | "pending"
  | "queued"
  | "running"
  | "awaiting_approval"
  | "paused"
  | "completed"
  | "failed"
  | "skipped"
  | "escalated";

export interface StepNodeData {
  label: string;
  role: string;
  assignment: string;
  prompt: string;
  capacity: number;
  timeout: number | null;
  inputs: string[];
  outputs: string[];
  execStatus?: NodeExecStatus;
  // optional badges
  durationSeconds?: number | null;
  errorCount?: number;
  toolCallCount?: number;
  // Live-activity fields (ADR-0113 D3/row 6). All optional and independently
  // absent-safe: a node with no live signal correlation yet (or a finished
  // run loaded from history, which never streamed) renders with none of
  // these set and looks exactly like the card did before this row existed.
  /** What the node is doing right now, while running. */
  activity?: NodeActivityKind | null;
  /** Tool name when activity is "tool". */
  activityDetail?: string | null;
  /** Token or event counter, whichever the provider reports. */
  counter?: number | null;
  /** Epoch ms of the most recent signal of ANY kind for this node — the ONLY
   *  input that drives whether/how fast this node's animation runs. Absent
   *  means "no live stream correlation", which never animates regardless of
   *  execStatus. */
  lastEventAt?: number | null;
  /** Epoch ms of the last signal that reported work in progress, as opposed
   *  to a lifecycle transition. Drives the stall reading and nothing else.
   *  Absent means no liveness signal exists for this node, which is not the
   *  same as one that went quiet, and must not read as stalled. */
  liveSignalAt?: number | null;
}

// Non-animation precedence cues (border weight + a left-edge status rail)
// that must remain readable at the minimum fit zoom (0.1), where the pulse
// ring and label text are effectively invisible. running is strongest (3px
// border, brightest rail), completed/failed/warn are moderate and mutually
// distinguishable by color alone (2px, distinct rail hue), pending/queued
// recede (1px, no rail) so they never compete with completed work for
// attention. skipped recedes with them by design: the node never ran, so it
// is neither an error nor an achievement, and giving it a rail would spend
// the reader's attention on the one part of the graph that did nothing.
// Exported so StepNode.test.ts can assert the precedence contract without
// mounting React Flow.
export interface NodeVisualStyle {
  borderWidth: number;
  borderColor: string;
  bgColor: string;
  labelColor: string;
  railColor: string;
}

export function computeNodeVisualStyle(status: NodeExecStatus, selected: boolean): NodeVisualStyle {
  const isTerminalError = status === "failed";
  const isWarn = status === "awaiting_approval" || status === "paused" || status === "escalated";

  const borderColor =
    status === "running"
      ? "var(--dag-running-border)"
      : status === "completed"
        ? "var(--dag-completed-border)"
        : isTerminalError
          ? "var(--dag-failed-border)"
          : isWarn
            ? "var(--dag-warn-border)"
            : selected
              ? "var(--status-selected)"
              : "var(--dag-pending-border)";

  const bgColor =
    status === "running"
      ? "var(--dag-running-bg)"
      : status === "completed"
        ? "var(--dag-completed-bg)"
        : isTerminalError
          ? "var(--dag-failed-bg)"
          : isWarn
            ? "var(--dag-warn-bg)"
            : "var(--dag-pending-bg)";

  const labelColor =
    status === "running"
      ? "var(--dag-running-label)"
      : status === "completed"
        ? "var(--dag-completed-label)"
        : isTerminalError
          ? "var(--dag-failed-label)"
          : isWarn
            ? "var(--dag-warn-label)"
            : "var(--content-primary)";

  const borderWidth =
    status === "running" ? 3 : status === "completed" || isTerminalError || isWarn ? 2 : 1;

  const railColor =
    status === "running"
      ? "var(--dag-running-border)"
      : status === "completed"
        ? "var(--dag-completed-border)"
        : isTerminalError
          ? "var(--dag-failed-border)"
          : isWarn
            ? "var(--dag-warn-border)"
            : "transparent";

  return { borderWidth, borderColor, bgColor, labelColor, railColor };
}

function StepNodeComponent({ data, selected }: NodeProps<StepNodeData>) {
  const t = useTranslations("history.detail");
  // roleColor arrives as a data-driven CSS var string — keep inline
  const roleColor = ROLE_VAR[data.role] || "var(--content-muted)";
  const status = data.execStatus ?? "pending";
  const reducedMotion = usePrefersReducedMotion();

  const cardRef = useRef<HTMLDivElement>(null);
  const inViewport = useInViewport(cardRef);
  const stalled = useStallState(data.liveSignalAt, status === "running");
  const pulseMs = useEventRatePulse(data.lastEventAt);
  // Every gate ADR-0113 D3 requires, all real signals: actually running,
  // fresh events (not stalled), a live stream correlation at all
  // (lastEventAt present — no signal means no animation, never a guess),
  // on-screen, and not overridden by prefers-reduced-motion. The cap is
  // applied last, via useAnimationSlot, so a node that already fails one of
  // these gates never even competes for a slot.
  const wantsToAnimate =
    status === "running" && !stalled && !reducedMotion && inViewport && data.lastEventAt != null;
  const animationGranted = useAnimationSlot(wantsToAnimate);
  const animating = wantsToAnimate && animationGranted;

  // These derive from status data (dag-* tokens) — keep inline
  const visual = computeNodeVisualStyle(status, !!selected);
  const borderWidth = selected ? Math.max(visual.borderWidth, 2) : visual.borderWidth;

  // The bottom-right corner always says something. Elapsed time once there is
  // any, the status word before that. A corner that can be empty makes the
  // card change shape as a run progresses, and a reader who has to re-find a
  // field has stopped reading the graph at a glance.
  const magnitude =
    data.durationSeconds != null && data.durationSeconds >= 0
      ? formatStepDuration(data.durationSeconds)
      : status === "pending"
        ? "—"
        : t(STATUS_WORD_KEY[status]);

  // The activity row's header word — the "current activity" half of row 6,
  // and where a stall becomes visible text rather than only an animation
  // that quietly stops. Independent of reducedMotion/animating: this is the
  // static-equivalent information those two must carry identically.
  //
  // A running node with no activity signal falls back to its status word, not
  // to "waiting". Absence of a signal means we do not know what the node is
  // doing; it does not mean the node is waiting, and a caption reading
  // "waiting" beside a node that is visibly running is a false statement about
  // the run. A queued node genuinely is waiting, so that branch keeps the word.
  // The status word also keeps the row occupied, which the fixed-height
  // contract above depends on.
  const activityWord = stalled
    ? "stalled"
    : status === "running"
      ? data.activity
        ? data.activityDetail
          ? `${ACTIVITY_LABEL[data.activity]}: ${data.activityDetail}`
          : ACTIVITY_LABEL[data.activity]
        : t(STATUS_WORD_KEY[status])
      : status === "queued"
        ? ACTIVITY_LABEL.waiting
        : "";
  const counterText = data.counter != null ? String(data.counter) : "";

  return (
    <div
      ref={cardRef}
      className="relative flex flex-col justify-between rounded-md px-2.5 py-2"
      style={{
        background: visual.bgColor,
        border: `${borderWidth}px solid ${visual.borderColor}`,
        width: NODE_WIDTH,
        height: NODE_HEIGHT,
        boxShadow:
          status === "running"
            ? "0 0 0 3px color-mix(in srgb, var(--dag-running-border) 18%, transparent)"
            : selected
              ? "0 0 0 2px color-mix(in srgb, var(--status-selected) 22%, transparent)"
              : "0 1px 3px rgba(0,0,0,0.12)",
        transition: "border-color 0.15s, background 0.15s, box-shadow 0.15s",
      }}
    >
      {/* Status rail — a left-edge color bar that survives the readability
          zoom floor even when the card is too small to read text or icons.
          A span, not a div: the card's rows() test selects direct div
          children of this card as its content rows, and the rail is
          decorative chrome, not one of them. */}
      <span
        className="pointer-events-none absolute inset-y-0 left-0 block rounded-l-md"
        style={{ width: 3, background: visual.railColor }}
      />

      <Handle
        type="target"
        position={Position.Left}
        style={{
          width: 8,
          height: 8,
          background: "var(--edge-default)",
          borderColor: "var(--surface-raised)",
          borderWidth: 1.5,
        }}
      />

      {/* Top row: what this step is, and what state it is in. */}
      <div className="flex items-start justify-between gap-1.5">
        <span
          className="truncate font-mono text-[length:var(--t-sm)] font-semibold leading-snug"
          style={{ color: visual.labelColor }}
        >
          {data.label}
        </span>
        {(data.errorCount ?? 0) > 0 && (
          <span className="shrink-0 font-mono text-[length:var(--t-xs)] tabular-nums leading-snug text-status-error">
            {data.errorCount}
          </span>
        )}
        {status === "completed" && (
          <span className="flex shrink-0 items-center text-status-success">
            <IconCheck size={10} strokeWidth={2.5} />
          </span>
        )}
        {status === "failed" && (
          <span className="flex shrink-0 items-center text-status-error">
            <IconClose size={10} strokeWidth={2.5} />
          </span>
        )}
        {status === "escalated" && (
          <span className="flex shrink-0 items-center text-status-warning">
            <IconWarning size={10} strokeWidth={2.5} />
          </span>
        )}
        {status === "awaiting_approval" && (
          <span className="flex shrink-0 items-center text-status-warning">
            <IconWarning size={10} strokeWidth={2.5} />
          </span>
        )}
        {status === "paused" && (
          <span className="flex shrink-0 items-center text-status-warning">
            <IconPause size={10} strokeWidth={2.5} />
          </span>
        )}
        {status === "running" && (
          <span
            className={`h-1.5 w-1.5 shrink-0 rounded-full${animating ? " animate-pulse" : ""}`}
            style={{ background: "var(--dag-running-border)" }}
          />
        )}
      </div>

      {/* Bottom row: what kind of step it is, and how much of it there has
          been so far. Both corners are fixed, so the same fact is always in
          the same place on every card at every zoom. The assignment and the
          tool-call count moved to the panel: they are what you read about one
          node you have already picked, not what you scan a graph for. */}
      <div className="flex items-end justify-between gap-1.5">
        <span
          className="truncate font-mono text-[length:var(--t-xs)] uppercase leading-tight tracking-wide"
          style={{ color: data.role ? roleColor : "transparent" }}
        >
          {data.role || "."}
        </span>
        <span className="shrink-0 font-mono text-[length:var(--t-xs)] tabular-nums leading-tight text-content-muted">
          {magnitude}
        </span>
      </div>

      {/* Live-activity row: what the node is doing, and a counter beside it
          where the provider reports one. Always rendered, so the row below the
          role never moves as a run progresses.

          This row used to sit above a two-line preview of the agent's latest
          text. Nothing on the wire fills that, so it drew empty on every card
          and has been removed until a signal carries per-node text — see the
          note on NODE_HEIGHT. The word here and the animation state above are
          two independent readouts of the same `stalled`/`activity` facts,
          which is what keeps them from disagreeing under
          prefers-reduced-motion. */}
      <div className="mt-0.5 flex items-center justify-between gap-1.5 font-mono text-[length:var(--t-xs)] uppercase leading-tight tracking-wide text-content-muted">
        <span className="truncate">{activityWord}</span>
        {counterText && <span className="shrink-0 tabular-nums">{counterText}</span>}
      </div>

      {status === "running" && (
        <div
          className="pointer-events-none absolute inset-0 rounded-md opacity-35"
          style={{
            border: "2px solid var(--dag-running-border)",
            animation: animating ? `pulse ${pulseMs}ms ease-in-out infinite` : "none",
          }}
        />
      )}

      <Handle
        type="source"
        position={Position.Right}
        style={{
          width: 8,
          height: 8,
          background: "var(--edge-default)",
          borderColor: "var(--surface-raised)",
          borderWidth: 1.5,
        }}
      />
    </div>
  );
}

function formatStepDuration(seconds: number): string {
  if (seconds < 1) return `${Math.round(seconds * 1000)}ms`;
  if (seconds < 60) return `${seconds.toFixed(seconds < 10 ? 1 : 0)}s`;
  const m = Math.floor(seconds / 60);
  return `${m}m`;
}

export default memo(StepNodeComponent);
