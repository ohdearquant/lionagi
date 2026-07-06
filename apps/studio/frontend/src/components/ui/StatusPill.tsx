import type { ReactNode } from "react";
import {
  IconApprox,
  IconArrowTurnRight,
  IconBan,
  IconCheck,
  IconClose,
  IconDotFilled,
  IconDotHalf,
  IconDotOutline,
  IconPause,
} from "@/components/ui/icons";

export type StatusKind = "lifecycle" | "verdict" | "integration" | "role" | "neutral";
export type StatusTone = "ok" | "running" | "failed" | "pending" | "blocked" | "neutral";

// ADR-0024 §C: explicit taxonomy so different vocabularies can't drift
// into the same tone. "session" = ADR-0025 status; "health" = ADR-0024
// derived health; "verdict" = critic/review verdicts; "play" = ADR-0011
// play vocabulary; "neutral" = catch-all.
export type StatusTaxonomy = "session" | "health" | "verdict" | "play" | "neutral";

export interface StatusPillProps {
  // Raw machine string (e.g. "director-managed-complete"). Used to derive
  // tone and human label when those aren't passed explicitly.
  value?: string | null;
  kind?: StatusKind;
  // ADR-0024 §C: explicit vocabulary marker. When set, tone resolution
  // looks up the value in the taxonomy-specific table so e.g. a session
  // status "running" and a play status "running" can stay distinct.
  taxonomy?: StatusTaxonomy;
  // Override the displayed label
  label?: ReactNode;
  // Override the tone resolution
  tone?: StatusTone;
  // Override the icon glyph (use sparingly — defaults come from kind+tone)
  icon?: ReactNode | null;
  className?: string;
}

// ─── Tone resolution ────────────────────────────────────────────────────────

const TONE_BY_VALUE: Record<string, StatusTone> = {
  // lifecycle (terminal good)
  ok: "ok",
  done: "ok",
  success: "ok",
  completed: "ok",
  finished: "ok",
  active: "ok",
  "run-complete": "ok",
  running_complete: "ok",
  // lifecycle (in flight)
  running: "running",
  executing: "running",
  open: "running",
  in_progress: "running",
  "director-managed": "running",
  // lifecycle (failed)
  failed: "failed",
  failure: "failed",
  error: "failed",
  // ADR-0025: timeout is a deliberate bound, not an error. Amber pill
  // signals "retry with more time," not "investigate."
  timed_out: "pending",
  timeout: "pending",
  // ADR-0025: aborted = user pressed Ctrl-C; cancelled = system/
  // orchestrator killed the task. Both render neutral (gray) — the
  // distinction matters for automation, not visual scanning.
  aborted: "neutral",
  cancelled: "neutral",
  canceled: "neutral",
  // lifecycle (waiting)
  pending: "pending",
  queued: "pending",
  planned: "pending",
  received: "pending",
  waiting: "pending",
  scheduled: "pending",
  stale: "pending",
  // lifecycle (terminal mixed)
  "director-managed-complete": "ok",
  // gating
  blocked: "blocked",
  gated: "blocked",
  needs_review: "blocked",
  // verdict
  passed: "ok",
  approved: "ok",
  approve_with_suggestions: "ok",
  rejected: "failed",
  "approve-with-fixes": "pending",
  // integration
  merged: "ok",
  merged_and_pushed: "ok",
  pushed: "ok",
  draft: "pending",
};

// ADR-0024 §C: per-taxonomy overrides. The shared TONE_BY_VALUE table
// covers the common case; these dictionaries narrow specific values
// when the taxonomy makes the intent clear. E.g. "stale" in the health
// taxonomy is amber (pending), not a generic blocked.
const TONE_BY_TAXONOMY: Record<StatusTaxonomy, Record<string, StatusTone>> = {
  session: {
    // ADR-0025: session lifecycle statuses.
    running: "running",
    completed: "ok",
    failed: "failed",
    timed_out: "pending", // deliberate bound — amber, not red
    aborted: "neutral", // user Ctrl-C
    cancelled: "neutral", // system / orchestrator cancellation
    // DESIGN-BRIEF §0/§4: a daemon-restart reap is housekeeping, not a
    // failure — distinct gray tone, never red.
    orphaned: "neutral",
  },
  health: {
    // ADR-0024: six-level derived health. Pre-sorted by severity for the
    // worst-of-group calculation in the grouped runs view.
    healthy: "ok",
    idle: "neutral",
    unresponsive: "pending", // amber — alive but past threshold
    stale: "pending", // amber/orange — process dead, has output
    orphaned: "blocked", // purple — never produced output
    zombie: "failed", // red — terminal, but resources leaked
  },
  verdict: {
    approve: "ok",
    approved: "ok",
    approve_with_suggestions: "ok",
    "approve-with-fixes": "pending",
    request_changes: "pending",
    // DESIGN-BRIEF §4: request-changes is the system working as designed —
    // amber, never red. Dash form is the canonical value from lib/runStatus.
    "request-changes": "pending",
    reject: "failed",
    rejected: "failed",
  },
  play: {},
  neutral: {},
};

