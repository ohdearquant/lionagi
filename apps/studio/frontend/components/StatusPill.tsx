import type { ReactNode } from "react";

export type StatusKind = "lifecycle" | "verdict" | "integration" | "role" | "neutral";
export type StatusTone = "ok" | "running" | "failed" | "pending" | "blocked" | "neutral";

export interface StatusPillProps {
  // Raw machine string (e.g. "director-managed-complete"). Used to derive
  // tone and human label when those aren't passed explicitly.
  value?: string | null;
  kind?: StatusKind;
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
  timed_out: "failed",
  timeout: "failed",
  cancelled: "failed",
  canceled: "failed",
  // lifecycle (waiting)
  pending: "pending",
  queued: "pending",
  planned: "pending",
  received: "pending",
  waiting: "pending",
  scheduled: "pending",
  // lifecycle (terminal mixed)
  "director-managed-complete": "ok",
  // gating
  blocked: "blocked",
  gated: "blocked",
  needs_review: "blocked",
  // verdict
  passed: "ok",
  approved: "ok",
  rejected: "failed",
  "approve-with-fixes": "pending",
  // integration
  merged: "ok",
  merged_and_pushed: "ok",
  pushed: "ok",
  draft: "pending",
};

function toneFromValue(value: string | null | undefined): StatusTone {
  if (!value) return "neutral";
  return TONE_BY_VALUE[value.toLowerCase().trim()] ?? "neutral";
}

// ─── Label humanization ─────────────────────────────────────────────────────

const LABEL_OVERRIDES: Record<string, string> = {
  "director-managed-complete": "Director complete",
  "director-managed": "Director running",
  running_complete: "Run complete",
  merged_and_pushed: "Merged + pushed",
  in_progress: "In progress",
  "approve-with-fixes": "Approve w/ fixes",
  timed_out: "Timed out",
  needs_review: "Needs review",
};

function humanize(value: string): string {
  const key = value.toLowerCase().trim();
  if (LABEL_OVERRIDES[key]) return LABEL_OVERRIDES[key];
  // Convert snake_case / kebab-case to Title Case with single spaces
  return key.replace(/[_-]+/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

// ─── Icon glyphs (text, not SVG — fits monospace UI) ────────────────────────

const ICON_BY_KIND: Record<StatusKind, Partial<Record<StatusTone, string>>> = {
  lifecycle: {
    ok: "●",
    running: "◐",
    failed: "✕",
    pending: "○",
    blocked: "⏸",
  },
  verdict: {
    ok: "✓",
    failed: "✕",
    pending: "≈",
    blocked: "⊘",
  },
  integration: {
    ok: "↪",
    failed: "✕",
    pending: "·",
    running: "◐",
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
  label,
  tone,
  icon,
  className,
}: StatusPillProps) {
  const resolvedTone = tone ?? toneFromValue(value);
  const resolvedLabel = label ?? (value ? humanize(value) : "");
  const resolvedIcon = icon === null ? null : (icon ?? ICON_BY_KIND[kind][resolvedTone] ?? null);

  return (
    <span
      title={typeof value === "string" ? value : undefined}
      className={[
        "inline-flex max-w-full items-center gap-1 rounded-full border px-1.5 py-0.5 text-[10px] font-medium leading-none tracking-wide",
        TONE_CLASS[resolvedTone],
        className,
      ]
        .filter(Boolean)
        .join(" ")}
    >
      {resolvedIcon ? (
        <span className="text-[9px] leading-none shrink-0">{resolvedIcon}</span>
      ) : null}
      <span className="truncate">{resolvedLabel}</span>
    </span>
  );
}