function toneFromValue(value: string | null | undefined, taxonomy?: StatusTaxonomy): StatusTone {
  if (!value) return "neutral";
  const key = value.toLowerCase().trim();
  if (taxonomy && TONE_BY_TAXONOMY[taxonomy][key]) {
    return TONE_BY_TAXONOMY[taxonomy][key];
  }
  return TONE_BY_VALUE[key] ?? "neutral";
}

// ─── Label humanization ─────────────────────────────────────────────────────

const LABEL_OVERRIDES: Record<string, string> = {
  "director-managed-complete": "Director complete",
  "director-managed": "Director running",
  running_complete: "Run complete",
  merged_and_pushed: "Merged + pushed",
  in_progress: "In progress",
  approve_with_suggestions: "Approve w/ suggestions",
  "approve-with-fixes": "Approve w/ fixes",
  timed_out: "Timed out",
  needs_review: "Needs review",
  // ADR-0024 health levels
  unresponsive: "Unresponsive",
  orphaned: "Orphaned",
  zombie: "Zombie",
};

function humanize(value: string): string {
  const key = value.toLowerCase().trim();
  if (LABEL_OVERRIDES[key]) return LABEL_OVERRIDES[key];
  // Convert snake_case / kebab-case to Title Case with single spaces
  return key.replace(/[_-]+/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

// ─── Icon glyphs — drawn on the shared 24-grid, bold stroke at pill scale ────

const dot = <IconDotFilled size={9} />;
const dotOutline = <IconDotOutline size={9} strokeWidth={2.5} />;
const dotHalf = <IconDotHalf size={9} strokeWidth={2.5} />;
const check = <IconCheck size={10} strokeWidth={2.5} />;
const cross = <IconClose size={10} strokeWidth={2.5} />;

const ICON_BY_KIND: Record<StatusKind, Partial<Record<StatusTone, ReactNode>>> = {
  lifecycle: {
    ok: dot,
    running: dotHalf,
    failed: cross,
    pending: dotOutline,
    blocked: <IconPause size={10} strokeWidth={2.5} />,
  },
  verdict: {
    ok: check,
    failed: cross,
    pending: <IconApprox size={10} strokeWidth={2.5} />,
    blocked: <IconBan size={10} strokeWidth={2.5} />,
  },
  integration: {
    ok: <IconArrowTurnRight size={10} strokeWidth={2.5} />,
    failed: cross,
    pending: <IconDotFilled size={6} />,
    running: dotHalf,
  },
  role: {},
  neutral: {},
};

// ─── Tone → utility classes ────────────────────────────────────────────────

const TONE_CLASS: Record<StatusTone, string> = {
  ok: "border-status-success/40 bg-status-success-bg text-status-success",
  running: "border-status-running/40 bg-status-running-bg text-status-running",
  failed: "border-status-error/40 bg-status-error-bg text-status-error",
  pending: "border-status-warning/40 bg-status-warning-bg text-status-warning",
  blocked: "border-status-selected/40 bg-status-selected-bg text-status-selected",
  neutral: "border-edge bg-surface-overlay text-content-secondary",
};

export default function StatusPill({
  value,
  kind = "lifecycle",
  taxonomy,
  label,
  tone,
  icon,
  className,
}: StatusPillProps) {
  const resolvedTone = tone ?? toneFromValue(value, taxonomy);
  const resolvedLabel = label ?? (value ? humanize(value) : "");
  const resolvedIcon = icon === null ? null : (icon ?? ICON_BY_KIND[kind][resolvedTone] ?? null);

  return (
    <span
      title={typeof value === "string" ? value : undefined}
      className={[
        "inline-flex max-w-full items-center gap-1 rounded-full border px-1.5 py-0.5 text-[length:var(--t-xs)] font-medium leading-none tracking-wide",
        TONE_CLASS[resolvedTone],
        className,
      ]
        .filter(Boolean)
        .join(" ")}
    >
      {resolvedIcon ? (
        <span aria-hidden="true" className="flex shrink-0 items-center">
          {resolvedIcon}
        </span>
      ) : null}
      <span className="truncate">{resolvedLabel}</span>
    </span>
  );
}
